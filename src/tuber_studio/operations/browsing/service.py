"""Coordinate browsing rounds, evidence preparation and scheduled work."""

import asyncio
import json
import re
import time
from uuid import uuid4

from ...direct_messages import MessagePolicy
from ...providers import public_error
from ...store import dump
from . import batches as batches_tasks
from . import comment_reader as comment_reader_tasks
from . import discovery as discovery_tasks
from . import progress as progress_tasks
from . import task_views as task_views_tasks
from .decisions import BrowsingDecisions
from .discovery import BackgroundDiscovery
from .evidence import BrowsingEvidence, EvidenceUnavailable
from .models import (
    BrowsingPolicy,
    browsing_filter_reason,
)
from .notifications import NotificationInbox
from .publication import BrowsingPublication
from .round_context import read_round
from .thread_context import comment_thread_context


class BrowsingService:
    def __init__(
        self, settings, store, runtime, comments, messages, dynamics, *, visual=None, read_interval=0.25
    ):
        self.settings, self.store, self.runtime = settings, store, runtime
        self.comments, self.messages, self.dynamics = comments, messages, dynamics
        self.operations, self.repo = runtime.operations, runtime.browsing_repository
        self.adapter = comments.adapter
        self.read_interval = read_interval
        self.read_lock = asyncio.Lock()
        self.last_read = 0
        self.evidence = BrowsingEvidence(runtime, self.adapter, self._read, visual)
        self.notifications = NotificationInbox(store, self._read, self.adapter)
        self.active = {}
        self.comment_active = {}
        self.comment_windows = {}
        self.loop = self.poller = None
        self.changed = asyncio.Event()
        self.interactions_changed = asyncio.Event()
        self.closed = False
        self.started_at = time.time()
        self.heartbeats = set()
        self.blockers = ["尚未核验当前账号及模型"]
        self.account = None
        self.last_error = None
        self.ready_signature = None
        self.sync_lock = asyncio.Lock()
        self.discovery = BackgroundDiscovery(self)
        self.decisions = BrowsingDecisions(self)
        self.publication = BrowsingPublication(self)
        self.drain_task = None
        self.abort_task = None
        self.control_generation = 0
        self.status_sequence = 0
        if hasattr(self.adapter, "request_gate"):
            self.adapter.request_gate = self._gate

    @property
    def visual_provider(self):
        return self.evidence.visual

    def replace_visual_provider(self, provider):
        previous = self.visual_provider
        self.evidence.visual = provider
        return previous

    def has_intent(self, comment, character):
        return self.publication.has_intent(comment, character)

    def review(self, identity, action, text=None):
        return self.publication.review(identity, action, text)

    async def publish(self, identity, *, automatic=False):
        return await self.publication.publish(identity, automatic=automatic)

    async def resume_continuation(self, identity, continuation_id):
        return await self.publication.resume_continuation(identity, continuation_id)

    def policy(self):
        return BrowsingPolicy.model_validate(self.store.get("browsing_policy", {})).model_dump()

    def configured(self):
        messages = MessagePolicy.model_validate(self.store.get("message_policy", {})).model_dump()
        base = self.policy() | {
            "message_poll_enabled": messages["enabled"] and messages["auto_poll"],
            "message_reply_enabled": messages["enabled"] and messages["auto_send"],
            "comment_handoff_enabled": messages["enabled"] and messages["allow_comment_handoff"],
        }
        review = self.store.get("browsing_review_session", {})
        character = self.runtime.character
        if review.get("state") in {"starting", "running"} and review.get("account_id") == (self.account or self.operations.policy()["account_id"]) and (
            review.get("character_id"), review.get("character_version")
        ) == (character["id"], character["version"]):
            base["review_session_id"] = review["id"]
        return base

    def effective(self):
        return self.operations.effective("browsing", self.configured())

    def permissions(self):
        config = self.configured()
        messages = MessagePolicy.model_validate(self.store.get("message_policy", {}))
        dynamic = self.store.get("dynamic_policy", {})
        def mode(enabled, automatic):
            return "auto" if enabled and automatic else "review" if enabled else "off"
        return {
            "video_enabled": config["discovery_enabled"],
            "top_level": mode(config["top_level_enabled"], config["top_level_auto_publish"]),
            "replies": mode(config["reply_enabled"], config["reply_auto_publish"]),
            "comment_like": mode(config["comment_like_enabled"], config["comment_like_auto_publish"]),
            "private_messages": mode(messages.enabled, messages.auto_send),
            "private_poll": messages.enabled and messages.auto_poll,
            "mentions": config["mentions_enabled"],
            "reply_notifications": config["replies_enabled"],
            "video_like": mode(config["video_like_enabled"], config["video_like_auto_publish"]),
            "video_fav": mode(config["video_fav_enabled"], config["video_fav_auto_publish"]),
            "dynamics": mode(dynamic.get("enabled", False), dynamic.get("auto_publish", True)),
        }

    def has_interactions(self, config=None):
        config = config or self.effective()
        return any(config.get(key) for key in (
            "reply_enabled", "comment_like_enabled", "mentions_enabled", "replies_enabled",
        ))

    def model_signature(self):
        llm, character = self.settings.data["llm"], self.runtime.character
        return dump(
            [
                llm.get("kind"),
                llm.get("model"),
                llm.get("base_url"),
                llm.get("enable_thinking"),
                character["id"],
                character["version"],
            ]
        )

    def control_state(self, control):
        if control["emergency"]:
            return "emergency"
        if self.closed:
            return "stopping"
        if self.operations.draining:
            return "draining"
        if self.operations.starting:
            return "starting"
        if self.blockers and (control["enabled"] or self.last_error):
            return "blocked"
        return "running" if control["enabled"] else "off"

    def status(self):
        control = self.operations.policy()
        rounds = [self.repo.round(i) for i in self.active]
        self.status_sequence += 1
        return {
            "status_sequence": self.status_sequence,
            "started_at": self.started_at,
            "policy": control,
            "settings": self.policy(),
            "permissions": self.permissions(),
            "review_session": self.store.get("browsing_review_session", {}),
            "state": self.control_state(control),
            "account_id": self.account or control["account_id"],
            "character": {k: self.runtime.character[k] for k in ("id", "version")},
            "base": {
                "comments": self.comments.policy()["base"],
                "messages": MessagePolicy.model_validate(self.store.get("message_policy", {})).model_dump(),
                "dynamics": self.store.get("dynamic_policy", {}),
                "browsing": self.policy(),
            },
            "effective": {
                "comments": self.comments.policy(),
                "messages": self.messages.policy(),
                "dynamics": self.dynamics.policy(),
                "browsing": self.effective(),
            },
            "blockers": self.blockers,
            "last_error": self.last_error,
            "vision": self.evidence.visual.capabilities(),
            "active_round": rounds[0] if rounds else None,
            "active_rounds": rounds,
            "discovery": self.discovery.status(),
            "metrics": {
                "cache_hits": self.evidence.hits,
                "merged_reads": self.evidence.merged,
                "notifications": self.notifications.last,
            },
            "daily_watch": self.repo.quotas.watch_status(
                self.account or control["account_id"],
                self.runtime.character["id"],
                self.policy().get("daily_watch_limit", 0),
                self.policy().get("business_timezone", "Asia/Taipei"),
            ),
            "automatic_sending_ready": self.comments.policy()["credentials_configured"] and not self.blockers,
            "semantic_validation": self.store.get("browsing_semantic_validation", {}),
            "sending_note": "按设置中已开放的动作与审核方式运行",
            "cache": self.runtime.cache.usage("browsing"),
        }

    async def configure(self, value):
        data = dict(value)
        data.pop("comment_handoff_enabled", None)
        data.pop("handoff_auto_send", None)
        current = self.policy()
        merged = current | data
        config = BrowsingPolicy.model_validate(merged).model_dump()
        # Saved channel permissions remain independent from the runtime switch.
        self.store.set("browsing_policy", config)
        self.operations.configure(
            {k: config[k] for k in ("max_per_hour", "max_per_day", "recipient_cooldown_seconds")}
        )
        self.changed.set()
        self.interactions_changed.set()
        return self.status()

    async def _current_account(self, expected=None):
        if self.adapter is not self.comments.adapter:
            visual = self.visual_provider
            await self.evidence.close()
            self.adapter = self.comments.adapter
            self.notifications.adapter = self.adapter
            self.evidence = BrowsingEvidence(self.runtime, self.adapter, self._read, visual)
            if hasattr(self.adapter, "request_gate"):
                self.adapter.request_gate = self._gate
        self.account = str(await self._read(self.comments.account_id))
        if expected is not None and str(expected) != self.account:
            raise ValueError("候选来源账号已变化")
        return self.account

    async def check_browsing_model(self):
        health = await self.runtime.llm.health()
        if self.settings.data["llm"]["kind"] == "demo" or health.get("ok") is False:
            raise ValueError("请连接实际运营模型；离线演示不支持自主决策")

    async def probe_discovery(self, config):
        source = config["sources"][0] if config["sources"] else "recommend"
        if source == "recommend" and config["keywords"]:
            source = "search"
        if source == "search" and not config["keywords"]:
            source = "recommend"
        if source == "specified":
            if not config["watch_videos"]:
                raise ValueError("指定视频模式未填写视频")
            return
        await self._read(
            self.adapter.discover,
            source,
            config["keywords"][0] if config["keywords"] else "",
            1,
            1,
        )

    async def preflight(self, *, for_automation=False):
        self.blockers = []
        try:
            await self._current_account()
        except (ValueError, AttributeError) as error:
            self.account = None
            self.blockers.append("账号身份核验失败：" + public_error(error))
        try:
            await self.check_browsing_model()
        except Exception as error:
            self.blockers.append("模型不可用：" + public_error(error))
        config = self.configured()
        video = config["discovery_enabled"] or not for_automation
        required = set()
        if video:
            required.update(("discover", "video_metadata", "subtitles"))
            if config["top_level_enabled"] and config["top_level_auto_publish"]:
                required.add("publish_top_level")
        if config["reply_enabled"] or config["comment_like_enabled"]:
            required.update(("read_public_thread", "fetch"))
            if config["reply_auto_publish"]:
                required.add("publish_resource_reply")
            if config["comment_like_enabled"]:
                required.add("like_comment")
        if config["mentions_enabled"] or config["replies_enabled"]:
            required.update(("notifications", "read_public_thread"))
        self.blockers.extend(
            "平台缺少能力：" + name
            for name in sorted(required)
            if not callable(getattr(self.adapter, name, None))
        )
        if video and self.account and not self.blockers:
            try:
                await self.probe_discovery(config)
            except ValueError as error:
                self.blockers.append("发现来源不可用：" + public_error(error))
        if self.account:
            for kind, enabled in (
                ("mention", config["mentions_enabled"]),
                ("reply", config["replies_enabled"]),
            ):
                if enabled:
                    try:
                        page = await self._read(self.adapter.notifications, kind, None)
                        if str(page["account_id"]) != self.account:
                            raise ValueError("通知账号与当前账号不同")
                    except ValueError as error:
                        self.blockers.append(
                            ("提及" if kind == "mention" else "收到回复")
                            + "读取不可用："
                            + public_error(error)
                        )
            if config["message_poll_enabled"]:
                try:
                    if not self.messages.injected and not self.comments.policy()["credentials_configured"]:
                        raise ValueError("私信读取需要当前账号登录凭证")
                    adapter = await self.messages._adapter()
                    own_uid, _ = await adapter.sessions()
                    if str(own_uid) != self.account:
                        raise ValueError("私信账号身份与公开互动不一致")
                except ValueError as error:
                    self.blockers.append("私信读取不可用：" + public_error(error))
        self.ready_signature = self.credential_signature() if not self.blockers else None
        return self.status()

    def credential_signature(self):
        return (
            id(self.comments.adapter),
            self.comments.credentials.get("sessdata"),
            self.comments.credentials.get("csrf"),
        )

    async def start_review_session(self, *, resume_session_id=None):
        """Start fresh review batches without discarding older paused progress."""
        if self.operations.policy()["enabled"] or self.active or self.operations.starting:
            raise ValueError("请先停止当前运营，再开始新的review验收")
        if "auto" in self.permissions().values():
            raise ValueError("请先将已启用渠道设为审核模式，再开始review验收")
        account = await self._current_account()
        if self.operations.policy()["enabled"] or self.active or self.operations.starting:
            raise ValueError("自主运营正在启动，请等待当前启动完成")
        if "auto" in self.permissions().values():
            raise ValueError("请先将已启用渠道设为审核模式，再开始review验收")
        character = self.runtime.character
        previous = self.store.get("browsing_review_session", {})
        if resume_session_id is not None and (
            not isinstance(resume_session_id, str) or previous.get("id") != resume_session_id
            or previous.get("account_id") != account or previous.get("character_id") != character["id"]
            or previous.get("character_version") != character["version"]
        ):
            raise ValueError("review会话与当前账号或角色不匹配，未恢复")
        session = {
            "id": str(uuid4()), "account_id": account,
            "character_id": character["id"], "character_version": character["version"],
            "started_at": time.time(), "state": "starting",
        }
        if resume_session_id is not None:
            session = {key: value for key, value in previous.items() if key not in {"ended_at", "paused_at"}}
            session.update(state="starting", resumed_at=time.time())
        self.store.set("browsing_review_session", session)
        try:
            await self.set_enabled(True)
            current = self.store.get("browsing_review_session", {})
            if current.get("state") != "starting" or not self.operations.policy()["enabled"]:
                raise ValueError("review验收启动已取消")
            self.store.set("browsing_review_session", current | {"state": "running"})
            return self.status()
        except BaseException:
            await self.stop()
            self.store.set("browsing_review_session", previous)
            raise

    async def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled须为布尔值")
        if not enabled:
            return await self.stop()
        if self.operations.policy()["emergency"]:
            raise ValueError("紧急停止仍有效")
        if self.operations.draining:
            raise ValueError("当前批次正在收尾")
        if self.operations.starting:
            raise ValueError("自主运营正在启动")
        if self.operations.policy()["enabled"]:
            return self.status()
        self.operations.starting = True
        self.last_error = None
        self.control_generation += 1
        generation = self.control_generation
        try:
            await self.preflight(for_automation=True)
            if generation != self.control_generation:
                raise ValueError("自主运营启动已取消")
            if self.blockers:
                raise ValueError("；".join(self.blockers))
            self.operations.configure(
                {"enabled": True}, account_id=self.account, character=self.runtime.character
            )
            self.discovery.remember_account(self.account)
            await self.messages.resume_pending()
            if generation != self.control_generation:
                raise ValueError("自主运营启动已取消")
        except (Exception, asyncio.CancelledError) as error:
            if generation == self.control_generation:
                self.operations.configure({"enabled": False})
                self.last_error = public_error(error)
                if not self.blockers:
                    self.blockers = [self.last_error]
            raise
        finally:
            self.operations.starting = False
        self.comments.policy_changed.set()
        self.messages.changed.set()
        self.dynamics.changed.set()
        self.changed.set()
        self.interactions_changed.set()
        return self.status()

    async def stop(self, *, emergency=False):
        if emergency or self.closed:
            await self._abort_operations(emergency=emergency)
        elif not self.operations.draining:
            self.control_generation += 1
            if self.operations.policy()["enabled"] and not self.operations.starting:
                self.operations.draining = True
                admitted = tuple(task for identity, task in self.active.items() if not self.repo.round(identity)["preview"])
                admitted += self.operations.admitted_work
                if admitted:
                    self.drain_task = asyncio.create_task(self._drain(admitted))
                else:
                    self._finish_stop()
            else:
                # The same endpoint still cancels a stand-alone read-only preview.
                await self._abort_operations()
        return self.status()

    def _finish_stop(self):
        self.operations.configure({"enabled": False})
        review = self.store.get("browsing_review_session", {})
        if review and review.get("state") in {None, "starting", "running"}:
            self.store.set("browsing_review_session", review | {"state": "stopped", "ended_at": time.time()})
        self.operations.draining = False
        self.last_error = None
        self.notifications.pause(self.account)
        if self.account:
            self.store.set("browsing_discovery_pending:" + self.account, False)

    async def _drain(self, admitted):
        try:
            await asyncio.gather(*(asyncio.shield(task) for task in admitted), return_exceptions=True)
            self._finish_stop()
        finally:
            self.drain_task = None

    async def _abort_operations(self, *, emergency=False):
        self.control_generation += 1
        self.operations.draining = True
        self.operations.configure({"enabled": False, **({"emergency": True} if emergency else {})})
        if self.abort_task is None:
            self.abort_task = asyncio.create_task(self._cancel_active())
        await asyncio.shield(self.abort_task)

    async def _cancel_active(self):
        try:
            if self.drain_task:
                self.drain_task.cancel()
                await asyncio.gather(self.drain_task, return_exceptions=True)
                self.drain_task = None
            children = list(self.active.values())
            for child in children:
                child.cancel()
            await asyncio.gather(self.messages.pause(), self.dynamics.pause(), *children, return_exceptions=True)
            for event in (
                self.comments.policy_changed,
                self.messages.changed,
                self.dynamics.changed,
                self.changed,
                self.interactions_changed,
            ):
                event.set()
            self._finish_stop()
        finally:
            self.abort_task = None

    async def _gate(self):
        identity = read_round.get()
        if identity:
            self.repo.reserve_budget(identity, reads=1)
        async with self.read_lock:
            await asyncio.sleep(max(0, self.last_read + self.read_interval - time.monotonic()))
            self.last_read = time.monotonic()

    async def _read(self, method, *args, **kwargs):
        identity = read_round.get()
        config = self.repo.round(identity)["config"] if identity else self.policy()
        for attempt in range(config["read_retries"] + 1):
            try:
                if not hasattr(self.adapter, "request_gate"):
                    await self._gate()
                return await method(*args, **kwargs)
            except ValueError as error:
                if getattr(error, "kind", None) != "network" or attempt >= config["read_retries"]:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))

    async def _prepare_evidence(
        self,
        video,
        config,
        *,
        force=False,
        task=None,
        deadline=None,
        visual_source_ids=(),
    ):
        return await progress_tasks.prepare_evidence(
            self,
            video,
            config,
            force=force,
            task=task,
            deadline=deadline,
            visual_source_ids=visual_source_ids,
        )

    async def run(
        self,
        *,
        automatic=False,
        preview=None,
        notifications_only=False,
        video=None,
        interaction_only=False,
        video_only=False,
        comment_visit=False,
        keywords=None,
        request_id=None,
    ):
        if self.closed:
            raise ValueError("浏览服务已停止")
        if automatic and self.operations.policy()["emergency"]:
            raise ValueError("紧急停止仍有效")
        preview = not automatic if preview is None else preview
        if not automatic and not preview:
            raise ValueError("手工运行仅允许预览")
        selection = {"video": video, "keywords": keywords}
        if request_id:
            if not preview or automatic or not video_only:
                raise ValueError("对话任务只支持单个只读视频轮次")
            if existing := self.repo.requested_preview(
                request_id, self.runtime.character, selection, self.comments.display_account_id()
            ):
                return {"id": existing["id"], "status": existing["status"], "reused": True, "preview": True}
        if automatic and not self.operations.accepting_work:
            raise ValueError("请先启动自主运营")
        if interaction_only:
            if not automatic or not self.operations.policy()["enabled"] or not self.account:
                raise ValueError("互动轮次尚未授权")
            config = self.effective() | {"interaction_only": True}
        else:
            if not automatic and self.discovery.round_id:
                raise ValueError("自动刷视频本轮进行中，请稍后再预览")
            if self.ready_signature != self.credential_signature() or not self.account:
                if self.active:
                    if self.discovery.enabled:
                        raise ValueError("自动刷视频本轮进行中，请稍后再预览")
                    await self._abort_operations()
                await self.preflight()
            if not automatic and self.discovery.round_id:
                raise ValueError("自动刷视频本轮进行中，请稍后再预览")
            if self.blockers:
                raise ValueError("；".join(self.blockers))
            config = self.configured() if preview else self.effective()
        if video_only:
            config = config | {"video_only": True}
        if request_id:
            config = config | {"operator_selection": selection}
        if automatic:
            # One fetched batch is the unit of work. Do not interrupt its agents
            # with a second, hidden shared request/token allowance.
            config = config | dict.fromkeys(
                ("max_reads", "max_model_calls", "max_input_tokens", "max_output_tokens"), 0
            )
            if not notifications_only and not interaction_only:
                config["autonomous_batch"] = True
        if video:
            config = config | {"sources": ["specified"], "watch_videos": [video], "force_read": True}
        elif keywords:
            config = config | {"sources": ["search"], "keywords": keywords}
        if comment_visit:
            config = config | {"comment_visit": True}
        if (
            automatic
            and not notifications_only
            and not interaction_only
            and time.time()
            < self.store.get("browsing_next:" + self.account + ":" + self.runtime.character["id"], 0)
        ):
            return {"status": "deferred", "reason": "OC有界延后计划尚未到期"}
        if automatic and not self.operations.accepting_work:
            raise ValueError("自主运营已停止接纳新轮次")
        row, created = self.repo.create_round(
            self.account, self.runtime.character, config, self.operations.policy()["epoch"], preview,
            request_id=request_id,
        )
        if automatic and not notifications_only and not interaction_only:
            self.store.set("browsing_discovery_pending:" + self.account, True)
        if created:
            task = asyncio.create_task(self._execute(row, notifications_only))
            self.active[row["id"]] = task
            task.add_done_callback(lambda done: self.active.pop(row["id"], None))
        return {
            "id": row["id"],
            "status": row["status"],
            "reused": not created,
            "preview": bool(row["preview"]),
        }

    def _check_round(self, row):
        current = self.runtime.character
        if current["id"] != row["character_id"] or current["version"] != row["character_version"]:
            raise ValueError("本轮角色版本已变化")
        if str(row["account_id"]) != str(self.account):
            raise ValueError("本轮账号身份已变化")
        if self.credential_signature() != self.ready_signature:
            raise ValueError("本轮账号凭证已变化")
        if not row["preview"]:
            self.operations.assert_authorized(
                automatic=True,
                channel="comments",
                account_id=row["account_id"],
                character=current,
                epoch=row["epoch"]
                if (
                    row["config"].get("interaction_only")
                    or row["config"].get("video_only")
                    or self.operations.policy()["enabled"]
                )
                else None,
            )

    def discovery_tasks(self):
        return tuple(
            task
            for identity, task in self.active.items()
            if not self.repo.round(identity)["config"].get("interaction_only")
        )

    async def _execute(self, row, notifications_only):
        return await batches_tasks.execute(self, row, notifications_only)

    async def _sample_resources(self, metrics):
        return await batches_tasks.sample_resources(self, metrics)

    async def _discover(self, row):
        return await discovery_tasks.discover(self, row)

    async def _discover_candidates(self, row, retries):
        return await discovery_tasks.discover_candidates(self, row, retries)

    async def _select_batch_candidates(self, row, retries, candidates):
        return await discovery_tasks.select_batch_candidates(self, row, retries, candidates)

    async def _plan_batch(self, row, candidates):
        return await discovery_tasks.plan_batch(self, row, candidates)

    def _candidate_view(self, row, item):
        return discovery_tasks.candidate_view(self, row, item)

    async def video_comment_context(self, row, bvid):
        return await comment_reader_tasks.video_comment_context(self, row, bvid)

    def commit_comment_context(self, context):
        return comment_reader_tasks.commit_comment_context(self, context)

    async def _resume_batch_publications(self, row, bvid):
        return await batches_tasks.resume_batch_publications(self, row, bvid)

    async def _process_discovery_video(self, row, candidate, metrics, semaphore):
        return await batches_tasks.process_discovery_video(self, row, candidate, metrics, semaphore)

    async def defer_comment_visit(self, row, bvid, metrics):
        return await batches_tasks.defer_comment_visit(self, row, bvid, metrics)

    async def _run_discovery_video(self, row, candidate, metrics):
        return await batches_tasks.run_discovery_video(self, row, candidate, metrics)

    @staticmethod
    def filter_reason(item, config):
        return browsing_filter_reason(item, config)

    async def _prepare_task(self, identity, row, semaphore, metrics):
        return await progress_tasks.prepare_task(self, identity, row, semaphore, metrics)

    async def _prepare_attempt(self, identity, row, semaphore, metrics, owner):
        return await progress_tasks.prepare_attempt(self, identity, row, semaphore, metrics, owner)

    async def _heartbeat(self, identity, owner, generation):
        return await progress_tasks.heartbeat(self, identity, owner, generation)

    async def comment_evidence(self, comment, character):
        await self._current_account()
        if comment["platform"] != "bilibili":
            raise ValueError("自动视频评论需要已核验平台来源")
        key, material = await self._prepare_evidence(comment["video_id"], self.effective())
        original = await self._read(self.adapter.fetch, comment["video_id"], 1)
        source = next(
            (item for item in original["items"] if str(item["comment_id"]) == str(comment["comment_id"])),
            None,
        )
        if (
            not source
            or source["content"] != comment["content"]
            or str(source.get("platform_user_id", "")) != str(comment["platform_user_id"])
        ):
            raise ValueError("当前页未核验到真实来源评论与作者")
        return self.evidence_payload([key], self._segments(material))

    def evidence_payload(self, keys, segments):
        materials = [self.evidence.load(key) for key in keys]
        return {
            "account_id": self.account,
            "material_keys": keys,
            "source_versions": [m["source_version"] for m in materials],
            "metadata": [m["metadata"] for m in materials],
            "segments": segments,
            "coverage": [m["coverage"] for m in materials],
        }

    def assert_comment_evidence(self, event):
        evidence = event.get("video_evidence")
        if not evidence or evidence["account_id"] != self.comments.display_account_id():
            raise ValueError("自动评论缺少当前账号的视频证据")
        thread = evidence.get("public_source_thread") or {}
        if not evidence.get("material_keys") and not (thread.get("public_text") or (thread.get("source") or {}).get("text")):
            raise ValueError("视频证据为空")
        for key, version in zip(evidence["material_keys"], evidence["source_versions"], strict=True):
            mat = self.evidence.load(key)
            if not self.evidence.matches_source_version(mat, version):
                raise ValueError("视频来源版本已失效")

    async def validate_private_source(self, draft):
        value = json.loads(draft.get("video_evidence") or "null")
        if not value and draft["source_kind"] == "message":
            return
        if not value or str(value.get("account_id")) != str(draft["account_id"]):
            raise ValueError("私信缺少当前账号核验过的来源")
        if value.get("origin_round_id"):
            parent = self.repo.round(value["origin_round_id"])
            if not parent or parent["status"] not in {"running", "completed", "partial"}:
                raise ValueError("移交父轮已停止，保留私信草稿")
        if draft["source_kind"] == "comment":
            original = value.get("public_source_thread")
            if not original:
                raise ValueError("私信缺少核验过的原评论")
            source, target = original["source"], original["target"]
            if event := value.get("source_event"):
                fresh = await self._read(self.adapter.read_public_thread, event)
                if fresh.get("public_text") != original.get("public_text"):
                    raise ValueError("公开移交原文已变化或撤销")
            elif hasattr(self.adapter, "read_video_thread"):
                fresh = await self._read(self.adapter.read_video_thread, target)
            else:
                fresh = {"source": await self._read(self.adapter.read_video_reply, target)}
            current = fresh["source"]
            if current["text"] != source["text"] or str(current["author_uid"]) != str(source["author_uid"]):
                raise ValueError("公开移交原文已变化或撤销")
            items = fresh.get("items")
            if items is None:
                return original
            item = next((item for item in items if str(item["comment_id"]) == str(source["comment_id"])), None)
            if item is None:
                raise ValueError("当前楼未找到原评论")
            grouped = {str(item["comment_id"]) for item in original.get("grouped_comments") or []}
            current_thread = comment_thread_context(
                item | {"grouped_comments": [item for item in items if str(item["comment_id"]) in grouped]},
                items, target,
            )
            return original | current_thread | {"grouped_comments": current_thread.get("grouped_comments", [])}
        keys = value.get("material_keys", [])
        refresh = False
        for key in keys:
            try:
                self.evidence.load(key)
            except EvidenceUnavailable:
                refresh = True
        if refresh:
            new_keys = await self._refresh_material_keys(keys, value["source_versions"])
            materials = [self.evidence.load(key) for key in new_keys]
            value.update(
                material_keys=new_keys,
                metadata=[m["metadata"] for m in materials],
                segments=[s for m in materials for s in self._segments(m)],
            )
            draft["video_evidence"] = dump(value)
            self.store.execute("UPDATE dm_drafts SET video_evidence=? WHERE id=?", (dump(value), draft["id"]))
        self.assert_comment_evidence({"video_evidence": value})

    async def _refresh_material_keys(self, keys, versions):
        result = []
        for key, version in zip(keys, versions, strict=True):
            try:
                material = self.evidence.load(key)
            except EvidenceUnavailable:
                source = self.store.one("SELECT bvid FROM browsing_materials WHERE id=?", (key,))
                if not source:
                    raise ValueError("原始来源标识已移除，不能恢复旧证据") from None
                key, material = await self._prepare_evidence(source["bvid"], self.effective(), force=True)
            else:
                old_meta = material["metadata"]
                fresh_meta = await self._read(self.adapter.video_metadata, old_meta["bvid"])
                if str(fresh_meta.get("aid")) != str(old_meta.get("aid")) or fresh_meta.get(
                    "description"
                ) != old_meta.get("description"):
                    raise ValueError("视频来源材料已变化，不能使用旧候选")
            if not self.evidence.matches_source_version(material, version):
                raise ValueError("视频来源材料已变化，不能使用旧候选")
            result.append(key)
        return result

    async def linked_message_evidence(self, draft):
        source = self.store.one("SELECT * FROM dm_inbox WHERE id=?", (draft["source_id"],))
        if not source or str(source.get("account_id", "")) != str(draft.get("account_id", "")):
            return None
        videos = list(dict.fromkeys(re.findall(r"\bBV1[0-9A-Za-z]{9}\b", source["content"])))[:2]
        if not videos and re.search("视频|刚才|继续|那个实验", source["content"]):
            previous = self.store.one(
                "SELECT video_evidence FROM dm_drafts WHERE account_id=? AND platform_user_id=? AND character_id=? AND id<? AND video_evidence IS NOT NULL ORDER BY id DESC LIMIT 1",
                (draft["account_id"], draft["platform_user_id"], draft["character_id"], draft["id"]),
            )
            if previous:
                old = json.loads(previous["video_evidence"])
                videos = [m["bvid"] for m in old.get("metadata", [])][:2]
        if not videos:
            return None
        await self._current_account(draft["account_id"])
        keys, segments = [], []
        for video in videos:
            key, material = await self._prepare_evidence(video, self.effective())
            keys.append(key)
            segments.extend(self._segments(material))
        return self.evidence_payload(keys, segments)

    def _segments(self, material):
        return [dict(s, cid=part["cid"]) for part in material["parts"] for s in part["segments"]]

    async def sync_notifications(self, row=None):
        return await self.notifications.sync(
            row["account_id"] if row else self.account,
            row["config"] if row else self.configured(),
            self.operations.policy()["enabled_at"],
            self.started_at,
        )

    async def _run_interactions(self, row, metrics):
        await comment_reader_tasks.run_interactions(self, row, metrics)
        await batches_tasks.process_comment_visits(self, row, metrics)
        await self._process_events(row, metrics)

    async def _collect_own_comments(self, row):
        return await comment_reader_tasks.collect_own_comments(self, row)

    async def _process_events(self, row, metrics, *, own_only=False):
        return await comment_reader_tasks.process_events(self, row, metrics, own_only=own_only)

    async def _process_event_batch(self, row, metrics, own_only):
        return await comment_reader_tasks.process_event_batch(self, row, metrics, own_only)

    async def _event(self, event_row, row, metrics):
        return await comment_reader_tasks.event(self, event_row, row, metrics)

    async def _event_unlocked(self, event_row, row, metrics, event, thread):
        return await comment_reader_tasks.event_unlocked(self, event_row, row, metrics, event, thread)

    def tasks(self, status=None, *, round_id=None, task_id=None):
        return task_views_tasks.tasks(self, status, round_id=round_id, task_id=task_id)

    def rounds(self):
        return task_views_tasks.rounds(self)

    def intents(self):
        return task_views_tasks.intents(self)

    def public_comment_intent(self, identity):
        return task_views_tasks.public_comment_intent(self, identity)

    def inbox(self):
        return task_views_tasks.inbox(self)

    async def reread(self, task_id):
        task = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("视频任务不存在")
        return await self.run(
            preview=True, video=task["bvid"],
            comment_visit=json.loads(task["payload"]).get("task_kind") == "comment_visit",
        )

    async def open(self):
        if self.loop is not None and not self.loop.done():
            return
        self.started_at = time.time()
        self.repo.recover()
        # Force cold start standby: autonomous operations never start unprompted on boot
        policy = self.store.get("operations_policy", {})
        if policy.get("enabled"):
            policy["enabled"] = False
            self.store.set("operations_policy", policy)
        for record in self.store.all("SELECT key FROM kv WHERE key LIKE 'browsing_discovery_pending:%'"):
            self.store.set(record["key"], False)
        self.loop = asyncio.create_task(self._loop())
        self.poller = asyncio.create_task(self._notifications_loop())

    async def close(self):
        self.closed = True
        for task in (self.loop, self.poller):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.loop, self.poller) if t), return_exceptions=True)
        await self.stop()
        await self.evidence.close()

    async def _loop(self):
        while True:
            self.changed.clear()
            try:
                if (
                    self.operations.accepting_work
                    and self.effective()["discovery_enabled"]
                    and not self.discovery_tasks()
                ):
                    await self.run(automatic=True, video_only=True)
            except Exception as error:
                self.last_error = public_error(error)
            try:
                await asyncio.wait_for(self.changed.wait(), self.policy()["interval_seconds"])
            except TimeoutError:
                pass

    async def _notifications_loop(self):
        while True:
            self.interactions_changed.clear()
            try:
                if self.operations.accepting_work and self.has_interactions():
                    await self.run(automatic=True, interaction_only=True)
            except Exception as error:
                self.last_error = public_error(error)
            try:
                await asyncio.wait_for(
                    self.interactions_changed.wait(), self.policy()["notification_interval_seconds"]
                )
            except TimeoutError:
                pass
