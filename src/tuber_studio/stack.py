"""One container and Python environment for the web app, broker and GPU children."""

import asyncio
import signal
from contextlib import AsyncExitStack, contextmanager

import uvicorn

from .app import create_app as create_web
from .inference import create_app as create_inference


class StackServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The supervisor owns signals for both HTTP listeners and GPU children.
        yield


async def _until_started(server, task, stopped):
    while not server.started:
        if stopped.is_set():
            return False
        if task.done():
            await task
            raise RuntimeError("Stack listener exited before startup completed")
        await asyncio.sleep(0.05)
    return not stopped.is_set()


async def _enter_lifespan(resources, app, stopper):
    startup = asyncio.create_task(resources.enter_async_context(app.router.lifespan_context(app)))
    try:
        done, _ = await asyncio.wait({startup, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:
            return False
        await startup
        return True
    finally:
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)


async def serve(stopped=None):
    install_signals = stopped is None
    stopped = stopped or asyncio.Event()
    loop = asyncio.get_running_loop()
    if install_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopped.set)
    broker_app = create_inference()
    broker = StackServer(
        uvicorn.Config(
            broker_app,
            host="127.0.0.1",
            port=9012,
            ws="none",
            lifespan="off",
            access_log=False,
            timeout_graceful_shutdown=10,
        )
    )
    broker_task = web = web_task = None
    stopper = asyncio.create_task(stopped.wait())
    resources = AsyncExitStack()
    web_resources = AsyncExitStack()
    try:
        # Own ASGI lifespan tasks directly so TERM can cancel model loading;
        # cancelling uvicorn.serve alone does not own its separate lifespan task.
        if not await _enter_lifespan(resources, broker_app, stopper):
            return
        broker_task = asyncio.create_task(broker.serve())
        if not await _until_started(broker, broker_task, stopped):
            return
        web_app = create_web()
        if not await _enter_lifespan(web_resources, web_app, stopper):
            return
        web = StackServer(
            uvicorn.Config(
                web_app,
                host="127.0.0.1",
                port=9020,
                ws="wsproto",
                lifespan="off",
                timeout_graceful_shutdown=10,
            )
        )
        web_task = asyncio.create_task(web.serve())
        if not await _until_started(web, web_task, stopped):
            return
        done, _ = await asyncio.wait({stopper, broker_task, web_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done - {stopper}:
            await task
            if not stopped.is_set():
                raise RuntimeError("Stack listener exited unexpectedly")
    finally:
        # Keep the broker alive while web shutdown cancels its in-flight inference HTTP calls.
        try:
            if web:
                web.should_exit = True
            if web_task:
                await asyncio.gather(web_task, return_exceptions=True)
            await web_resources.aclose()
        finally:
            broker.should_exit = True
            if broker_task:
                await asyncio.gather(broker_task, return_exceptions=True)
            try:
                await resources.aclose()
            finally:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
                if install_signals:
                    for signum in (signal.SIGTERM, signal.SIGINT):
                        loop.remove_signal_handler(signum)


if __name__ == "__main__":
    asyncio.run(serve())
