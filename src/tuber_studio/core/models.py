"""Validated data at untrusted platform, operator and model boundaries."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .text import validate_operation_text
from .validation import endpoint

Emotion = Literal["idle", "cheerful", "angry", "shy", "confused", "excited"]
Animation = Literal["idle", "talk", "talk_happy", "angry", "shy", "confused", "gift"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=160)
    platform: str = Field(default="mock", pattern=r"^[a-z0-9_-]{1,32}$")
    room_id: str = Field(default="debug", min_length=1, max_length=80)
    platform_user_id: str | None = Field(default=None, max_length=100)
    display_name: str = Field(default="匿名观众", max_length=80)
    type: Literal["chat", "gift", "song", "disconnect", "sfx", "activity", "idle"] = "chat"
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
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("台词含控制字符")
        text = value.strip()
        if not text:
            raise ValueError("台词为空")
        return text


class PublicMentionRequest(StrictModel):
    label: str = Field(min_length=2, max_length=101, pattern=r"^@")
    account: str = Field(min_length=1, max_length=100)

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and not stripped.startswith("@"):
                return f"@{stripped}"
            return stripped
        return value


ReflectionDirection = Literal["gratitude", "touched", "amused", "curious", "inspired", "reflective"]


class OperationReply(Reply):
    spoken_text: str = Field(min_length=1)
    mentions: list[PublicMentionRequest] = Field(default_factory=list)
    public_reflection: ReflectionDirection | None = None

    @field_validator("spoken_text")
    @classmethod
    def plain_speech(cls, value):
        # Override live speech normalization; operations keep the original prose.
        return validate_operation_text(value)


class BilibiliOwner(StrictModel):
    uid: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    name: str = Field(min_length=1, max_length=100)
    avatar_url: str | None = None


_PROFILE_LIMIT = 120
DEFAULT_CHARACTER_VOICE = "neutral_reference"
DEFAULT_CHARACTER_MANIFEST = "assets/default/manifest.json"
_IDENTITY_START = re.compile(r"^(?:[我你她他]是(?!否)|(?:I am|I'm|You are|You're)\s+)", re.I)
_STYLE_START = re.compile(r"^(?:说话|语气|口吻|对白|措辞|用词|表达方式|自称|I speak\b|You speak\b|Speak\b)", re.I)


def _legacy_profile_clause(value, opening):
    """Take a complete leading declaration, never a length-cut prefix of prose."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    prefix = opening.match(value)
    if not prefix:
        return None
    match = re.match(r"([^，,。.!！?？；;：:\n\r]{1,80})(?:[，,。.!！；;\n\r]|$)", value)
    if match and match[1][prefix.end():].strip():
        return match[1].strip()
    return None


def resident_character_profile(character):
    """Read the conversational identity without materializing defaults into a saved card."""
    profile = {"name": character["name"]}
    if aliases := character.get("aliases"):
        profile["aliases"] = aliases
    for key, section, legacy_key, opening in (
        ("self_description", "core", "identity", _IDENTITY_START),
        ("speaking_style", "style", "instructions", _STYLE_START),
    ):
        text = character.get(key)
        if not text:
            legacy = character.get(section, {}).get(legacy_key)
            if key == "speaking_style" and isinstance(legacy, list):
                legacy = next(iter(legacy), None)
            text = _legacy_profile_clause(legacy, opening)
        if isinstance(text, str):
            text = text.strip()
            if 0 < len(text) <= _PROFILE_LIMIT and len(text.splitlines()) == 1:
                profile[key] = text
    return profile


LEGACY_VOICE_IDS = {"whale_sovits_v2": "whale_reference", "yaoyao_sovits_v2": "yaoyao_reference"}


