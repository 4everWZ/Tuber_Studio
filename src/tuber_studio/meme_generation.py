"""Optional image generation boundary; existing libraries remain the default.

Generators own model/network resources and cancellation cleanup. They return
bounded original image bytes, never locators or platform publication requests.
"""

import asyncio
import hashlib
import math
import re
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, StrictBytes, field_validator

from .live_media import StrictModel
from .memes import MAX_BYTES, MemeLicense, MemeProvider
from .models import Emotion


class MemeGenerationRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    character_id: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    prompt: str = Field(min_length=1, max_length=4000)
    title: str = Field(min_length=1, max_length=120)
    emotions: tuple[Emotion, ...] = Field(min_length=1, max_length=6)
    license: MemeLicense

    @field_validator("prompt", "title")
    @classmethod
    def nonempty_text(cls, value, info):
        if not value.strip() or info.field_name == "title" and re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("生成描述和名称须为有效文字，名称不能包含控制字符")
        return value

    @field_validator("emotions")
    @classmethod
    def unique_emotions(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("表情情绪不能重复")
        return value


class GeneratedMemeImage(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: StrictBytes = Field(min_length=1, max_length=MAX_BYTES)
    mime_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


class MemeImageGenerator(Protocol):
    id: str
    version: str
    timeout_seconds: float

    async def generate(self, request: MemeGenerationRequest) -> GeneratedMemeImage: ...
    async def close(self) -> None: ...


class MemeGenerationService:
    def __init__(self, catalog: MemeProvider, generator: MemeImageGenerator | None = None):
        self.catalog, self.generator = catalog, generator
        self.gate = asyncio.Lock()
        self.closed = False
        self.identity = None
        if generator is not None:
            for value, limit in ((generator.id, 300), (generator.version, 200)):
                if (
                    not isinstance(value, str)
                    or not 0 < len(value) <= limit
                    or not value.strip()
                    or re.search(r"[\x00-\x1f\x7f]", value)
                ):
                    raise ValueError("图片生成模型须声明有效ID和版本")
            seconds = generator.timeout_seconds
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 3600:
                raise ValueError("图片生成模型须声明最长3600秒的有界超时")
            self.identity = {"id": generator.id, "version": generator.version, "timeout_seconds": seconds}

    def describe(self):
        return {
            "available": self.generator is not None and not self.closed,
            "provider": self.identity["id"] if self.identity else None,
            "version": self.identity["version"] if self.identity else None,
            "reason": "图片生成服务已关闭"
            if self.closed
            else "图片生成模型尚未挂载；请使用现有图库"
            if self.generator is None
            else "已挂载生成接口；图像质量、许可与设备资源仍需单独核验",
        }

    async def generate(self, request: MemeGenerationRequest) -> dict:
        if not self.describe()["available"]:
            raise ValueError(self.describe()["reason"])
        if not isinstance(request, MemeGenerationRequest):
            raise ValueError("图片生成需要类型化请求")
        snapshot = MemeGenerationRequest.model_validate(request.model_dump())
        self.catalog.list(snapshot.character_id, include_disabled=True)
        async with self.gate:
            if self.closed:
                raise ValueError("图片生成服务已关闭")
            async with asyncio.timeout(self.identity["timeout_seconds"]):
                image = await self.generator.generate(snapshot.model_copy(deep=True))
            if not isinstance(image, GeneratedMemeImage):
                raise ValueError("生成模型只能返回图片字节结果，不能返回URL或文件路径")
            image = GeneratedMemeImage.model_validate(image.model_dump())
            return await self.catalog.register(
                snapshot.character_id,
                image.content,
                title=snapshot.title,
                emotions=list(snapshot.emotions),
                source={
                    "kind": "generated",
                    "author": self.identity["id"],
                    "revision": self.identity["version"],
                    "note": "可选生成Provider输出；提示词SHA256="
                    + hashlib.sha256(snapshot.prompt.encode("utf-8")).hexdigest(),
                },
                license=snapshot.license.model_dump(),
                mime_type=image.mime_type,
                enabled=False,
            )

    async def close(self):
        self.closed = True
        async with self.gate:
            if self.generator is not None:
                await self.generator.close()
                self.generator = None
