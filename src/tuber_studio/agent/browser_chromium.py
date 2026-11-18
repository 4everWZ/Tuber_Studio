"""At most two private Chromium pages, leased to individual tasks."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil


class ChromiumPage:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="studio-reading-")
        path = self.directory.name
        env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "LOCALAPPDATA", "PLAYWRIGHT_BROWSERS_PATH",
                                          "HOME", "PATH", "ProgramFiles", "ProgramFiles(x86)", "TUBER_READING_BROWSER_CHANNEL")
               if k in os.environ}
        env.update(TEMP=path, TMP=path, TMPDIR=path)
        worker = Path(__file__).with_name("browser_worker.py")
        self.proc = subprocess.Popen([sys.executable, "-I", str(worker)], cwd=path, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.idle = None
        self.busy = False

    def exchange(self, data):
        self.proc.stdin.write(json.dumps(data, ensure_ascii=True).encode() + b"\n")
        self.proc.stdin.flush()
        out = self.proc.stdout.readline(1024 * 1024)
        if not out.endswith(b"\n"):
            raise ValueError("隔离 Chromium 未返回完整视口，请核对安装渠道和进程预算")
        value = json.loads(out)
        if "error" in value:
            raise ValueError(value["error"])
        return value

    def close(self, force):
        if self.proc.poll() is None:
            root = psutil.Process(self.proc.pid)
            children = root.children(recursive=True)
            if not force:
                self.proc.stdin.close()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    force = True
            if force:
                for process in [root, *reversed(children)]:
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                self.proc.wait(timeout=3)
                psutil.wait_procs(children, timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.directory.cleanup()

    def check_memory(self):
        if self.proc.poll() is not None:
            return
        root = psutil.Process(self.proc.pid)
        rss = 0
        for process in [root, *root.children(recursive=True)]:
            try:
                rss += process.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        if rss > 768 * 1024**2:
            raise ValueError("阅读页超过内存预算")


class ChromiumReaders:
    def __init__(self):
        self.pages = {}
        self.lock = asyncio.Lock()
        self.closing = set()

    def release(self, identity, *, force=False):
        if page := self.pages.pop(identity, None):
            if page.idle:
                page.idle.cancel()
            closing = asyncio.create_task(asyncio.to_thread(page.close, force or page.busy))
            self.closing.add(closing)
            closing.add_done_callback(self.closing.discard)
            return closing

    async def close(self):
        for identity in list(self.pages):
            self.release(identity)
        await asyncio.gather(*self.closing)

    async def render(self, identity, data):
        async with self.lock:
            page = self.pages.get(identity)
            if not page:
                if len(self.pages) == 2:
                    idle = next((key for key, value in self.pages.items() if not value.busy), None)
                    if idle is None:
                        raise ValueError("两个阅读页均在使用中")
                    await self.release(idle)
                if self.closing:
                    await asyncio.gather(*self.closing)
                page = self.pages[identity] = ChromiumPage()
            if page.busy:
                raise ValueError("页面正在操作中")
            page.busy = True
            if page.idle:
                page.idle.cancel()
        job = asyncio.create_task(asyncio.to_thread(page.exchange, data))
        try:
            async with asyncio.timeout(16):
                while not job.done():
                    await asyncio.wait({job}, timeout=0.25)
                    page.check_memory()
                return job.result()
        except BaseException:
            if closing := self.release(identity, force=True):
                await asyncio.shield(closing)
            await asyncio.gather(job, return_exceptions=True)
            raise
        finally:
            page.busy = False
            if self.pages.get(identity) is page:
                page.idle = asyncio.get_running_loop().call_later(45, self.release, identity)
