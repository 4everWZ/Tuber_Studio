import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from tuber_studio import stack


@pytest.mark.parametrize("blocked", ["broker", "web", None])
async def test_stop_cancels_startup_and_closes_web_before_broker(monkeypatch, blocked):
    events = []
    reached = asyncio.Event()
    stopped = asyncio.Event()

    def app(name):
        @asynccontextmanager
        async def lifespan(_):
            try:
                events.append(name + ":enter")
                if name == blocked:
                    reached.set()
                    await asyncio.Event().wait()
                yield
            finally:
                events.append(name + ":cleaned")

        return FastAPI(lifespan=lifespan)

    class Listener:
        def __init__(self, config):
            self.name = "broker" if config.port == 9012 else "web"
            self.started = self.should_exit = False
            assert config.lifespan == "off"

        async def serve(self):
            self.started = True
            events.append(self.name + ":listening")
            if self.name == "web" and blocked is None:
                reached.set()
            while not self.should_exit:
                await asyncio.sleep(0.001)
            events.append(self.name + ":closed")

    monkeypatch.setattr(stack, "create_inference", lambda: app("broker"))
    monkeypatch.setattr(stack, "create_web", lambda: app("web"))
    monkeypatch.setattr(stack, "StackServer", Listener)
    task = asyncio.create_task(stack.serve(stopped))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        stopped.set()
        await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert "broker:cleaned" in events
    if blocked != "broker":
        assert events.index("web:cleaned") < events.index("broker:closed")
        assert events.index("broker:closed") < events.index("broker:cleaned")
    if blocked is None:
        assert events.index("web:closed") < events.index("web:cleaned")
