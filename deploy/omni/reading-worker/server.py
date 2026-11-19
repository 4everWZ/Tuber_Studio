"""Serve the existing bounded Chromium reader over a private volume socket."""

import asyncio
import fcntl
import json
import os
from pathlib import Path

from tuber_studio.agent.browser_chromium import ChromiumReaders

SOCKET = Path("/run/tuber-reading/reader.sock")


async def session(reader, writer, pages):
    # A socket belongs to one Core page lease. The peer supplies no task IDs.
    pending = set()
    try:
        request = await asyncio.wait_for(reader.readline(), timeout=50)
        while request:
            render = asyncio.create_task(pages.render(writer, json.loads(request)))
            following = asyncio.create_task(reader.readline())
            pending = {render, following}
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if following.done():
                # The client closed during rendering or violated the serial protocol.
                break
            try:
                reply = render.result()
            except ValueError as exc:
                reply = {"error": str(exc)}
            writer.write(json.dumps(reply, ensure_ascii=True).encode() + b"\n")
            await writer.drain()
            request = await asyncio.wait_for(following, timeout=50)
    except (ConnectionError, TimeoutError, ValueError):
        pass
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if closing := pages.release(writer, force=True):
            await closing
        writer.close()
        await writer.wait_closed()


async def main():
    os.umask(0o077)
    pages = ChromiumReaders()
    # A replacement starts only after its predecessor releases the socket volume.
    with (SOCKET.parent / "server.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = await asyncio.start_unix_server(
            lambda r, w: session(r, w, pages), SOCKET, limit=2 * 1024 * 1024
        )
        async with server:
            try:
                await server.serve_forever()
            finally:
                await pages.close()


if __name__ == "__main__":
    asyncio.run(main())
