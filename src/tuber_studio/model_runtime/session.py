"""Own inference-node observations and resource mode transitions."""

import asyncio

import httpx

from ..inference_node_client import refresh_capabilities
from ..providers import public_error
from ..runtime_modes import (
    ObservationOwner,
    apply_mode_profile,
    inference_transition,
    profile_alignment_error,
)
from ..video_vision_service import restore_admission


class InferenceRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        settings = runtime.settings
        broker = settings.data.get("inference_broker", {})
        self.client = (
            httpx.AsyncClient(
                base_url=broker["base_url"],
                headers={"X-Tuber-Inference-Key": settings.inference_token},
                timeout=httpx.Timeout(400, connect=5),
                trust_env=False,
            )
            if broker.get("enabled")
            else None
        )
        mode = broker.get("mode", "text") if self.client else "standalone"
        self.status = {"mode": "text" if mode == "exclusive" else mode, "active": None}
        self.observer = None
        self.mode_lock = asyncio.Lock()
        self.cleanup_tasks = set()
        self.capabilities = None
        self.revision = 0
        self.profile_alignment_failed = False

    def start_observer(self):
        self.observer = asyncio.create_task(self.observe())

    async def stop_observer(self):
        if self.observer:
            self.observer.cancel()
            await asyncio.gather(self.observer, return_exceptions=True)

    async def close(self):
        if self.client:
            await self.client.aclose()
        await asyncio.gather(*self.cleanup_tasks, return_exceptions=True)

    async def refresh(self):
        owner = ObservationOwner.capture(self.runtime)
        if owner.client is None:
            raise ValueError("当前运行方式未启用设备资源模式")
        try:
            is_remote = self.runtime.settings.data.get("inference_broker", {}).get("deployment") == "remote"
            health_timeout = httpx.Timeout(10, connect=5) if is_remote else httpx.Timeout(4, connect=2)
            response = await owner.client.get("/health", timeout=health_timeout)
            response.raise_for_status()
            observed = response.json()
        except Exception:
            if not owner.current(self.runtime):
                return self.status
            raise
        if not owner.current(self.runtime):
            return self.status
        if observed.get("mode") == "exclusive":
            observed["mode"] = "text"
        observed["active"] = observed.get("active_provider")
        if error := profile_alignment_error(self.runtime, observed):
            self.profile_alignment_failed = True
            observed = observed | {"state": "error", "error": error, "provider_alignment_error": True}
        self.status = observed
        if observed.get("mode") in {"text", "live"}:
            self.runtime.settings.data["inference_broker"]["mode"] = observed["mode"]
        return observed

    async def observe(self):
        iteration = 0
        consecutive_failures = 0
        while True:
            owner = ObservationOwner.capture(self.runtime)
            try:
                await self.refresh()
                consecutive_failures = 0
                if self.runtime.error and ("超时" in self.runtime.error or "连接" in self.runtime.error):
                    self.runtime.error = None
            except Exception as error:
                consecutive_failures += 1
                if owner.current(self.runtime) and (
                    consecutive_failures >= 3 or self.status.get("state") == "error"
                ):
                    self.status = self.status | {
                        "state": "error",
                        "active": None,
                        "error": public_error(error),
                    }
            if (
                self.runtime.settings.data.get("inference_broker", {}).get("deployment") == "remote"
                and iteration % 5 == 0
            ):
                owner = ObservationOwner.capture(self.runtime)
                try:
                    await refresh_capabilities(self.runtime)
                    if (
                        owner.current(self.runtime)
                        and self.status.get("mode") == "text"
                        and getattr(self.runtime, "browsing", None)
                    ):
                        await restore_admission(self.runtime)
                except Exception as error:
                    if owner.current(self.runtime) and self.capabilities is not None:
                        self.capabilities = self.capabilities | {"monitoring_error": public_error(error)}
            iteration += 1
            await asyncio.sleep(2)

    async def set_mode(self, mode):
        if mode == "live" and not self.runtime.settings.data.get("features", {}).get("live_enabled", False):
            raise ValueError("当前设备尚未启用直播功能，请先在设备配置中显式启用")
        if mode not in {"text", "live"}:
            raise ValueError("工作模式应为text或live")
        if self.client is None:
            raise ValueError("当前运行方式未启用设备资源模式")
        async with self.mode_lock:
            with inference_transition(self.runtime):
                if mode == "text":
                    # Stop producers and invalidate playback before releasing TTS.
                    await self.runtime.control("stop")
                async with self.runtime.generation_lock:
                    previous_mode = self.runtime.settings.data.get("inference_broker", {}).get("mode", "text")
                    try:
                        response = await self.client.post("/mode", json={"mode": mode})
                        response.raise_for_status()
                        observed = response.json()
                        if observed.get("mode") != mode:
                            raise ValueError("设备未确认目标工作模式")
                        await apply_mode_profile(self.runtime, mode)
                        observed["active"] = observed.get("active_provider")
                        self.status = observed
                        self.runtime.settings.data["inference_broker"]["mode"] = mode
                        return observed
                    except BaseException as error:
                        if self.runtime.settings.data.get("work_mode_profiles"):

                            async def rollback():
                                response = await self.client.post("/mode", json={"mode": previous_mode})
                                response.raise_for_status()
                                if response.json().get("mode") != previous_mode:
                                    raise ValueError("原工作模式未恢复")

                            cleanup = asyncio.create_task(rollback())
                            while not cleanup.done():
                                try:
                                    await asyncio.shield(cleanup)
                                except asyncio.CancelledError:
                                    continue
                                except Exception:
                                    break
                            try:
                                cleanup.result()
                            except BaseException:
                                self.runtime.error = "模式切换结果待核对，请恢复推理节点连接"
                        try:
                            await self.refresh()
                        except Exception:
                            self.status = self.status | {
                                "state": "error",
                                "error": "工作模式结果待设备重新确认",
                            }
                        if isinstance(error, asyncio.CancelledError):
                            raise
                        raise ValueError("工作模式切换失败：" + public_error(error)) from None
