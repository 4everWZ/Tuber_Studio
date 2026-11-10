"""Opt-in CPU transcription of an operator-supplied, CID-bound public clip."""

import asyncio
import hashlib
import io
import json
import os
import re
import time
import wave
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from ...media_cache import linked


class LocalVideoASR:
    def __init__(self, settings):
        self.directory = settings.runtime / "public-audio-clips"
        self.executable = os.getenv("TUBER_VIDEO_ASR_CLI")
        self.model = os.getenv("TUBER_VIDEO_ASR_MODEL")
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(self.executable and self.model)

    async def transcribe(self, metadata, part, *, deadline=None):
        bvid, cid = metadata["bvid"], str(part["cid"])
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or not re.fullmatch(r"[1-9][0-9]{0,19}", cid):
            return None
        clip = self.directory / f"{bvid}-{cid}.wav"
        if linked(self.directory) or linked(clip) or not clip.is_file():
            return None
        async with asyncio.timeout(min(45, max(0, deadline - time.time())) if deadline else 45):
            async with self.lock:
                with clip.open("rb") as source:
                    raw = source.read(3 * 1024 * 1024 + 1)
                if len(raw) > 3 * 1024 * 1024:
                    raise ValueError("本地ASR音频超过3MiB")
                with wave.open(io.BytesIO(raw)) as source:
                    if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
                        raise ValueError("本地ASR需要16kHz单声道16-bit PCM")
                    duration = source.getnframes() / 16000
                    if not 0 < duration <= min(90, part["duration"]):
                        raise ValueError("本地ASR片段须在对应分P前90秒内")
                    if len(source.readframes(source.getnframes())) != 2 * source.getnframes():
                        raise ValueError("本地ASR音频不完整")
                model_stat = Path(self.model).stat()
                identity = hashlib.sha256(raw).hexdigest()
                model_version = f"{Path(self.model).name}:{model_stat.st_size}:{model_stat.st_mtime_ns}"
                key = (bvid, cid, identity, model_version)
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return self.cache[key]
                segments = await self._read(raw, cid, duration)
                if not segments:
                    return None
                result = {
                    "cid": cid, "bvid": bvid, "track_id": f"local-asr:{identity}:{model_version}",
                    "language": "auto", "is_ai": True, "asr": True, "segments": segments,
                    "coverage": {"kind": "sampled_audio", "complete": False, "start": 0, "end": duration},
                }
                self.cache[key] = result
                if len(self.cache) > 8:
                    self.cache.popitem(last=False)
                return result

    async def _read(self, raw, cid, duration):
        with TemporaryDirectory(prefix="asr-", dir=self.directory) as tmp:
            clip = Path(tmp) / "clip.wav"
            output = Path(tmp) / "transcript"
            clip.write_bytes(raw)
            child = await asyncio.create_subprocess_exec(
                self.executable, "-m", self.model, "-f", str(clip), "-l", "auto", "-ng", "-t", "2",
                "-oj", "-of", str(output), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                if await child.wait():
                    raise ValueError("本地ASR执行失败")
            finally:
                if child.returncode is None:
                    with suppress(ProcessLookupError):
                        child.kill()
                    await child.wait()
            with output.with_suffix(".json").open("rb") as source:
                data = source.read(131073)
        if len(data) > 131072:
            raise ValueError("本地ASR结果过大")
        result = json.loads(data)
        if not isinstance(result, dict) or not isinstance(result.get("transcription"), list):
            raise ValueError("本地ASR结果格式无效")
        segments = []
        for row in result.get("transcription", [])[:200]:
            if not isinstance(row, dict) or not isinstance(row.get("offsets"), dict):
                raise ValueError("本地ASR时码无效")
            text = row.get("text")
            start, end = (row["offsets"].get(k) for k in ("from", "to"))
            if isinstance(text, str) and text.strip() and type(start) is int and type(end) is int and (
                0 <= start < min(end, duration * 1000) and end <= duration * 1000 + 20
            ):
                segments.append({
                    "id": f"{cid}:asr:{len(segments) + 1}", "start": start / 1000,
                    "end": min(end / 1000, duration), "text": text.strip()[:1000],
                })
        return segments
