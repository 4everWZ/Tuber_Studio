"""Regressions for native PCM bursts in the program bridge."""

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

MEDIA = Path(__file__).resolve().parents[1] / "deploy" / "omni" / "media"


def load_media(name):
    spec = importlib.util.spec_from_file_location(name, MEDIA / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def media(monkeypatch):
    publisher = load_media("publisher")
    monkeypatch.setitem(sys.modules, "publisher", publisher)
    # The standalone container installs websockets. These relay tests supply both transports.
    monkeypatch.setitem(sys.modules, "websockets", ModuleType("websockets"))
    bridge = load_media("bridge")
    monkeypatch.setattr(publisher, "load_frames", lambda _: ((1, b""),))
    return bridge, publisher


class Client:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    async def receive_json(self):
        return await self.incoming.get()

    async def send_json(self, payload):
        self.sent.append(payload)


class Engine:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    async def send(self, message):
        self.sent.put_nowait(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)


def audio(ident, pcm, *, epoch=1):
    return {
        "type": "response.output_audio.delta",
        "response_id": ident,
        "metadata": {"epoch": epoch},
        "delta": base64.b64encode(pcm).decode("ascii"),
    }


def pcm_frame(size, sample):
    return sample.to_bytes(2, "little") * (size // 2)


async def played_frame(publisher, relay):
    async with asyncio.timeout(2):
        while True:
            await asyncio.sleep(0)
            if relay.done():
                relay.result()
            frame, audible = publisher.take()
            if audible:
                return frame


async def forwarded_events(client, count, relay):
    async with asyncio.timeout(2):
        while len(client.sent) < count:
            if relay.done():
                relay.result()
            await asyncio.sleep(0)
    return client.sent


@pytest.mark.asyncio
async def test_program_bridge_plays_oversized_codec_delta_in_order(media):
    bridge, media_publisher = media
    publisher = media_publisher.ProgramPublisher("rtsp://example.invalid/program", MEDIA)
    publisher.task = asyncio.create_task(asyncio.Event().wait())
    client, engine = Client(), Engine()
    frame_bytes = media_publisher.PCM_PER_FRAME
    frame_count = media_publisher.FPS * 4 + 7
    frames = [pcm_frame(frame_bytes, sample) for sample in range(1, frame_count + 4)]
    oversized = audio("large", b"".join(frames[:frame_count]))
    tail = audio("large", b"".join(frames[frame_count:]))
    engine.incoming.put_nowait(oversized)
    engine.incoming.put_nowait(tail)
    relay = asyncio.create_task(bridge.relay(client, engine, publisher))
    try:
        assert [await played_frame(publisher, relay) for _ in frames] == frames
        engine.incoming.put_nowait(None)
        await asyncio.wait_for(relay, 2)
        assert client.sent == [oversized, tail]
        assert publisher.take() == (bytes(frame_bytes), False)
    finally:
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)
        await publisher.close()


@pytest.mark.asyncio
async def test_program_reclaims_consumed_spill_while_next_delta_is_queued(media, monkeypatch):
    _, media_publisher = media
    publisher = media_publisher.ProgramPublisher("rtsp://example.invalid/program", MEDIA)
    original_open = media_publisher.TemporaryFile
    spills = []

    def tracked_spill():
        handle = original_open()
        spills.append(handle)
        return handle

    monkeypatch.setattr(media_publisher, "TemporaryFile", tracked_spill)
    count = media_publisher.FPS * 4 + 1
    first = pcm_frame(media_publisher.PCM_PER_FRAME, 11)
    second = pcm_frame(media_publisher.PCM_PER_FRAME, 22)
    try:
        publisher.push(first * count, "first")
        publisher.push(second * count, "second")
        for _ in range(count):
            assert publisher.take() == (first, True)
        consumed, pending = spills
        assert consumed.closed
        assert not pending.closed
        assert publisher.take() == (second, True)
    finally:
        await publisher.close()
    assert pending.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["client", "engine"])
async def test_program_bridge_interrupt_clears_queued_codec_delta(media, interrupt):
    bridge, media_publisher = media
    publisher = media_publisher.ProgramPublisher("rtsp://example.invalid/program", MEDIA)
    publisher.task = asyncio.create_task(asyncio.Event().wait())
    client, engine = Client(), Engine()
    frame_bytes = media_publisher.PCM_PER_FRAME
    old_frame = pcm_frame(frame_bytes, 101)
    new_frame = pcm_frame(frame_bytes, 203)
    oversized = old_frame * (media_publisher.FPS * 8 + 1)
    old_packets = [audio("old", oversized), audio("old", old_frame)]
    for event in old_packets:
        engine.incoming.put_nowait(event)
    relay = asyncio.create_task(bridge.relay(client, engine, publisher))
    try:
        assert await played_frame(publisher, relay) == old_frame
        expected = old_packets.copy()
        assert await forwarded_events(client, len(expected), relay) == expected
        if interrupt == "client":
            cancel = {"type": "response.cancel", "response_id": "old"}
            client.incoming.put_nowait(cancel)
            assert await asyncio.wait_for(engine.sent.get(), 2) == cancel
        else:
            clear = {"type": "output_audio_buffer.cleared", "response_id": "old"}
            engine.incoming.put_nowait(clear)
            expected.append(clear)
            assert await forwarded_events(client, len(expected), relay) == expected
        old_event = len(expected)
        engine.incoming.put_nowait(audio("old", old_frame))
        assert publisher.take() == (bytes(frame_bytes), False)
        fresh = audio("new", new_frame)
        engine.incoming.put_nowait(fresh)
        assert await forwarded_events(client, old_event + 1, relay) == [*expected, fresh]
        assert await played_frame(publisher, relay) == new_frame
        engine.incoming.put_nowait(None)
        await asyncio.wait_for(relay, 2)
        assert publisher.take() == (bytes(frame_bytes), False)
    finally:
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)
        await publisher.close()
