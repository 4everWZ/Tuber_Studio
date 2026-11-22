"""Barrage ingestion, interactive filters, polls and text reply generation."""

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from ..activity_filter import ActivitySummary, LiveActivityFilter
from .state import HANDOFF_PLATFORM, LIVE_OUTCOMES


class LiveSessionBarrageMixin:
    async def submit(self, event, *, online_snapshot=None, verified_account_id=None):
        if event.platform == HANDOFF_PLATFORM:
            raise ValueError("直播交接须从号主对话发起")
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

    async def _submit(self, event, *, online_snapshot=None, verified_account_id=None, internal=False, handoff_account=None):
        session_id = self.session_id
        now = datetime.now(UTC)
        age = (now - event.timestamp).total_seconds()
        status, reason = "queued", None
        if event.type == "idle" and not internal:
            raise ValueError("冷场任务只由直播调度器生成")
        metadata = dict(event.metadata)
        metadata.pop("_live_account", None)
        metadata.pop("_live_input", None)
        metadata.pop("_handoff_account", None)
        for key in LIVE_OUTCOMES:
            metadata.pop(key, None)
        if event.platform == HANDOFF_PLATFORM and internal:
            metadata["_handoff_account"] = handoff_account
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
            filtered = None if event.platform == HANDOFF_PLATFORM else self.activity_filter.check_event(event)
            if filtered:
                status, reason = filtered
                return {"id": None, "status": status, "reason": reason}
            if event.type == "chat" and event.platform != HANDOFF_PLATFORM and (text := clean_barrage(event.text)):
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
                if event.type == "chat" and event.platform != HANDOFF_PLATFORM:
                    # 1. Check if Draw & Guess is active and user guessed correctly
                    if self.draw_and_guess.is_active():
                        matched_win = self.draw_and_guess.check_guess(
                            user_id=event.platform_user_id,
                            display_name=event.display_name,
                            text=event.text,
                            character_id=self.runtime.character["id"],
                            character_version=self.runtime.character["version"],
                        )
                        if matched_win:
                            event = event.model_copy(
                                update={
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


def clean_barrage(text):
    text = text.strip()
    return text if len(set(text)) >= 2 else ""
