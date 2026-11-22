"""Non-secret configuration and runtime-owned scoped credentials."""

import json
import os
import secrets
import tempfile
from copy import deepcopy
from pathlib import Path

from .models import DEFAULT_CHARACTER_VOICE, ResponsesConfig, canonical_voice_id
from .retention import validate_days
from .validation import endpoint, project_wav, validate_api_key

ROOT = Path(__file__).resolve().parents[3]

RETIRED_SECTIONS = {"llm", "vision", "video_vision", "tts", "responses", "inference_broker", "work_mode_profiles"}
RESPONSE_OPTIONS = {"max_output_tokens", "parallel_reads", "timeout_seconds"}
VIDEO_OPTIONS = {"background_model_revision", "max_frames", "max_pixels", "max_bytes", "max_output_tokens",
                 "timeout_seconds", "frame_strategy", "image_order"}
GENERATION_OPTIONS = {"auto_compact_token_limit", "context_tokens", "native_context_tokens",
                      "min_p", "presence_penalty", "repeat_penalty", "seed"}


def omni_base(url):
    base = endpoint(url)
    return base if base.endswith("/v1") else base + "/v1"


def responses_profile(omni):
    return {**omni.get("responses_options", {}), "base_url": omni["base_url"].removesuffix("/v1") + "/llm/v1",
            "model": omni["responses_model"]}


def operation_profile(omni):
    """Operational decisions share the background text engine and gateway credential."""
    profile = responses_profile(omni)
    return omni | {
        "base_url": profile["base_url"],
        "model": profile["model"],
        "structured_output": "json_schema",
        "enable_thinking": True,
    }


def video_profile(omni):
    background = operation_profile(omni)
    options = {key: value for key, value in omni.get("video_options", {}).items() if key in VIDEO_OPTIONS}
    pin = options.pop("background_model_revision", "")
    return {**options, "base_url": background["base_url"], "model": background["model"],
            "model_revision": pin, "enable_thinking": background["enable_thinking"]}


def load_profile(saved, defaults):
    """Read old volumes in memory. Carry routes and secrets only from an identified Omni connection."""
    data = {name: value for name, value in (defaults | saved).items() if name not in RETIRED_SECTIONS}
    profile = deepcopy(defaults["omni"])
    old = saved.get("llm", {})
    video = saved.get("video_vision", {})
    speech = saved.get("tts", {})
    legacy_base = None
    if "omni" in saved:
        if not isinstance(saved["omni"], dict):
            raise ValueError("Omni 配置须为对象")
        profile.update(saved["omni"])
        if isinstance(profile.get("video_options"), dict):
            # A saved Omni checkpoint pin belongs to the former vision route.
            profile["video_options"] = {key: value for key, value in profile["video_options"].items()
                                        if key != "model_revision"}
    elif old.get("kind") == "openai_compatible" and (
        old.get("context_source") == "model_card" and old.get("model") == "omni-default"
        or video.get("adapter") == "omni" and video.get("base_url") == old.get("base_url")
        or speech.get("kind") == "omni_reference" and speech.get("base_url") == old.get("base_url")
    ):
        profile.update({name: old[name] for name in (profile.keys() | GENERATION_OPTIONS) & old.keys()
                        if name not in {"context_source", "prompt_token_endpoint", "structured_output"}})
        legacy_base = endpoint(old["base_url"])
        if video.get("base_url") == old["base_url"] and video.get("model") == old["model"]:
            profile["video_options"] = {key: video[key] for key in VIDEO_OPTIONS & video.keys()}
        responses = saved.get("responses", {})
        if responses.get("base_url", "").rstrip("/") == responses_profile(profile)["base_url"]:
            profile["responses_model"] = responses["model"]
            profile["responses_options"].update({key: responses[key] for key in RESPONSE_OPTIONS & responses.keys()})
    profile["base_url"] = omni_base(profile["base_url"])
    data["omni"] = profile
    selected = saved.get("voice_id") or speech.get("voice_id")
    if not selected:
        legacy_voice = canonical_voice_id(speech.get("voice"))
        if speech.get("kind") == "openai_compatible":
            selected = "external_api"
        elif speech.get("kind") == "prerecorded":
            selected = "whale_sample_v1"
        elif legacy_voice in ("whale_reference", "yaoyao_reference", "whale_sample_v1", "external_api"):
            selected = legacy_voice
    data["voice_id"] = canonical_voice_id(selected or defaults["voice_id"])
    return data, legacy_base



