"""Loopback Inochi2D media and ordinary gateway for the private voice acceptance run."""

import asyncio
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import ClientResponseError, ClientSession, web
from services.avatar.app import create_app as avatar_app
from services.gateway.app import Config
from services.gateway.app import create_app as gateway_app


class TicketedRealtime:
    """Connect the gateway's engine socket to an actual application-facing Realtime endpoint."""

    def __init__(self, root, key):
        self.root, self.key = root, key

    @asynccontextmanager
    async def ws_connect(self, path, *, params, **options):
        if path != "/v1/realtime":
            raise ValueError("Unexpected engine request")
        async with ClientSession(base_url=self.root, trust_env=False) as client:
            started = asyncio.get_running_loop().time()
            async with client.post(
                "/v1/browser-tickets",
                json={"path": path},
                allow_redirects=False,
                headers={"Authorization": "Bearer " + self.key},
            ) as reply:
                reply.raise_for_status()
                ticket = (await reply.json())["ticket"]
                if not ticket or ticket == self.key:
                    raise ValueError("Invalid upstream ticket")
            try:
                async with client.ws_connect(path, params=params | {"ticket": ticket}, **options) as socket:
                    elapsed = asyncio.get_running_loop().time() - started
                    print(
                        f"Workstation ticket and Realtime upgrade accepted in {elapsed:.2f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    yield socket
            except ClientResponseError as error:
                print(f"Upstream Realtime HTTP {error.status}", file=sys.stderr, flush=True)
                raise


async def main():
    rig_dir = Path(os.environ["QA_RIG_DIR"])
    internal = secrets.token_urlsafe(32)
    avatar = avatar_app(
        internal,
        registry={
            key: {"driver": "inochi2d", "root": rig_dir / key / "model.inp"} for key in ("regular", "q")
        },
    )
    runner = web.AppRunner(avatar, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        root = os.environ["OMNI_GATEWAY_URL"]
        app = gateway_app(
            Config(
                engine_url=root,
                model="omni-default",
                checkpoint="remote",
                tokens=(os.environ["QA_LOCAL_TOKEN"],),
                admin_token=secrets.token_urlsafe(32),
                engine_token="",
                # This hop mints a ticket and waits for admission on another gateway.
                # Studio's Realtime handshake deadline bounds both operations.
                health_timeout=30,
                avatar_url=f"http://127.0.0.1:{runner.addresses[0][1]}",
                avatar_token=internal,
            )
        )

        async def clocks(request):
            session = avatar["service"].sessions.get(request.match_info["id"])
            if not session or session.owner != request["owner"]:
                raise web.HTTPNotFound()
            from services.avatar.media import VIDEO_SAMPLES
            from services.avatar.sessions import RATE

            ages = {}
            now = session.now_sample()
            for peer in session.peers.values():
                for sender in peer.getSenders():
                    track = sender.track
                    if track and track.index:
                        step = VIDEO_SAMPLES if track.kind == "video" else 480
                        sample = track.start_sample + (track.index - 1) * step
                        ages[track.kind] = round((now - sample) * 1000 / RATE)
            return web.json_response(ages)

        app.router.add_get("/qa/sender-clocks/{id}", clocks)
        gateway = web.AppRunner(app, access_log=None)
        await gateway.setup()
        try:
            app["gateway"].engine = TicketedRealtime(root, os.environ["OMNI_STUDIO_TOKEN"])
            await web.TCPSite(gateway, "127.0.0.1", 0).start()
            print(gateway.addresses[0][1], flush=True)
            await asyncio.to_thread(input)
        finally:
            await gateway.cleanup()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
