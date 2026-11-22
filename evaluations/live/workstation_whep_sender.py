"""Run a time-limited, inference-free WHEP measurement on the Linux media host.

Use the Avatar service interpreter with its source tree on PYTHONPATH. Mount the
registered rigs read-only and set OMNI_AVATAR_REGISTRY. Supply a one-run application
bearer on stdin. The first stdout JSON reports the private gateway port. A second
line on stdin (or EOF) releases the listener and prints the datagram counters.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import secrets
import struct
import sys
from contextlib import AsyncExitStack
from unittest.mock import patch

import aioice
from aiohttp import web
from aiortc.rtp import is_rtcp
from services.avatar.app import create_app as avatar_app
from services.avatar.media import TimelineTrack
from services.gateway.app import create_app as gateway_app
from services.gateway.config import Config


async def serve(bind, mtu):
    reader = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    pipe, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        token = (await asyncio.wait_for(reader.readline(), 15)).decode().strip()
        if len(token) < 32:
            raise ValueError("Supply a one-run application token of at least 32 characters")
        internal = secrets.token_urlsafe(32)
        counters = dict(
            datagrams=0,
            max_udp_payload=0,
            over_ipv4_mtu=0,
            over_ipv6_mtu=0,
            first_rtp={},
            first_track_sample={},
        )
        send = aioice.Connection.send
        recv = TimelineTrack.recv

        async def measured_recv(track):
            first = track.index == 0
            frame = await recv(track)
            if first:
                counters["first_track_sample"][track.kind] = track.clock.start_sample
            return frame

        async def measured_send(connection, data):
            await send(connection, data)
            size = len(data)
            counters["datagrams"] += 1
            counters["max_udp_payload"] = max(counters["max_udp_payload"], size)
            counters["over_ipv4_mtu"] += size + 28 > mtu
            counters["over_ipv6_mtu"] += size + 48 > mtu
            # The fixed RTP header stays visible after SRTP protection.
            if size >= 12 and data[0] >> 6 == 2 and not is_rtcp(data):
                timestamp, ssrc = struct.unpack_from("!II", data, 4)
                counters["first_rtp"].setdefault(ssrc, timestamp)

        async with AsyncExitStack() as stack:
            stack.enter_context(patch.object(aioice.Connection, "send", measured_send))
            stack.enter_context(patch.object(TimelineTrack, "recv", measured_recv))

            async def listen(app, address):
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                stack.push_async_callback(runner.cleanup)
                await web.TCPSite(runner, address, 0).start()
                return runner.addresses[0][1]

            media_port = await listen(avatar_app(internal, capacity=1), "127.0.0.1")
            config = Config(
                engine_url="",
                model="",
                checkpoint="",
                engine_token="",
                tokens=(token,),
                admin_token=secrets.token_urlsafe(32),
                avatar_url=f"http://127.0.0.1:{media_port}",
                avatar_token=internal,
            )
            port = await listen(gateway_app(config), str(bind))
            print(json.dumps({"port": port}), flush=True)
            try:
                await asyncio.wait_for(reader.readline(), 180)
            except TimeoutError:
                pass
        print(json.dumps(counters), flush=True)
    finally:
        pipe.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=ipaddress.ip_address)
    parser.add_argument("--mtu", required=True, type=int, help="MTU of the selected media interface")
    args = parser.parse_args()
    if args.bind.is_unspecified or args.mtu <= 48 or not os.getenv("OMNI_AVATAR_REGISTRY"):
        parser.error("Specify the media host's private interface address, MTU and OMNI_AVATAR_REGISTRY")
    asyncio.run(serve(args.bind, args.mtu))
