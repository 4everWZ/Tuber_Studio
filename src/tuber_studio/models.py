"""Validated data at untrusted platform, operator and model boundaries."""

import re
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

Emotion = Literal["idle", "cheerful", "angry", "shy", "confused", "excited"]
Animation = Literal["idle", "talk", "talk_happy", "angry", "shy", "confused", "gift"]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=160)
    platform: str = Field(default="mock", pattern=r"^[a-z0-9_-]{1,32}$")
    room_id: str = Field(default="debug", min_length=1, max_length=80)
    platform_user_id: str | None = Field(default=None, max_length=100)
    display_name: str = Field(default="匿名观众", max_length=80)
    type: Literal["chat", "gift", "song", "disconnect", "sfx"] = "chat"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    text: str = Field(default="", max_length=500)
    metadata: dict = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("timestamp 必须包含时区")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value):
        import json

        if len(json.dumps(value, ensure_ascii=False)) > 2000:
            raise ValueError("metadata 过大")
        return value


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spoken_text: str = Field(min_length=1, max_length=120)
    emotion: Emotion = "idle"
    animation_id: Animation = "talk"
    reply_to: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("spoken_text")
    @classmethod
    def plain_speech(cls, value):
        if re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("台词含控制字符")
        return value.strip()


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    name: str = Field(min_length=1, max_length=60)
    version: int = Field(default=1, ge=1)
    core: dict
    style: dict
    rules: list[str] = Field(default_factory=list, max_length=20)
    lore: list[dict] = Field(default_factory=list, max_length=100)
    voice_profile: str
    avatar_manifest: str = "assets/whale/manifest.json"
    sources: list[str] = Field(default_factory=list)
    license_status: str = "unknown"

    @field_validator("style")
    @classmethod
    def sentence_limits(cls, value):
        minimum, maximum = value.get("min_chars", 1), value.get("max_chars", 60)
        if not isinstance(minimum, int) or not isinstance(maximum, int) or not 1 <= minimum <= maximum <= 120:
            raise ValueError("角色台词长度必须满足 1 ≤ min_chars ≤ max_chars ≤ 120")
        return value
