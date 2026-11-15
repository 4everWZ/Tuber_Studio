"""Forward one authenticated Realtime session to a dedicated studio program stream."""

import asyncio
import base64
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from publisher import PCM_RATE, ProgramPublisher, publish_address

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger(__name__)
stage = asyncio.Lock()


def secret(name):
    if path := os.getenv(name + "_FILE"):
        return Path(path).read_text(encoding="utf-8").strip()
    return os.getenv(name, "").strip()


def authorized(header):
    access = secret("MEDIA_ACCESS_TOKEN")
    return bool(access) and hmac.compare_digest(header.encode(), ("Bearer " + access).encode())


def engine_url():
    target = urlsplit(os.getenv("OMNI_GATEWAY_URL", "http://omni-gateway:8000"))
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
        raise ValueError("invalid inference gateway URL")
    query = "duplex=1&program=1" if os.getenv("OMNI_PROGRAM_PRIORITY") == "1" else "duplex=1"
    return urlunsplit(("wss" if target.scheme == "https" else "ws", target.netloc,
                       target.path.rstrip("/") + "/v1/realtime", query, ""))


@app.get("/healthz")
async def health():
    return {"process": "ready", "active_program": stage.locked(), "avatar": "studio-gif"}


@app.websocket("/session")
async def session(client: WebSocket):
    if not authorized(client.headers.get("authorization", "")):
        await client.close(code=1008)
        return
    if stage.locked():
        await client.close(code=1013)
        return
    async with stage:
        await client.accept()
        publisher = None
        try:
            model = os.getenv("OMNI_MODEL_ALIAS", "omni-default")
            hello = await asyncio.wait_for(client.receive_json(), timeout=10)
            if not isinstance(hello, dict) or hello.get("type") != "session.update" or not isinstance(hello.get("session"), dict):
                await client.close(code=1008)
                return
            hello["session"].update(model=model, output_audio_format="pcm16")
            token = secret("OMNI_GATEWAY_TOKEN")
            url = publish_address(os.environ["MEDIA_PUBLISH_URL"], os.getenv("MEDIA_PUBLISH_USER"),
                                  secret("MEDIA_PUBLISH_PASSWORD"))
            async with websockets.connect(engine_url(), additional_headers={"Authorization": "Bearer " + token},
                                          open_timeout=10, close_timeout=2, max_size=2_097_152, proxy=None) as engine:
                await engine.send(json.dumps(hello))
                opened = json.loads(await asyncio.wait_for(engine.recv(), timeout=30))
                capabilities = (opened.get("session") or {}).get("capabilities") or {}
                if opened.get("type") != "session.created" or capabilities.get("implementation_level") != "model_native_duplex":
                    await client.send_json({"type": "error", "error": {"message": "Native duplex was not negotiated"}})
                    await client.close(code=1011)
                    return
                publisher = ProgramPublisher(url, Path(os.getenv("AVATAR_ASSETS", "/assets/whale")))
                await publisher.start()
                await client.send_json(opened)
                await relay(client, engine, publisher)
                await client.close(code=1000)
        except WebSocketDisconnect:
            pass
        except websockets.ConnectionClosed:
            await client.close(code=1011)
        except Exception as error:
            log.warning("omni media session failed: %s", type(error).__name__)
            await client.send_json({"type": "error", "error": {"message": "Program media session ended unexpectedly"}})
            await client.close(code=1011)
        finally:
            if publisher:
                await publisher.close()


async def relay(client, engine, publisher):
    latest = None
    cancelled = None
    epoch = -1

    async def upstream():
        nonlocal cancelled
        while True:
            event = await client.receive_json()
            if not isinstance(event, dict):
                raise ValueError("unexpected realtime message")
            kind = event.get("type")
            if kind == "session.update":
                raise ValueError("media session settings are fixed for the active program")
            if kind in {"response.cancel", "output_audio_buffer.clear", "barge_in"} or (
                kind == "turn.signal" and event.get("event") == "barge_in"
            ):
                target = event.get("response_id")
                if not target or target == latest:
                    publisher.clear()
                    cancelled = latest
            await engine.send(json.dumps(event))

    async def downstream():
        nonlocal epoch, latest, cancelled
        async for message in engine:
            event = json.loads(message)
            kind = event.get("type")
            response_id = event.get("response_id")
            if (
                kind == "output_audio_buffer.cleared"
                or kind == "response.done" and (event.get("response") or {}).get("status") in {"cancelled", "failed"}
            ) and (not response_id or response_id == latest):
                publisher.clear()
                cancelled = latest
            if kind == "response.output_audio.delta":
                metadata = event.get("metadata") or {}
                new_epoch = metadata.get("epoch", epoch)
                if new_epoch < epoch or (cancelled and response_id == cancelled):
                    continue
                if new_epoch > epoch:
                    publisher.clear()
                    epoch = new_epoch
                if event.get("sample_rate_hz", PCM_RATE) != PCM_RATE or event.get("format", "pcm16") != "pcm16":
                    raise ValueError("unexpected output audio format")
                latest = response_id
                publisher.push(base64.b64decode(event["delta"], validate=True))
            await client.send_json(event)

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream()), publisher.task]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
