"""Optional loopback protocol test. Run with the omni-avatar-api venv and that repo on PYTHONPATH.

Only the model engine is simulated. Studio and the unchanged gateway run as separate services.
Configure the isolated Studio with origin http://127.0.0.1:18766 and a file containing private-call-fixture.
"""

import asyncio
import base64
import json

from aiohttp import web
from services.gateway.app import Config, create_app


async def main():
    observations = {"input": 0, "played": 0, "closed": 0}

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
                await socket.send_json({"type": "response.output_audio.delta", "response_id": "test-response",
                    "item_id": "test-item", "format": "pcm16", "sample_rate_hz": 16000, "delta": message["audio"]})
            elif message["type"] == "playback.ack":
                assert message["response_id"] == "test-response" and message["item_id"] == "test-item"
                observations["played"] = message["played_ms"]
            elif message["type"] == "session.close":
                observations["closed"] += 1
                break
        return socket

    async def results(request):
        return web.json_response(observations)

    engine = web.Application()
    engine.add_routes([web.get("/v1/realtime", realtime), web.get("/observations", results)])
    gateway = create_app(Config(engine_url="http://127.0.0.1:18767", model="fixture", checkpoint="fixture",
        tokens=("private-call-fixture",), admin_token="not-an-application-token", engine_token="engine-fixture"))
    for app, port in ((engine, 18767), (gateway, 18766)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
