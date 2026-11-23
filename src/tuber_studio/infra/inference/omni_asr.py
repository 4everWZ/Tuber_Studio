"""Gateway client for CPU faster-whisper audio transcription (/v1/audio/transcriptions)."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from ...core.validation import endpoint
from .http_api import inference_transport

MAX_ASR_UPLOAD_BYTES = 25 * 1024 * 1024


class AsrUnavailable(ValueError):
    """Raised when the gateway ASR endpoint is disabled, unconfigured, or unreachable."""


def _asr_base_url(raw_url: str) -> str:
    clean = endpoint(raw_url).rstrip("/")
    parsed = urlsplit(clean)
    path = parsed.path.rstrip("/")
    if path.endswith("/llm/v1"):
        path = path[: -len("/llm/v1")] + "/v1"
    elif path.endswith("/llm"):
        path = path[: -len("/llm")] + "/v1"
    elif not path.endswith("/v1"):
        path = path + "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") + "/", "", ""))


class OmniAsrClient:
    """Calls gateway POST /v1/audio/transcriptions and returns timestamped speech cues."""

    def __init__(self, base_url: str, api_key: str | None = None, *, timeout: float = 180.0):
        self.base_url = _asr_base_url(base_url)
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        transport = inference_transport(self.base_url)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": "Bearer " + self.api_key} if self.api_key else {},
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            trust_env=False,
            follow_redirects=False,
            **({"transport": transport} if transport else {}),
        )

    @classmethod
    def from_settings(cls, settings) -> OmniAsrClient | None:
        if settings is None:
            return None
        data = getattr(settings, "data", None) or {}
        omni = data.get("omni") or {}
        raw_url = str(omni.get("base_url") or "").strip()
        if not raw_url:
            return None
        try:
            endpoint(raw_url)
        except ValueError:
            return None
        api_key = settings.provider_key("omni") if hasattr(settings, "provider_key") else None
        return cls(raw_url, api_key)

    async def close(self):
        await self._client.aclose()

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str = "audio.mp3",
        mime_type: str = "audio/mpeg",
        language: str = "zh",
    ) -> dict:
        if not audio_bytes or len(audio_bytes) > MAX_ASR_UPLOAD_BYTES:
            raise AsrUnavailable("音频数据为空或超过转写大小限制")
        data = {"response_format": "verbose_json"}
        if language:
            data["language"] = language
        try:
            response = await self._client.post(
                "audio/transcriptions",
                files={"file": (filename, audio_bytes, mime_type)},
                data=data,
            )
        except (httpx.HTTPError, TimeoutError) as error:
            raise AsrUnavailable("ASR 服务暂不可达") from error

        if response.status_code in {404, 405, 501, 502, 503, 504}:
            raise AsrUnavailable(f"ASR 服务未启用或暂不可用（HTTP {response.status_code}）")
        if response.status_code >= 400:
            raise AsrUnavailable(f"ASR 转写请求被拒绝（HTTP {response.status_code}）")

        try:
            payload = response.json()
        except ValueError as error:
            raise AsrUnavailable("ASR 未返回有效 JSON") from error
        if not isinstance(payload, dict):
            raise AsrUnavailable("ASR 返回格式无效")

        cues = []
        raw_segments = payload.get("segments")
        if isinstance(raw_segments, list):
            for idx, item in enumerate(raw_segments):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start = max(0.0, float(item.get("start", 0.0)))
                    end = max(start, float(item.get("end", start)))
                except (TypeError, ValueError):
                    continue
                cues.append({"id": f"asr:{idx}", "start": round(start, 3), "end": round(end, 3), "text": text})
        if not cues:
            full_text = str(payload.get("text") or "").strip()
            if full_text:
                try:
                    duration = max(0.0, float(payload.get("duration") or 0.0))
                except (TypeError, ValueError):
                    duration = 0.0
                cues.append({"id": "asr:0", "start": 0.0, "end": round(duration, 3), "text": full_text})
        if not cues:
            raise AsrUnavailable("ASR 未识别出有效语音内容")
        return {
            "language": str(payload.get("language") or language or "zh"),
            "duration": float(payload.get("duration") or cues[-1]["end"]),
            "text": str(payload.get("text") or " ".join(seg["text"] for seg in cues)).strip(),
            "segments": cues,
        }
