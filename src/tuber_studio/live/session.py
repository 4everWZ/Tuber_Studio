"""Live session, job queue and overlay playback ownership."""

import asyncio
import json
import secrets
import time
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

from ..models import Event, Reply
from ..providers import public_error
from ..store import character_card
from .activity_filter import ActivitySummary, LiveActivityFilter
from .draw_and_guess import DrawAndGuessService
from .game_livestream import GameLivestreamService
from .interactive_rules import InteractiveRulesService
from .media import LiveMedia, inspect_wav
from .online_songs import OnlineSongs
from .options import LiveLookup, LiveOptions

TERMINAL = {"completed", "failed", "cancelled", "expired"}
IDLE_AFTER = 90
IDLE_COOLDOWN = 180
IDLE_WINDOW = 600
IDLE_LINES = (
    "小窝安静得能听见气泡冒上去。你们慢慢潜水，我也歇会儿。",
    "给尾巴找个舒服的位置……好，这下谁都别挪我的坐垫。",
    "刚刚桌上还有一碗白饭。至于现在去哪儿了，你们别问。",
)


class LiveSession:
    def __init__(self, runtime, *, singing_provider=None, live_scene=None):
        self.runtime = runtime
        self.settings, self.store = runtime.settings, runtime.store
        self.interactive_rules = InteractiveRulesService(self.store)
        self.draw_and_guess = DrawAndGuessService(daily_memory=runtime.daily_memory)
        self.game_livestream = GameLivestreamService(interactive_rules=self.interactive_rules)
        self.live_scene = live_scene
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
        self.last_played = None
        self.timings = {"llm_ms": None, "tts_ms": None, "first_audio_ms": None}
        self.accept_times = {}
        self.submissions = {}
        self.last_gift = False
        self.media = LiveMedia(self.settings, self.store, singing_provider=singing_provider)
        self.playback = None
        self.spoken_parts = []
        self.media_changed = asyncio.Event()
        self.online_songs = OnlineSongs(runtime)
        self.activity_filter = LiveActivityFilter()
        self.poll_timer = None
        self.recent_barrage = deque(maxlen=20)
        self.recent_activities = deque(maxlen=8)
        self.lookup = LiveLookup()
        self.last_activity = time.monotonic()
        self.idle_attempts = deque(maxlen=2)
        self.idle_index = 0

    @property
    def busy(self):
        return bool(self.current_id or self.queue)

    def start_worker(self):
        self.worker = asyncio.create_task(self._loop())

    async def stop_worker(self):
        self.reset_poll()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

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
        self.online_songs.choices.clear()
        self.reset_poll()
        self.recent_barrage.clear()
        self.recent_activities.clear()
        self.accept_times.clear()
        self.last_played = None
        self.timings = dict.fromkeys(self.timings)
        self.idle_attempts.clear()
        self.runtime.relationships.clear_live()
        self.last_activity = time.monotonic()
        self.session_id = str(uuid4())
        character = self.runtime.character
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

    async def control(self, action):
        if action in {"media-pause", "media-resume"}:
            if not self.playback or not self.playback.get("song"):
                raise ValueError("当前没有可暂停的歌曲")
            if self.store.job(self.current_id)["status"] != "playing":
                raise ValueError("请等待歌曲实际开始播放")
            paused = action == "media-pause"
            await self.send_overlay({"type": "media-control", "job_id": self.current_id, "paused": paused})
            return self.runtime.status()
        if action in {"start", "resume"} and not self.settings.data.get("features", {}).get(
            "live_enabled", False
        ):
            raise ValueError("直播功能已后置；当前使用非直播工作台")
        if action == "resume" and self.state == "stopped":
            raise ValueError("请先启动Mock会话")
        if action in {"start", "resume"} and self.runtime.inference.client is not None:
            await self.runtime.inference.set_mode("live")
        if action == "start":
            if self.state == "stopped":
                self.new_session()
            self.state = "live"
            self.runtime.error = None
        elif action == "pause":
            if self.state != "stopped":
                self.state = "paused"
                if self.idle_unplayed():
                    await self.cancel_current("已暂停冷场任务")
        elif action == "resume":
            self.state = "live"
            self.runtime.error = None
        elif action == "stop":
            self.state = "stopped"
            self.online_songs.choices.clear()
            self.reset_poll()
            self.recent_barrage.clear()
            self.recent_activities.clear()
            self.runtime.relationships.clear_live()
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
        self.last_activity = time.monotonic()
        self.wake.set()
        self.online_songs.sync()
        return self.runtime.status()

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
            {"type": "character", "character": character_card(result), "assets": self.settings.assets(result)}
        )
        self.wake.set()
        return result

    async def submit(self, event, *, online_snapshot=None, verified_account_id=None):
        key = (event.platform, event.room_id, event.event_id)
        while key in self.submissions:
            await asyncio.shield(self.submissions[key])
        if existing := self.store.duplicate_event(event):
            return {"id": existing, "status": "duplicate", "reason": "重复事件，不再入队"}
        finished = asyncio.get_running_loop().create_future()
        self.submissions[key] = finished
        try:
            return await self._submit(
                event, online_snapshot=online_snapshot, verified_account_id=verified_account_id
            )
        finally:
            self.submissions.pop(key, None)
            finished.set_result(None)

    async def _submit(self, event, *, online_snapshot=None, verified_account_id=None, internal=False):
        session_id = self.session_id
        now = datetime.now(UTC)
        age = (now - event.timestamp).total_seconds()
        status, reason = "queued", None
        if event.type == "idle" and not internal:
            raise ValueError("冷场任务只由直播调度器生成")
        metadata = dict(event.metadata)
        metadata.pop("_live_account", None)
        metadata.pop("_live_input", None)
        original_type, original_text = event.type, event.text
        if verified_account_id and event.platform == "bilibili":
            metadata["_live_account"] = str(verified_account_id)
        event = event.model_copy(update={"metadata": metadata})
        if self.state != "stopped" and -30 <= age <= self.settings.data["event_ttl_seconds"]:
            self.expire_poll()
            if event.type == "activity" and event.metadata.get("summary") is not None:
                activity = ActivitySummary.model_validate(event.metadata["summary"]).model_dump()
                item = activity | {"platform": event.platform, "room_id": event.room_id}
                fields = ("platform", "room_id", "id", "phase")
                duplicate = any(all(old[k] == item[k] for k in fields) for old in self.recent_activities)
                if not duplicate:
                    self.recent_activities.append(item)
                return {"id": None, "status": "duplicate" if duplicate else "recorded", "reason": "活动摘要"}
            filtered = self.activity_filter.check_event(event)
            if filtered:
                status, reason = filtered
                return {"id": None, "status": status, "reason": reason}
            if event.type == "chat" and (text := clean_barrage(event.text)):
                self.recent_barrage.append({
                    "platform": event.platform,
                    "room_id": event.room_id,
                    "event_id": event.event_id,
                    "uid": event.platform_user_id,
                    "display_name": event.display_name,
                    "text": text[:240],
                    "at": event.timestamp.timestamp(),
                })
            try:
                if event.type == "chat":
                    # 1. Check if Draw & Guess is active and user guessed correctly
                    if self.draw_and_guess.is_active():
                        matched_win = self.draw_and_guess.check_guess(
                            user_id=event.platform_user_id,
                            display_name=event.display_name,
                            text=event.text,
                            character_id=self.runtime.character["id"] if self.runtime.character else "whale",
                            character_version=self.runtime.character["version"]
                            if self.runtime.character
                            else 1,
                        )
                        if matched_win:
                            event = event.model_copy(
                                update={
                                    "text": matched_win["spoken_text"],
                                    "metadata": {**event.metadata, "draw_guess_won": matched_win},
                                }
                            )

                    # 2. Check if chat command requests to start Draw & Guess
                    if not event.metadata.get("draw_guess_won"):
                        should_start, _ = self.interactive_rules.match_draw_and_guess_start(event)
                        if should_start:
                            start_info = self.draw_and_guess.start_round(trigger_user=event.display_name)
                            event = event.model_copy(
                                update={
                                    "text": start_info["spoken_text"],
                                    "metadata": {**event.metadata, "draw_guess_start": start_info},
                                }
                            )

                    # 3. Check if chat command requests switching game
                    if not (event.metadata.get("draw_guess_won") or event.metadata.get("draw_guess_start")):
                        target_gid, _ = self.interactive_rules.match_game_switch(event)
                        if target_gid and target_gid != self.game_livestream.active_game_id:
                            sw_info = self.game_livestream.switch_game(
                                target_gid, reason=f"{event.display_name} 弹幕点播"
                            )
                            event = event.model_copy(
                                update={
                                    "text": sw_info["spoken_text"],
                                    "metadata": {**event.metadata, "game_switch": sw_info},
                                }
                            )

                    interpreted = await self.online_songs.interpret(event)
                    if interpreted and isinstance(interpreted, tuple):
                        _, online_snapshot = interpreted
                    elif interpreted and interpreted.get("status") == "needs_confirmation":
                        db_id, fresh = self.store.add_event(
                            event, self.session_id, "needs_confirmation", interpreted["reason"]
                        )
                        return {**interpreted, "id": db_id} if fresh else {"id": db_id, "status": "duplicate"}
                    elif interpreted:
                        online_snapshot = interpreted
                    if online_snapshot:
                        event = event.model_copy(
                            update={"type": "song", "text": online_snapshot["song"]["title"], "metadata": {}}
                        )
                elif event.type == "gift":
                    # 1. Game switch via gift (if targeted game switch requested)
                    target_gid, _ = self.interactive_rules.match_game_switch(event)
                    if target_gid and target_gid != self.game_livestream.active_game_id:
                        sw_info = self.game_livestream.switch_game(
                            target_gid, reason=f"{event.display_name} 送礼点播"
                        )
                        event = event.model_copy(
                            update={
                                "text": sw_info["spoken_text"],
                                "metadata": {**event.metadata, "game_switch": sw_info},
                            }
                        )
                    else:
                        # 2. In-game bounty action via gift
                        action, label = self.interactive_rules.match_game_action(
                            event, self.game_livestream.active_game_id
                        )
                        if action and label:
                            gname, _, _ = self.interactive_rules.extract_gift_info(event)
                            act_info = self.game_livestream.handle_gift_action(
                                action, label, event.display_name, gname
                            )
                            event = event.model_copy(
                                update={
                                    "text": act_info["spoken_text"],
                                    "metadata": {**event.metadata, "game_action": act_info},
                                }
                            )
                        else:
                            # 3. Draw & Guess via gift
                            should_start, _ = self.interactive_rules.match_draw_and_guess_start(event)
                            if should_start:
                                gname, _, _ = self.interactive_rules.extract_gift_info(event)
                                start_info = self.draw_and_guess.start_round(
                                    trigger_user=event.display_name, gift_name=gname
                                )
                                event = event.model_copy(
                                    update={
                                        "text": start_info["spoken_text"],
                                        "metadata": {**event.metadata, "draw_guess_start": start_info},
                                    }
                                )
                elif event.type == "song" and "online_id" in event.metadata:
                    online_snapshot = await self.online_songs.selection(event.metadata["online_id"], event)
            except ValueError as error:
                status, reason = "filtered", str(error)
        age = (datetime.now(UTC) - event.timestamp).total_seconds()
        if session_id != self.session_id:
            return {"id": None, "status": "filtered", "reason": "处理期间直播场次已切换"}
        if self.state == "stopped":
            status, reason = "filtered", "Mock会话未启动"
        elif age > self.settings.data["event_ttl_seconds"] or age < -30:
            status, reason = "expired", "事件过期或时间在未来"
        elif event.type == "song" and status == "queued":
            try:
                if online_snapshot:
                    self.online_songs.require_enabled()
                    if online_snapshot["online"]["epoch"] != self.online_songs.epoch:
                        raise ValueError("在线点歌设置已改变，请重新点歌")
                media = online_snapshot or self.media.request(event, self.runtime.character)
                queued_songs = [self.store.job(j) for j in self.queue if self.store.job(j).get("media")]
                if len(queued_songs) >= self.media.config()["queue_limit"]:
                    raise ValueError("点歌队列已满")
                active = ([self.current_id] if self.current_id else []) + self.queue
                if any(
                    self.store.job(j).get("media")
                    and json.loads(self.store.job(j)["media"])["song"]["id"] == media["song"]["id"]
                    for j in active
                ):
                    raise ValueError("这首歌正在播放或已排队")
            except ValueError as error:
                status, reason = "filtered", str(error)
        elif event.type == "disconnect":
            status, reason = "filtered", "模拟平台断线；需人工恢复"
        elif event.type == "chat" and not clean_barrage(event.text):
            status, reason = "filtered", "空消息或重复字符"
        # Bound both acceptance rate and the tracking map; anonymous viewers are not merged by nickname.
        user_key = (
            (event.platform, event.room_id, event.platform_user_id, "song" if event.type == "song" else "interaction")
            if event.platform_user_id
            else None
        )
        cooldown = self.media.config()["cooldown_seconds"] if event.type == "song" else 1
        if (
            user_key
            and status == "queued"
            and time.monotonic() - self.accept_times.get(user_key, -10000) < cooldown
        ):
            status, reason = "filtered", "同用户短时限流"
        if original_type in {"chat", "gift"} and (event.type, event.text) != (original_type, original_text):
            event = event.model_copy(update={
                "metadata": {**event.metadata, "_live_input": {"type": original_type, "text": original_text}},
            })
        db_id, fresh = self.store.add_event(event, self.session_id, status, reason)
        if not fresh:
            return {"id": db_id, "status": "duplicate", "reason": "重复事件，不再入队"}
        if event.type == "disconnect" and self.state != "stopped":
            self.state, self.runtime.error = "degraded", reason
            self.wake.set()
        if (
            event.type == "gift" and event.metadata.get("_live_account")
            and event.metadata.get("paid_support") is True
            and self.state != "stopped" and -30 <= age <= self.settings.data["event_ttl_seconds"]
        ):
            self.observe_relationship(self.runtime.character, event.model_dump(mode="json"), support=True)
        if status != "queued":
            return {"id": db_id, "status": status, "reason": reason}
        if event.type != "idle":
            self.last_activity = time.monotonic()
        if user_key:
            self.accept_times[user_key] = time.monotonic()
            if len(self.accept_times) > 2000:
                horizon = max(60, self.media.config()["cooldown_seconds"])
                self.accept_times = {
                    k: t for k, t in self.accept_times.items() if time.monotonic() - t < horizon
                }
                while len(self.accept_times) > 2000:
                    self.accept_times.pop(next(iter(self.accept_times)))
        if len(self.queue) >= self.settings.data["queue_limit"]:
            self.store.execute("UPDATE events SET status='dropped',reason='队列已满' WHERE id=?", (db_id,))
            return {"id": db_id, "status": "dropped", "reason": "队列已满"}
        job_id, character = str(uuid4()), self.runtime.character
        self.store.execute(
            "INSERT INTO jobs(id,event_id,session_id,character_id,character_version,status,created_at,"
            "updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
            (job_id, db_id, self.session_id, character["id"], character["version"], time.time(), time.time()),
        )
        if event.type == "song":
            self.store.update_job(job_id, media=media)
        self.queue.append(job_id)
        if event.type != "idle" and self.idle_unplayed():
            await self.cancel_current("观众互动优先")
        if online_snapshot:
            self.online_songs.choices.pop(online_snapshot["online"].get("choice_token"), None)
        self.online_songs.sync()
        self.wake.set()
        return {"id": db_id, "job_id": job_id, "status": "queued"}

    def options(self):
        return LiveOptions.model_validate(self.store.get("live_options", {}))

    def set_options(self, options):
        if options.weather and self.settings.data.get("commercial_mode"):
            raise ValueError("当前免费天气来源用于非商业直播")
        self.store.set("live_options", options.model_dump())
        self.wake.set()
        return options

    def timezone(self):
        return self.options().timezone or self.runtime.browsing.policy().get("business_timezone", "Asia/Taipei")

    def room_context(self, event):
        room = (event.get("platform"), event.get("room_id"))
        event_id = event.get("event_id")
        cutoff = time.time() - self.settings.data["event_ttl_seconds"]
        return {
            "弹幕": [
                {key: item[key] for key in ("event_id", "uid", "display_name", "text")}
                for item in self.recent_barrage
                if (item["platform"], item["room_id"]) == room
                and item["event_id"] != event_id and item["at"] >= cutoff
            ][-6:],
            "活动": [
                {k: v for k, v in item.items() if k not in {"platform", "room_id", "started_at", "duration"}}
                for item in self.recent_activities
                if (item["platform"], item["room_id"]) == room
            ][-2:],
        }

    def observe_relationship(self, character, event, *, support=False):
        source = "live:" + json.dumps(
            [self.session_id, event["platform"], event["room_id"], event["event_id"]],
            separators=(",", ":"),
        )
        return self.runtime.relationships.observe_live(
            self.session_id, character["id"], event["platform"], event["platform_user_id"],
            event["display_name"], source, event["text"],
            account_id=event["metadata"].get("_live_account", ""), support=support,
        )

    async def generate_text_reply(self, generate, messages, character, event):
        options = self.options()
        if options.weather and self.settings.data.get("commercial_mode"):
            options = options.model_copy(update={"weather": None})
        provider = self.runtime.llm
        if (options.web_fetch or options.weather) and getattr(provider, "supports_operation_web_fetch", False):
            return await self.lookup.generate(provider, messages, event["event_id"], options)
        generate_live = getattr(provider, "generate_live", generate)
        return await generate_live(messages, character, event["event_id"], event["text"])

    def start_poll(self, request):
        if self.state not in {"live", "paused"}:
            raise ValueError("请先启动直播会话")
        self.expire_poll()
        activity = self.activity_filter.start_poll(request)
        self.recent_activities.append(activity)
        self.poll_timer = asyncio.get_running_loop().call_later(request.duration_seconds, self.finish_poll)
        return activity

    def finish_poll(self):
        if self.poll_timer:
            self.poll_timer.cancel()
            self.poll_timer = None
        result = self.activity_filter.end_poll()
        if result:
            self.recent_activities.append(result)
        return result

    def expire_poll(self):
        if self.activity_filter.active_poll and not self.activity_filter.is_poll_active():
            self.finish_poll()

    def reset_poll(self):
        if self.poll_timer:
            self.poll_timer.cancel()
            self.poll_timer = None
        self.activity_filter = LiveActivityFilter()

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
                    line = (
                        IDLE_LINES[self.idle_index] if self.runtime.character["id"] == "whale"
                        else "安静一会儿，你们想聊了就叫我。"
                    )
                    await self._submit(Event(type="idle", text=line), internal=True)
                    self.idle_index = (self.idle_index + 1) % len(IDLE_LINES)
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
            character = self.store.character(job["character_id"], job["character_version"])
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
                    elif meta.get("draw_guess_won"):
                        reply = Reply(
                            spoken_text=meta["draw_guess_won"]["spoken_text"],
                            emotion="cheerful",
                            animation_id="talk_happy",
                            reply_to=[event["event_id"]],
                        )
                        answer_check = None
                        self.timings["llm_ms"] = 0
                    elif meta.get("draw_guess_start"):
                        reply = Reply(
                            spoken_text=meta["draw_guess_start"]["spoken_text"],
                            emotion="cheerful",
                            animation_id="talk_happy",
                            reply_to=[event["event_id"]],
                        )
                        answer_check = None
                        self.timings["llm_ms"] = 0
                    elif meta.get("game_switch"):
                        reply = Reply(
                            spoken_text=meta["game_switch"]["spoken_text"],
                            emotion="cheerful",
                            animation_id="talk",
                            reply_to=[event["event_id"]],
                        )
                        answer_check = None
                        self.timings["llm_ms"] = 0
                    elif meta.get("game_action"):
                        reply = Reply(
                            spoken_text=meta["game_action"]["spoken_text"],
                            emotion="excited",
                            animation_id="talk_happy",
                            reply_to=[event["event_id"]],
                        )
                        answer_check = None
                        self.timings["llm_ms"] = 0
                    else:
                        started = time.perf_counter()
                        reply, answer_check = await self.runtime.generate_reply(
                            "live:" + self.session_id,
                            character,
                            event,
                            self.runtime.relationships.live_view(
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
                    parts = self.runtime.voices.live_parts(reply.spoken_text, character["voice_profile"])
                    if len(parts) == 1:
                        started = time.perf_counter()
                        audio = await self.runtime.voices.synthesize_live(
                            reply.spoken_text, character["voice_profile"]
                        )
                        self.timings["tts_ms"] = round((time.perf_counter() - started) * 1000)
                        self.store.update_job(job_id, tts_ms=self.timings["tts_ms"], error=audio.warning)
                    emotion, animation = reply.emotion, reply.animation_id
            if len(parts) > 1:
                await self._play_live_parts(job_id, parts, character, reply, answer_check)
                return
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
            await self._play(job_id, audio, character, emotion, animation)
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
                warning = (
                    "; ".join(filter(None, (audio.warning, (answer_check or {}).get("warning")))) or None
                )
                self.store.update_job(
                    job_id,
                    status="ready_to_play",
                    tts_ms=tts_ms,
                    audio_ref=audio.ref,
                    error=warning,
                )
                final_segment = index == len(parts) - 1
                pending = None if final_segment else asyncio.create_task(synthesize(parts[index + 1]))
                await self._play(
                    job_id,
                    audio,
                    character,
                    reply.emotion,
                    reply.animation_id,
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
                "character": character_card(character),
                "assets": self.settings.assets(character),
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
                "character": character_card(self.runtime.character),
                "assets": self.settings.assets(self.runtime.character),
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
            if event["type"] not in {"song", "idle"}:
                if account_id := event["metadata"].get("_live_account"):
                    event["account_id"] = account_id
                self.runtime.context.record(
                    "live:" + job["session_id"],
                    character,
                    event,
                    spoken,
                    memory_kind=check.get("memory_kind"),
                )
            if event["type"] == "chat":
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


def clean_barrage(text):
    text = text.strip()
    return text if len(set(text)) >= 2 else ""
