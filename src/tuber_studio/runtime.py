"""One bounded live worker; only the current overlay owner can confirm playback."""

import asyncio
import json
import secrets
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psutil

from .api_providers import VisionService, create_llm
from .context import ContextManager
from .memory import missing_slots
from .providers import VoiceService, public_error
from .retention import INTERVAL_SECONDS, RetentionManager

TERMINAL = {"completed", "failed", "cancelled", "expired"}


class Runtime:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.llm = create_llm(settings)
        self.voices = VoiceService(settings)
        self.vision = VisionService(
            settings.data.get("vision", {"kind": "disabled"}), settings.provider_key("vision")
        )
        self.context = ContextManager(settings, store, self.llm)
        self.retention = RetentionManager(settings, store)
        self.retention_worker = None
        self.retention_error = None
        self.generation_lock = asyncio.Lock()
        self.state = "stopped"
        self.muted = False
        self.session_id = None
        self.queue = []
        self.current_id = None
        self.task = None
        self.worker = None
        self.wake = asyncio.Event()
        self.overlay = None
        self.overlay_id = None
        self.playback_done = asyncio.Event()
        self.error = None
        self.last_played = None
        self.timings = {"llm_ms": None, "tts_ms": None}
        self.accept_times = {}
        self.last_gift = False
        broker = settings.data.get("inference_broker", {})
        self.inference_client = (
            httpx.AsyncClient(
                base_url=broker["base_url"],
                headers={"X-Tuber-Inference-Key": settings.inference_token},
                timeout=httpx.Timeout(400, connect=5),
                trust_env=False,
            )
            if broker.get("enabled")
            else None
        )
        mode = broker.get("mode", "text") if self.inference_client else "standalone"
        self.inference_status = {"mode": "text" if mode == "exclusive" else mode, "active": None}
        self.inference_observer = None
        self.mode_lock = asyncio.Lock()

    @property
    def character(self):
        return self.store.character()

    async def open(self):
        try:
            await self.llm.health()
        except Exception as error:
            self.error = public_error(error)
        await self.cleanup_retention(automatic=True)
        self.retention_worker = asyncio.create_task(self._retention_loop())
        self.worker = asyncio.create_task(self._loop())
        if self.settings.data.get("inference_broker", {}).get("enabled"):
            self.inference_observer = asyncio.create_task(self._observe_inference())

    async def close(self):
        if self.retention_worker:
            self.retention_worker.cancel()
            await asyncio.gather(self.retention_worker, return_exceptions=True)
        await self.control("stop")
        if self.inference_observer:
            self.inference_observer.cancel()
            await asyncio.gather(self.inference_observer, return_exceptions=True)
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.llm.close()
        await self.voices.close()
        await self.vision.close()
        if self.inference_client:
            await self.inference_client.aclose()

    async def cleanup_retention(self, before=None, *, automatic=False):
        if automatic and self.retention.days is None:
            return None
        if self.current_id or self.queue or self.generation_lock.locked():
            if automatic:
                return None
            raise ValueError("正在生成或播放，请暂停并等待当前任务结束后清理")
        async with self.generation_lock:
            result = self.retention.cleanup(before, manual=not automatic)
            self.retention_error = None
            if self.last_played and not self.store.job(self.last_played["id"]):
                self.last_played = None
            return result

    async def _retention_loop(self):
        while True:
            await asyncio.sleep(60)
            last = (self.retention.last_result or {}).get("completed_at", 0)
            if time.time() - last >= INTERVAL_SECONDS:
                try:
                    await self.cleanup_retention(automatic=True)
                except Exception as error:
                    self.retention_error = public_error(error)

    def status(self):
        memory = psutil.virtual_memory()
        return {
            "state": self.state,
            "muted": self.muted,
            "character": self.character,
            "session_id": self.session_id,
            "queue_length": len(self.queue),
            "overlay_connected": self.overlay is not None,
            "current_job": self.store.job(self.current_id) if self.current_id else None,
            "error": self.error,
            "last_played": self.last_played,
            "providers": {"llm": self.settings.data["llm"]["kind"], "tts": self.settings.data["tts"]["kind"]},
            "resources": {
                "cpu_percent": psutil.cpu_percent(),
                "ram_used_mb": round(memory.used / 2**20),
                "ram_total_mb": round(memory.total / 2**20),
                "process_mb": round(psutil.Process().memory_info().rss / 2**20),
                "gpu": "未接入GPU采样；Jetson可用tegrastats查看",
            },
            "timings": self.timings,
            "context": self.context.status(),
            "inference": self.inference_status,
        }

    async def refresh_inference(self):
        if self.inference_client is None:
            raise ValueError("当前运行方式未启用设备资源模式")
        response = await self.inference_client.get("/health", timeout=httpx.Timeout(2, connect=1))
        response.raise_for_status()
        observed = response.json()
        if observed.get("mode") == "exclusive":
            observed["mode"] = "text"
        observed["active"] = observed.get("active_provider")
        self.inference_status = observed
        if observed.get("mode") in {"text", "live"}:
            self.settings.data["inference_broker"]["mode"] = observed["mode"]
        return observed

    async def _observe_inference(self):
        while True:
            try:
                await self.refresh_inference()
            except Exception as error:
                self.inference_status = self.inference_status | {
                    "state": "error",
                    "active": None,
                    "error": public_error(error),
                }
            await asyncio.sleep(2)

    async def set_work_mode(self, mode):
        if mode == "live" and not self.settings.data.get("features", {}).get("live_enabled", False):
            raise ValueError("直播功能已后置；当前使用非直播工作台")
        if mode not in {"text", "live"}:
            raise ValueError("工作模式应为text或live")
        if self.inference_client is None:
            raise ValueError("当前运行方式未启用设备资源模式")
        async with self.mode_lock:
            if mode == "text":
                # Stop producers and invalidate playback before releasing TTS.
                await self.control("stop")
            async with self.generation_lock:
                try:
                    response = await self.inference_client.post("/mode", json={"mode": mode})
                    response.raise_for_status()
                    observed = response.json()
                    if observed.get("mode") != mode:
                        raise ValueError("设备未确认目标工作模式")
                    observed["active"] = observed.get("active_provider")
                    self.inference_status = observed
                    self.settings.data["inference_broker"]["mode"] = mode
                    return observed
                except Exception as error:
                    try:
                        await self.refresh_inference()
                    except Exception:
                        self.inference_status = self.inference_status | {
                            "state": "error",
                            "error": "工作模式结果待设备重新确认",
                        }
                    raise ValueError("工作模式切换失败：" + public_error(error)) from None

    async def generate_reply(self, scope, character, event, relationship=None, mode="live", *, context=None):
        """Caller owns generation_lock; evidence and one repair remain in its lease."""
        context = context or self.context
        evidence = context.memory.retrieve(scope, character, event["text"])
        messages = await context.prepare(scope, character, event, relationship, mode, evidence=evidence)
        reply = await self.llm.generate(messages, character, event["event_id"], event["text"])
        missing = missing_slots(reply.spoken_text, evidence)
        audit = {
            "sources": evidence["evidence"],
            "required_slots": evidence["required_slots"],
            "missing": missing,
            "retried": False,
            "warning": None,
            "memory_kind": "derived_reply" if evidence["evidence"] else "role_story",
        }
        if missing:
            audit["retried"] = True
            repair = messages + [
                {
                    "role": "system",
                    "content": "上一次回答没有完整回应当前明确问题。只根据已补回的带来源原句补齐这些所问字段："
                    + ", ".join(missing)
                    + "。不要新增没有原句依据的细节，不需要复述问题未问的全部事实；仍按原JSON结构和字数回答。",
                }
            ]
            try:
                revised = await self.llm.generate(repair, character, event["event_id"], event["text"])
                remaining = missing_slots(revised.spoken_text, evidence)
                if len(remaining) < len(missing):
                    reply, missing = revised, remaining
                audit["missing"] = missing
            except Exception as error:
                audit["warning"] = "证据补答未完成：" + public_error(error)
        if missing:
            labels = {"location": "地点", "person": "人物", "item": "物品", "quantity": "数量"}
            warning = "回答可能仍遗漏所问的" + "、".join(labels.get(slot, slot) for slot in missing)
            audit["warning"] = "; ".join(filter(None, [audit["warning"], warning]))
        return reply, audit

    async def send_overlay(self, message):
        if self.overlay:
            try:
                await self.overlay.send_json(message)
                return True
            except Exception:
                self.overlay = None
                self.overlay_id = None
        return False

    def new_session(self):
        self.session_id = str(uuid4())
        character = self.character
        self.store.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?)",
            (self.session_id, character["id"], character["version"], time.time(), ""),
        )

    async def cancel_current(self, reason="人工取消"):
        job_id = self.current_id
        if job_id:
            job = self.store.job(job_id)
            if job and job["status"] not in TERMINAL:
                self.store.update_job(job_id, status="cancelled", error=reason)
                self.store.execute(
                    "UPDATE events SET status='cancelled',reason=? WHERE id=?", (reason, job["event_id"])
                )
        # Cancel the producer before yielding so a late synthesis cannot emit another play instruction.
        task = self.task
        if task and not task.done():
            task.cancel()
        self.playback_done.set()
        await self.send_overlay({"type": "stop", "job_id": job_id})
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def clear_queue(self, reason="清空待播队列"):
        for job_id in self.queue:
            job = self.store.job(job_id)
            self.store.update_job(job_id, status="cancelled", error=reason)
            self.store.execute(
                "UPDATE events SET status='cancelled',reason=? WHERE id=?", (reason, job["event_id"])
            )
        self.queue.clear()

    async def control(self, action):
        if action in {"start", "resume"} and not self.settings.data.get("features", {}).get(
            "live_enabled", False
        ):
            raise ValueError("直播功能已后置；当前使用非直播工作台")
        if action == "resume" and self.state == "stopped":
            raise ValueError("请先启动Mock会话")
        if action in {"start", "resume"} and self.inference_client is not None:
            await self.set_work_mode("live")
        if action == "start":
            if self.state == "stopped":
                self.new_session()
            self.state = "live"
            self.error = None
        elif action == "pause":
            if self.state != "stopped":
                self.state = "paused"
        elif action == "resume":
            self.state = "live"
            self.error = None
        elif action == "stop":
            self.state = "stopped"
            self.clear_queue()
            await self.cancel_current("停止会话")
        elif action == "mute":
            self.muted = True
            await self.cancel_current("紧急静音")
        elif action == "unmute":
            self.muted = False
        elif action == "skip":
            await self.cancel_current("跳过当前任务")
        elif action == "clear":
            self.clear_queue()
        else:
            raise ValueError("未知控制命令")
        self.wake.set()
        return self.status()

    async def activate(self, character_id, version=None):
        self.settings.voice(self.store.character(character_id, version)["voice_profile"])
        previous_state = self.state
        if previous_state == "live":
            self.state = "paused"
        self.clear_queue("角色切换")
        await self.cancel_current("角色切换")
        self.clear_queue("角色切换期间入队")
        result = self.store.activate(character_id, version)
        self.state = previous_state
        if self.state != "stopped":
            self.new_session()
        await self.send_overlay(
            {"type": "character", "character": result, "assets": self.settings.assets(result)}
        )
        self.wake.set()
        return result

    async def submit(self, event):
        now = datetime.now(UTC)
        age = (now - event.timestamp).total_seconds()
        status, reason = "queued", None
        if self.state == "stopped":
            status, reason = "filtered", "Mock会话未启动"
        elif age > self.settings.data["event_ttl_seconds"] or age < -30:
            status, reason = "expired", "事件过期或时间在未来"
        elif event.type == "song":
            status, reason = "filtered", "点歌播放属于后续直播适配，不假称已执行"
        elif event.type == "disconnect":
            status, reason = "filtered", "模拟平台断线；需人工恢复"
        elif event.type == "chat" and (not event.text.strip() or len(set(event.text.strip())) < 2):
            status, reason = "filtered", "空消息或重复字符"
        # Bound both acceptance rate and the tracking map; anonymous viewers are not merged by nickname.
        user_key = (event.platform, event.platform_user_id) if event.platform_user_id else None
        if user_key and status == "queued" and time.monotonic() - self.accept_times.get(user_key, -100) < 1:
            status, reason = "filtered", "同用户短时限流"
        db_id, fresh = self.store.add_event(event, self.session_id, status, reason)
        if not fresh:
            return {"id": db_id, "status": "duplicate", "reason": "重复事件，不再入队"}
        if event.type == "disconnect" and self.state != "stopped":
            self.state, self.error = "degraded", reason
            self.wake.set()
        if status != "queued":
            return {"id": db_id, "status": status, "reason": reason}
        if user_key:
            self.accept_times[user_key] = time.monotonic()
            if len(self.accept_times) > 2000:
                self.accept_times = {k: t for k, t in self.accept_times.items() if time.monotonic() - t < 60}
                while len(self.accept_times) > 2000:
                    self.accept_times.pop(next(iter(self.accept_times)))
        if len(self.queue) >= self.settings.data["queue_limit"]:
            self.store.execute("UPDATE events SET status='dropped',reason='队列已满' WHERE id=?", (db_id,))
            return {"id": db_id, "status": "dropped", "reason": "队列已满"}
        job_id, character = str(uuid4()), self.character
        self.store.execute(
            "INSERT INTO jobs(id,event_id,session_id,character_id,character_version,status,created_at,"
            "updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
            (job_id, db_id, self.session_id, character["id"], character["version"], time.time(), time.time()),
        )
        self.queue.append(job_id)
        self.wake.set()
        return {"id": db_id, "job_id": job_id, "status": "queued"}

    def pick(self):
        # Basic P0 gift priority, alternating with chat to prevent starvation; P1 audio quotas remain separate.
        selected = 0
        if not self.last_gift:
            for i, job_id in enumerate(self.queue):
                if self.store.event(self.store.job(job_id)["event_id"])["payload"]["type"] == "gift":
                    selected = i
                    break
        job_id = self.queue.pop(selected)
        self.last_gift = self.store.event(self.store.job(job_id)["event_id"])["payload"]["type"] == "gift"
        return job_id

    async def _loop(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.queue and self.state == "live" and not self.muted:
                self.current_id = self.pick()
                self.task = asyncio.create_task(self._process(self.current_id))
                try:
                    await self.task
                except asyncio.CancelledError:
                    # A control command cancels only the child. Shutdown cancels this worker too.
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.current_id, self.task = None, None

    async def _process(self, job_id):
        try:
            job = self.store.job(job_id)
            row = self.store.event(job["event_id"])
            event = row["payload"]
            age = time.time() - datetime.fromisoformat(event["timestamp"]).timestamp()
            if age > self.settings.data["event_ttl_seconds"]:
                self.store.update_job(job_id, status="expired", error="排队期间过期")
                self.store.execute("UPDATE events SET status='expired' WHERE id=?", (row["id"],))
                return
            character = self.store.character(job["character_id"], job["character_version"])
            async with self.generation_lock:
                if event["type"] == "sfx":
                    asset = next(
                        (
                            a
                            for a in self.settings.assets(character)
                            if a["id"] == event["metadata"].get("asset_id") and a["path"].endswith(".wav")
                        ),
                        None,
                    )
                    if asset is None:
                        raise ValueError("指定的预录音效不存在")
                    if self.settings.data.get("commercial_mode"):
                        raise ValueError("当前原型预录资产未配置商业授权")
                    self.store.update_job(job_id, status="synthesizing")
                    audio = await self.voices.prerecorded(asset["path"], asset.get("text", "预录音效"))
                    emotion, animation = "cheerful", "gift"
                else:
                    self.store.update_job(job_id, status="generating")
                    started = time.perf_counter()
                    reply, answer_check = await self.generate_reply(
                        "live:" + self.session_id,
                        character,
                        event,
                        self.store.relationship(character["id"], row["user_id"]),
                    )
                    self.timings["llm_ms"] = round((time.perf_counter() - started) * 1000)
                    self.store.update_job(
                        job_id,
                        status="synthesizing",
                        text=reply.spoken_text,
                        emotion=reply.emotion,
                        animation_id=reply.animation_id,
                        llm_ms=self.timings["llm_ms"],
                        answer_check=answer_check,
                    )
                    started = time.perf_counter()
                    audio = await self.voices.synthesize(
                        reply.spoken_text, self.settings.voice(character["voice_profile"])
                    )
                    self.timings["tts_ms"] = round((time.perf_counter() - started) * 1000)
                    self.store.update_job(job_id, tts_ms=self.timings["tts_ms"], error=audio.warning)
                    emotion, animation = reply.emotion, reply.animation_id
            # Preserve model suggestion separately in error when prerecorded text differs.
            suggestion = self.store.job(job_id)["text"]
            warning = audio.warning
            check = self.store.job(job_id).get("answer_check")
            if check:
                check = json.loads(check) if isinstance(check, str) else check
                warning = "; ".join(filter(None, [warning, check.get("warning")])) or None
            if warning and suggestion != audio.text:
                warning += f"；模型候选：{suggestion}"
            self.store.update_job(
                job_id,
                status="ready_to_play",
                text=audio.text,
                emotion=emotion,
                animation_id=animation,
                audio_ref=audio.ref,
                error=warning,
            )
            deadline = time.monotonic() + self.settings.data.get("playback_wait_seconds", 45)
            while not self.overlay:
                if time.monotonic() > deadline:
                    raise ValueError("没有overlay播放器，未播报；请打开OBS地址或浏览器播放器")
                await asyncio.sleep(0.1)
            token = secrets.token_urlsafe(24)
            self.store.update_job(job_id, playback_token=token)
            self.playback_done.clear()
            sent = await self.send_overlay(
                {
                    "type": "play",
                    "job_id": job_id,
                    "playback_token": token,
                    "text": audio.text,
                    "emotion": emotion,
                    "animation_id": animation,
                    "audio_ref": audio.ref,
                    "duration": audio.duration,
                    "character": character,
                    "assets": self.settings.assets(character),
                }
            )
            if not sent:
                raise ValueError("播放器连接已断开，未重播")
            await asyncio.wait_for(self.playback_done.wait(), timeout=audio.duration + 20)
            if self.store.job(job_id)["status"] == "ready_to_play":
                raise ValueError("播放器未确认开始")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = public_error(error)
            if isinstance(error, TimeoutError):
                message = "播放确认超时；不视为已播报，不自动重播"
            job = self.store.job(job_id)
            if job["status"] not in TERMINAL:
                self.store.update_job(job_id, status="failed", error=message)
                self.store.execute(
                    "UPDATE events SET status='failed',reason=? WHERE id=?", (message, job["event_id"])
                )
                await self.send_overlay({"type": "stop", "job_id": job_id})
            self.error = message

    async def attach_overlay(self, websocket):
        if self.overlay:
            await websocket.send_json({"type": "standby", "reason": "已有播放者；此窗口不重复播报"})
            return None
        owner = str(uuid4())
        self.overlay, self.overlay_id = websocket, owner
        await websocket.send_json(
            {"type": "hello", "character": self.character, "assets": self.settings.assets(self.character)}
        )
        return owner

    async def detach_overlay(self, owner):
        if owner and owner == self.overlay_id:
            self.overlay, self.overlay_id = None, None
            await self.cancel_current("overlay断线，任务取消且不重播")

    async def acknowledge(self, owner, data):
        if not owner or owner != self.overlay_id or data.get("job_id") != self.current_id:
            return False
        job = self.store.job(self.current_id)
        if not secrets.compare_digest(str(data.get("playback_token", "")), str(job["playback_token"] or "")):
            return False
        if data.get("type") == "started" and job["status"] == "ready_to_play":
            self.store.update_job(job["id"], status="playing")
            return True
        if data.get("type") == "ended" and self.store.complete(job["id"]):
            event = self.store.event(job["event_id"])["payload"]
            character = self.store.character(job["character_id"], job["character_version"])
            check = json.loads(job["answer_check"]) if job.get("answer_check") else {}
            self.context.record(
                "live:" + job["session_id"],
                character,
                event,
                job["text"],
                memory_kind=check.get("memory_kind"),
            )
            self.last_played = self.store.job(job["id"])
            self.playback_done.set()
            return True
        if data.get("type") == "failed" and job["status"] not in TERMINAL:
            self.store.update_job(
                job["id"], status="failed", error="浏览器播放失败，请检查自动播放权限／音频设备"
            )
            self.playback_done.set()
            return True
        return False
