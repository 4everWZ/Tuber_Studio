"""One container and Python environment for the web app, broker and GPU children."""

import asyncio
import signal
from contextlib import contextmanager, suppress

import uvicorn

from .app import create_app as create_web
from .inference import create_app as create_inference


class StackServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The supervisor owns signals for both HTTP listeners and GPU children.
        yield


async def _until_started(server, task):
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("Stack listener exited before startup completed")
        await asyncio.sleep(0.05)


async def serve():
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    broker = StackServer(
        uvicorn.Config(
            create_inference(),
            host="127.0.0.1",
            port=9012,
            ws="none",
            access_log=False,
            timeout_graceful_shutdown=10,
        )
    )
    broker_task = asyncio.create_task(broker.serve())
    web = web_task = None
    stopper = asyncio.create_task(stopped.wait())
    try:
        await _until_started(broker, broker_task)
        web = StackServer(
            uvicorn.Config(
                create_web(), host="127.0.0.1", port=9020, ws="wsproto", timeout_graceful_shutdown=10
            )
        )
        web_task = asyncio.create_task(web.serve())
        await _until_started(web, web_task)
        done, _ = await asyncio.wait({stopper, broker_task, web_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done - {stopper}:
            await task
            if not stopped.is_set():
                raise RuntimeError("Stack listener exited unexpectedly")
    finally:
        # Keep the broker alive while web shutdown cancels its in-flight inference HTTP calls.
        if web:
            web.should_exit = True
        if web_task:
            with suppress(asyncio.CancelledError):
                await web_task
        broker.should_exit = True
        with suppress(asyncio.CancelledError):
            await broker_task
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


if __name__ == "__main__":
    asyncio.run(serve())
