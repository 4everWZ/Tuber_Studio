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
                if event.type in {"chat", "gift", "song"} and event.platform != HANDOFF_PLATFORM:
                    wakeup = self.cold_director.on_viewer_activity()
                    if wakeup:
                        event = event.model_copy(
                            update={"metadata": {**event.metadata, "cold_wakeup": wakeup}}
                        )
                    if self.overtime_theater.active:
                        ot_res = None
                        if event.type == "gift":
                            gname, coin, _ = self.interactive_rules.extract_gift_info(event)
                            ot_res = self.overtime_theater.on_gift(
                                user_id=event.platform_user_id or event.display_name,
                                display_name=event.display_name,
                                gift_name=gname,
                                coins=coin,
                            )
                        elif event.type == "chat":
                            ot_res = self.overtime_theater.on_chat(
                                user_id=event.platform_user_id or event.display_name,
                                display_name=event.display_name,
                                text=event.text,
                            )
                        if ot_res and ot_res.get("extended"):
                            event = event.model_copy(
                                update={"metadata": {**event.metadata, "overtime_extended": ot_res}}
                            )

                if event.type == "chat" and event.platform != HANDOFF_PLATFORM:
                    event, rundown_early = await self._handle_rundown_command(event)
                    if rundown_early is not None:
                        return rundown_early
                    rundown_handled = any(
                        k in event.metadata
                        for k in ("rundown_vote_opened", "rundown_vote_progress", "rundown_chat_extended")
                    )

                    # 1. Check if Draw & Guess is active and evaluate guess (fast-path + LLM)
                    if not rundown_handled and self.draw_and_guess.is_active():
                        guess_res = await self.draw_and_guess.evaluate_guess_llm(
                            self.runtime.llm,
                            user_id=event.platform_user_id,
                            display_name=event.display_name,
                            text=event.text,
                            character_id=self.runtime.character["id"],
                            character_version=self.runtime.character["version"],
                        )
                        if guess_res:
                            if guess_res.get("won") or guess_res.get("status") == "won":
                                event = event.model_copy(
                                    update={"metadata": {**event.metadata, "draw_guess_won": guess_res}}
                                )
                            elif guess_res.get("near_miss"):
                                event = event.model_copy(
                                    update={"metadata": {**event.metadata, "draw_guess_near_miss": guess_res}}
                                )

                    # 2. Check if Turtle Soup is active and judge Q&A / solve
                    if not rundown_handled and self.turtle_soup.is_active() and not event.metadata.get("draw_guess_won"):
                        if self.interactive_rules.match_turtle_soup_hint(event):
                            ts_res = self.turtle_soup.give_hint()
                            event = event.model_copy(
                                update={"metadata": {**event.metadata, "turtle_soup_qa": ts_res}}
                            )
                        else:
                            ts_res = await self.turtle_soup.handle_danmaku(
                                self.runtime.llm,
                                user_id=event.platform_user_id or event.display_name,
                                display_name=event.display_name,
                                text=event.text,
                            )
                            if ts_res:
                                ts_fine = self.fine_settings()["turtle_soup"]
                                q_count = len(self.turtle_soup.qa_history)
                                auto_every = int(ts_fine.get("auto_hint_every_n_questions") or 0)
                                max_q = int(ts_fine.get("max_questions") or 0)
                                if ts_res.get("verdict") != "solved" and self.turtle_soup.is_active():
                                    if max_q > 0 and q_count >= max_q:
                                        rev = self.turtle_soup.reveal_and_stop()
                                        ts_res = {
                                            **ts_res,
                                            "verdict": "solved",
                                            "spoken_text": f"{ts_res['spoken_text']} 提问达到上限，{rev['spoken_text']}",
                                        }
                                    elif auto_every > 0 and q_count > 0 and q_count % auto_every == 0:
                                        hint_res = self.turtle_soup.give_hint()
                                        if hint_res.get("hint"):
                                            ts_res = {
                                                **ts_res,
                                                "clues_found": list(self.turtle_soup.clues_found),
                                                "spoken_text": f"{ts_res['spoken_text']} 顺便给个提示：{hint_res['hint']}",
                                            }
                                ts_key = (
                                    "turtle_soup_solved"
                                    if ts_res.get("verdict") == "solved"
                                    else "turtle_soup_qa"
                                )
                                event = event.model_copy(
                                    update={"metadata": {**event.metadata, ts_key: ts_res}}
                                )

                    # 3. Check if Roast Radio debate is active and record votes
                    if not rundown_handled and self.roast_radio.is_active() and not (
                        event.metadata.get("draw_guess_won") or event.metadata.get("turtle_soup_solved")
                    ):
                        vote = self.roast_radio.record_vote(
                            user_id=event.platform_user_id or event.display_name,
                            display_name=event.display_name,
                            text=event.text,
                        )
                        if vote:
                            if not clean_barrage(event.text):
                                return {"id": None, "status": "recorded", "reason": "辩论已计票", "vote": vote}
                            event = event.model_copy(
                                update={"metadata": {**event.metadata, "roast_radio_vote": vote}}
                            )

                    # 4. Check if chat command requests starting Draw & Guess / Turtle Soup / Roast Radio / Game switch
                    if not rundown_handled and not (
                        event.metadata.get("draw_guess_won")
                        or event.metadata.get("turtle_soup_solved")
                        or event.metadata.get("turtle_soup_qa")
                    ):
                        should_start, theme = self.interactive_rules.match_draw_and_guess_start(event)
                        if should_start:
                            start_info = await self.draw_and_guess.start_round_llm(
                                self.runtime.llm,
                                trigger_user=event.display_name,
                                theme=theme,
                            )
                            event = event.model_copy(
                                update={
                                    "metadata": {**event.metadata, "draw_guess_start": start_info},
                                }
                            )
                        else:
                            should_ts, genre = self.interactive_rules.match_turtle_soup_start(event)
                            if should_ts:
                                ts_start = await self.turtle_soup.start_round_llm(
                                    self.runtime.llm,
                                    trigger_user=event.display_name,
                                    genre=genre,
                                )
                                event = event.model_copy(
                                    update={"metadata": {**event.metadata, "turtle_soup_start": ts_start}}
                                )
                            else:
                                should_debate, debate_args = self.interactive_rules.match_roast_radio_debate(event)
                                if should_debate and debate_args:
                                    debate_info = self.roast_radio.start_debate(
                                        topic=debate_args["topic"],
                                        side_a=debate_args.get("side_a", "正方"),
                                        side_b=debate_args.get("side_b", "反方"),
                                        trigger_user=event.display_name,
                                    )
                                    event = event.model_copy(
                                        update={"metadata": {**event.metadata, "roast_radio_debate": debate_info}}
                                    )
                                else:
                                    should_roast, roast_topic = self.interactive_rules.match_roast_radio_roast(event)
                                    if should_roast:
                                        roast_info = await self.roast_radio.generate_roast_segment(
                                            self.runtime.llm,
                                            self.store,
                                            topic=roast_topic,
                                        )
                                        event = event.model_copy(
                                            update={"metadata": {**event.metadata, "roast_radio_roast": roast_info}}
                                        )

                    # 5. Check if chat command requests switching game
                    if not rundown_handled and not (
                        event.metadata.get("draw_guess_won")
                        or event.metadata.get("draw_guess_start")
                        or event.metadata.get("turtle_soup_start")
                        or event.metadata.get("roast_radio_debate")
                        or event.metadata.get("roast_radio_roast")
                    ):
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

                    interpreted = None if rundown_handled else await self.online_songs.interpret(event)
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
                            # 3. Draw & Guess / Turtle Soup / Roast Radio via gift
                            gname, _, _ = self.interactive_rules.extract_gift_info(event)
                            should_start, theme = self.interactive_rules.match_draw_and_guess_start(event)
                            if should_start:
                                start_info = await self.draw_and_guess.start_round_llm(
                                    self.runtime.llm,
                                    trigger_user=event.display_name,
                                    gift_name=gname,
                                    theme=theme,
                                )
                                event = event.model_copy(
                                    update={
                                        "metadata": {**event.metadata, "draw_guess_start": start_info},
                                    }
                                )
                            else:
                                should_ts, genre = self.interactive_rules.match_turtle_soup_start(event)
                                if should_ts:
                                    ts_start = await self.turtle_soup.start_round_llm(
                                        self.runtime.llm,
                                        trigger_user=event.display_name,
                                        genre=genre,
                                    )
                                    event = event.model_copy(
                                        update={"metadata": {**event.metadata, "turtle_soup_start": ts_start}}
                                    )
                                else:
                                    should_debate, debate_args = self.interactive_rules.match_roast_radio_debate(event)
                                    if should_debate and debate_args:
                                        debate_info = self.roast_radio.start_debate(
                                            topic=debate_args["topic"],
                                            side_a=debate_args.get("side_a", "正方"),
                                            side_b=debate_args.get("side_b", "反方"),
                                            trigger_user=event.display_name,
                                        )
                                        event = event.model_copy(
                                            update={"metadata": {**event.metadata, "roast_radio_debate": debate_info}}
                                        )
                                    else:
                                        should_roast, roast_topic = self.interactive_rules.match_roast_radio_roast(event)
                                        if should_roast:
                                            roast_info = await self.roast_radio.generate_roast_segment(
                                                self.runtime.llm,
                                                self.store,
                                                topic=roast_topic,
                                            )
                                            event = event.model_copy(
                                                update={"metadata": {**event.metadata, "roast_radio_roast": roast_info}}
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

    async def _handle_rundown_command(self, event):
        cmd = (event.text or "").strip()
        plan = self.rundown.plan()
        if cmd in {"#投票选游戏", "#换游戏"} or cmd.startswith("#投票选游戏 ") or cmd.startswith("#换游戏 "):
            if not plan.chat_entry.allow_chat_vote_trigger:
                return event, None
            if self.activity_filter.is_poll_active():
                return event, {"id": None, "status": "recorded", "reason": "游戏池投票正在进行中"}
            uid = event.platform_user_id or event.display_name
            self.rundown._vote_trigger_voters.add(uid)
            threshold = plan.chat_entry.vote_trigger_threshold
            if len(self.rundown._vote_trigger_voters) >= threshold:
                vote_res = await self.rundown.open_game_vote()
                updated = event.model_copy(
                    update={
                        "metadata": {
                            **event.metadata,
                            "rundown_vote_opened": {
                                "spoken_text": f"收到！{event.display_name}发起选游戏投票，弹幕快扣数字或游戏名投票～",
                                "poll": vote_res["poll"],
                            },
                        }
                    }
                )
                return updated, None
            count = len(self.rundown._vote_trigger_voters)
            updated = event.model_copy(
                update={
                    "metadata": {
                        **event.metadata,
                        "rundown_vote_progress": {
                            "spoken_text": f"{event.display_name}想投票换游戏（{count}/{threshold}），再有观众响应就开投！",
                            "count": count,
                            "threshold": threshold,
                        },
                    }
                }
            )
            return updated, None
        if cmd == "#继续聊" or cmd.startswith("#继续聊 "):
            if not plan.chat_entry.allow_chat_extend or not self.rundown.active:
                return event, None
            ext_mins = plan.chat_entry.extend_chat_minutes
            if time.time() - self.rundown._last_chat_extend_at >= 60.0:
                ext = await self.rundown.extend_chat(minutes=ext_mins)
                if ext.get("scene_changed"):
                    await self.send_overlay({"type": "scene", "scene": self.media.scene()})
                spoken = f"好啦好啦，看在{event.display_name}这么想聊的份上，再继续闲聊 {ext_mins} 分钟！"
            else:
                spoken = "刚刚已经加过聊天时间啦，咱们先把当前这段聊完～"
            updated = event.model_copy(
                update={"metadata": {**event.metadata, "rundown_chat_extended": {"spoken_text": spoken}}}
            )
            return updated, None
        return event, None

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
