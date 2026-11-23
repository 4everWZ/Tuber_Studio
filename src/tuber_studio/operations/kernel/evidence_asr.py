"""CC-missing Bilibili audio-stream ASR fallback helpers for BrowsingEvidence."""

from __future__ import annotations

import time

from ...infra.inference.omni_asr import AsrUnavailable
from ...infra.inference.providers import public_error


async def fetch_asr_subtitle_part(adapter, reader, asr, metadata, part, languages):
    """Fetch bounded DASH audio via adapter and transcribe into timestamped subtitle cues."""
    if asr is None:
        raise AsrUnavailable("未配置语音转写服务")
    audio_data = await reader(lambda: adapter.video_stream_audio(metadata, cid=part["cid"]))
    preferred = next(
        (lang.split("-", 1)[0] for lang in (languages or ()) if isinstance(lang, str) and lang),
        "zh",
    )
    if preferred == "ai":
        preferred = "zh"
    transcript = await asr.transcribe(
        audio_data["audio"],
        filename=audio_data.get("filename", "audio.mp3"),
        mime_type=audio_data.get("mime_type", "audio/mpeg"),
        language=preferred or "zh",
    )
    prefix = f"{part['cid']}:"
    segments = [
        {
            "id": identity if (identity := str(seg["id"])).startswith(prefix) else prefix + identity,
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": str(seg["text"]),
        }
        for seg in transcript["segments"]
        if str(seg.get("text") or "").strip()
    ]
    if not segments:
        raise AsrUnavailable("ASR 未产出有效的语音分段")
    return {
        "bvid": metadata["bvid"],
        "cid": str(part["cid"]),
        "track_id": "asr:whisper",
        "language": transcript.get("language") or preferred or "zh",
        "is_ai": True,
        "asr": True,
        "subtitle_source": "asr",
        "segments": segments,
        "coverage": {
            "complete": not audio_data.get("truncated", False),
            "start": segments[0]["start"],
            "end": segments[-1]["end"],
            "segment_count": len(segments),
        },
        "fetched_at": time.time(),
    }


async def collect_subtitles_or_asr(evidence, metadata, languages):
    """Read CC subtitles per part, falling back cleanly to ASR when available."""
    parts, missing = [], []
    subtitle_error = None
    for part in metadata["parts"]:
        try:
            parts.append(await evidence.subtitle_part(metadata, part, languages))
        except (ValueError, TimeoutError) as error:
            kind = getattr(error, "kind", "invalid")
            subtitle_error = subtitle_error or public_error(error)
            if evidence.has_asr_provider() and hasattr(evidence.adapter, "video_stream_audio"):
                try:
                    parts.append(
                        await fetch_asr_subtitle_part(
                            evidence.adapter, evidence.reader, evidence.asr, metadata, part, languages
                        )
                    )
                    continue
                except (AsrUnavailable, ValueError, TimeoutError, OSError) as asr_error:
                    subtitle_error = f"{subtitle_error}；ASR回退：{public_error(asr_error)}"
            missing.append({"cid": part["cid"], "reason": kind})
    return parts, missing, subtitle_error
