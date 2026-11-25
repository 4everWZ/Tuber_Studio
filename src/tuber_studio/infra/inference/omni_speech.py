"""Studio-side sentence boundaries, verified transcription and playback WAV assembly."""
import base64
import binascii
from difflib import SequenceMatcher
import io
import re
import wave

from .http_api import check_response_status


def sentences(text):
    return re.findall(r'.+?(?:(?:[。！？!?]+|\.(?=\s|$))[”’」』）]*\s*|\n+|\Z)', text, re.DOTALL)


def spoken_words(value):
    # Keep word boundaries in Latin text and numeric separators in dates or decimals.
    return re.findall(r"[A-Za-z0-9]+(?:[.,:][0-9]+)*|[^\s，。！？、；：‘’“”《》,.!?;:\"「」『』…]", value)


def transcript_matches(expected: str, heard: str) -> bool:
    """Verify ASR transcript against expected line, allowing minor homophone drift on long lines."""
    exp = [w.lower() for w in spoken_words(expected)]
    got = [w.lower() for w in spoken_words(heard)]
    if not exp or not got:
        return False
    if exp == got:
        return True
    if len(exp) <= 4:
        return False
    ratio_len = len(got) / len(exp)
    if not (0.60 <= ratio_len <= 1.40):
        return False
    return SequenceMatcher(None, exp, got).ratio() >= 0.65


async def read_sentence(client, model, text, reference):
    payload = {"model": model, "input": text, "response_format": "wav", "reference_audio": reference}
    async with client.stream("POST", "audio/speech", json=payload) as response:
        if response.status_code == 422:
            raise ValueError("Omni 未能核实朗读与指定台词一致")
        await check_response_status(response)
        try:
            heard = base64.b64decode(response.headers["X-Omni-Transcript"], validate=True).decode()
        except (KeyError, binascii.Error, UnicodeError):
            raise ValueError("Omni 未返回有效的音频转写") from None
        if not transcript_matches(text, heard):
            raise ValueError("Omni 音频转写与指定台词不符")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 20_000_000:
                raise ValueError("Omni 朗读音频过大")
        return data


async def render(client, model, text, reference):
    source = {"format": "wav", "data": base64.b64encode(reference).decode("ascii")}
    frames = bytearray()
    format_ = None
    for part in sentences(text):
        data = await read_sentence(client, model, part, source)
        with wave.open(io.BytesIO(data), "rb") as reader:
            params = reader.getparams()
            spec = (params.nchannels, params.sampwidth, params.framerate)
            if format_ is not None and spec != format_:
                raise ValueError("Omni 逐句音频格式不同")
            format_ = spec
            pcm = reader.readframes(params.nframes)
            frame_size = params.nchannels * params.sampwidth
            if len(pcm) != params.nframes * frame_size:
                raise ValueError("Omni 朗读音频截断")
            if not pcm.strip(b"\x80" if params.sampwidth == 1 else b"\x00"):
                raise ValueError("Omni 朗读返回全静音")
            if len(frames) + len(pcm) > min(20_000_000, params.framerate * frame_size * 90):
                raise ValueError("朗读音频超过时长或大小限制")
            frames.extend(pcm)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((*format_, 0, "NONE", "PCM"))
        writer.writeframes(frames)
    return output.getvalue()
