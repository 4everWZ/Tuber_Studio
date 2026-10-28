"""Non-secret configuration and runtime-owned scoped credentials."""

import json
import os
import secrets
from pathlib import Path

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
        self._validate()
        self.path = path
        self.admin_token = os.getenv("TUBER_ADMIN_TOKEN") or self._secret("admin-token")
        self.overlay_token = self._secret("overlay-token")
        self.inference_token = (
            self._secret("inference-token") if self.data.get("inference_broker", {}).get("enabled") else None
        )

    def _validate(self):
        llm, tts = self.data["llm"], self.data["tts"]
        if llm["kind"] not in {"llama_cpp", "demo"} or tts["kind"] not in {"gpt_sovits", "prerecorded"}:
            raise ValueError("未知模型或语音Provider")
        if llm["kind"] != "demo" and not 131072 <= llm["context_tokens"] <= llm.get(
            "native_context_tokens", 131072
        ):
            raise ValueError("真实模型上下文至少128K，且不能超过已确认原生上限")
        if llm.get("auto_compact_token_limit_scope", "total") not in {"total", "body_after_prefix"}:
            raise ValueError("未知上下文压缩阈值范围")
        if not 1 <= llm.get("effective_context_window_percent", 95) <= 95:
            raise ValueError("有效上下文比例必须在1到95之间")
        if not 1 <= self.data["queue_limit"] <= 1000 or not 1 <= self.data["event_ttl_seconds"] <= 3600:
            raise ValueError("队列深度或TTL越界")
        from .retention import validate_days

        validate_days(self.data["conversation_retention_days"])

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
