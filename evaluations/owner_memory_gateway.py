"""Run the independent gateway checkout on loopback against an ordinary upstream application binding."""

import asyncio
import json
import secrets
import sys
from contextlib import asynccontextmanager

from aiohttp import ClientSession, web
from services.gateway.app import Config, create_app


class PublicUpstream:
    """Use the same application endpoints the deployed gateway exposes to Studio."""

    def __init__(self, http, origin):
        self.http, self.origin = http, origin

    def post(self, path, **kwargs):
        if path != "/v1/chat/completions":
            raise ValueError("Unexpected transcription endpoint")
        return self.http.post(path, **kwargs)

    @asynccontextmanager
    async def ws_connect(self, path, *, params, **options):
        if path != "/v1/realtime":
            raise ValueError("Unexpected realtime endpoint")
        async with self.http.post("/v1/browser-tickets", json={"path":path}, allow_redirects=False) as reply:
            reply.raise_for_status()
            ticket = (await reply.json())["ticket"]
        async with ClientSession(base_url=self.origin, trust_env=False) as tickets:
            async with tickets.ws_connect(path, params=params | {"ticket":ticket}, **options) as socket:
                yield socket


async def serve():
    upstream, token, local_token = json.loads(sys.stdin.readline())
    app = create_app(Config(engine_url=upstream, engine_token=token, model="omni-default",
                            checkpoint="ordinary-upstream", tokens=(local_token,), admin_token=secrets.token_urlsafe(),
                            shared_capacity=True, batch_cap=2))
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    try:
        gateway = app["gateway"]
        gateway.engine = PublicUpstream(gateway.engine, upstream)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        print(runner.addresses[0][1], flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())
