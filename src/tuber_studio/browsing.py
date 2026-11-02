"""Evidence-first autonomous browsing. Workers prepare; this service alone admits replies."""

import asyncio
import contextvars
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from uuid import uuid4

import psutil

from .bilibili import PublishUncertain
from .browsing_evidence import BrowsingEvidence, EvidenceUnavailable
from .browsing_inputs import (
    ContentDecisionInput,
    ResponseDecisionInput,
    bind_content_decision,
    format_content_input,
)
from .browsing_models import (
    CONTENT_DECISION_INTENT_GLOSS,
    RESPONSE_INSTRUCTION,
    RESPONSE_REFERENCE_INSTRUCTION,
    BrowsePlan,
    BrowsingPolicy,
    ContentDecision,
    ResponseDecision,
    browsing_filter_reason,
    content_decision_schema,
    response_schema,
)
from .browsing_notifications import NotificationInbox
from .comments import DEFAULT_POLICY, category
from .compaction import ContextWindowExceeded
from .direct_messages import MessagePolicy
from .generation_meter import GenerationMeter, budgeted_json
from .operation_continuations import content_hash
from .operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    continuation_view,
    generation_view,
    publication_binding,
)
from .operation_generation import generate_with_recovery
from .operation_text import operation_character, operation_output_diagnostics, validate_operation_text
from .operations_control import canonical_intent
from .providers import SYSTEM_RULES, public_error
from .publication_errors import PlatformRejected, UnknownPublication
from .store import dump

