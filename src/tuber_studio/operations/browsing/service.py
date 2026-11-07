"""Coordinate browsing rounds, evidence preparation and scheduled work."""

import asyncio
import inspect
import json
import re
import time
from contextlib import nullcontext
from uuid import uuid4

import psutil

from ...comments import DEFAULT_POLICY, category
from ...direct_messages import MessagePolicy
from ...generation_meter import BrowsingBudgetDeferred
from ...operation_feedback import (
    continuation_view,
    generation_view,
)
from ...operation_turns import operation_lane
from ...operations_control import canonical_intent
from ...providers import public_error
from ...store import dump
from .decisions import BrowsingDecisions
from .discovery import BackgroundDiscovery
from .evidence import BrowsingEvidence, EvidenceUnavailable
from .models import (
    BrowsePlan,
    BrowsingPolicy,
    CandidateBatchPlan,
    browsing_filter_reason,
)
from .notifications import NotificationInbox
from .publication import BrowsingPublication
from .round_context import agent_depth, discovery_retry, read_round


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
        self.overlay_starting = False
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

    def effective(self):
        messages = MessagePolicy.model_validate(self.store.get("message_policy", {})).model_dump()
        base = self.policy() | {
            "message_poll_enabled": messages["enabled"] and messages["auto_poll"],
            "message_reply_enabled": messages["enabled"] and messages["auto_send"],
            "comment_handoff_enabled": messages["enabled"] and messages["allow_comment_handoff"],
        }
        return self.operations.effective("browsing", base)

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

    def semantic_ready(self):
        proof = self.store.get("browsing_semantic_validation", {})
        return proof.get("passed") is True and proof.get("signature") == self.model_signature()

    def status(self):
        control = self.operations.policy()
        rounds = [self.repo.round(i) for i in self.active]
        return {
            "policy": control,
            "settings": self.policy(),
            "state": "stopping"
            if self.closed
            else "blocked"
            if control["enabled"] and self.blockers
            else "running"
            if rounds
            else "paused"
            if control["emergency"]
            else "off"
            if not control["enabled"]
            else "running",
            "account_id": self.account or control["account_id"],
            "character": {k: self.runtime.character[k] for k in ("id", "version")},
            "base": {
                "comments": DEFAULT_POLICY | self.store.get("comment_policy", {}),
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
            "automatic_sending_ready": self.semantic_ready(),
            "sending_note": "新授权候选可直接发送"
            if self.semantic_ready()
            else "模型语义验收未通过，保留待审核草稿",
            "cache": self.runtime.cache.usage("browsing"),
        }

    async def configure(self, value):
        data = dict(value)
        data.pop("comment_handoff_enabled", None)
        data.pop("handoff_auto_send", None)
        current = self.policy()
        merged = current | data
        config = BrowsingPolicy.model_validate(merged).model_dump()
        # Changing base fields never snapshots/replaces the overlay's previous values.
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
        required = (
            "discover",
            "video_metadata",
            "subtitles",
            "notifications",
            "read_public_thread",
            "publish_top_level",
            "publish_resource_reply",
        )
        self.blockers.extend(
            "平台缺少能力：" + name for name in required if not callable(getattr(self.adapter, name, None))
        )
        config = self.effective()
        if for_automation:
            config = config | {
                "mentions_enabled": True,
                "replies_enabled": True,
                "message_poll_enabled": True,
            }
        # Probe the selected default route; don't silently substitute popular videos.
        if self.account and not self.blockers:
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

    async def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled须为布尔值")
        if enabled:
            if self.overlay_starting:
                raise ValueError("全渠道自主运营正在启动")
            self.overlay_starting = True
            try:
                await self.preflight(for_automation=True)
            finally:
                self.overlay_starting = False
            if self.blockers:
                raise ValueError("；".join(self.blockers))
            self.operations.configure(
                {"enabled": True}, account_id=self.account, character=self.runtime.character
            )
            b_policy = self.store.get("browsing_policy", {})
            b_policy["discovery_enabled"] = True
            self.store.set("browsing_policy", b_policy)
        else:
            self.operations.configure({"enabled": False})
            b_policy = self.store.get("browsing_policy", {})
            b_policy["discovery_enabled"] = False
            self.store.set("browsing_policy", b_policy)
            await self.stop(keep_discovery=True)
        self.comments.policy_changed.set()
        self.messages.changed.set()
        self.changed.set()
        self.interactions_changed.set()
        return self.status()

    async def stop(self, *, emergency=False, keep_discovery=False):
        if emergency:
            self.operations.configure({"emergency": True})
            self.comments.policy_changed.set()
            self.messages.changed.set()
            self.dynamics.changed.set()
            await self.discovery.stop()
        children = [
            task
            for identity, task in self.active.items()
            if not (keep_discovery and self.repo.round(identity)["config"].get("discovery_id"))
        ]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        self.notifications.discard_pending(self.account)
        if self.account:
            self.store.set("browsing_discovery_pending:" + self.account, False)
        return self.status()

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
        require_visual=False,
        visual_source_ids=(),
    ):
        """Keep visual generation and token probes inside their parent round."""
        identity = read_round.get()
        if identity:
            task = dict(task or {})
            if task.get("round_id", identity) != identity:
                raise ValueError("视觉任务不能跨越父轮预算")
            task.setdefault("id", str(uuid4()))
            task["round_id"] = identity
            row = self.repo.round(identity)
            if not row:
                raise ValueError("视觉任务的父轮已不存在")
            deadline = min(deadline or row["deadline"], row["deadline"])
        return await self.evidence.prepare(
            video,
            config,
            force=force,
            task=task,
            deadline=deadline,
            require_visual=require_visual,
            visual_source_ids=visual_source_ids,
        )

    async def run(
        self,
        *,
        automatic=False,
        preview=None,
        notifications_only=False,
        video=None,
        discovery_id=None,
        interaction_only=False,
        video_only=False,
    ):
        if self.closed:
            raise ValueError("浏览服务已停止")
        if automatic and self.operations.policy()["emergency"]:
            raise ValueError("紧急停止仍有效")
        preview = not automatic if preview is None else preview
        if not automatic and not preview and not discovery_id:
            raise ValueError("手工运行仅允许预览")
        if discovery_id:
            self.discovery.verify(discovery_id)
            preview = False
            config = self.policy() | {
                "discovery_id": discovery_id,
                "discovery_publish": True,
                "top_level_enabled": True,
                "reply_enabled": True,
                "video_like_enabled": False,
                "video_fav_enabled": False,
                "read_retries": 0,
                "max_takeovers": 0,
            }
            config["top_level_limit"] = config["inspect_limit"]
        elif interaction_only:
            if not automatic or not self.operations.policy()["enabled"] or not self.account:
                raise ValueError("互动轮次尚未授权")
            config = self.effective() | {"interaction_only": True}
        else:
            if self.discovery.round_id:
                raise ValueError("自动刷视频本轮进行中，请稍后再预览")
            if self.ready_signature != self.credential_signature() or not self.account:
                if self.active:
                    if self.discovery.enabled:
                        raise ValueError("自动刷视频本轮进行中，请稍后再预览")
                    await self.stop()
                await self.preflight()
            if self.discovery.round_id:
                raise ValueError("自动刷视频本轮进行中，请稍后再预览")
            if self.blockers:
                raise ValueError("；".join(self.blockers))
            config = self.effective()
        if video_only:
            config = config | {"video_only": True}
        if discovery_id or automatic:
            # One fetched batch is the unit of work. Do not interrupt its agents
            # with a second, hidden shared request/token allowance.
            config = config | dict.fromkeys(
                ("max_reads", "max_model_calls", "max_input_tokens", "max_output_tokens"), 0
            )
            if not notifications_only and not interaction_only:
                config["autonomous_batch"] = True
        if video:
            config = config | {"sources": ["specified"], "watch_videos": [video], "force_read": True}
        elif automatic and "sources" not in self.store.get("browsing_policy", {}):
            legacy = self.comments.policy().get("base", {})
            if legacy.get("auto_browse") and legacy.get("watch_videos"):
                config = config | {"sources": ["specified"], "watch_videos": legacy["watch_videos"][:20]}
        if (
            automatic
            and not notifications_only
            and not interaction_only
            and time.time()
            < self.store.get("browsing_next:" + self.account + ":" + self.runtime.character["id"], 0)
        ):
            return {"status": "deferred", "reason": "OC有界延后计划尚未到期"}
        row, created = self.repo.create_round(
            self.account, self.runtime.character, config, self.operations.policy()["epoch"], preview
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
        if discovery_id := row["config"].get("discovery_id"):
            self.discovery.verify(discovery_id)
            return
        if self.credential_signature() != self.ready_signature:
            raise ValueError("本轮账号凭证已变化")
        if not row["preview"]:
            self.operations.assert_allowed(
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
        identity, started = row["id"], time.monotonic()
        token = read_round.set(identity)
        lane = operation_lane.set("video")
        metrics = {
            "workers": row["config"]["workers"],
            "prepared": 0,
            "failures": 0,
            "viewed": 0,
            "lock_wait_seconds": 0,
            "preparation_seconds": 0,
            "peak_rss_bytes": psutil.Process().memory_info().rss,
            "cache_hits_before": self.evidence.hits,
            "merged_before": self.evidence.merged,
        }
        self.store.execute("UPDATE browsing_rounds SET status='running' WHERE id=?", (identity,))
        round_error = None
        sampler = asyncio.create_task(self._sample_resources(metrics))
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + max(0, row["deadline"] - time.time())
            ):
                self._check_round(row)
                if row["config"].get("interaction_only"):
                    await self._run_interactions(row, metrics)
                    status = "partial" if metrics["failures"] else "completed"
                    return
                discovery_only = bool(row["config"].get("discovery_id"))
                poll_interactions = not (discovery_only or row["config"].get("video_only")) and (
                    not row["preview"] or notifications_only or not self.operations.policy()["enabled"]
                )
                if poll_interactions:
                    await self.sync_notifications(row)
                    await self._process_events(row, metrics)
                if not notifications_only or self.store.get(
                    "browsing_discovery_pending:" + row["account_id"], False
                ):
                    self.store.set("browsing_discovery_pending:" + row["account_id"], False)
                    candidates = await self._discover(row)
                    metrics["workers"] = row["config"].get("batch_workers", row["config"]["workers"])
                    semaphore = asyncio.Semaphore(
                        row["config"].get("batch_workers", row["config"]["workers"])
                    )
                    if discovery_only:
                        async with asyncio.TaskGroup() as group:
                            for candidate in candidates:
                                group.create_task(
                                    self._process_discovery_video(row, candidate, metrics, semaphore)
                                )
                        status = "partial" if metrics["failures"] else "completed"
                        return
                    tasks = [self.repo.task(identity, c) for c in candidates]
                    preparation_start = time.monotonic()
                    async with asyncio.TaskGroup() as group:
                        children = [
                            group.create_task(self._prepare_task(i, row, semaphore, metrics)) for i in tasks
                        ]
                    prepared = [child.result() for child in children]
                    metrics["preparation_seconds"] = time.monotonic() - preparation_start
                    for result in prepared:
                        if poll_interactions:
                            await self._process_events(row, metrics)
                        if result:
                            await self.decisions._evaluate_task(result, row, metrics)
                    if poll_interactions:
                        await self._process_events(row, metrics)
                status = "partial" if metrics["failures"] else "completed"
        except (asyncio.CancelledError, Exception) as error:
            stopping_discovery = self.discovery.state == "stopping" and self.discovery.round_id == identity
            if isinstance(error, asyncio.CancelledError) or stopping_discovery:
                # A candidate may see revoked authorization before TaskGroup delivers cancellation.
                status, task_error = "cancelled", "父轮取消"
            else:
                status = "partial" if metrics["viewed"] or metrics["prepared"] else "failed"
                self.last_error = round_error = task_error = public_error(error)
            self.store.execute(
                "UPDATE browsing_tasks SET status='cancelled',error=? WHERE round_id=? AND status IN ('discovered','claimed','preparing','evidence_ready')",
                (task_error, identity),
            )
        finally:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
            heartbeats = [task for task in self.heartbeats if task.get_name() == identity]
            for heartbeat in heartbeats:
                heartbeat.cancel()
            await asyncio.gather(*heartbeats, return_exceptions=True)
            self.heartbeats.difference_update(heartbeats)
            metrics["elapsed_seconds"] = time.monotonic() - started
            metrics["cache_hits"] = self.evidence.hits - metrics.pop("cache_hits_before")
            metrics["merged_reads"] = self.evidence.merged - metrics.pop("merged_before")
            metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
            metrics.update(self.repo.round(identity)["metrics"])
            self.repo.finish_round(identity, status, metrics, round_error)
            operation_lane.reset(lane)
            read_round.reset(token)

    async def _sample_resources(self, metrics):
        while True:
            metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
            used = psutil.virtual_memory().used
            metrics["peak_system_ram_bytes"] = max(metrics.get("peak_system_ram_bytes", 0), used)
            await asyncio.sleep(0.1)

    async def _discover(self, row):
        config = row["config"]
        retries = self.repo.retry_candidates(row) if config.get("discovery_publish") else []
        if len(retries) >= config["inspect_limit"]:
            return await self._plan_batch(row, retries)
        try:
            return await self._discover_candidates(row, retries)
        except Exception as error:
            if not retries:
                raise
            metrics = self.repo.round(row["id"])["metrics"] | {"source_error": public_error(error)}
            self.store.execute("UPDATE browsing_rounds SET metrics=? WHERE id=?", (dump(metrics), row["id"]))
            return retries

    async def _discover_candidates(self, row, retries):
        config = row["config"]
        sources = list(config["sources"]) or ["recommend"]
        keywords = list(dict.fromkeys(config["keywords"]))[: config["max_search_terms"]]
        plan = None
        if config["autonomous_topics"] and not keywords:
            feedback = []
            for task in self.store.all(
                "SELECT t.payload,t.material_key FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE r.character_id=? AND t.status IN ('selected','viewed_without_comment') ORDER BY t.updated_at DESC LIMIT 6",
                (row["character_id"],),
            ):
                try:
                    raw = self.evidence.load(task["material_key"])
                    feedback.append(
                        {
                            "title": raw["metadata"]["title"],
                            "decisions": json.loads(task["payload"]).get("decisions", []),
                        }
                    )
                except EvidenceUnavailable:
                    continue
            plan = await self.decisions._model(
                row,
                BrowsePlan,
                {"public_interests": self.runtime.character["core"], "public_browsing_feedback": feedback},
                "只提出有界浏览计划。空词默认recommend；search必须明确返回真实关键词；可以wait。不得使用私人记忆。",
            )
            self.store.execute(
                "UPDATE browsing_rounds SET metrics=? WHERE id=?",
                (dump({"plan": plan.model_dump()}), row["id"]),
            )
            if plan.action == "wait":
                self.store.set(
                    "browsing_next:" + row["account_id"] + ":" + row["character_id"],
                    time.time() + plan.delay_seconds,
                )
                return retries
            sources, keywords = [plan.action], plan.keywords
            if plan.action == "search" and (
                not keywords or any(not k.strip() or len(k) > 100 for k in keywords)
            ):
                raise ValueError("OC搜索计划无有效关键词")
        elif keywords and sources == ["recommend"]:
            sources = ["search"]
        merged = {}
        for source in sources:
            if source == "specified":
                for video in config["watch_videos"]:
                    merged.setdefault(video, {"bvid": video, "sources": []})["sources"].append(
                        {"source": "specified"}
                    )
                continue
            if source == "search" and not keywords:
                source = "recommend"
            for keyword in keywords if source == "search" else [""]:
                for page in range(1, config["max_pages"] + 1):
                    result = await self._read(
                        self.adapter.discover, source, keyword, page, config["candidate_limit"]
                    )
                    for item in result["items"]:
                        record = merged.setdefault(item["bvid"], dict(item, sources=[]))
                        record["sources"].append(
                            {
                                "source": source,
                                "keyword": keyword,
                                "page": page,
                                "plan_reason": plan.reason if plan else "user_configuration",
                            }
                        )
                    if len(merged) >= config["candidate_limit"] or not result.get("has_more", False):
                        break
        candidates = list(merged.values())[: config["candidate_limit"]]
        cursor_key = "browsing_rotation:" + row["account_id"] + ":" + row["character_id"]
        offset = self.store.get(cursor_key, 0) % max(1, len(candidates))
        candidates = candidates[offset:] + candidates[:offset]
        self.store.set(cursor_key, offset + config["inspect_limit"])
        selected = retries[: config["inspect_limit"]]
        selected_ids = {item["bvid"] for item in selected}
        for item in candidates:
            if row["preview"] and len(selected) >= config["inspect_limit"]:
                break
            if item["bvid"] in selected_ids or (
                config.get("discovery_publish") and self.repo.video_retry(row, item["bvid"])
            ):
                continue
            reason = self.filter_reason(item, config)
            if reason:
                prior_filter = self.store.one(
                    "SELECT t.id FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id "
                    "WHERE r.account_id=? AND r.character_id=? AND t.bvid=? AND t.status='skipped' AND t.error=? LIMIT 1",
                    (row["account_id"], row["character_id"], item["bvid"], reason),
                )
                if prior_filter:
                    continue
                identity = self.repo.task(row["id"], item)
                self.store.execute(
                    "UPDATE browsing_tasks SET status='skipped',error=? WHERE id=?", (reason, identity)
                )
                continue
            previous = self.store.one(
                "SELECT t.id FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE t.bvid=? AND r.character_id=? AND r.character_version=? AND (t.status IN ('selected','viewed_without_comment') OR (t.status='checkpoint' AND r.account_id=? AND json_extract(t.payload,'$.capacity_signature')=?)) AND t.updated_at>?",
                (
                    item["bvid"],
                    row["character_id"],
                    row["character_version"],
                    row["account_id"],
                    self.decisions._capacity_signature(config),
                    time.time() - 21600,
                ),
            )
            if previous and not config.get("force_read") and not config.get("reply_enabled"):
                continue
            selected.append(item)
            selected_ids.add(item["bvid"])
            if row["preview"] and len(selected) >= config["inspect_limit"]:
                break
        return await self._plan_batch(row, selected)

    async def _plan_batch(self, row, candidates):
        if row["preview"] or not candidates:
            return candidates[: row["config"]["inspect_limit"]]
        config = row["config"]
        plan = await self.decisions._model(
            row,
            CandidateBatchPlan,
            {
                "candidates": [
                    {key: item.get(key) for key in ("bvid", "title", "owner", "duration", "sources")}
                    | {
                        "already_commented": bool(
                            self.store.one(
                                "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                                (
                                    row["account_id"],
                                    row["character_id"],
                                    canonical_intent(
                                        "bilibili", item["bvid"], "top-level", row["character_id"]
                                    ),
                                ),
                            )
                        )
                    }
                    for item in candidates
                ],
                "max_videos": config["inspect_limit"],
                "max_workers": config["workers"],
            },
            "这一批视频已按BV号去重。按你的兴趣决定想看哪些和先后顺序，返回对应BV号；"
            "可以少选或不选。workers是你想同时交给视频子Agent处理的数量，不超过max_workers。"
            "already_commented表示已有视频主回复，不再重复发主回复，但可以继续翻评论、回复新留言。"
            "子Agent会读取真实字幕和画面，再自行决定发言、回复评论或略过；这一批完成后再看下一批。",
            max_tokens=min(config["decision_max_tokens"], 1024),
        )
        config["batch_workers"] = plan.workers
        metrics = self.repo.round(row["id"])["metrics"] | {"batch_plan": plan.model_dump()}
        self.store.execute(
            "UPDATE browsing_rounds SET config=?,metrics=? WHERE id=?",
            (dump(config), dump(metrics), row["id"]),
        )
        by_id = {item["bvid"]: item for item in candidates}
        return [by_id[bvid] for bvid in plan.videos]

    async def video_comment_context(self, row, bvid):
        """Read new arrivals and advance through older pages independently."""
        fetch = self.adapter.fetch
        parameters = inspect.signature(fetch).parameters
        if row["preview"] or "start_page" not in parameters:
            return await self._read(fetch, bvid, 1)
        cursor_key = f"browsing_comment_page:{row['account_id']}:{row['character_id']}:{bvid}"
        page = max(1, int(self.store.get(cursor_key, 1)))
        newest = await self._read(fetch, bvid, 1, newest=True) if "newest" in parameters else None
        context = await self._read(fetch, bvid, 1, start_page=page)
        items, seen = [], set()
        for source in (newest, context):
            for item in (source or {}).get("items", []):
                identity = str(item.get("comment_id", ""))
                if identity and identity not in seen:
                    items.append(item)
                    seen.add(identity)
        return context | {
            "items": items,
            "_scroll_progress": {"key": cursor_key, "next_page": context.get("next_page") or 1},
        }

    def commit_comment_context(self, context):
        if progress := (context or {}).get("_scroll_progress"):
            self.store.set(progress["key"], progress["next_page"])

    async def _send_discovery_intent(self, identity, row):
        self.discovery.verify(row["config"]["discovery_id"])
        with self.discovery.performing("publishing"):
            result = await self.publish(identity, automatic=True)
        state = self.store.one("SELECT status FROM browsing_intents WHERE id=?", (identity,))["status"]
        if state == "automatic_pending":
            raise ValueError("平台拒绝，已准备修订" if result["status"] == "revised" else "回应修订尚未完成")

    async def _resume_discovery_writes(self, row, bvid):
        for item in self.store.all(
            """SELECT i.id,i.status FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id
               WHERE i.account_id=? AND i.character_id=? AND i.character_version=?
               AND json_extract(i.target,'$.bvid')=? AND json_extract(r.config,'$.discovery_publish')=1
               AND (i.status='publish_uncertain' OR (i.status='automatic_pending' AND
                   json_extract(r.config,'$.discovery_id')=?)) ORDER BY i.created_at""",
            (*self.repo.retry_scope(row), bvid, row["config"]["discovery_id"]),
        ):
            if item["status"] == "publish_uncertain":
                self.discovery.verify(row["config"]["discovery_id"])
                with self.discovery.performing("reconciling"):
                    result = await self.publication._reconcile(item["id"])
                if result is None:
                    raise ValueError("原提交回执尚待确认")
            else:
                await self._send_discovery_intent(item["id"], row)

    async def _process_discovery_video(self, row, candidate, metrics, semaphore):
        async with semaphore:
            await self._run_discovery_video(row, candidate, metrics)

    async def _run_discovery_video(self, row, candidate, metrics):
        bvid = candidate["bvid"]
        saved = self.repo.video_retry(row, bvid)
        failures = saved["failures"] if saved else 0
        error = saved["error"] if saved else None
        while failures < 3:
            self._check_round(row)
            identity = self.repo.task(row["id"], candidate)
            token = discovery_retry.set(
                {"bvid": bvid, "failures": failures, "error": error, "resume": bool(saved or failures)}
            )
            try:
                try:
                    if failures:
                        await self._resume_discovery_writes(row, bvid)
                except BrowsingBudgetDeferred as failure:
                    self.store.execute(
                        "UPDATE browsing_tasks SET status='checkpoint',error=? WHERE id=?",
                        (public_error(failure), identity),
                    )
                    self.repo.defer_video(row, bvid, identity, failures)
                    return
                except Exception as failure:
                    metrics["failures"] += 1
                    self.store.execute(
                        "UPDATE browsing_tasks SET status='failed',error=? WHERE id=?",
                        (public_error(failure), identity),
                    )
                else:
                    prepared = await self._prepare_task(identity, row, nullcontext(), metrics)
                    if prepared:
                        await self.decisions._evaluate_task(prepared, row, metrics)
            finally:
                discovery_retry.reset(token)
            self._check_round(row)
            task = self.store.one("SELECT status,error,payload FROM browsing_tasks WHERE id=?", (identity,))
            payload = json.loads(task["payload"])
            reason = payload.get("reason")
            if payload.get("retry_kind") == "round_budget":
                self.repo.defer_video(row, bvid, identity, failures)
                return
            if task["status"] == "checkpoint":
                self.repo.finish_video_attempt(row, candidate, identity, 0)
                return
            if task["status"] in {"selected", "viewed_without_comment"} or (
                task["status"] == "skipped"
                and reason in {"no_subtitles", "empty", "filtered", "visual_unavailable"}
            ):
                self.repo.finish_video_attempt(row, candidate, identity, 0)
                return
            failures += 1
            error = task["error"]
            self.repo.finish_video_attempt(row, candidate, identity, failures)
            if failures >= 2:
                if failures == 3:
                    self.store.execute(
                        """UPDATE browsing_intents SET status='automatic_skipped',error=?
                           WHERE account_id=? AND character_id=? AND character_version=? AND status='automatic_pending'
                           AND json_extract(target,'$.bvid')=? AND round_id IN
                           (SELECT id FROM browsing_rounds WHERE json_extract(config,'$.discovery_id')=?)""",
                        (error, *self.repo.retry_scope(row), bvid, row["config"]["discovery_id"]),
                    )
                return

    @staticmethod
    def filter_reason(item, config):
        return browsing_filter_reason(item, config)

    async def _prepare_task(self, identity, row, semaphore, metrics):
        current_depth = agent_depth.get()
        if current_depth >= 1:
            raise PermissionError("Subagent nesting prohibited; depth must not exceed 1")
        token = agent_depth.set(current_depth + 1)
        try:
            for attempt in range(row["config"]["max_takeovers"] + 1):
                owner = str(uuid4())
                try:
                    return await self._prepare_attempt(identity, row, semaphore, metrics, owner)
                except Exception as error:
                    task = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (identity,))
                    if (
                        not task
                        or task["owner"] != owner
                        or self.repo.round(row["id"])["status"] not in {"queued", "running"}
                    ):
                        metrics["late_results_rejected"] = metrics.get("late_results_rejected", 0) + 1
                        return None
                    if attempt < row["config"]["max_takeovers"]:
                        self.store.execute(
                            "UPDATE browsing_tasks SET lease_until=0 WHERE id=? AND owner=? AND generation=?",
                            (identity, owner, task["generation"]),
                        )
                        metrics["takeovers"] = metrics.get("takeovers", 0) + 1
                        continue
                    metrics["failures"] += 1
                    self.repo.complete(
                        identity,
                        owner,
                        task["generation"],
                        "failed",
                        json.loads(task["payload"]) | {"reason": "worker_failed"},
                        error=public_error(error),
                    )
                    return None
        finally:
            agent_depth.reset(token)

    async def _prepare_attempt(self, identity, row, semaphore, metrics, owner):
        async with semaphore:
            generation = self.repo.claim(identity, owner)
            heartbeat = asyncio.create_task(self._heartbeat(identity, owner, generation), name=row["id"])
            self.heartbeats.add(heartbeat)
            heartbeat.add_done_callback(self.heartbeats.discard)
            ready = False
            try:
                item = json.loads(
                    self.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
                )
                progress = self.repo.content_progress(row, item["bvid"])
                if progress and not row["config"].get("force_read"):
                    material_key, material = await self.evidence._store(progress["material"])
                    self.evidence.hits += 1
                else:
                    material_key, material = await self._prepare_evidence(
                        item["bvid"],
                        row["config"],
                        task={"id": identity},
                        deadline=row["deadline"],
                        force=row["config"].get("force_read", False),
                    )
                # Requirement: 无字幕的视频，暂时标记不刷视频，TBD等待后续接入ASR
                has_subtitles = any(
                    isinstance(p, dict) and p.get("segments") for p in material.get("parts", [])
                )
                if not has_subtitles:
                    raise EvidenceUnavailable(
                        "no_subtitles", "视频无可用字幕，暂时跳过（待后续接入ASR语音识别）"
                    )
                reason = self.filter_reason(material["metadata"], row["config"])
                if reason:
                    raise EvidenceUnavailable("filtered", reason)
                if not progress or row["config"].get("force_read"):
                    self.repo.save_content_progress(row, material, [])
                self.repo.complete(
                    identity,
                    owner,
                    generation,
                    "evidence_ready",
                    item
                    | {
                        "metadata": self.decisions._metadata_summary(material["metadata"]),
                        "coverage": material["coverage"],
                        "missing_parts": material["missing_parts"],
                    },
                    material_key,
                )
                metrics["prepared"] += 1
                ready = True
                return {
                    "id": identity,
                    "owner": owner,
                    "generation": generation,
                    "material_key": material_key,
                    "material": material,
                    "heartbeat": heartbeat,
                }
            except ValueError as error:
                kind = getattr(error, "kind", "")
                deferred = isinstance(error, BrowsingBudgetDeferred)
                metrics["failures"] += int(
                    kind not in {"no_subtitles", "empty", "filtered", "visual_unavailable"}
                )
                self.repo.complete(
                    identity,
                    owner,
                    generation,
                    "checkpoint" if deferred else "skipped" if kind else "failed",
                    item
                    | {"reason": kind or "preparation_failed"}
                    | ({"retry_kind": "round_budget"} if deferred else {}),
                    error=public_error(error),
                )
                return None
            finally:
                if not ready:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, identity, owner, generation):
        while True:
            await asyncio.sleep(5)
            self.repo.heartbeat(identity, owner, generation)

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
        if not evidence.get("material_keys") and not evidence.get("public_source_thread", {}).get(
            "public_text"
        ):
            raise ValueError("视频证据为空")
        for key, version in zip(evidence["material_keys"], evidence["source_versions"], strict=True):
            mat = self.evidence.load(key)
            if not self.evidence.matches_source_version(mat, version):
                raise ValueError("视频来源版本已失效")

    async def validate_private_source(self, draft):
        await self._current_account(draft.get("account_id") or None)
        value = json.loads(draft.get("video_evidence") or "null")
        if not value:
            # Direct messages without a video association retain their private
            # conversation context and never inherit the current browsing video.
            if draft["source_kind"] == "message":
                return
            source = self.store.one("SELECT * FROM comments WHERE id=?", (draft["source_id"],))
            if not source:
                raise ValueError("移交的公开来源已清理")
            value = await self.comment_evidence(source, self.runtime.character)
        if value.get("origin_round_id"):
            parent = self.repo.round(value["origin_round_id"])
            if not parent or parent["status"] not in {"running", "completed", "partial"}:
                raise ValueError("移交父轮已停止，保留私信草稿")
        event = value.get("source_event")
        if event:
            fresh = await self._read(self.adapter.read_public_thread, event)
            original = value["public_source_thread"]
            if fresh["source"] != original["source"] or fresh.get("public_text") != original.get(
                "public_text"
            ):
                raise ValueError("公开移交原文已变化或撤销")
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
            row["config"] if row else self.effective(),
            self.operations.policy()["enabled_at"],
            self.started_at,
        )

    async def _run_interactions(self, row, metrics):
        if hasattr(self.adapter, "own_uploads") and row["config"]["reply_enabled"]:
            try:
                await self._collect_own_comments(row)
            except (ValueError, TimeoutError) as error:
                metrics["own_upload_error"] = public_error(error)
                metrics["failures"] += 1
        await self._process_events(row, metrics, own_only=True)
        try:
            await self.sync_notifications(row)
        except (ValueError, TimeoutError) as error:
            metrics["notification_error"] = public_error(error)
            metrics["failures"] += 1
        await self._process_events(row, metrics)

    async def _collect_own_comments(self, row):
        account = row["account_id"]
        cutoff = max(int(self.started_at), int(self.operations.policy()["enabled_at"] or 0))
        for video in await self._read(self.adapter.own_uploads, account):
            result = await self._read(lambda bvid: self.adapter.fetch(bvid, newest=True), video["bvid"])
            self._check_round(row)
            for comment in result["items"]:
                timestamp = int(comment["created_at"])
                author = str(comment["platform_user_id"])
                if timestamp < cutoff or author == account or category(comment["content"]) == "spam":
                    continue
                identity = f"own:{account}:{video['aid']}:{comment['comment_id']}"
                event = {
                    "event_id": identity,
                    "kind": "own_comment",
                    "account_id": account,
                    "sender_uid": author,
                    "sender_name": comment["display_name"],
                    "resource_type": "video",
                    "oid": video["aid"],
                    "bvid": video["bvid"],
                    "root": comment["thread_id"],
                    "parent": comment["comment_id"],
                    "comment_id": comment["comment_id"],
                    "timestamp": timestamp,
                    "source_text": comment["content"],
                }
                self.store.execute(
                    "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,'received',?,NULL,?)",
                    (
                        identity,
                        account,
                        "own_comment",
                        str(comment["comment_id"]),
                        timestamp,
                        dump(event),
                        time.time(),
                    ),
                )

    async def _process_events(self, row, metrics, *, own_only=False):
        async with self.sync_lock:
            lane = operation_lane.set("reply")
            try:
                await self._process_event_batch(row, metrics, own_only)
            finally:
                operation_lane.reset(lane)

    async def _process_event_batch(self, row, metrics, own_only):
        events = self.store.all(
            "SELECT * FROM browsing_events WHERE account_id=? AND status='received' "
            "AND (?=0 OR kind='own_comment') ORDER BY (kind='own_comment') DESC,timestamp DESC,id LIMIT 2",
            (row["account_id"], own_only),
        )
        for event_row in events:
            try:
                await self._event(event_row, row, metrics)
            except Exception as error:
                continuation = getattr(error, "continuation", None)
                if continuation:
                    self.runtime.continuations.bind_source_view(
                        error.continuation["id"],
                        "browsing",
                        "event:" + str(event_row["id"]),
                        "通知回应生成记录",
                    )
                    self.store.execute(
                        "UPDATE browsing_events SET status='checkpoint',error=? WHERE id=?",
                        (
                            dump(
                                {
                                    "message": "生成进度已保存，可继续",
                                    "generation_continuation_id": error.continuation["id"],
                                }
                            ),
                            event_row["id"],
                        ),
                    )
                    metrics["failures"] += 1
                elif isinstance(error, ValueError):
                    self.store.execute(
                        "UPDATE browsing_events SET status='skipped',error=? WHERE id=?",
                        (public_error(error), event_row["id"]),
                    )
                    metrics["failures"] += 1
                else:
                    raise

    async def _event(self, event_row, row, metrics):
        event = json.loads(event_row["payload"])
        config = row["config"]
        if (
            event_row["kind"] == "mention"
            and not config["mention_reply_enabled"]
            or not config["reply_enabled"]
        ):
            return
        thread = await self._read(self.adapter.read_public_thread, event)
        target = thread["target"]
        resource = target.get("bvid") or "dynamic:" + str(target["oid"])
        root = str(target.get("root") or thread["source"]["comment_id"])
        if root == "0":
            root = str(thread["source"]["comment_id"])
        scope = (str(row["account_id"]), row["character_id"], resource, root)
        async with self.runtime.conversation_turn(scope) as waited:
            if waited:
                self._check_round(row)
                thread = await self._read(self.adapter.read_public_thread, event)
            return await self._event_unlocked(event_row, row, metrics, event, thread)

    async def _event_unlocked(self, event_row, row, metrics, event, thread):
        config = row["config"]
        source, target = thread["source"], dict(thread["target"])
        target["parent"] = str(source["comment_id"])
        if (
            str(thread["account_id"]) != str(row["account_id"])
            or str(source["author_uid"]) == str(row["account_id"])
            or category(source["text"]) == "spam"
        ):
            raise ValueError("账号来源无效、本人循环或广告互动")
        resource = target.get("bvid") or "dynamic:" + str(target["oid"])
        canonical = canonical_intent("bilibili", resource, source["comment_id"], row["character_id"])
        if self.store.one(
            "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
            (row["account_id"], row["character_id"], canonical),
        ):
            self.store.execute(
                "UPDATE browsing_events SET status='skipped',error='同源回应意图已存在' WHERE id=?",
                (event_row["id"],),
            )
            return
        if self.store.one(
            "SELECT d.id FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE c.platform='bilibili' AND c.video_id=? AND c.comment_id=?",
            (resource, source["comment_id"]),
        ):
            raise ValueError("同一公开来源已有评论或私信移交草稿")
        material, keys = None, []
        if event_row["kind"] == "own_comment":
            allowed = {"public:" + str(source["comment_id"])}
        elif thread["resource_type"] == "video":
            key, material = await self._prepare_evidence(target["bvid"], config)
            keys = [key]
            allowed = {s["id"] for s in self._segments(material)}
        elif thread["resource_type"] == "dynamic":
            if not thread.get("public_text"):
                raise ValueError("原文字动态不可访问")
            allowed = {"public:" + str(source["comment_id"])}
        else:
            raise ValueError("未支持通知资源类型")
        self.store.execute(
            "UPDATE browsing_events SET status='context_ready',source_key=? WHERE id=?",
            (canonical, event_row["id"]),
        )
        self.runtime.relationships.observe(
            row["character_id"],
            "bilibili",
            str(source["author_uid"]),
            event.get("sender_name", ""),
            canonical,
            source["text"],
            account_id=row["account_id"],
        )
        items_list = thread.get("items", [])
        root_id = str(target.get("root") or "")
        parent_id = str(target.get("parent") or "")
        root_row = next((r for r in items_list if str(r.get("comment_id")) == root_id), None)
        parent_row = next((r for r in items_list if str(r.get("comment_id")) == parent_id), None)
        thread["root"] = {
            "comment_id": root_id,
            "author_name": root_row.get("display_name", "") if root_row else "",
            "text": root_row.get("content", "") if root_row else "",
        }
        if parent_id and parent_id != root_id:
            thread["parent"] = {
                "comment_id": parent_id,
                "author_name": parent_row.get("display_name", "") if parent_row else "",
                "text": parent_row.get("content", "") if parent_row else "",
            }
        thread["timeline"] = [
            {
                "author_name": r.get("display_name", "观众"),
                "text": r.get("content", ""),
                "is_root": str(r.get("comment_id")) == root_id,
                "is_target": str(r.get("comment_id")) == str(source.get("comment_id")),
            }
            for r in items_list[:8]
        ]
        reply = await self.decisions._response(row, material, thread, allowed, "reply_public", metrics)
        if reply.action in {"skip", "wait"}:
            self.store.execute(
                "UPDATE browsing_events SET status=?,error=? WHERE id=?",
                ("skipped", reply.reason, event_row["id"]),
            )
            return
        target.update(recipient=str(source["author_uid"]), source_text=source["text"], source_event=event)
        if reply.action == "handoff_private":
            if not config["comment_handoff_enabled"]:
                raise ValueError("评论转私信未授权")
            intent = self.publication._intent(row, "private_handoff", target, reply, keys, reply.evidence_ids)
            # Only normalized, freshly verified platform messages reach this
            # internal import. The operator CSV API keeps its original grammar.
            self.store.execute(
                "INSERT OR IGNORE INTO comments(platform,video_id,comment_id,thread_id,parent_comment_id,platform_user_id,display_name,content,category,created_at,imported_at) VALUES('bilibili',?,?,?,?,?,?,?,'interaction',?,?)",
                (
                    resource,
                    str(source["comment_id"]),
                    str(target["root"]),
                    str(target["parent"]),
                    str(source["author_uid"]),
                    event.get("sender_name", ""),
                    source["text"],
                    str(event["timestamp"]),
                    time.time(),
                ),
            )
            comment = self.store.one(
                "SELECT * FROM comments WHERE video_id=? AND comment_id=?",
                (resource, str(source["comment_id"])),
            )
            evidence = self.evidence_payload(keys, self._segments(material) if material else [])
            evidence["public_source_thread"] = thread
            evidence["source_event"] = event
            evidence["origin_round_id"] = row["id"]
            dm_id = self.messages.enqueue(
                "comment",
                comment["id"],
                automatic=bool(intent["auto_authorized"]),
                reason=reply.reason,
                character=self.runtime.character,
                account_id=row["account_id"],
                video_evidence=evidence,
                schedule=False,
            )
            self.store.execute(
                "UPDATE browsing_intents SET reason=? WHERE id=?",
                ("已移交私信草稿 #" + str(dm_id), intent["id"]),
            )
            await self.messages._generate(dm_id, automatic=bool(intent["auto_authorized"]))
        else:
            intent = self.publication._intent(row, "reply_public", target, reply, keys, reply.evidence_ids)
            if not intent:
                self.store.execute(
                    "UPDATE browsing_events SET status='skipped',error='已达到单视频回复上限或同源候选已存在' WHERE id=?",
                    (event_row["id"],),
                )
                return
            if intent["auto_authorized"]:
                await self.publish(intent["id"], automatic=True)
        self.store.execute("UPDATE browsing_events SET status='drafted' WHERE id=?", (event_row["id"],))

    def tasks(self, status=None, *, round_id=None, task_id=None):
        filters = [
            (key, value)
            for key, value in (("t.status", status), ("t.round_id", round_id), ("t.id", task_id))
            if value is not None
        ]
        where = " WHERE " + " AND ".join(key + "=?" for key, _ in filters) if filters else ""
        args = tuple(value for _, value in filters)
        rows = self.store.all(
            "SELECT t.*,r.account_id,r.character_id,r.character_version,"
            "COALESCE(json_extract(r.config,'$.discovery_publish'),0) AS autonomous "
            "FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id"
            + where
            + " ORDER BY t.created_at DESC LIMIT 100",
            args,
        )
        result = []
        for record in rows:
            task = dict(self.repo.decode(record), **json.loads(record["payload"]))
            continuation_id = task.get("generation_continuation_id")
            task["continuation"] = generation_view(self.runtime, continuation_id, "browsing", task["id"])
            if task["continuation"] and task["continuation"].get("output_visibility") in {
                "private",
                "unresolved",
            }:
                task["error"] = "生成原文在私信工作区查看"
            task["cache_status"] = "not_prepared"
            if record["material_key"]:
                try:
                    material = self.evidence.load(record["material_key"], allow_expired=True)
                    task["cache_status"] = "available"
                    task["metadata"] = material["metadata"]
                    task["coverage"] = material.get("coverage", task.get("coverage"))
                    task["missing_parts"] = material.get("missing_parts", [])
                    task["evidence"] = [
                        segment
                        for segment in self._segments(material)
                        if segment["id"] in task.get("evidence_ids", [])
                    ]
                except EvidenceUnavailable:
                    task["cache_status"] = "missing"
                    task["evidence"] = []
                    task["subtitle_tracks"] = []
                    task["missing_parts"] = []
            result.append(task)
        return result

    def rounds(self):
        return [
            self.repo.decode(r)
            for r in self.store.all(
                """SELECT r.*, (SELECT COUNT(*) FROM browsing_intents i
                       WHERE i.round_id=r.id AND i.kind IN ('top_level','reply_public')) AS public_drafts
                   FROM browsing_rounds r ORDER BY r.created_at DESC LIMIT 30"""
            )
        ]

    def intents(self):
        return [
            continuation_view(self.runtime, "browsing", self.repo.decode(r))
            for r in self.store.all("SELECT * FROM browsing_intents ORDER BY created_at DESC LIMIT 100")
        ]

    def public_comment_intent(self, identity):
        row = self.repo.decode(self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        if not row:
            return None
        if row["kind"] not in {"top_level", "reply_public", "private_handoff"}:
            return None
        if row["kind"] == "private_handoff" or row["status"] == "private_handoff":
            return {"id": row["id"], "status": "private_handoff"}
        evidence, unavailable = [], 0
        references = set(row["evidence"] or [])
        for key in row["material_keys"] or []:
            try:
                material = self.evidence.load(key, allow_expired=True)
                evidence.extend(
                    segment for segment in self._segments(material) if segment["id"] in references
                )
            except EvidenceUnavailable:
                unavailable += 1
        row["evidence_segments"] = evidence
        row["unavailable_materials"] = unavailable
        row["can_publish"] = self.comments.policy()["credentials_configured"] and not unavailable
        return continuation_view(self.runtime, "browsing", row)

    def inbox(self):
        rows = [
            self.repo.decode(r)
            for r in self.store.all("SELECT * FROM browsing_events ORDER BY timestamp DESC LIMIT 100")
        ]
        for row in rows:
            if row["status"] == "checkpoint":
                saved = json.loads(row["error"] or "{}")
                row["continuation"] = generation_view(
                    self.runtime, saved.get("generation_continuation_id"), "browsing", "event:" + row["id"]
                )
                row["error"] = saved.get("message")
        count = self.store.one("SELECT COUNT(*) AS n FROM dm_inbox")["n"]
        return {"items": rows, "private_count": count, "private_href": "#messages"}

    async def reread(self, task_id):
        task = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("视频任务不存在")
        return await self.run(preview=True, video=task["bvid"])

    async def open(self):
        self.started_at = time.time()
        self.repo.recover()
        # Force cold start standby: autonomous operations never start unprompted on boot
        policy = self.store.get("operations_policy", {})
        if policy.get("enabled"):
            policy["enabled"] = False
            self.store.set("operations_policy", policy)
        b_policy = self.store.get("browsing_policy", {})
        if b_policy.get("discovery_enabled"):
            b_policy["discovery_enabled"] = False
            self.store.set("browsing_policy", b_policy)
        for record in self.store.all("SELECT key FROM kv WHERE key LIKE 'browsing_discovery_pending:%'"):
            self.store.set(record["key"], False)
        self.loop = asyncio.create_task(self._loop())
        self.poller = asyncio.create_task(self._notifications_loop())

    async def close(self):
        self.closed = True
        await self.discovery.stop()
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
                    self.operations.policy()["enabled"]
                    and self.effective()["discovery_enabled"]
                    and not self.discovery.enabled
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
                if self.operations.policy()["enabled"]:
                    await self.run(automatic=True, interaction_only=True)
            except Exception as error:
                self.last_error = public_error(error)
            try:
                await asyncio.wait_for(
                    self.interactions_changed.wait(), self.policy()["notification_interval_seconds"]
                )
            except TimeoutError:
                pass
