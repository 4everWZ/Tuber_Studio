"""Audio metadata inspection and upload format limits."""

import array
import math
import sys
import wave

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

MAX_UPLOAD = 96 * 1024 * 1024
MEDIA_TYPES = {".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg"}


def inspect_wav(path, *, envelope=False, scan=True):
    """Bounded PCM validation; envelope uses the isolated vocal, never accompaniment."""
    try:
        with wave.open(str(path), "rb") as stream:
            channels, width, rate, count = (
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getframerate(),
                stream.getnframes(),
            )
            duration = count / rate
            if (
                channels not in {1, 2}
                or width != 2
                or not 8000 <= rate <= 96000
                or not 0.05 <= duration <= 600
            ):
                raise ValueError("请使用0.05至600秒、单／双声道的16-bit PCM WAV")
            if not scan:
                return {"duration": duration, "envelope": [], "step": 0.05}
            values, peak = [], 0
            frames = max(1, round(rate * 0.05))
            actual = 0
            while chunk := stream.readframes(frames):
                samples = array.array("h", chunk)
                if sys.byteorder != "little":
                    samples.byteswap()
                actual += len(chunk)
                level = max((abs(v) for v in samples), default=0)
                peak = max(peak, level)
                if envelope:
                    values.append(round(math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768, 4))
            if actual != count * channels * width or peak < 20:
                raise ValueError("音频不完整或全静音")
            return {"duration": duration, "envelope": values, "step": 0.05}
    except (wave.Error, EOFError, ZeroDivisionError, OSError):
        raise ValueError("音频须为完整有效的16-bit PCM WAV") from None


def inspect_audio(path, *, scan=True):
    """Identify the stream, never trust a URL, extension or provider quality label."""
    with path.open("rb") as source:
        header = source.read(12)
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return {**inspect_wav(path, scan=scan), "format": "wav", "media_type": "audio/wav"}
    try:
        audio = FLAC(path) if header[:4] == b"fLaC" else MP3(path)
        info = audio.info
        if not math.isfinite(info.length) or not 0.05 <= info.length <= 600:
            raise ValueError("歌曲须为0.05至600秒")
        if info.channels not in {1, 2} or not 8000 <= info.sample_rate <= 192000:
            raise ValueError("歌曲声道或采样率不支持")
        if isinstance(audio, MP3) and (info.layer != 3 or info.sketchy):
            raise ValueError("MP3 数据不完整或格式无效")
        kind = "flac" if isinstance(audio, FLAC) else "mp3"
        return {
            "duration": info.length,
            "format": kind,
            "media_type": MEDIA_TYPES["." + kind],
            "bitrate": info.bitrate,
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "bits_per_sample": getattr(info, "bits_per_sample", None),
        }
    except (MutagenError, OSError, EOFError):
        raise ValueError("音频须为完整有效的 PCM WAV、FLAC 或 MP3") from None

