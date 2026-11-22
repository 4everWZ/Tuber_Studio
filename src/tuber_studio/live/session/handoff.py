"""Overlay WebSocket lifecycle, playback sync and operator live handoff."""

import asyncio
import json
import secrets
import time
from uuid import uuid4

from ...core.models import Event
from ...core.store import dump
from ..media import inspect_wav
from .state import HANDOFF_PLATFORM, TERMINAL


class LiveSessionHandoffMixin:
    async def send_overlay(self, message):
        if self.overlay:
            try:
                await self.overlay.send_json(message)
                return True
            except Exception:
                self.overlay = None
                self.overlay_id = None
        return False

    async def submit_handoff(self, request_id, session_id, text, *, account):
        event = Event(platform=HANDOFF_PLATFORM, room_id=session_id, event_id=request_id, text=text,
                      display_name="本场交接")
        if event_id := self.store.duplicate_event(event):
            job = self.store.one("SELECT id,status FROM jobs WHERE event_id=?", (event_id,))
            return {"job_id": job["id"], "status": job["status"]} if job else {"status": "unavailable"}
        if self.state not in {"live", "paused"} or self.session_id != session_id:
            raise ValueError("本场直播已变化")
        return await self._submit(event, internal=True, handoff_account=account)

    def handoff_result(self, session, job_id):
        job = self.store.job(job_id) if job_id else None
        if not job or job["session_id"] != session:
            return {"status": "unavailable"}
        return {"status": job["status"], "spoken": job["text"] if job["status"] == "completed" else None}

    async def cancel_handoff(self, request_id, session):
        job = self.store.one(
            "SELECT j.id,j.event_id,j.status FROM jobs j JOIN events e ON e.id=j.event_id WHERE e.event_key=?",
            (dump([HANDOFF_PLATFORM, session, request_id]),),
        )
        if not job:
            return
        if job["status"] in TERMINAL:
            return self.handoff_result(session, job["id"])
        reason = "Agent 交接已取消"
        if self.current_id == job["id"]:
            await self.cancel_current(reason)
        elif job["id"] in self.queue:
            self.queue.remove(job["id"])
            self.store.update_job(job["id"], status="cancelled", error=reason)
            self.store.execute("UPDATE events SET status='cancelled',reason=? WHERE id=?", (reason, job["event_id"]))
        result = self.handoff_result(session, job["id"])
        if result["status"] not in TERMINAL:
            raise ValueError("直播交接的停止结果待核对")
        return result

    async def _play(
        self,
        job_id,
        audio,
        character,
        emotion,
        animation,
        *,
        media=None,
        activity=None,
        memory_audit=None,
        final_segment=True,
    ):
        if not media and self.settings.data.get("features", {}).get("live_enabled"):
            try:
                activity = await asyncio.to_thread(inspect_wav, audio.path, envelope=True)
            except ValueError:
                activity = None
        if self.idle_unplayed() and (self.state != "live" or not self.options().idle):
            job = self.store.job(job_id)
            self.store.update_job(job_id, status="cancelled", error="冷场任务已暂停或关闭")
            self.store.execute("UPDATE events SET status='cancelled' WHERE id=?", (job["event_id"],))
            return
        deadline = time.monotonic() + self.settings.data.get("playback_wait_seconds", 45)
        while not self.overlay:
            if time.monotonic() > deadline:
                raise ValueError("没有overlay播放器，未播报；请打开OBS地址或浏览器播放器")
            await asyncio.sleep(0.1)
        self.runtime.validate_memory_delivery(memory_audit)
        token = secrets.token_urlsafe(24)
        self.store.update_job(job_id, playback_token=token)
        self.playback_done.clear()
        self.playback = {
            "job_id": job_id,
            "song": media,
            "text": audio.text,
            "animation_id": animation,
            "emotion": emotion,
            "position": 0,
            "duration": audio.duration,
            "paused": False,
            "started": False,
            "final_segment": final_segment,
            "reported_at": time.time(),
            "mouth": 0,
            "visual_warning": (
                "缺少独立人声，口型保持待机"
                if media and not activity
                else "缺少角色嘴型素材"
                if media
                and not {"sing_open", "sing_closed"}.issubset(
                    {asset["id"] for asset in self.settings.assets(character)}
                )
                else None
            ),
        }
        sent = await self.send_overlay(
            {
                "type": "play",
                "job_id": job_id,
                "playback_token": token,
                "text": audio.text,
                "emotion": emotion,
                "animation_id": animation,
                "audio_ref": self.store.job(job_id)["audio_ref"],
                "duration": audio.duration,
                **self.overlay_character(character),
                "media": media,
                "activity": activity,
                "volume": self.media.config()["volume"] if media else 1,
            }
        )
        if not sent:
            raise ValueError("播放器连接已断开，未重播")
        remaining = audio.duration + 20
        while not self.playback_done.is_set():
            began = time.monotonic()
            self.media_changed.clear()
            try:
                await asyncio.wait_for(self.media_changed.wait(), timeout=min(1, remaining))
            except TimeoutError:
                pass
            if not (self.playback or {}).get("paused"):
                remaining -= time.monotonic() - began
            elif time.time() - self.playback["reported_at"] > 15:
                raise ValueError("暂停时播放器失去心跳")
            if remaining <= 0:
                raise TimeoutError
        if self.store.job(job_id)["status"] == "ready_to_play":
            raise ValueError("播放器未确认开始")

    async def attach_overlay(self, websocket):
        if self.overlay:
            await websocket.send_json({"type": "standby", "reason": "已有播放者；此窗口不重复播报"})
            return None
        owner = str(uuid4())
        self.overlay, self.overlay_id = websocket, owner
        self.last_activity = time.monotonic()
        self.wake.set()
        await websocket.send_json(
            {
                "type": "hello",
                **self.overlay_character(self.runtime.character),
            }
        )
        return owner

    async def detach_overlay(self, owner):
        if owner and owner == self.overlay_id:
            self.overlay, self.overlay_id = None, None
            await self.cancel_current("overlay断线，任务取消且不重播")
            self.wake.set()

    async def acknowledge(self, owner, data):
        if not owner or owner != self.overlay_id or data.get("job_id") != self.current_id:
            return False
        job = self.store.job(self.current_id)
        token = job["playback_token"]
        if not token or not secrets.compare_digest(str(data.get("playback_token", "")), token):
            return False
        if data.get("type") == "progress" and job["status"] == "playing" and self.playback:
            position, mouth = data.get("position"), data.get("mouth", 0)
            if (
                type(position) not in {float, int}
                or not 0 <= position <= self.playback["duration"] + 1
                or type(mouth) not in {float, int}
                or not 0 <= mouth <= 1
                or type(data.get("paused")) is not bool
            ):
                return False
            self.playback.update(
                position=position, paused=data["paused"], mouth=mouth, reported_at=time.time()
            )
            self.media_changed.set()
            return True
        if data.get("type") == "started" and job["status"] == "ready_to_play":
            self.store.update_job(job["id"], status="playing")
            if self.timings["first_audio_ms"] is None:
                received_at = self.store.event(job["event_id"])["created_at"]
                self.timings["first_audio_ms"] = max(0, round((time.time() - received_at) * 1000))
            if self.playback:
                self.playback["started"] = True
            return True
        if data.get("type") == "ended" and job["status"] == "playing" and self.playback:
            self.spoken_parts.append(self.playback["text"])
            if not self.playback["final_segment"]:
                self.store.update_job(job["id"], status="synthesizing", playback_token=None)
                self.playback = None
                self.playback_done.set()
                self.media_changed.set()
                return True
            spoken = "".join(self.spoken_parts)
            if not self.store.complete(job["id"], spoken):
                return False
            event = self.store.event(job["event_id"])["payload"]
            character = self.store.character(job["character_id"], job["character_version"])
            check = json.loads(job["answer_check"]) if job.get("answer_check") else {}
            if event["type"] not in {"song", "idle"} and event["platform"] != HANDOFF_PLATFORM:
                if account_id := event["metadata"].get("_live_account"):
                    event["account_id"] = account_id
                self.runtime.context.record(
                    "live:" + job["session_id"],
                    character,
                    event,
                    None if check.get("organized_memory") or "memory_preference_revision" in check else spoken,
                    memory_kind=check.get("memory_kind"),
                )
            if event["type"] == "chat" and event["platform"] != HANDOFF_PLATFORM:
                self.observe_relationship(character, event)
            self.last_played = self.store.job(job["id"])
            self.last_activity = time.monotonic()
            self.playback = None
            self.playback_done.set()
            self.media_changed.set()
            return True
        if data.get("type") == "failed" and job["status"] not in TERMINAL:
            message = "浏览器播放失败，请检查自动播放权限／音频设备"
            self.store.update_job(
                job["id"], status="failed", error=message
            )
            self.store.execute(
                "UPDATE events SET status='failed',reason=? WHERE id=?", (message, job["event_id"])
            )
            self.runtime.error = message
            self.playback = None
            self.playback_done.set()
            self.media_changed.set()
            return True
        return False