class _EndpointKey(str):
    """Carry the original route through copies of the settings transaction's key map."""

    def __new__(cls, value, base_url):
        validate_api_key(value)
        instance = super().__new__(cls, value)
        instance.base_url = endpoint(base_url) if base_url else None
        return instance

    def __getnewargs__(self):
        return str(self), self.base_url


class Settings:
    def __init__(self, root: Path = ROOT, runtime: Path | None = None):
        self.root = root.resolve()
        self.runtime = (runtime or Path(os.getenv("TUBER_RUNTIME", root / "runtime"))).resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.audio = self.runtime / "audio"
        self.audio.mkdir(exist_ok=True)
        self.path = Path(os.getenv("TUBER_CONFIG", self.runtime / "studio.json"))
        defaults = json.loads((self.root / "config/studio.example.json").read_text(encoding="utf-8"))
        self._omni_fields = defaults["omni"].keys() | GENERATION_OPTIONS | {"video_options"}
        saved = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else defaults
        self.data, legacy_base = load_profile(saved, defaults)
        self.data.pop("event_retention_days", None)
        self.data.setdefault("conversation_retention_days", 30)
        self.data.setdefault("features", {}).setdefault("live_enabled", False)
        self._validate()
        self.admin_token = os.getenv("TUBER_ADMIN_TOKEN") or self._secret("admin-token")
        self.overlay_token = self._secret("overlay-token")
        self.credentials_path = self.runtime / "provider-credentials.json"
        self._legacy_public = "omni" not in saved
        self.provider_keys = self.read_provider_keys(self.credentials_path, legacy_base=legacy_base)

    @staticmethod
    def read_provider_keys(path, *, legacy_base=None):
        if path.is_symlink():
            raise ValueError("API凭证文件不能使用符号链接")
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(saved, dict) or saved.keys() - {"omni", "llm", "tts", "vision", "responses"}:
            raise ValueError("API凭证文件格式无效")
        entry = saved.get("omni")
        if entry is None and legacy_base:
            old = saved.get("llm")
            if isinstance(old, dict) and old.get("base_url") == legacy_base:
                entry = old
        if entry is None:
            return {}
        if not isinstance(entry, dict) or set(entry) != {"key", "base_url"} or not entry["base_url"]:
            raise ValueError("Omni 凭证需要绑定服务地址")
        return {"omni": _EndpointKey(entry["key"], entry["base_url"])}

    @property
    def responses_config(self):
        return responses_profile(self.data["omni"])

    @property
    def video_config(self):
        return video_profile(self.data["omni"])

    def _validate(self, data=None):
        data = self.data if data is None else data
        if RETIRED_SECTIONS.intersection(data):
            raise ValueError("仅支持 Omni 连接")
        omni = data["omni"]
        if not isinstance(omni, dict) or omni_base(omni.get("base_url")) != omni["base_url"]:
            raise ValueError("Omni 地址须为有效的 /v1 接口")
        if omni.keys() - self._omni_fields:
            raise ValueError("未知 Omni 配置字段")
        if (omni.get("context_source"), omni.get("structured_output"), omni.get("prompt_token_endpoint")) != (
            "model_card", "structural_tag", True
        ):
            raise ValueError("Omni 配置需要模型卡窗口、结构化输出及服务计量")
        if not isinstance(omni.get("model"), str) or not omni["model"].strip():
            raise ValueError("请填写 Omni 模型别名")
        if type(omni.get("enable_thinking")) is not bool:
            raise ValueError("思考开关须为布尔值")
        if omni.get("auto_compact_token_limit_scope") not in {"total", "body_after_prefix"}:
            raise ValueError("未知上下文压缩阈值范围")
        percent = omni.get("effective_context_window_percent")
        if type(percent) is not int or not 1 <= percent <= 95:
            raise ValueError("有效上下文比例必须在1到95之间")
        if "context_tokens" in omni or "native_context_tokens" in omni:
            window = omni.get("context_tokens", omni.get("native_context_tokens"))
            native = omni.get("native_context_tokens", window)
            if type(window) is not int or type(native) is not int or not 4096 <= window <= native <= 2_147_483_647:
                raise ValueError("保存的窗口范围无效")
        for name in ("max_tokens", "operations_max_tokens"):
            if type(omni.get(name)) is not int or not 1 <= omni[name] <= 65536:
                raise ValueError("输出额度须为1到65536 tokens")
        for name, fields in (("responses_options", RESPONSE_OPTIONS), ("video_options", VIDEO_OPTIONS)):
            options = omni.get(name, {})
            if not isinstance(options, dict) or options.keys() - fields:
                raise ValueError("未知 Omni 能力配置")
        ResponsesConfig.model_validate(responses_profile(omni))
        if not isinstance(data["voice_id"], str) or not data["voice_id"]:
            raise ValueError("音色标识无效")
        if not 1 <= data["queue_limit"] <= 1000 or not 1 <= data["event_ttl_seconds"] <= 3600:
            raise ValueError("队列深度或TTL越界")
        validate_days(data["conversation_retention_days"])

    @property
    def provider_keys(self):
        return self._provider_keys

    @provider_keys.setter
    def provider_keys(self, keys):
        if keys.keys() - {"omni"}:
            raise ValueError("仅支持 Omni 凭证")
        self._provider_keys = {name: value if isinstance(value, _EndpointKey)
                               else _EndpointKey(value, self.data["omni"]["base_url"])
                               for name, value in keys.items()}

    def provider_key(self, name):
        if name != "omni":
            raise ValueError("仅支持 Omni 凭证")
        value = self.provider_keys.get(name)
        return str(value) if value and value.base_url == self.data["omni"]["base_url"] else None

    def public_data(self):
        return redact_credentials(self.data) | {"provider_credentials": {"omni": bool(self.provider_key("omni"))}}

    def migrate_connection(self):
        """Persist the converted connection once the application owns the business writer."""
        if self._legacy_public:
            self.save_public(self.data)

    def save_public(self, data):
        self._validate(data)
        if self._legacy_public and self.credentials_path.exists():
            self.save_connection(data, self.provider_keys)
        else:
            atomic_json(self.path, data)
            self._legacy_public = False

    def save_keys(self, keys):
        if keys.keys() - {"omni"}:
            raise ValueError("仅支持 Omni 凭证")
        records = {}
        for name, value in keys.items():
            bound = value if isinstance(value, _EndpointKey) else _EndpointKey(value, self.data["omni"]["base_url"])
            records[name] = {"base_url": bound.base_url, "key": str(bound)}
        atomic_json(self.credentials_path, records, private=True)

    def save_connection(self, data, keys):
        """Commit both files under the caller's configuration or staged-volume lock."""
        self._validate(data)
        bound = {name: value if isinstance(value, _EndpointKey) else _EndpointKey(value, data["omni"]["base_url"])
                 for name, value in keys.items()}
        files = (self.path, self.credentials_path)
        if any(path.is_symlink() for path in files):
            raise ValueError("配置文件不能使用符号链接")
        previous = {path: path.read_bytes() if path.exists() else None for path in files}
        try:
            self.save_keys(bound)
            atomic_json(self.path, data)
        except BaseException:
            restored = True
            for path, content in previous.items():
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_bytes(path, content, private=path == self.credentials_path)
                except (OSError, ValueError):
                    restored = False
            if not restored:
                raise RuntimeError("Omni 设置恢复结果未确认，请重新检查状态") from None
            raise
        self._legacy_public = False

    def _secret(self, name: str) -> str:
        path = self.runtime / name
        if not path.exists():
            path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            path.chmod(0o600)
        return path.read_text(encoding="utf-8").strip()

    def asset_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root / "assets") or not path.is_file():
            raise ValueError("素材不存在或路径越界")
        return path

    def voice_reference_path(self, voice: dict) -> Path:
        """Read only a configured local voice's project-relative reference WAV."""
        if not isinstance(voice, dict) or voice != self.voice(voice.get("id")):
            raise ValueError("参考音频须属于已配置的本地音色")
        relative = voice.get("reference_asset")
        if voice.get("provider") != "omni_reference" or not isinstance(relative, str) or not relative:
            raise ValueError("参考音频须属于已配置的本地音色")
        if Path(relative).anchor:
            raise ValueError("参考音频须使用项目相对路径")
        return project_wav(self.root, relative, runtime=self.runtime)

    def voices(self) -> list[dict]:
        return json.loads((self.root / "config/voices.json").read_text(encoding="utf-8"))

    def voice(self, voice_id: str) -> dict:
        voice_id = canonical_voice_id(voice_id)
        voice = next((v for v in self.voices() if v["id"] == voice_id), None)
        if voice is None:
            raise ValueError("未知音色")
        if self.data.get("commercial_mode") and not voice.get("commercial_allowed", False):
            raise ValueError("商业模式不能启用未获商业授权的音色")
        return voice

    def default_character_voice(self) -> str:
        try:
            voice = self.voice("yaoyao_reference")
            self.voice_reference_path(voice)
        except (ValueError, OSError):
            return DEFAULT_CHARACTER_VOICE
        return voice["id"]

    def assets(self, character: dict | None = None) -> list[dict]:
        manifest = (character or {}).get("avatar_manifest", "assets/whale/manifest.json")
        try:
            value = json.loads(self.asset_path(manifest).read_text(encoding="utf-8"))
            entries = value if isinstance(value, list) else value.get("entries", value.get("assets", []))
            for entry in entries:
                path = entry["path"].replace("\\", "/")
                entry["url"] = "/" + path
                if still := entry.get("still_path"):
                    try:
                        entry["still_url"] = "/" + self.asset_path(still).relative_to(self.root).as_posix()
                    except ValueError:
                        pass
                # A character owns its manifest reference. A cloned card may deliberately
                # reuse that manifest without inheriting the original character identity.
                if character:
                    entry["character_id"] = character["id"]
            return entries
        except (ValueError, OSError, KeyError):
            return []

    def idle_avatar(self, character: dict) -> dict:
        for entry in self.assets(character):
            if entry.get("tag") == "idle":
                try:
                    self.asset_path(entry["path"])
                except ValueError:
                    continue
                return entry
        return {}


def redact_credentials(value):
    if isinstance(value, dict):
        private = {
            "api_key",
            "apikey",
            "authorization",
            "password",
            "cookie",
            "credentials",
            "secret",
            "client_secret",
            "token",
            "access_token",
            "refresh_token",
            "ac_time_value",
            "pending_confirmation",
            "admin_token",
            "inference_token",
            "overlay_token",
        }
        return {key: redact_credentials(item) for key, item in value.items() if key.lower() not in private}
    if isinstance(value, list):
        return [redact_credentials(item) for item in value]
    return value


def atomic_json(path, data, *, private=False):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), private=private)


def atomic_bytes(path, data, *, private=False):
    if path.is_symlink():
        raise ValueError("配置文件不能使用符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".provider-config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.chmod(
                temporary, 0o600 if private else (path.stat().st_mode & 0o777 if path.exists() else 0o600)
            )
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
