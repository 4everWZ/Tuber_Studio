"""Transcribe operator-supplied public audio clips through the configured Omni API."""

import asyncio
import base64
import hashlib
import io
import os
import re
import time
import wave
from collections import OrderedDict

from ...http_api import InferenceQueueBusy, api_client, response_json
from ...media_cache import linked


def windows(raw, maximum_seconds):
    with wave.open(io.BytesIO(raw)) as clip:
        rate = clip.getframerate()
        if (clip.getnchannels(), clip.getsampwidth(), rate) != (1, 2, 16000):
            raise ValueError("公开音频须为 16 kHz 单声道 16-bit PCM")
        count = clip.getnframes()
        if not 0 < count <= min(90, maximum_seconds) * rate:
            raise ValueError("音频长度超出分 P 或 90 秒取材范围")
        for start in range(0, count, 15 * rate):
            length = min(15 * rate, count - start)
            pcm = clip.readframes(length)
            if len(pcm) != length * 2:
                raise ValueError("公开音频不完整")
            if not pcm.strip(b"\x00"):
                continue
            stream = io.BytesIO()
            with wave.open(stream, "wb") as output:
                output.setparams((1, 2, rate, 0, "NONE", ""))
                output.writeframes(pcm)
            yield start / rate, (start + length) / rate, stream.getvalue()


class PublicClipTranscription:
    def __init__(self, settings):
        self.settings = settings
        self.directory = settings.runtime / "public-audio-clips"
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    @property
    def enabled(self):
        return os.getenv("TUBER_TRANSCRIBE_PUBLIC_CLIPS") == "1" and self.directory.is_dir()

    async def transcribe(self, metadata, part, *, deadline=None):
        bvid, cid = metadata["bvid"], str(part["cid"])
        if not self.enabled or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or not re.fullmatch(r"[1-9][0-9]{0,19}", cid):
            return None
        clip = self.directory / f"{bvid}-{cid}.wav"
        if linked(self.directory) or linked(clip) or not clip.is_file():
            return None
        timeout = self.settings.data["omni"]["timeout_seconds"]
        async with asyncio.timeout(min(timeout, max(0, deadline - time.time())) if deadline else timeout):
            async with self.lock:
                with clip.open("rb") as source:
                    raw = source.read(3 * 1024 * 1024 + 1)
                if len(raw) > 3 * 1024 * 1024:
                    raise ValueError("公开音频超过 3 MiB")
                profile = self.settings.data["omni"]
                identity = hashlib.sha256(raw).hexdigest()
                route = (profile["base_url"], profile["model"])
                key = (bvid, cid, identity, part["duration"], route)
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return self.cache[key]
                segments = []
                async with api_client(profile, self.settings.provider_key("omni")) as client:
                    for start, end, data in windows(raw, part["duration"]):
                        audio = {"format": "wav", "data": base64.b64encode(data).decode("ascii")}
                        payload = {
                            "model": profile["model"], "modalities": ["text"], "max_tokens": 512,
                            "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
                            "messages": [
                                {"role": "system", "content": "逐字转写录音里的话，保留原语言和标点。只输出原话。无清晰人声时输出空字符串。"},
                                {"role": "user", "content": [{"type": "input_audio", "input_audio": audio}]},
                            ],
                        }
                        while True:
                            try:
                                reply = await response_json(client, "POST", "chat/completions", payload=payload)
                                break
                            except InferenceQueueBusy as busy:
                                await asyncio.sleep(busy.retry_after)
                        try:
                            choice = reply["choices"][0]
                            text = choice["message"]["content"]
                        except (IndexError, KeyError, TypeError):
                            raise ValueError("Omni 返回的音频转写格式无效") from None
                        if choice.get("finish_reason") != "stop" or not isinstance(text, str) or len(text) > 2000:
                            raise ValueError("Omni 未返回完整的音频转写")
                        if text.strip() not in {"", '""', "“”"}:
                            segments.append({"id": f"{cid}:asr:{len(segments) + 1}", "start": start,
                                             "end": end, "text": text.strip()})
                if not segments:
                    return None
                result = {"cid": cid, "bvid": bvid, "track_id": f"omni-asr:{identity}:{route[1]}",
                          "asr": True, "is_ai": True, "language": "auto", "segments": segments,
                          "coverage": {"kind": "sampled_audio", "complete": False,
                                       "start": segments[0]["start"], "end": segments[-1]["end"]}}
                self.cache[key] = result
                if len(self.cache) > 8:
                    self.cache.popitem(last=False)
                return result