def canonical_voice_id(voice_id):
    """Resolve only the two renamed reference recordings; preserve every other voice selection."""
    return LEGACY_VOICE_IDS.get(voice_id, voice_id) if isinstance(voice_id, str) else voice_id


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    name: str = Field(min_length=1, max_length=60)
    display_name: str | None = Field(default=None, max_length=60, exclude_if=lambda value: value is None)
    owner_address: str | None = Field(default=None, max_length=40, exclude_if=lambda value: not value)
    aliases: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]] | None = Field(
        default=None, max_length=12, exclude_if=lambda value: value is None
    )
    self_description: str | None = Field(default=None, max_length=_PROFILE_LIMIT, exclude_if=lambda value: value is None)
    speaking_style: str | None = Field(default=None, max_length=_PROFILE_LIMIT, exclude_if=lambda value: value is None)
    version: int = Field(default=1, ge=1)
    core: dict
    style: dict
    rules: list[str] = Field(default_factory=list, max_length=20)
    lore: list[dict] = Field(default_factory=list, max_length=100)
    voice_profile: str
    avatar_manifest: str = "assets/whale/manifest.json"
    sources: list[str] = Field(default_factory=list)
    license_status: str = "unknown"
    bilibili_owner: BilibiliOwner | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("voice_profile")
    @classmethod
    def reference_voice(cls, value):
        return canonical_voice_id(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value):
        if value is None:
            return None
        if any(len(name.splitlines()) != 1 for name in value):
            raise ValueError("每个称呼须为一行")
        return list(dict.fromkeys(value))

    @field_validator("display_name")
    @classmethod
    def single_line_display_name(cls, value):
        if value is None:
            return None
        value = value.strip()
        if len(value.splitlines()) > 1:
            raise ValueError("前台显示名须为一行")
        return value

    @field_validator("owner_address", mode="before")
    @classmethod
    def single_line_owner_address(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value.splitlines()) > 1:
                raise ValueError("对用户的称呼须为一行")
            return value or None
        return value

    @field_validator("self_description", "speaking_style", mode="before")
    @classmethod
    def single_line_profile(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value.splitlines()) > 1:
                raise ValueError("请用一行填写常驻资料")
            return value or None
        return value

    @model_validator(mode="before")
    @classmethod
    def read_legacy_character(cls, value):
        if isinstance(value, dict):
            return {
                key: field for key, field in value.items()
                if key not in {"persona_type", "fine_tuned_model", "compact_system_prompt"}
            }
        return value

    @field_validator("style")
    @classmethod
    def sentence_limits(cls, value):
        minimum, maximum = value.get("min_chars", 1), value.get("max_chars", 60)
        if not isinstance(minimum, int) or not isinstance(maximum, int) or not 1 <= minimum <= maximum <= 120:
            raise ValueError("角色台词长度必须满足 1 ≤ min_chars ≤ max_chars ≤ 120")
        return value


MAX_MEME_BYTES = 8 * 1024 * 1024


def content_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ResponsesConfig(StrictModel):
    base_url: str
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=4096, gt=0, strict=True)
    timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    parallel_reads: int = Field(default=2, ge=1, strict=True)

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value):
        return endpoint(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value):
        if not value.strip():
            raise ValueError("后台模型ID不能为空")
        return value


def tool_function(name, description, arguments, *, require_all=True):
    """Build a strict Responses API function tool definition from a schema or StrictModel."""
    schema = arguments.copy() if isinstance(arguments, dict) else arguments.model_json_schema()
    definitions = schema.pop("$defs", {})

    def wire(node):
        if isinstance(node, list):
            return [wire(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = node["$ref"].removeprefix("#/$defs/")
            node = definitions[target] | {key: value for key, value in node.items() if key != "$ref"}
        result = {key: wire(value) for key, value in node.items() if key not in {"title", "default", "properties"}}
        if "properties" in node:
            result["properties"] = {field: wire(value) for field, value in node["properties"].items()}
            if require_all:
                result["required"] = list(result["properties"])
        return result

    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": wire(schema),
        "strict": require_all,
    }
