"""Loopback-only fixed-voice GPT-SoVITS v2 service: health and buffered WAV only."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import compact
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from tuber_studio.inference_validation import validate_speech_request

MAX_BODY = 2 * 1024 * 1024
MAX_PCM = 20_000_000
LOGGER = logging.getLogger(__name__)


class VoiceMismatch(ValueError):
    pass


class VoiceAssetsChanged(RuntimeError):
    pass


def select_voice(root, override=None):
    return compact.selected_voice_id(root, override)


class SpeechEngine:
    def __init__(self, root, voice_id, pipeline):
        self.root, self.voice_id, self.pipeline = root, voice_id, pipeline
        self.metadata = pipeline.fixed_reference_metadata
        self.signature = pipeline.fixed_reference_signature
        self.failed = False

    @classmethod
    def load(cls, root, voice_id):
        pipeline = compact.create_pipeline(root, voice_id=voice_id, prepare_if_needed=True)
        return cls(root, voice_id, pipeline)

    def request(self, body):
        if self.failed:
            raise VoiceAssetsChanged("Speech service needs a clean restart")
        try:
            current = compact.reference_signature(self.root, voice_id=self.voice_id, metadata=self.metadata)
        except (OSError, ValueError, KeyError):
            current = None
        if current != self.signature:
            self.failed = True
            raise VoiceAssetsChanged(
                "Voice assets changed; restart speech to refresh their verified features"
            )
        voice = self.signature["voice"]
        params = {**voice.get("params", {}), **body}
        validate_speech_request(params)
        if not isinstance(params.get("text"), str) or not params["text"].strip():
            raise ValueError("Speech needs nonempty text")
        identity = self.metadata["identity"]
        reference = params.get("ref_audio_path", identity["paths"]["reference"])
        try:
            reference = compact._file(self.root, reference, ("runtime/tts/ref", "assets"))
        except (OSError, ValueError, RuntimeError):
            raise VoiceMismatch("Speech accepts only its selected configured reference") from None
        prompt = params.get("prompt_text", identity["reference_text"])
        language = params.get("prompt_lang", identity["prompt_lang"])
        if (
            str(reference) != identity["paths"]["reference"]
            or language != identity["prompt_lang"]
            or compact._normal_prompt(prompt, language) != identity["prompt_text"]
        ):
            raise VoiceMismatch("Reference text/language does not match the selected configured voice")
        params.update(ref_audio_path=str(reference), prompt_text=prompt, prompt_lang=language)
        params.setdefault("media_type", "wav")
        params.setdefault("streaming_mode", False)
        params.setdefault("batch_size", 1)
        params.setdefault("seed", -1)
        return params

    def synthesize(self, params):
        iterator = self.pipeline.run(params)
        chunks, sample_rate, total = [], None, 0
        try:
            for rate, audio in iterator:
                buffer = memoryview(audio)
                if (
                    type(rate) is not int
                    or not 8000 <= rate <= 192000
                    or buffer.ndim != 1
                    or buffer.itemsize != 2
                    or buffer.format != "h"
                    or not (bool(audio.any()) if hasattr(audio, "any") else any(audio))
                ):
                    raise RuntimeError("Speech returned invalid or silent audio")
                if sample_rate is not None and sample_rate != rate:
                    raise RuntimeError("Speech changed sample rate within one buffered response")
                sample_rate = rate
                total += buffer.nbytes
                if total > MAX_PCM:
                    raise RuntimeError("Speech exceeded the buffered audio budget")
                chunks.append(buffer.tobytes())
        finally:
            iterator.close()
        if not chunks:
            raise RuntimeError("Speech returned no audio")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"".join(chunks))
        return output.getvalue()


async def _body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(413, "Speech request exceeds 2MiB")
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Speech request must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Speech request must be an object")
    return body


def create_app(root=None, *, voice_id=None, engine=None):
    root = Path(root or os.environ.get("TUBER_ROOT", "/workspace/tuber")).resolve()
    selected = select_voice(root, voice_id)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = engine or await asyncio.to_thread(SpeechEngine.load, root, selected)
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/docs")
    async def health():
        active = getattr(app.state, "engine", None)
        ready = active is not None and not active.failed
        return JSONResponse(
            {
                "status": "ready" if ready else "unavailable",
                "backend": "gpt_sovits_v2_compact",
                "voice_id": selected,
            },
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/tts")
    async def speech(request: Request):
        body = await _body(request)
        async with lock:
            active = getattr(app.state, "engine", None)
            if active is None:
                raise HTTPException(503, "Speech is not ready")
            try:
                params = active.request(body)
            except VoiceMismatch as error:
                raise HTTPException(409, str(error)) from None
            except VoiceAssetsChanged as error:
                raise HTTPException(503, str(error)) from None
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            try:
                work = asyncio.create_task(asyncio.to_thread(active.synthesize, params))
                try:
                    audio = await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Cancelling a Python await does not stop a running thread.
                    # Keep this request's lock until the real synthesis exits;
                    # process-level broker cancellation still kills the group.
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    try:
                        work.result()
                    except Exception:
                        active.failed = True
                        LOGGER.exception("Cancelled speech synthesis also failed")
                    raise
            except Exception:
                active.failed = True
                LOGGER.exception("Speech synthesis failed; no audio returned")
                raise HTTPException(503, "Speech synthesis failed; restart the speech resource") from None
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-a", "--address", choices=["127.0.0.1"], default="127.0.0.1")
    parser.add_argument("-p", "--port", type=int, default=9011)
    parser.add_argument("--voice-id")
    parser.add_argument("--prepare-reference", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ.get("TUBER_ROOT", "/workspace/tuber")).resolve()
    selected = select_voice(root, args.voice_id)
    if args.prepare_reference:
        compact.prepare(root, voice_id=selected)
        return
    if sys.byteorder != "little":
        raise RuntimeError("The fixed PCM backend requires a little-endian host")
    import uvicorn

    uvicorn.run(
        create_app(root, voice_id=selected), host=args.address, port=args.port, access_log=False, ws="none"
    )


if __name__ == "__main__":
    main()
