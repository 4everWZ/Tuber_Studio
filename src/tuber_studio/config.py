"""Non-secret configuration and runtime-owned scoped credentials."""

import json
import os
import secrets
import tempfile
from pathlib import Path

from .http_api import endpoint

ROOT = Path(__file__).resolve().parents[2]


class Settings:
    def __init__(self, root: Path = ROOT, runtime: Path | None = None):
        self.root = root.resolve()
        self.runtime = (runtime or Path(os.getenv("TUBER_RUNTIME", root / "runtime"))).resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.audio = self.runtime / "audio"
        self.audio.mkdir(exist_ok=True)
        path = Path(os.getenv("TUBER_CONFIG", str(self.runtime / "studio.json")))
        example = self.root / "config/studio.example.json"
        self.data = json.loads((path if path.exists() else example).read_text(encoding="utf-8"))
        # Conversation retention now has its own persisted policy; the old
        # seven-day event setting must not silently override the requested 30 days.
        self.data.pop("event_retention_days", None)
        self.data.setdefault("conversation_retention_days", 30)
        self.data.setdefault("features", {}).setdefault("live_enabled", False)
        self.data.setdefault("vision", {"kind": "disabled", "location": "local", "base_url": "", "model": ""})
        self.data["tts"].setdefault("model", "")
        self.data["tts"].setdefault("voice", "")
        self._validate()
        self.path = path
        self.admin_token = os.getenv("TUBER_ADMIN_TOKEN") or self._secret("admin-token")
        self.overlay_token = self._secret("overlay-token")
        self.inference_token = (
            self._secret("inference-token") if self.data.get("inference_broker", {}).get("enabled") else None
        )
        self.credentials_path = self.runtime / "provider-credentials.json"
        if self.credentials_path.is_symlink():
            raise ValueError("API凭证文件不能使用符号链接")
        self.provider_keys = (
            json.loads(self.credentials_path.read_text(encoding="utf-8"))
            if self.credentials_path.exists()
            else {}
        )
        if not isinstance(self.provider_keys, dict) or set(self.provider_keys) - {"llm", "tts", "vision"}:
            raise ValueError("API凭证文件格式无效")
        for value in self.provider_keys.values():
            validate_api_key(value)

    def _validate(self, data=None):
        data = self.data if data is None else data
        llm, tts = data["llm"], data["tts"]
        if llm["kind"] not in {"llama_cpp", "openai_compatible", "demo"} or tts["kind"] not in {
            "gpt_sovits",
            "openai_compatible",
            "prerecorded",
        }:
            raise ValueError("未知模型或语音Provider")
        window, native = llm["context_tokens"], llm.get("native_context_tokens", 131072)
        if (
            type(window) is not int
            or type(native) is not int
            or not 4096 <= window <= native <= 2_147_483_647
        ):
            raise ValueError("上下文须为至少4096的整数，且不能超过服务或模型上限")
        if not 1 <= llm.get("max_tokens", 1024) < window * 95 // 100:
            raise ValueError("输出预算须小于有效上下文")
        if llm["kind"] != "demo":
            endpoint(llm["base_url"])
            if not isinstance(llm.get("model"), str) or not llm["model"].strip():
                raise ValueError("请填写模型ID")
        if tts["kind"] != "prerecorded":
            endpoint(tts["base_url"])
        if tts["kind"] == "openai_compatible" and any(
            not isinstance(tts.get(key), str) or not tts[key].strip() for key in ("model", "voice")
        ):
            raise ValueError("语音API需要模型ID及voice ID")
        vision = data.get("vision", {"kind": "disabled"})
        if vision.get("kind") not in {"disabled", "openai_compatible"} or vision.get(
            "location", "local"
        ) not in {"local", "remote"}:
            raise ValueError("未知视觉接口配置")
        if vision["kind"] != "disabled":
            endpoint(vision["base_url"])
            if not isinstance(vision.get("model"), str) or not vision["model"].strip():
                raise ValueError("视觉接口需要模型ID")
        if llm.get("auto_compact_token_limit_scope", "total") not in {"total", "body_after_prefix"}:
            raise ValueError("未知上下文压缩阈值范围")
        if not 1 <= llm.get("effective_context_window_percent", 95) <= 95:
            raise ValueError("有效上下文比例必须在1到95之间")
        if not 1 <= data["queue_limit"] <= 1000 or not 1 <= data["event_ttl_seconds"] <= 3600:
            raise ValueError("队列深度或TTL越界")
        from .retention import validate_days

        validate_days(data["conversation_retention_days"])

    def provider_key(self, name):
        return self.provider_keys.get(name)

    def broker_key_for(self, base_url):
        broker = self.data.get("inference_broker", {})
        base = broker.get("base_url", "").rstrip("/")
        allowed = {base + path for path in ("/tts", "/llm/2b", "/llm/1b", "/llm/1bq8", "/llm/qwen35")}
        return self.inference_token if broker.get("enabled") and base_url.rstrip("/") in allowed else None

    def public_data(self):
        return redact_credentials(self.data) | {
            "provider_credentials": {
                key: bool(self.provider_keys.get(key)) for key in ("llm", "tts", "vision")
            }
        }

    def save_public(self, data):
        self._validate(data)
        atomic_json(self.path, data)

    def save_keys(self, keys):
        atomic_json(self.credentials_path, keys, private=True)

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

    def voices(self) -> list[dict]:
        return json.loads((self.root / "config/voices.json").read_text(encoding="utf-8"))

    def voice(self, voice_id: str) -> dict:
        voice = next((v for v in self.voices() if v["id"] == voice_id), None)
        if voice is None:
            raise ValueError("未知音色")
        if self.data.get("commercial_mode") and not voice.get("commercial_allowed", False):
            raise ValueError("商业模式不能启用未获商业授权的音色")
        return voice

    def assets(self, character: dict | None = None) -> list[dict]:
        manifest = (character or {}).get("avatar_manifest", "assets/whale/manifest.json")
        try:
            value = json.loads(self.asset_path(manifest).read_text(encoding="utf-8"))
            entries = value if isinstance(value, list) else value.get("entries", value.get("assets", []))
            for entry in entries:
                path = entry["path"].replace("\\", "/")
                entry["url"] = "/" + path
                # A character owns its manifest reference. A cloned card may deliberately
                # reuse that manifest without inheriting the original character identity.
                if character:
                    entry["character_id"] = character["id"]
            return entries
        except (ValueError, OSError, KeyError):
            return []


def validate_api_key(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise ValueError("API key格式无效")


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