read_round = contextvars.ContextVar("browsing_read_round", default=None)
generation_resume = contextvars.ContextVar("browsing_generation_resume", default=False)


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


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
        self.closed = False
        self.started_at = time.time()
        self.heartbeats = set()
        self.blockers = ["尚未核验当前账号及模型"]
        self.account = None
        self.last_error = None
        self.ready_signature = None
        self.sync_lock = asyncio.Lock()
        if hasattr(self.adapter, "request_gate"):
            self.adapter.request_gate = self._gate

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
        config = BrowsingPolicy.model_validate(value).model_dump()
        # Changing base fields never snapshots/replaces the overlay's previous values.
        self.store.set("browsing_policy", config)
        self.operations.configure(
            {k: config[k] for k in ("max_per_hour", "max_per_day", "recipient_cooldown_seconds")}
        )
        self.changed.set()
        return self.status()

    async def _current_account(self, expected=None):
        if self.adapter is not self.comments.adapter:
            await self.evidence.close()
            self.adapter = self.comments.adapter
            self.notifications.adapter = self.adapter
            self.evidence = BrowsingEvidence(self.runtime, self.adapter, self._read)
            if hasattr(self.adapter, "request_gate"):
                self.adapter.request_gate = self._gate
        self.account = str(await self._read(self.comments.account_id))
        if expected is not None and str(expected) != self.account:
            raise ValueError("候选来源账号已变化")
        return self.account

    async def preflight(self, *, for_automation=False):
        self.blockers = []
        try:
            await self._current_account()
        except (ValueError, AttributeError) as error:
            self.account = None
            self.blockers.append("账号身份核验失败：" + public_error(error))
        try:
            health = await self.runtime.llm.health()
            if self.settings.data["llm"]["kind"] == "demo" or health.get("ok") is False:
                self.blockers.append("请连接当前Mini文本模型；离线演示不支持自主决策")
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
                source = config["sources"][0] if config["sources"] else "recommend"
                if source == "recommend" and config["keywords"]:
                    source = "search"
                if source == "search" and not config["keywords"]:
                    source = "recommend"
                if source == "specified" and not config["watch_videos"]:
                    raise ValueError("指定视频模式未填写视频")
                if source != "specified":
                    await self._read(
                        self.adapter.discover,
                        source,
                        config["keywords"][0] if config["keywords"] else "",
                        1,
                        1,
                    )
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
            await self.preflight(for_automation=True)
            if self.blockers:
                raise ValueError("；".join(self.blockers))
            self.operations.configure(
                {"enabled": True}, account_id=self.account, character=self.runtime.character
            )
        else:
            self.operations.configure({"enabled": False})
            await self.stop()
        self.comments.policy_changed.set()
        self.messages.changed.set()
        self.changed.set()
        return self.status()

    async def stop(self, *, emergency=False):
        if emergency:
            self.operations.configure({"emergency": True})
            self.comments.policy_changed.set()
            self.messages.changed.set()
            self.dynamics.changed.set()
        children = list(self.active.values())
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        await self.evidence.cancel_pending()
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

    async def _read(self, method, *args):
        identity = read_round.get()
        config = self.repo.round(identity)["config"] if identity else self.policy()
        for attempt in range(config["read_retries"] + 1):
            try:
                if not hasattr(self.adapter, "request_gate"):
                    await self._gate()
                return await method(*args)
            except ValueError as error:
                if getattr(error, "kind", None) != "network" or attempt >= config["read_retries"]:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))

    async def _prepare_evidence(
        self, video, config, *, force=False, task=None, deadline=None, require_visual=False
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
            video, config, force=force, task=task, deadline=deadline, require_visual=require_visual
        )

    async def run(self, *, automatic=False, preview=None, notifications_only=False, video=None):
        if self.closed:
            raise ValueError("浏览服务已停止")
        if automatic and self.operations.policy()["emergency"]:
            raise ValueError("紧急停止仍有效")
        preview = not automatic if preview is None else preview
        if not automatic and not preview:
            raise ValueError("手工运行仅允许预览")
        if self.ready_signature != self.credential_signature() or not self.account:
            if self.active:
                await self.stop()
            await self.preflight()
        if self.blockers:
            raise ValueError("；".join(self.blockers))
        config = self.effective()
        if video:
            config = config | {"sources": ["specified"], "watch_videos": [video], "force_read": True}
        elif automatic and "sources" not in self.store.get("browsing_policy", {}):
            legacy = self.comments.policy().get("base", {})
            if legacy.get("auto_browse") and legacy.get("watch_videos"):
                config = config | {"sources": ["specified"], "watch_videos": legacy["watch_videos"][:20]}
        if (
            automatic
            and not notifications_only
            and time.time()
            < self.store.get("browsing_next:" + self.account + ":" + self.runtime.character["id"], 0)
        ):
            return {"status": "deferred", "reason": "OC有界延后计划尚未到期"}
        row, created = self.repo.create_round(
            self.account, self.runtime.character, config, self.operations.policy()["epoch"], preview
        )
        if automatic and not notifications_only:
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
            self.operations.assert_allowed(
                automatic=True,
                channel="comments",
                account_id=row["account_id"],
                character=current,
                epoch=row["epoch"] if self.operations.policy()["enabled"] else None,
            )

    async def _execute(self, row, notifications_only):
        identity, started = row["id"], time.monotonic()
        token = read_round.set(identity)
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
        sampler = asyncio.create_task(self._sample_resources(metrics))
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + max(0, row["deadline"] - time.time())
            ):
                self._check_round(row)
                await self.sync_notifications(row)
                await self._process_events(row, metrics)
                if not notifications_only or self.store.get(
                    "browsing_discovery_pending:" + row["account_id"], False
                ):
                    self.store.set("browsing_discovery_pending:" + row["account_id"], False)
                    candidates = await self._discover(row)
                    tasks = [self.repo.task(identity, c) for c in candidates]
                    semaphore = asyncio.Semaphore(row["config"]["workers"])
                    preparation_start = time.monotonic()
                    async with asyncio.TaskGroup() as group:
                        children = [
                            group.create_task(self._prepare_task(i, row, semaphore, metrics)) for i in tasks
                        ]
                    prepared = [child.result() for child in children]
                    metrics["preparation_seconds"] = time.monotonic() - preparation_start
                    for result in prepared:
                        await self._process_events(row, metrics)
                        if result:
                            await self._evaluate_task(result, row, metrics)
                    await self._process_events(row, metrics)
                status = "partial" if metrics["failures"] else "completed"
        except asyncio.CancelledError:
            status = "cancelled"
            self.store.execute(
                "UPDATE browsing_tasks SET status='cancelled',error='父轮取消' WHERE round_id=? AND status IN ('discovered','claimed','preparing','evidence_ready')",
                (identity,),
            )
            await self.evidence.cancel_pending()
        except Exception as error:
            status = "partial" if metrics["viewed"] or metrics["prepared"] else "failed"
            self.last_error = public_error(error)
            self.store.execute(
                "UPDATE browsing_tasks SET status='cancelled',error=? WHERE round_id=? AND status IN ('discovered','claimed','preparing','evidence_ready')",
                (self.last_error, identity),
            )
        finally:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
            heartbeats = list(self.heartbeats)
            for heartbeat in heartbeats:
                heartbeat.cancel()
            await asyncio.gather(*heartbeats, return_exceptions=True)
            self.heartbeats.difference_update(heartbeats)
            metrics["elapsed_seconds"] = time.monotonic() - started
            metrics["cache_hits"] = self.evidence.hits - metrics.pop("cache_hits_before")
            metrics["merged_reads"] = self.evidence.merged - metrics.pop("merged_before")
            metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
            metrics.update(self.repo.round(identity)["metrics"])
            self.repo.finish_round(
                identity, status, metrics, self.last_error if status in {"failed", "partial"} else None
            )
            read_round.reset(token)

    async def _sample_resources(self, metrics):
        while True:
            metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
            used = psutil.virtual_memory().used
            metrics["peak_system_ram_bytes"] = max(metrics.get("peak_system_ram_bytes", 0), used)
            await asyncio.sleep(0.1)

    async def _discover(self, row):
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
            plan = await self._model(
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
                return []
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
        selected = []
        for item in candidates:
            reason = self.filter_reason(item, config)
            if reason:
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
                    self._capacity_signature(config),
                    time.time() - 21600,
                ),
            )
            if previous and not config.get("force_read"):
                continue
            selected.append(item)
            if len(selected) >= config["inspect_limit"]:
                break
        return selected

    @staticmethod
    def filter_reason(item, config):
        return browsing_filter_reason(item, config)

    async def _prepare_task(self, identity, row, semaphore, metrics):
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
                    {"reason": "worker_failed"},
                    error=public_error(error),
                )
                return None

    async def _prepare_attempt(self, identity, row, semaphore, metrics, owner):
        async with semaphore:
            generation = self.repo.claim(identity, owner)
            heartbeat = asyncio.create_task(self._heartbeat(identity, owner, generation))
            self.heartbeats.add(heartbeat)
            heartbeat.add_done_callback(self.heartbeats.discard)
            ready = False
            try:
                item = json.loads(
                    self.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
                )
                material_key, material = await self._prepare_evidence(
                    item["bvid"],
                    row["config"],
                    task={"id": identity},
                    deadline=row["deadline"],
                    force=row["config"].get("force_read", False),
                )
                reason = self.filter_reason(material["metadata"], row["config"])
                if reason:
                    raise EvidenceUnavailable("filtered", reason)
                self.repo.complete(
                    identity,
                    owner,
                    generation,
                    "evidence_ready",
                    item
                    | {
                        "metadata": self._metadata_summary(material["metadata"]),
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
                metrics["failures"] += int(
                    getattr(error, "kind", "") not in {"no_subtitles", "empty", "filtered"}
                )
                self.repo.complete(
                    identity,
                    owner,
                    generation,
                    "skipped" if hasattr(error, "kind") else "failed",
                    item | {"reason": getattr(error, "kind", "preparation_failed")},
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

    def _model_input(self, row, schema, data, instruction, *, response_input=None):
        if schema is ResponseDecision:
            response_input = response_input or ResponseDecisionInput(data)
            instruction += RESPONSE_REFERENCE_INSTRUCTION
        content = (
            response_input.format()
            if response_input is not None
            else format_content_input(data)
            if schema is ContentDecision
            else dump(data)
        )
        character = operation_character(self.store.character(row["character_id"], row["character_version"]))
        messages = [
            {
                "role": "system",
                "content": SYSTEM_RULES + dump({k: character[k] for k in ("core", "style", "rules")}),
            },
            {
                "role": "system",
                "content": instruction,
            },
            {
                "role": "user",
                "content": "公开来源材料：" + content,
            },
        ]
        model_schema = schema.model_json_schema()
        if schema is ContentDecision:
            model_schema = content_decision_schema({segment["id"] for segment in data["segments"]})
        elif schema is ResponseDecision:
            allowed = response_input.allowed_aliases
            model_schema = response_schema(allowed, allow_handoff=data.get("kind") != "top_level")
        return character, messages, model_schema

    async def _model(
        self, row, schema, data, instruction, metrics=None, *, diagnose=None, source_binding=None
    ):
        self._check_round(row)
        response_input = ResponseDecisionInput(data) if schema is ResponseDecision else None
        character, messages, model_schema = self._model_input(
            row, schema, data, instruction, response_input=response_input
        )
        output = row["config"]["decision_max_tokens"]

        def guard():
            self._check_round(row)
            source = data.get("source_identity")
            if source and self.store.one(
                "SELECT d.id FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE c.platform='bilibili' AND c.video_id=? AND c.comment_id=? AND d.character_id=?",
                (source["resource"], source["comment_id"], row["character_id"]),
            ):
                raise ValueError("等待模型期间同源评论已有候选，合并处理")

        async def invoke(packet):
            wait = time.monotonic()
            async with self.runtime.offline_generation():
                if metrics is not None:
                    metrics["lock_wait_seconds"] += time.monotonic() - wait
                guard()
                tokens = None
                with GenerationMeter(self.repo, row["id"]):
                    if not getattr(self.runtime.llm, "generation_metered", False):
                        counted = (
                            self.runtime.llm.prepare_json_messages(packet, model_schema, max_tokens=output)
                            if hasattr(self.runtime.llm, "prepare_json_messages")
                            else packet
                        )
                        tokens = await self.runtime.llm.count_tokens(counted)
                        if tokens + output > self.settings.data["llm"]["context_tokens"] * 95 // 100:
                            raise ContextWindowExceeded("浏览输入超出真实窗口，未静默截断材料")
                    return await budgeted_json(
                        self.runtime.llm, packet, model_schema, max_tokens=output, input_tokens=tokens
                    )

        def validate(result):
            value = (
                response_input.validate_wire(result)
                if response_input is not None
                else schema.model_validate(result.model_dump() if hasattr(result, "model_dump") else result)
            )
            if schema is ContentDecision:
                allowed = {segment["id"] for segment in data["segments"]}
                if (
                    not set(value.evidence_ids) <= allowed
                    or value.worth_commenting
                    and not value.evidence_ids
                ):
                    raise ValueError("内容判断引用不存在或为空")
            elif schema is ResponseDecision:
                if value.action in {"reply_public", "handoff_private"} and (
                    not value.evidence_ids or not value.text
                ):
                    raise ValueError("回应缺少有效正文或来源引用")
                if value.action == "reply_public" and value.reason not in {
                    "specific_detail",
                    "light_interaction",
                }:
                    raise ValueError("公开回复渠道与理由不一致")
                if value.action == "handoff_private" and (
                    data.get("kind") == "top_level"
                    or value.reason not in {"private_details", "user_requested", "long_followup"}
                ):
                    raise ValueError("私信渠道与来源／理由不一致")
            return value

        binding = {
            "task": "generation",
            "account_id": row["account_id"],
            "character": {"id": row["character_id"], "version": row["character_version"]},
            "schema": schema.__name__,
            "source_sha256": content_hash(
                {
                    "backend_source": source_binding,
                    "data": {
                        key: (
                            {k: v for k, v in value.items() if k != "confirmed_relationship"}
                            if key == "thread" and isinstance(value, dict)
                            else value
                        )
                        for key, value in data.items()
                        if key != "recent_public_responses"
                    },
                }
            ),
        }
        if response_input is not None:
            binding["input_format"] = response_input.FORMAT_VERSION
        value, _ = await generate_with_recovery(
            self.runtime,
            channel="generation.browsing",
            identity=content_hash(binding),
            binding=binding,
            character=character,
            messages=messages,
            generate=invoke,
            validate=validate,
            max_tokens=output,
            guard=guard,
            parent_round_id=row["id"],
            deadline=row["deadline"],
            generation_context=self.runtime.offline_generation,
            context_data={
                "instruction": messages[1]["content"],
                "source": response_input.format() if response_input is not None else data,
            },
            reuse_completed=generation_resume.get(),
            diagnose=diagnose,
        )
        return response_input.resolve(value) if response_input is not None else value

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
            if self.evidence.load(key)["source_version"] != version:
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
            source = self.store.one("SELECT bvid FROM browsing_materials WHERE id=?", (key,))
            if not source:
                raise ValueError("原始来源标识已移除，不能恢复旧证据")
            identity, material = await self._prepare_evidence(source["bvid"], self.effective(), force=True)
            if material["source_version"] != version:
                raise ValueError("视频来源材料已变化，不能使用旧候选")
            result.append(identity)
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

    @staticmethod
    def _metadata_summary(metadata):
        return {
            key: metadata[key]
            for key in ("bvid", "aid", "title", "owner_uid", "owner", "duration")
            if key in metadata
        }

    @staticmethod
    def _content_instruction():
        return (
            "interest=like表示想参与这个话题，吐槽、共情和分享也属于参与；"
            "worth_commenting表示有具体想说的话。"
            "evidence_ids选择相关片段的id；值得发言时至少选一项。"
            "字幕/画面按分P和材料类型列出，[id|起始时码]标记可选择的内容块；evidence_ids选择这些短id。"
            "source_kinds和has_visual描述本次提供的材料，已有视觉观察和字幕都能作为依据。"
            "coverage_required表示还要补充的内容：当前材料够用选provided，"
            "需新的画面选visual，需其它分P选unread_part；也可以不感兴趣或暂不发言。"
            + CONTENT_DECISION_INTENT_GLOSS
        )

    def _capacity_signature(self, config):
        llm = self.settings.data["llm"]
        return content_hash(
            {
                "context_tokens": llm["context_tokens"],
                "kind": llm.get("kind"),
                "model": llm.get("model"),
                "base_url": llm.get("base_url"),
                "output_tokens": config["decision_max_tokens"],
                "max_input_tokens": config["max_input_tokens"],
                "instruction": self._content_instruction(),
                "system": SYSTEM_RULES,
                "packing_protocol": "full-message-v1",
                "response_input_format": ResponseDecisionInput.FORMAT_VERSION,
            }
        )

    async def _chunks(self, material, row):
        # Count the same role, source and decoder schema that generation uses.
        segments = self._segments(material)
        presentation = ContentDecisionInput(material)
        output = row["config"]["decision_max_tokens"]
        context = self.settings.data["llm"]["context_tokens"]
        available = min(context // 2, context * 95 // 100 - output, row["config"]["max_input_tokens"] // 2)
        if available <= 0:
            raise ContextWindowExceeded(
                "显式输出预留已占满可用窗口；调整窗口或输出设置后可重读，原文保持完整"
            )

        async def count(source):
            data, _ = presentation.present(source)
            _, messages, schema = self._model_input(row, ContentDecision, data, self._content_instruction())
            if hasattr(self.runtime.llm, "prepare_json_messages"):
                messages = self.runtime.llm.prepare_json_messages(messages, schema, max_tokens=output)
            async with self.runtime.offline_generation():
                self._check_round(row)
                with GenerationMeter(self.repo, row["id"]):
                    return await self.runtime.llm.count_tokens(messages)

        chunks, start = [], 0
        while start < len(segments):
            lower, upper, best = start + 1, len(segments), start
            if await count(segments[start:]) <= available:
                chunks.append(segments[start:])
                break
            while lower <= upper:
                middle = (lower + upper) // 2
                if await count(segments[start:middle]) <= available:
                    best, lower = middle, middle + 1
                else:
                    upper = middle - 1
            if best == start:
                raise ContextWindowExceeded(
                    "角色、Schema和至少一个原文片段连同显式输出预留超出当前容量；调整容量设置后可重读，未截断原文"
                )
            chunks.append(segments[start:best])
            start = best
        return chunks

    async def _content_decisions(self, material, row, metrics):
        decisions = []
        presentation = ContentDecisionInput(material)
        for chunk in await self._chunks(material, row):
            data, aliases = presentation.present(chunk)
            decision = await self._model(
                row,
                ContentDecision,
                data,
                self._content_instruction(),
                metrics,
                source_binding={
                    "source_version": self.evidence.source_version(material),
                    "chunk_ids": [segment["id"] for segment in chunk],
                },
            )
            decisions.append(bind_content_decision(decision, aliases))
        return decisions

    async def _evaluate_task(self, result, row, metrics):
        identity, material = result["id"], result["material"]
        heartbeat = result["heartbeat"]
        decisions, followup = [], None
        try:
            decisions = await self._content_decisions(material, row, metrics)
            if any(decision.coverage_required == "visual" for decision in decisions) and not any(
                part.get("visual") for part in material["parts"]
            ):
                followup = {
                    "status": "requested" if row["config"]["visual_enabled"] else "disabled",
                    "decisions_before": [decision.model_dump() for decision in decisions],
                    "source_material_key": result["material_key"],
                }
                if row["config"]["visual_enabled"]:
                    initial = json.loads(
                        self.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                            "payload"
                        ]
                    ) | {"decisions": followup["decisions_before"], "visual_followup": followup}
                    self.repo.complete(
                        identity,
                        result["owner"],
                        result["generation"],
                        "evidence_ready",
                        initial,
                        result["material_key"],
                    )
                    try:
                        key, material = await self._prepare_evidence(
                            material["metadata"]["bvid"],
                            row["config"],
                            require_visual=True,
                            task={"id": identity},
                            deadline=row["deadline"],
                        )
                        result.update(material_key=key, material=material)
                        followup.update(status="prepared", material_key=key)
                        self.repo.complete(
                            identity,
                            result["owner"],
                            result["generation"],
                            "evidence_ready",
                            initial
                            | {
                                "coverage": material["coverage"],
                                "missing_parts": material["missing_parts"],
                                "visual_followup": followup,
                            },
                            key,
                        )
                        decisions = await self._content_decisions(material, row, metrics)
                        followup.update(
                            status="completed",
                            needs_visual_again=any(
                                decision.coverage_required == "visual" for decision in decisions
                            ),
                        )
                    except Exception as error:
                        followup.update(status="failed", error=public_error(error))
                        raise
            actionable = [
                d
                for d in decisions
                if d.interest == "like"
                and d.worth_commenting
                and d.coverage_required == "provided"
                and d.reason == "specific_detail"
            ]
            payload = json.loads(
                self.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
            ) | {
                "metadata": self._metadata_summary(material["metadata"]),
                "coverage": material["coverage"],
                "decisions": [d.model_dump() for d in decisions],
                "subtitle_tracks": [
                    {k: part.get(k) for k in ("cid", "track_id", "language", "is_ai", "coverage")}
                    for part in material["parts"]
                ],
                "evidence_ids": sorted({ref for decision in decisions for ref in decision.evidence_ids}),
            }
            if followup is not None:
                payload["visual_followup"] = followup
            status = "viewed_without_comment"
            source_key = canonical_intent(
                "bilibili", material["metadata"]["bvid"], "top-level", row["character_id"]
            )
            existing = self.store.one(
                "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (row["account_id"], row["character_id"], source_key),
            )
            if (
                actionable
                and row["config"]["top_level_enabled"]
                and not existing
                and self.repo.round(row["id"])["budget"]["top_levels"] < row["config"]["top_level_limit"]
            ):
                self.repo.reserve_budget(row["id"], top_levels=1)
                chosen = {ref for d in actionable for ref in d.evidence_ids}
                context = await self._read(
                    self.adapter.fetch,
                    material["metadata"]["bvid"],
                    1,
                )
                response = await self._response(row, material, context, chosen, "top_level", metrics)
                if response.action == "reply_public":
                    target = {
                        "type": 1,
                        "oid": str(material["metadata"]["aid"]),
                        "bvid": material["metadata"]["bvid"],
                        "root": "0",
                        "parent": "0",
                    }
                    intent = self._intent(
                        row, "top_level", target, response, [result["material_key"]], chosen
                    )
                    payload["intent_id"] = intent["id"]
                    status = "selected"
                    if intent["auto_authorized"]:
                        await self.publish(intent["id"], automatic=True)
            self.repo.complete(
                identity, result["owner"], result["generation"], status, payload, result["material_key"]
            )
            metrics["viewed"] += 1
        except Exception as error:
            metrics["failures"] += 1
            continuation = getattr(error, "continuation", None)
            if continuation:
                self.runtime.continuations.bind_source_view(
                    error.continuation["id"],
                    "browsing",
                    identity,
                    "浏览回应生成记录 · " + material["metadata"]["bvid"],
                )
            self.repo.complete(
                identity,
                result["owner"],
                result["generation"],
                "checkpoint" if continuation or isinstance(error, ContextWindowExceeded) else "failed",
                {
                    "reason": "生成进度已保存，可继续" if continuation else public_error(error),
                    **({"generation_continuation_id": error.continuation["id"]} if continuation else {}),
                    **(
                        {"capacity_signature": self._capacity_signature(row["config"])}
                        if isinstance(error, ContextWindowExceeded)
                        else {}
                    ),
                    "decisions": [decision.model_dump() for decision in decisions],
                    **({"visual_followup": followup} if followup is not None else {}),
                },
                result["material_key"],
                public_error(error),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _response(self, row, material, context, allowed, kind, metrics):
        segments = [s for s in self._segments(material) if s["id"] in allowed] if material else []
        recent = self.store.all(
            "SELECT text FROM browsing_intents WHERE status='published' AND character_id=? ORDER BY updated_at DESC LIMIT ?",
            (row["character_id"], row["config"]["recent_sample_limit"]),
        )
        data = {
            "kind": kind,
            "evidence": segments,
            "thread": context,
            "recent_public_responses": recent,
            "coverage": material["coverage"] if material else "public_text",
        }
        if not material:
            data["allowed_evidence_ids"] = list(allowed)
        if context.get("source") and context.get("target"):
            target = context["target"]
            data["source_identity"] = {
                "resource": target.get("bvid") or "dynamic:" + str(target["oid"]),
                "comment_id": str(context["source"]["comment_id"]),
            }
        return await self._model(
            row,
            ResponseDecision,
            data,
            RESPONSE_INSTRUCTION,
            metrics,
            diagnose=lambda value: (
                self.repetition_reason(value.text, recent, context)
                or (
                    "正文包含大量重复段落；结合表达意图选择保留、修订或暂缓"
                    if operation_output_diagnostics(value.text)["needs_review"]
                    else None
                )
                if value.action in {"reply_public", "handoff_private"}
                else None
            ),
        )

    @staticmethod
    def repetition_reason(text, recent, context):
        value = normalized(text)
        samples = [r["text"] for r in recent] + [
            r.get("content", r.get("text", "")) for r in context.get("items", [])
        ]
        for sample in samples:
            other = normalized(sample)
            if other and (
                value == other
                or len(value) >= 12
                and SequenceMatcher(None, value, other).ratio() > 0.85
                or len(value) >= 12
                and value[:12] == other[:12]
            ):
                return "正文与近期公开发言／热评重复"
        return None

    def _intent(self, row, kind, target, response, material_keys, evidence_ids):
        resource = target.get("bvid") or "dynamic:" + str(target["oid"])
        source = "top-level" if kind == "top_level" else str(target["parent"])
        source_key = canonical_intent("bilibili", resource, source, row["character_id"])
        identity, now = str(uuid4()), time.time()
        auto = (
            not row["preview"]
            and self.operations.policy()["enabled"]
            and self.operations.policy()["epoch"] == row["epoch"]
            and self.semantic_ready()
        )
        self.store.execute(
            "INSERT OR IGNORE INTO browsing_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                row["account_id"],
                row["character_id"],
                row["character_version"],
                source_key,
                kind,
                row["id"],
                row["epoch"],
                int(auto),
                "private_handoff" if kind == "private_handoff" else "pending_review",
                "" if kind == "private_handoff" else response.text,
                response.reason,
                dump(target),
                dump(sorted(evidence_ids)),
                dump(material_keys),
                dump([self.evidence.load(k)["source_version"] for k in material_keys]),
                None,
                "not_checked",
                None,
                now,
                now,
            ),
        )
        return self.repo.decode(
            self.store.one(
                "SELECT * FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (row["account_id"], row["character_id"], source_key),
            )
        )

    def has_intent(self, comment, character):
        key = canonical_intent(
            comment["platform"], comment["video_id"], comment["comment_id"], character["id"]
        )
        return bool(
            self.store.one(
                "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (self.account or self.operations.policy()["account_id"], character["id"], key),
            )
        )

    def review(self, identity, action, text=None):
        row = self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        if (
            not row
            or row["status"] not in {"pending_review", "approved"}
            or not isinstance(action, str)
            or action not in {"approve", "reject"}
        ):
            raise ValueError("当前候选不能审核")
        if text is not None and not isinstance(text, str):
            raise ValueError("正文须为字符串")
        text = row["text"] if text is None else text.strip()
        text = validate_operation_text(text)
        self.store.execute(
            "UPDATE browsing_intents SET status=?,text=?,auto_authorized=0,updated_at=? WHERE id=?",
            ("approved" if action == "approve" else "rejected", text, time.time(), identity),
        )
        return self.repo.decode(self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))

    def _publication_binding(self, row, character, metadata):
        target = row["target"]
        source_text = target.get("source_text")
        source = (
            source_text
            if source_text is not None
            else [
                {key: item.get(key) for key in ("bvid", "aid", "title", "description", "owner_uid", "parts")}
                for item in metadata
            ]
        )
        return publication_binding(
            "comments",
            row["account_id"],
            character,
            row["source_key"],
            {
                "platform": "bilibili",
                "resource_type": target.get("resource_type", "video"),
                "oid": str(target["oid"]),
                "root": str(target.get("root") or "0"),
                "parent": str(target.get("parent") or "0"),
                "recipient_uid": str(target.get("recipient") or ""),
            },
            {
                "kind": row["kind"],
                "local_id": row["id"],
                "platform_id": str(target.get("parent") or target["oid"]),
                "content_sha256": content_hash(source),
            },
        )

    def _intent_materials(self, row):
        return [self.evidence.load(key) for key in row["material_keys"]]

    async def _publication_context(self, identity, automatic):
        row = self.repo.decode(self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        if not row:
            raise ValueError("候选不存在")
        account = await self._current_account(row["account_id"])
        character = self.store.character(row["character_id"], row["character_version"])
        materials = self._intent_materials(row)
        metadata = [item["metadata"] for item in materials]
        binding = self._publication_binding(row, character, metadata)
        signature = self.credential_signature()

        def guard():
            fresh = self.repo.decode(self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
            if self.closed:
                raise asyncio.CancelledError
            if (
                not fresh
                or any(
                    fresh.get(key) != row.get(key)
                    for key in (
                        "status",
                        "text",
                        "source_key",
                        "account_id",
                        "character_id",
                        "character_version",
                        "target",
                        "input_version",
                    )
                )
                or signature != self.credential_signature()
            ):
                raise ValueError("恢复期间候选、账号或原始互动已变化")
            current = self._intent_materials(fresh)
            if self._publication_binding(fresh, character, [item["metadata"] for item in current]) != binding:
                raise ValueError("恢复期间原始视频或互动已变化")
            policy = self.effective()
            if automatic and not (
                policy["top_level_enabled" if row["kind"] == "top_level" else "reply_enabled"]
                and row["auto_authorized"]
                and self.semantic_ready()
            ):
                raise ValueError("自动回应授权已停止")
            self.operations.assert_allowed(
                automatic=automatic,
                channel="comments",
                account_id=account,
                character=character,
                epoch=row["epoch"] if automatic else None,
            )

        segments = [segment for material in materials for segment in self._segments(material)]
        selected = set(row["evidence"])
        used = [segment for segment in segments if segment["id"] in selected]
        context = {
            "target_source": row["target"].get("source_text"),
            "metadata": metadata,
            "evidence": used,
            "coverage": [item["coverage"] for item in materials],
        }
        parent = self.repo.round(row["round_id"])
        active = parent and parent["status"] in {"queued", "running"}
        return PublicationContext(
            binding,
            row["text"],
            character,
            context,
            guard,
            parent_round_id=parent["id"] if active else None,
            deadline=parent["deadline"] if active else None,
            max_tokens=parent["config"]["decision_max_tokens"]
            if active
            else self.policy()["decision_max_tokens"],
            on_defer=lambda prepared, state: self._defer_revision(identity, prepared, state),
            manual_approved=row["status"] == "approved",
        )

    def _apply_revision(self, identity, prepared, text):
        prepared.guard()
        text = validate_operation_text(text)
        self.store.execute(
            "UPDATE browsing_intents SET status='pending_review',text=?,error=NULL,updated_at=? WHERE id=?",
            (text, time.time(), identity),
        )

    def _defer_revision(self, identity, prepared, state):
        prepared.guard()
        self.store.execute(
            "UPDATE browsing_intents SET status=?,updated_at=? WHERE id=?",
            ("rejected" if state == "abandoned" else "pending_review", time.time(), identity),
        )

    async def resume_continuation(self, identity, continuation_id):
        if identity.startswith("generation:"):
            if self.active:
                raise ValueError("浏览轮正在执行，结束后可继续生成检查点")
            local_id = identity.removeprefix("generation:")
            event = local_id.startswith("event:")
            if event:
                source = self.store.one(
                    "SELECT * FROM browsing_events WHERE id=?", (local_id.removeprefix("event:"),)
                )
                saved = json.loads(source["error"] or "{}") if source else {}
            else:
                source = self.store.one(
                    "SELECT t.*,r.account_id FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE t.id=?",
                    (local_id,),
                )
                saved = json.loads(source["payload"]) if source else {}
            continuation = self.runtime.continuations.get(continuation_id)
            await self._current_account()
            character = self.runtime.character
            if (
                not source
                or saved.get("generation_continuation_id") != continuation_id
                or str(source["account_id"]) != str(self.account)
                or continuation["binding"]["character"]
                != {"id": character["id"], "version": character["version"]}
                or not continuation["can_resume"]
            ):
                raise ValueError("生成检查点的账号、角色、原始任务或状态已变化")
            token = generation_resume.set(True)
            try:
                if event:
                    policy = self.effective()
                    if (
                        not policy["reply_enabled"]
                        or source["kind"] == "mention"
                        and not policy["mention_reply_enabled"]
                    ):
                        raise ValueError("通知回应尚未启用")
                    self.store.execute(
                        "UPDATE browsing_events SET status='received' WHERE id=? AND status='checkpoint'",
                        (source["id"],),
                    )
                    return await self.run(preview=True, notifications_only=True)
                return await self.reread(local_id)
            finally:
                generation_resume.reset(token)
        return await FeedbackPublication(self.runtime, self.store, self.operations).resume(
            "browsing",
            identity,
            continuation_id,
            prepare=lambda: self._publication_context(identity, False),
            apply_revision=lambda prepared, text: self._apply_revision(identity, prepared, text),
            reconcile=lambda token, prepared: self._reconcile(identity),
        )

    async def _reconcile(self, identity):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        async def read(row, attempt):
            if not hasattr(self.adapter, "reconcile_publication"):
                return {"status": "unsupported"}
            try:
                await self._current_account(row["binding"]["account_id"])
            except ValueError:
                return {"status": "check_failed", "reason": "account_unavailable"}
            return await self.adapter.reconcile_publication(
                row["binding"]["target"],
                author_uid=row["binding"]["account_id"],
                text=row["feedback"]["sent_payload"]["text"],
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )

        def commit(receipt, verification):
            self.store.execute(
                "UPDATE browsing_intents SET status='published',receipt=?,visibility=?,error=NULL,updated_at=? WHERE id=?",
                (dump(receipt), verification["status"], time.time(), identity),
            )

        return await feedback.reconcile("browsing", identity, read, commit)

    async def publish(self, identity, *, automatic=False):
        row = self.store.one("SELECT epoch FROM browsing_intents WHERE id=?", (identity,))
        return await FeedbackPublication(self.runtime, self.store, self.operations).submit(
            "browsing",
            identity,
            prepare=lambda: self._publication_context(identity, automatic),
            send=lambda ticket, binding, body_hash: self._publish_once(
                identity, automatic=automatic, revision_ticket=ticket, binding=binding, body_hash=body_hash
            ),
            apply_revision=lambda prepared, text: self._apply_revision(identity, prepared, text),
            automatic=automatic,
            epoch=row["epoch"] if row and automatic else None,
            reconcile=lambda token, prepared: self._reconcile(identity),
        )

    async def _publish_once(
        self, identity, *, automatic=False, revision_ticket=None, binding=None, body_hash=None
    ):
        async with self.comments.publish_lock:
            row = self.repo.decode(self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
            if not row or row["status"] != ("pending_review" if automatic else "approved"):
                raise ValueError("候选未获授权或已提交")
            if automatic and (not row["auto_authorized"] or not self.semantic_ready()):
                raise ValueError("该候选不能自动发送")
            character = self.store.character(row["character_id"], row["character_version"])
            account = await self._current_account(row["account_id"])
            signature = self.credential_signature()
            if account != row["account_id"]:
                raise ValueError("候选来源账号已变化")
            row["material_keys"] = await self._refresh_material_keys(
                row["material_keys"], json.loads(row["input_version"])
            )
            self.store.execute(
                "UPDATE browsing_intents SET material_keys=? WHERE id=?",
                (dump(row["material_keys"]), identity),
            )
            if row["kind"] != "top_level":
                verified = await self._read(self.adapter.read_public_thread, row["target"]["source_event"])
                if verified["source"]["text"] != row["target"]["source_text"] or str(
                    verified["source"]["author_uid"]
                ) != str(row["target"]["recipient"]):
                    raise ValueError("原始互动已删除或变化")
            materials = self._intent_materials(row)
            actual_binding = self._publication_binding(
                row, character, [item["metadata"] for item in materials]
            )
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash({"text": row["text"]})
            ):
                raise ValueError("发送前原始绑定或正文已变化")
            token = None

            def before_send():
                nonlocal token
                if token is not None:
                    raise ValueError("同一意图不能二次进入提交边界")
                if signature != self.credential_signature() or self.closed:
                    raise ValueError("账号／服务已变化，取消提交")
                fresh = self.repo.decode(
                    self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
                )
                if not fresh or any(
                    fresh.get(key) != row.get(key)
                    for key in (
                        "status",
                        "text",
                        "source_key",
                        "target",
                        "account_id",
                        "character_id",
                        "character_version",
                        "input_version",
                    )
                ):
                    raise ValueError("准备期间候选或审核状态已变化，保留最新状态")
                if automatic:
                    policy = self.effective()
                    if not policy["top_level_enabled" if row["kind"] == "top_level" else "reply_enabled"]:
                        raise ValueError("自动回应已关闭")
                self.operations.assert_allowed(
                    automatic=automatic,
                    channel="comments",
                    account_id=account,
                    character=character,
                    epoch=row["epoch"] if automatic else None,
                )
                for key in row["material_keys"]:
                    self.evidence.load(key)
                token = self.operations.reserve(
                    account,
                    character,
                    row["source_key"],
                    "comments",
                    recipient=row["target"].get("recipient", ""),
                    automatic=automatic,
                    limits={
                        "max_per_hour": self.comments.policy()["max_replies_per_hour"],
                        "max_per_day": self.comments.policy()["max_replies_per_day"],
                    },
                    epoch=row["epoch"] if automatic else None,
                    binding=actual_binding,
                    content_hash=content_hash({"text": row["text"]}),
                    revision_ticket=revision_ticket,
                )
                self.store.execute(
                    "UPDATE browsing_intents SET status='publishing',updated_at=? WHERE id=?",
                    (time.time(), identity),
                )

            try:
                method = (
                    self.adapter.publish_top_level
                    if row["kind"] == "top_level"
                    else self.adapter.publish_resource_reply
                )
                receipt = await method(row["target"], row["text"], before_send=before_send)
                if (
                    not token
                    or not isinstance(receipt, dict)
                    or not str(receipt.get("rpid", "")).isdigit()
                    or int(receipt["rpid"]) <= 0
                ):
                    raise PublishUncertain("没有有效一次提交回执")
            except BaseException as error:
                if token:
                    uncertain = isinstance(
                        error, (PublishUncertain, asyncio.CancelledError)
                    ) or not isinstance(error, ValueError)
                    self.operations.finish(
                        token, "uncertain" if uncertain else "rejected", error=public_error(error)
                    )
                    if uncertain:
                        FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                            "browsing",
                            identity,
                            token,
                            actual_binding,
                            {"text": row["text"]},
                            public_error(error),
                        )
                    self.store.execute(
                        "UPDATE browsing_intents SET status=?,error=?,updated_at=? WHERE id=?",
                        (
                            "publish_uncertain"
                            if uncertain
                            else row["status"]
                            if isinstance(error, PlatformRejected)
                            else "rejected_by_platform",
                            public_error(error),
                            time.time(),
                            identity,
                        ),
                    )
                    if isinstance(error, PlatformRejected):
                        error.write_token = token
                        error.body_hash = content_hash({"text": row["text"]})
                        error.sent_payload = {"text": row["text"]}
                    elif uncertain and not isinstance(error, asyncio.CancelledError):
                        raise UnknownPublication(public_error(error), token) from None
                raise
            try:
                self.store.execute(
                    "UPDATE browsing_intents SET status='published',receipt=?,updated_at=? WHERE id=?",
                    (dump(receipt), time.time(), identity),
                )
                self.operations.finish(token, "published", receipt=receipt)
            except Exception:
                self.operations.finish(token, "uncertain", receipt=receipt, error="平台成功后本地保存失败")
                FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                    "browsing",
                    identity,
                    token,
                    actual_binding,
                    {"text": row["text"]},
                    "平台成功后本地保存失败",
                )
                self.store.execute(
                    "UPDATE browsing_intents SET status='publish_uncertain',error='本地回执保存失败，禁止重发' WHERE id=?",
                    (identity,),
                )
                raise UnknownPublication("平台提交结果需人工核对，禁止重发", token) from None
            try:
                visibility = await self.adapter.verify_publication(row["target"], receipt)
                self.store.execute(
                    "UPDATE browsing_intents SET visibility=? WHERE id=?", (visibility["status"], identity)
                )
            except Exception:
                self.store.execute(
                    "UPDATE browsing_intents SET visibility='check_failed' WHERE id=?", (identity,)
                )
            return self.repo.decode(self.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))

    async def sync_notifications(self, row=None):
        return await self.notifications.sync(
            row["account_id"] if row else self.account,
            row["config"] if row else self.effective(),
            self.operations.policy()["enabled_at"],
            self.started_at,
        )

    async def _process_events(self, row, metrics):
        events = self.store.all(
            "SELECT * FROM browsing_events WHERE account_id=? AND status='received' ORDER BY timestamp,id LIMIT 2",
            (row["account_id"],),
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
        if thread["resource_type"] == "video":
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
        relationship = self.runtime.relationships.observe(
            row["character_id"],
            "bilibili",
            str(source["author_uid"]),
            event.get("sender_name", ""),
            canonical,
            source["text"],
            account_id=row["account_id"],
        )
        thread["confirmed_relationship"] = relationship
        reply = await self._response(row, material, thread, allowed, "reply_public", metrics)
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
            intent = self._intent(row, "private_handoff", target, reply, keys, reply.evidence_ids)
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
            intent = self._intent(row, "reply_public", target, reply, keys, reply.evidence_ids)
            if intent["auto_authorized"]:
                await self.publish(intent["id"], automatic=True)
        self.store.execute("UPDATE browsing_events SET status='drafted' WHERE id=?", (event_row["id"],))

    def tasks(self, status=None):
        where, args = (" WHERE t.status=?", (status,)) if status else ("", ())
        rows = self.store.all(
            "SELECT t.*,r.account_id,r.character_id,r.character_version FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id"
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
                    material = self.evidence.load(record["material_key"])
                    task["cache_status"] = "available"
                    task["metadata"] = material["metadata"]
                    task["evidence"] = [
                        segment
                        for segment in self._segments(material)
                        if segment["id"] in task.get("evidence_ids", [])
                    ]
                except EvidenceUnavailable:
                    task["cache_status"] = "missing"
                    task["evidence"] = []
                    task["subtitle_tracks"] = []
            result.append(task)
        return result

    def rounds(self):
        return [
            self.repo.decode(r)
            for r in self.store.all("SELECT * FROM browsing_rounds ORDER BY created_at DESC LIMIT 30")
        ]

    def intents(self):
        return [
            continuation_view(self.runtime, "browsing", self.repo.decode(r))
            for r in self.store.all("SELECT * FROM browsing_intents ORDER BY created_at DESC LIMIT 100")
        ]

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
            try:
                if self.effective()["discovery_enabled"]:
                    await self.run(automatic=True)
            except Exception as error:
                self.last_error = public_error(error)
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), self.policy()["interval_seconds"])
            except TimeoutError:
                pass

    async def _notifications_loop(self):
        while True:
            try:
                if self.effective()["mentions_enabled"] or self.effective()["replies_enabled"]:
                    if self.active:
                        row = self.repo.round(next(iter(self.active)))
                        token = read_round.set(row["id"])
                        try:
                            await self.sync_notifications(row)
                        finally:
                            read_round.reset(token)
                    else:
                        await self.run(automatic=True, notifications_only=True)
            except Exception as error:
                self.last_error = public_error(error)
            await asyncio.sleep(self.policy()["notification_interval_seconds"])
