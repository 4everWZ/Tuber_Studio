"""Application-authenticated workstation image jobs with locally approved OC inputs."""
import base64
import asyncio
import io
import os
import re
from typing import Literal

from PIL import Image
from pydantic import Field

from .http_api import api_client
from .models import StrictModel

MODEL = "black-forest-labs/FLUX.2-klein-4B"
REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"


class ImageBrief(StrictModel):
    variant: Literal["q", "mature"]
    prompt: str = Field(min_length=1, max_length=3400)


def gateway_base(settings):
    root = os.environ.get("OMNI_GATEWAY_URL", "").rstrip("/")
    llm = settings.data["llm"]
    if not root or llm["kind"] != "openai_compatible" or llm["base_url"].rstrip("/") != root + "/v1":
        raise ValueError("工作站图片网关未配置或普通凭据作用域不匹配")
    return root + "/v1"


def gateway(settings):
    base = gateway_base(settings)
    key = settings.provider_key("llm")
    if not key:
        raise ValueError("工作站图片网关缺少普通应用凭据")
    return api_client({"base_url": base, "timeout_seconds": 45}, key)


def image_payload(brief, data):
    return {
        "prompt": "Create one scene featuring the character in the reference. Preserve the reference's "
                  "anatomy, appendages, proportions, face, colors, clothing and rendering style. "
                  "Use a single composition. Scene: " + brief.prompt,
        "reference_image_base64": base64.b64encode(data).decode("ascii"),
    }


async def submit(client, payload):
    # A 429 explicitly rejects admission. A transport failure after a POST
    # leaves delivery uncertain and must not be replayed.
    async with asyncio.timeout(1200):
        while True:
            response = await client.post("images/jobs", json=payload)
            if response.status_code != 429:
                response.raise_for_status()
                break
            delay = response.headers.get("Retry-After", "1")
            await asyncio.sleep(max(1, int(delay)) if delay.isdecimal() else 1)
    data = response.json()
    if response.status_code != 202 or not re.fullmatch("[0-9a-f]{32}", str(data.get("id", ""))):
        raise ValueError("工作站未返回有效图片任务回执")
    return data["id"]


async def result_png(client, remote_id):
    async with client.stream("GET", f"images/jobs/{remote_id}/result") as response:
        response.raise_for_status()
        if response.headers.get("content-type", "").partition(";")[0] != "image/png":
            raise ValueError("工作站图片结果格式不符")
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            if len(data) > 8 * 1024**2:
                raise ValueError("图片工件过大")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.size != (1024, 1024) or image.n_frames != 1:
            raise ValueError("图片工件尺寸或格式不符")
        image.load()
    return bytes(data)
