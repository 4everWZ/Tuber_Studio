"""Render the existing studio avatar against the outgoing audio and publish both tracks."""

import asyncio
import os
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from PIL import Image, ImageSequence

WIDTH, HEIGHT, FPS = 640, 360, 25
VIDEO_FRAME_BYTES = WIDTH * HEIGHT * 3
PCM_RATE = 24000
PCM_PER_FRAME = PCM_RATE * 2 // FPS
MAX_PENDING = PCM_RATE * 2 * 4


def load_frames(path: Path):
    background = Image.new("RGB", (WIDTH, HEIGHT), "#128218")
    output = []
    with Image.open(path) as source:
        for image in ImageSequence.Iterator(source):
            avatar = image.convert("RGBA")
            avatar.thumbnail((HEIGHT, HEIGHT), Image.Resampling.LANCZOS)
            frame = background.copy()
            frame.paste(avatar, ((WIDTH - avatar.width) // 2, (HEIGHT - avatar.height) // 2), avatar)
            output.append((max(1, round(image.info.get("duration", 100) * FPS / 1000)), frame.tobytes()))
    if not output:
        raise ValueError("avatar asset does not contain a frame")
    return tuple(output)


def publish_address(value, user, password):
    url = urlsplit(value)
    if url.scheme != "rtsp" or not url.hostname or url.username or url.password:
        raise ValueError("media destination must be an RTSP service URL without embedded credentials")
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user and password else ""
    return urlunsplit((url.scheme, auth + url.netloc, url.path, url.query, ""))


class ProgramPublisher:
    def __init__(self, address, assets):
        self.address = address
        self.animations = {False: load_frames(assets / "idle.gif"), True: load_frames(assets / "talking.gif")}
        self.pending = deque()
        self.pending_bytes = 0
        self.proc = None
        self.video = None
        self.task = None

    def push(self, pcm):
        if not pcm or len(pcm) % 2:
            raise ValueError("invalid PCM16 media payload")
        if self.pending_bytes + len(pcm) > MAX_PENDING:
            raise BufferError("program audio is behind realtime")
        self.pending.append(pcm)
        self.pending_bytes += len(pcm)

    def clear(self):
        self.pending.clear()
        self.pending_bytes = 0

    def take(self):
        collected = bytearray()
        while self.pending and len(collected) < PCM_PER_FRAME:
            chunk = self.pending.popleft()
            amount = min(len(chunk), PCM_PER_FRAME - len(collected))
            collected.extend(chunk[:amount])
            self.pending_bytes -= amount
            if amount < len(chunk):
                self.pending.appendleft(chunk[amount:])
        return bytes(collected).ljust(PCM_PER_FRAME, b"\0"), bool(collected)

    async def start(self):
        read, write = os.pipe()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-thread_queue_size", "4", "-f", "rawvideo", "-pixel_format", "rgb24",
                "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS), "-i", f"pipe:{read}",
                "-probesize", "32", "-analyzeduration", "1", "-thread_queue_size", "4",
                "-f", "s16le", "-ar", str(PCM_RATE), "-ac", "1", "-i", "pipe:0",
                "-c:v", "libx264", "-threads:v", "2", "-profile:v", "baseline", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", str(FPS), "-c:a", "libopus", "-ar", "48000", "-application", "lowdelay",
                "-muxdelay", "0", "-rtsp_transport", "tcp", "-f", "rtsp", self.address,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(read,),
            )
            loop = asyncio.get_running_loop()
            handle = os.fdopen(write, "wb", buffering=0)
            write = None
            try:
                transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, handle)
            except BaseException:
                handle.close()
                raise
            # FFmpeg opens the pipes serially. Its first PCM packet can span several
            # video ticks, so allow those video frames to wait while audio arrives.
            transport.set_write_buffer_limits(high=4 * VIDEO_FRAME_BYTES, low=2 * VIDEO_FRAME_BYTES)
            self.video = asyncio.StreamWriter(transport, protocol, None, loop)
            self.task = asyncio.create_task(self.render())
        finally:
            os.close(read)
            if write is not None:
                os.close(write)

    async def render(self):
        loop = asyncio.get_running_loop()
        due = loop.time()
        speaking, index, ticks = False, 0, 0
        while self.proc.returncode is None:
            pcm, audio = self.take()
            if audio != speaking:
                speaking, index, ticks = audio, 0, 0
            duration, picture = self.animations[speaking][index]
            self.video.write(picture)
            self.proc.stdin.write(pcm)
            await asyncio.gather(self.video.drain(), self.proc.stdin.drain())
            ticks += 1
            if ticks >= duration:
                index = (index + 1) % len(self.animations[speaking])
                ticks = 0
            due = max(due + 1 / FPS, loop.time())
            await asyncio.sleep(due - loop.time())
        raise RuntimeError(f"program encoder exited with status {self.proc.returncode}")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.video:
            self.video.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
