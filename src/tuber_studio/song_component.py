"""Optional Node adapter process; stdio is private and never carries model text."""

import asyncio
import json
import os
import shutil
import subprocess


class SongComponent:
    def __init__(self, root, runtime=None):
        self.directory = root / "components/song_requests"
        self.temporary = (runtime or root / "runtime") / "song_component"
        self.process = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.error = None
        self.capabilities = {}
        self.lock = asyncio.Lock()

    def installed(self):
        return bool(
            shutil.which("node")
            and (self.directory / "helper.cjs").is_file()
            and (self.directory / "node_modules/netease-metadata/package.json").is_file()
            and (self.directory / "node_modules/@unblockneteasemusic/server/package.json").is_file()
        )

    def status(self):
        return {
            "installed": self.installed(),
            "running": bool(self.process and self.process.returncode is None),
            "error": self.error,
            "pid": self.process.pid if self.process and self.process.returncode is None else None,
            "migu_configured": self.capabilities.get("migu_configured", False),
        }

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                return
            if not self.installed():
                self.error = "未安装可选组件：需要 Node.js 22+，在 components/song_requests 执行 npm ci --ignore-scripts"
                raise ValueError(self.error)
            if self.temporary.is_symlink():
                self.error = "点歌组件临时目录不能使用链接"
                raise ValueError(self.error)
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            try:
                self.process = await asyncio.create_subprocess_exec(
                    shutil.which("node"),
                    str(self.directory / "helper.cjs"),
                    cwd=self.directory,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=1_048_576,
                    env={**os.environ, "TUBER_SONG_TEMP": str(self.temporary)},
                    **options,
                )
            except OSError as error:
                self.error = f"无法启动在线点歌组件：{error.strerror}"
                raise ValueError(self.error) from None
            self.reader = asyncio.create_task(self._read(self.process))
            try:
                self.capabilities = await self.call("ping", timeout=15, stop_on_timeout=False)
                if self.capabilities.get("protocol") != 1:
                    raise ValueError("在线点歌组件协议不兼容")
                self.error = None
            except BaseException:
                await self._stop()
                raise

    async def _read(self, process):
        try:
            while line := await process.stdout.readline():
                value = json.loads(line)
                if value.get("fatal"):
                    self.error = str(value["fatal"])[:400]
                    break
                future = self.pending.get(value.get("id"))
                if future and not future.done():
                    if "error" in value:
                        future.set_exception(ValueError(str(value["error"])[:400]))
                    else:
                        future.set_result(value.get("result"))
            await process.wait()
            if not self.error and self.process is process:
                self.error = f"在线点歌组件已退出（代码 {process.returncode}），可点击重启"
        except asyncio.CancelledError:
            return
        except Exception:
            self.error = "在线点歌组件通信异常，可点击重启"
        finally:
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(ValueError(self.error or "在线点歌组件已关闭"))

    async def call(self, method, params=None, *, timeout=40, stop_on_timeout=True):
        process = self.process
        if not process or process.returncode is not None:
            raise ValueError(self.error or "在线点歌组件未启动")
        if len(self.pending) >= 32:
            raise ValueError("在线点歌组件繁忙，请稍后重试")
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            process.stdin.write(
                (json.dumps({"id": identity, "method": method, "params": params or {}}) + "\n").encode()
            )
            await process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            message = "在线点歌组件请求超时，请重启组件后重试"
            if stop_on_timeout:
                async with self.lock:
                    if self.process is process:
                        self.error = message
                        await self._stop()
            else:
                self.error = message
            raise ValueError(message) from None
        except (BrokenPipeError, ConnectionResetError):
            raise ValueError("在线点歌组件已断开，可点击重启") from None
        finally:
            self.pending.pop(identity, None)

    async def stop(self):
        async with self.lock:
            await self._stop()

    async def _stop(self):
        process, reader = self.process, self.reader
        self.process, self.reader = None, None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
