"""Background live session worker loop, job picking and playback dispatching."""

import asyncio
import json
import time
from datetime import datetime

from ...core.models import Event, Reply
from ...infra.inference.omni_speech import sentences
from ...infra.inference.providers import public_error
from ...persona.memory.daily import MemoryChanged
from .state import HANDOFF_PLATFORM, IDLE_AFTER, IDLE_COOLDOWN, IDLE_WINDOW, TERMINAL


class LiveSessionRunnerMixin:
    def start_worker(self):
        self.worker = asyncio.create_task(self._loop())

    async def stop_worker(self):
        self.reset_poll()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

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
        self.playback = None
        self.media_changed.set()
        self.online_songs.sync()
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
        self.online_songs.sync()

    def current_is_online(self):
        job = self.store.job(self.current_id) if self.current_id else None
        return bool(job and job.get("media") and json.loads(job["media"]).get("online"))

    def clear_online_queue(self):
        for identity in list(self.queue):
            job = self.store.job(identity)
            if job.get("media") and json.loads(job["media"]).get("online"):
                self.queue.remove(identity)
                self.store.update_job(identity, status="cancelled", error="关闭／重新配置在线点歌组件")
                self.store.execute(
                    "UPDATE events SET status='cancelled',reason=? WHERE id=?",
                    ("在线点歌组件关闭／重配", job["event_id"]),
                )

    def idle_deadline(self):
        if self.state != "live" or self.muted or not self.overlay or not self.options().idle:
            return None
        due = self.last_activity + IDLE_AFTER
        if self.idle_attempts:
            due = max(due, self.idle_attempts[-1] + IDLE_COOLDOWN)
        if len(self.idle_attempts) == self.idle_attempts.maxlen:
            due = max(due, self.idle_attempts[0] + IDLE_WINDOW)
        return due

    def idle_unplayed(self):
        job = self.store.job(self.current_id) if self.current_id else None
        return bool(
            job and job["status"] not in TERMINAL | {"playing"}
            and self.store.event(job["event_id"])["payload"]["type"] == "idle"
        )

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
            deadline = self.idle_deadline()
            delay = max(0.001, deadline - time.monotonic()) if deadline is not None else None
            if not self.queue or self.state != "live" or self.muted:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
            self.wake.clear()
            idle_at = self.idle_deadline()
            if not self.queue and idle_at is not None and time.monotonic() >= idle_at:
                if self.runtime.generation_lock.locked():
                    self.last_activity = time.monotonic()
                else:
                    self.idle_attempts.append(time.monotonic())
                    line = self.options().idle_text or "想聊的话，随时在弹幕里说。"
                    await self._submit(Event(type="idle", text=line), internal=True)
            while self.queue and self.state == "live" and not self.muted:
                self.current_id = self.pick()
                self.online_songs.sync()
                self.task = asyncio.create_task(self._process(self.current_id))
                try:
                    await self.task
                except asyncio.CancelledError:
                    # A control command cancels only the child. Shutdown cancels this worker too.
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.current_id, self.task = None, None
                    self.playback = None
                    self.online_songs.sync()

    async def _process(self, job_id):
        try:
            answer_check = None
            self.timings = dict.fromkeys(self.timings)
            self.spoken_parts.clear()
            job = self.store.job(job_id)
            row = self.store.event(job["event_id"])
            event = row["payload"]
            account_id = event["metadata"].get("_live_account")
            if account_id:
                event["account_id"] = account_id
            if event["type"] == "song":
                await self._process_song(job_id, job)
                return
            age = time.time() - datetime.fromisoformat(event["timestamp"]).timestamp()
            if age > self.settings.data["event_ttl_seconds"]:
                self.store.update_job(job_id, status="expired", error="排队期间过期")
                self.store.execute("UPDATE events SET status='expired' WHERE id=?", (row["id"],))
                return
            character = self.store.persona_defaults(self.store.character(job["character_id"], job["character_version"]))
            parts = []
            async with self.runtime.generation_lock:
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
                    audio = await self.runtime.voices.prerecorded(
                        asset["path"], asset.get("text", "预录音效")
                    )
                    emotion, animation = "cheerful", "gift"
                else:
                    self.store.update_job(job_id, status="generating")
                    meta = event.get("metadata") or {}
                    if event["type"] == "idle":
                        reply = Reply(spoken_text=event["text"], emotion="cheerful", animation_id="talk")
                        answer_check = None
                        self.timings["llm_ms"] = 0
                    elif announcement := meta.get("draw_guess_start") or meta.get("game_switch"):
                        reply = Reply(
                            spoken_text=announcement["spoken_text"],
                            emotion="cheerful",
                            animation_id="talk",
                            reply_to=[event["event_id"]],
                        )
                        answer_check = None
                        self.timings["llm_ms"] = 0
                    else:
                        for kind in ("draw_guess_won", "game_action"):
                            if kind in meta:
                                event["live_outcome"] = {"kind": kind, **meta[kind]}
                        started = time.perf_counter()
                        reply, answer_check = await self.runtime.generate_reply(
                            "live:" + self.session_id,
                            character,
                            event,
                            {} if event["platform"] == HANDOFF_PLATFORM else self.runtime.relationships.live_view(
                                self.session_id, character["id"], event["platform"],
                                event["platform_user_id"], account_id=account_id or "",
                            ),
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
                    parts = sentences(reply.spoken_text)
                    if len(parts) == 1:
                        started = time.perf_counter()
                        audio = await self.runtime.voices.synthesize_live(
                            reply.spoken_text, character["voice_profile"]
                        )
                        self.timings["tts_ms"] = round((time.perf_counter() - started) * 1000)
                        self.store.update_job(job_id, tts_ms=self.timings["tts_ms"])
                    emotion, animation = reply.emotion, reply.animation_id
            if len(parts) > 1:
                await self._play_live_parts(job_id, parts, character, reply, answer_check)
                return
            self.store.update_job(
                job_id,
                status="ready_to_play",
                text=audio.text,
                emotion=emotion,
                animation_id=animation,
                audio_ref=audio.ref,
                error=(answer_check or {}).get("warning"),
            )
            await self._play(job_id, audio, character, emotion, animation, memory_audit=answer_check)
        except MemoryChanged:
            if self.state in {"live", "paused"} and not self.spoken_parts:
                self.store.update_job(job_id, status="queued", text="", audio_ref=None, answer_check=None)
                self.queue.insert(0, job_id)
                self.wake.set()
            else:
                job = self.store.job(job_id)
                error = "记忆已变更，后续台词未播出"
                self.store.update_job(job_id, status="cancelled", error=error, text="".join(self.spoken_parts))
                self.store.execute("UPDATE events SET status='cancelled',reason=? WHERE id=?", (error, job["event_id"]))
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
            self.runtime.error = message

    async def _play_live_parts(self, job_id, parts, character, reply, answer_check):
        async def synthesize(part):
            async with self.runtime.generation_lock:
                started = time.perf_counter()
                audio = await self.runtime.voices.synthesize_live(part, character["voice_profile"])
                return audio, round((time.perf_counter() - started) * 1000)

        pending = None
        tts_ms = 0
        try:
            for index, part in enumerate(parts):
                audio, elapsed = await (pending if pending is not None else synthesize(part))
                tts_ms += elapsed
                self.timings["tts_ms"] = tts_ms
                self.store.update_job(
                    job_id,
                    status="ready_to_play",
                    tts_ms=tts_ms,
                    audio_ref=audio.ref,
                    error=(answer_check or {}).get("warning"),
                )
                final_segment = index == len(parts) - 1
                pending = None if final_segment else asyncio.create_task(synthesize(parts[index + 1]))
                await self._play(
                    job_id,
                    audio,
                    character,
                    reply.emotion,
                    reply.animation_id,
                    memory_audit=answer_check,
                    final_segment=final_segment,
                )
                if self.store.job(job_id)["status"] in TERMINAL:
                    return
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _process_song(self, job_id, job):
        snapshot = json.loads(job["media"])
        if time.time() - job["created_at"] > snapshot["ttl"]:
            raise ValueError("点歌排队已过期")
        self.store.update_job(job_id, status="synthesizing")
        character = self.store.character(job["character_id"], job["character_version"])
        if snapshot.get("online"):
            snapshot = await self.online_songs.prepared(job_id)
            self.store.update_job(job_id, media=snapshot)
            audio, activity = await asyncio.to_thread(self.media.prepare, snapshot)
        elif snapshot["mode"] == "sing":
            async with self.runtime.generation_lock:
                audio, activity = await self.media.singing.prepare(snapshot, character)
        else:
            audio, activity = await asyncio.to_thread(self.media.prepare, snapshot)
        ref = "/media/music/" + audio.path.stem
        self.store.update_job(job_id, status="ready_to_play", text=audio.text, audio_ref=ref)
        await self._play(job_id, audio, character, "cheerful", "idle", media=snapshot, activity=activity)
