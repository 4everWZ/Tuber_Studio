"""Optional loopback protocol test. Run with the omni-avatar-api venv and that repo on PYTHONPATH.

Only the model engine is simulated. Studio and the unchanged gateway run as separate services.
Configure the isolated Studio with origin http://127.0.0.1:18766 and a file containing private-call-fixture.
"""

import asyncio
import base64
import json
import math
import os
import struct

from aiohttp import web
from services.gateway.app import Config, create_app


async def main():
    whep = os.getenv("STUDIO_TEST_WHEP") == "1"
    base = 18865 if whep else 18765
    rate = 24000 if whep else 16000
    observations = {"input": 0, "played": 0, "closed": 0, "played_by_response": {}}
    connections = set()
    silence = bytes(rate)
    tone = b"".join(struct.pack("<h", round(9000 * math.sin(index * math.tau * 220 / rate)))
                    for index in range(2 * rate))
    sound = base64.b64encode(silence + tone + silence).decode()
    sequence = 0

    async def stimulus(request):
        nonlocal sequence
        if len(connections) != 1:
            raise web.HTTPConflict()
        socket, = connections
        if request.match_info["kind"] == "interrupt":
            await socket.send_json({"type": "output_audio_buffer.cleared", "response_id": f"voice-{sequence}"})
        else:
            sequence += 1
            await socket.send_json({"type": "response.output_audio.delta", "response_id": f"voice-{sequence}",
                                    "item_id": "test-item", "format": "pcm16", "sample_rate_hz": rate, "delta": sound})
        return web.json_response({"sequence": sequence})

    async def realtime(request):
        assert dict(request.query) == {"duplex": "1", "autostart": "0"}
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        hello = await socket.receive_json()
        assert hello["type"] == "session.update" and hello["session"]["playback_commit_policy"] == "ack_only"
        assert "instructions" in hello["session"]
        await socket.send_json({"type": "session.created", "session": {"capabilities": {
            "implementation_level": "model_native_duplex", "chunk_period_ms": 1000,
        }}})
        sent_transcript = False
        connections.add(socket)
        async for frame in socket:
            message = json.loads(frame.data)
            if message["type"] == "input_audio_buffer.append":
                samples = base64.b64decode(message["audio"], validate=True)
                assert len(samples) == 32000 and any(samples) and message["sample_rate_hz"] == 16000
                observations["input"] += 1
                await socket.send_json({"type": "response.output_audio_transcript.done", "transcript": "助手的话"})
                if not sent_transcript:
                    await socket.send_json({"type": "conversation.item.added", "item": {"id": "test-user", "role": "user",
                        "type": "message", "content": [{"type": "input_audio"}]}})
                    await socket.send_json({"type": "input_audio_buffer.committed", "item_id": "test-user"})
                    await socket.send_json({"type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "test-user", "content_index": 0, "transcript": "整理一份私有提纲"})
                    sent_transcript = True
            elif message["type"] == "playback.ack":
                assert message["response_id"].startswith("voice-") and message["item_id"] == "test-item"
                observations["played"] = message["played_ms"]
                observations["played_by_response"][message["response_id"]] = message["played_ms"]
            elif message["type"] == "output_audio_buffer.clear":
                await socket.send_json({"type": "output_audio_buffer.cleared", "response_id": f"voice-{sequence}"})
            elif message["type"] == "session.close":
                observations["closed"] += 1
                break
        connections.discard(socket)
        return socket

    async def results(request):
        if whep:
            observations["avatars"] = [{"state": s.state, "source": s.source, "avatar": s.avatar_id,
                                         "generation": s.generation, "peers": len(s.peers), "ack": s.last_ack}
                                        for s in avatar["service"].sessions.values()]
        return web.json_response(observations)

    engine = web.Application()
    engine.add_routes([web.get("/v1/realtime", realtime), web.get("/observations", results),
                       web.post("/stimulus/{kind:speech|interrupt}", stimulus)])
    apps = []
    avatar_options = {}
    if whep:
        from private_whep_fixture import avatar_app
        avatar = avatar_app()
        apps.append((avatar, base + 3))
        avatar_options = {"avatar_url": f"http://127.0.0.1:{base + 3}", "avatar_token": "internal-fixture"}
    gateway = create_app(Config(engine_url=f"http://127.0.0.1:{base + 2}", model="fixture", checkpoint="fixture",
        **avatar_options,
        tokens=("private-call-fixture",), admin_token="not-an-application-token", engine_token="engine-fixture"))
    for app, port in [*apps, (engine, base + 2), (gateway, base + 1)]:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
