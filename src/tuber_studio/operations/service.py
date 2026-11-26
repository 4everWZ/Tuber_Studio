"""Unified operations service coordinating Discovery (Pipeline A), Inbox (Pipeline B), and shared kernel."""

import asyncio
import json
import re
import time

from ..core.store import dump
from ..core.turns import operation_checkpoint, operation_lane
from ..infra.inference.providers import public_error
from . import dispatch, lifecycle
from .channels.comments.timeline import comment_thread_context, ensure_comment_timeline
from .discovery import batches as discovery_batches
from .discovery import history as discovery_history
from .discovery import progress as discovery_progress
from .discovery import sources as discovery_sources
from .discovery.comments import cursor as discovery_comment_cursor
from .discovery.comments import reader as discovery_comment_reader
from .discovery.decisions import DiscoveryDecisions
from .discovery.publication import DiscoveryPublisher, background_publications_pending
from .discovery.repository import BrowsingBatchStore
from .discovery.runner import DiscoveryRunner
from .discovery.sources import BackgroundDiscovery
from .inbox import comment_events as inbox_comment_events
from .inbox import inbound as inbox_inbound
from .inbox.notifications import NotificationInbox
from .inbox.runner import INBOX_RUNNER_ACTIVE, InboxRunner
from .kernel.admission import CommentAdmission
from .kernel.context import read_round
from .kernel.evidence import BrowsingEvidence, EvidenceUnavailable
from .kernel.models import browsing_filter_reason
from .kernel.read_gate import PlatformReadGate
from .kernel.schedules import OperationSchedules
from .kernel.sends import OperationSends
from .kernel.telemetry import install_flow_ledger


class OperationsService:
    """Coordinates Pipeline A (Discovery), Pipeline B (Inbox), and shared kernel send/quota queues."""

    def __init__(
        self,
        settings,
        store,
        runtime,
        comments,
        messages,
        dynamics,
        *,
        visual=None,
        read_interval=0.25,
        bilibili_account=None,
    ):
        self.settings, self.store, self.runtime = settings, store, runtime
        self.comments, self.messages, self.dynamics = comments, messages, dynamics
        self.bilibili_account = bilibili_account or comments.account
        self.operations, self.repo = runtime.operations, runtime.browsing_repository
        if getattr(self.repo, "batches", None) is None:
            self.repo.batches = BrowsingBatchStore(store)
        self.adapter = self.bilibili_account.adapter
        self.read_gate = PlatformReadGate(read_interval=read_interval)
        self.evidence = BrowsingEvidence(runtime, self.adapter, self._read, visual)
        self.notifications = NotificationInbox(store, self._read, self.adapter)
        self.active = {}
        self.comment_active = {}
        self.event_active = set()
        self.event_changed = asyncio.Event()
        self.comment_windows = {}
        self.loop = self.poller = self.interaction_worker = None
        self.changed = asyncio.Event()
        self.interactions_changed = asyncio.Event()
        self.notification_schedule = asyncio.Event()
        self.closed = False
        self.started_at = time.time()
        self.heartbeats = set()
        self.blockers = []
        self.preflight_lock = asyncio.Lock()
        self.account = None
        self.last_error = None
        self.ready_signature = None
        self.discovery = BackgroundDiscovery(self)
        self.decisions = DiscoveryDecisions(self)
        self.publication = DiscoveryPublisher(self)
        self.comment_admission = CommentAdmission(self)
        self.sends = OperationSends(
            store,
            self.operations,
            self.operation_scope,
            self._send_automatic,
            waiting_for_image=self._send_waiting_for_image,
            settled=self._send_settled,
        )
        self.discovery_runner = DiscoveryRunner(self)
        self.inbox_runner = InboxRunner(self)
        self.schedules = OperationSchedules(self)
        self.control_generation = 0
        self.drain_task = None
        self.abort_task = None
        self.status_sequence = 0
        self.operations.on_reserved = self._send_reserved
        if hasattr(self.adapter, "request_gate"):
            self.adapter.request_gate = self._gate
        install_flow_ledger(store)
        ensure_comment_timeline(store)

    @property
    def read_interval(self):
        return self.read_gate.read_interval

    @read_interval.setter
    def read_interval(self, value):
        self.read_gate.read_interval = float(value)

    @property
    def read_lock(self):
        return self.read_gate.read_lock

    @property
    def last_read(self):
        return self.read_gate.last_read

    @last_read.setter
    def last_read(self, value):
        self.read_gate.last_read = float(value)

    @property
    def visual_provider(self):
        return self.evidence.visual

    def replace_visual_provider(self, visual):
        previous = self.visual_provider
        self.evidence.visual = visual
        return previous

    def has_intent(self, comment, character):
        return self.publication.has_intent(comment, character)

    def review(self, identity, action, text=None, **options):
        return self.publication.review(identity, action, text, **options)

    async def publish(self, identity, *, automatic=False):
        return await self.publication.publish(identity, automatic=automatic)

    async def resume_continuation(self, identity, continuation_id):
        return await self.publication.resume_continuation(identity, continuation_id)

    def policy(self):
        return lifecycle.policy(self)

    def configured(self):
        return lifecycle.configured(self)

    def effective(self):
        return self.operations.effective("browsing", self.configured())

    def discovery_enabled(self) -> bool:
        return bool(self.operations.policy()["enabled"] and self.effective().get("discovery_enabled", False))

    def inbox_enabled(self) -> bool:
        return bool(self.operations.policy()["enabled"] and self.effective().get("inbox_enabled", False))

    def has_interactions(self, config=None):
        return lifecycle.has_interactions(self, config)

    def permissions(self):
        return lifecycle.permissions(self)

    def model_signature(self):
        return lifecycle.model_signature(self)

    def control_state(self, control=None):
        return lifecycle.control_state(self, control)

    def has_uncertain_writes(self) -> bool:
        return lifecycle.has_uncertain_writes(self)

    def inbox_status(self) -> dict:
        return lifecycle.inbox_status(self)

    def status(self):
        return lifecycle.status(self)

    async def configure(self, data):
        return await lifecycle.configure(self, data)

    async def set_discovery_enabled(self, enabled: bool):
        return await lifecycle.set_discovery_enabled(self, enabled)

    async def set_inbox_enabled(self, enabled: bool):
        return await lifecycle.set_inbox_enabled(self, enabled)

    def credential_signature(self):
        return lifecycle.credential_signature(self)

    async def _current_account(self, expected=None):
        return await lifecycle.current_account(self, expected)

    async def handle_auth_loss(self, error=None):
        return await lifecycle.handle_auth_loss(self, error)

    async def check_browsing_model(self):
        return await lifecycle.check_browsing_model(self)

    async def probe_discovery(self, config):
        return await lifecycle.probe_discovery(self, config)

    async def preflight(self, *, for_automation=False):
        return await lifecycle.preflight(self, for_automation=for_automation)

    async def _check_preflight(self, *, for_automation=False):
        return await lifecycle.check_preflight(self, for_automation=for_automation)

    async def start_review_session(self, *, resume_session_id=None):
        return await lifecycle.start_review_session(self, resume_session_id=resume_session_id)

    async def set_enabled(self, enabled, *, guard=None):
        return await lifecycle.set_enabled(self, enabled, guard=guard)

    async def stop(self, *, emergency=False):
        return await lifecycle.stop(self, emergency=emergency)

    async def _drain(self, admitted):
        return await lifecycle.drain(self, admitted)

    def _finish_stop(self, *, emergency=False, stop_sends=True):
        return lifecycle.finish_stop(self, emergency=emergency, stop_sends=stop_sends)

    async def _abort_operations(self, *, emergency=False):
        return await lifecycle.abort_operations(self, emergency=emergency)

    def _uses_adapter_gate(self):
        return bool(getattr(self.adapter, "_gate_managed", False))

    async def _gate(self):
        return await self.read_gate.gate(
            reserve_budget=lambda identity: self.repo.reserve_budget(identity, reads=1),
            quota_check=self._comment_quota_check,
            account_id=self.account or "",
        )

    def _comment_quota_check(self):
        return self.comment_window_due()

    async def _read(self, method, *args, retries=0, pipeline=None, reconciliation=False, **kwargs):
        account_id = str(self.account or "")
        return await self.read_gate.read(
            method,
            *args,
            retries=retries,
            gate_fn=self._gate,
            use_adapter_gate=self._uses_adapter_gate(),
            pipeline=pipeline,
            account_id=account_id,
            reconciliation=reconciliation,
            **kwargs,
        )

    async def _read_proactively(self, method, *args, **kwargs):
        return await self.read_gate.read_proactively(method, *args, **kwargs)

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
        return await discovery_progress.prepare_evidence(
            self,
            video,
            config,
            force=force,
            task=task,
            deadline=deadline,
            visual_source_ids=visual_source_ids,
        )

    async def queue_full_video(self, request_id, character, selection):
        return await discovery_progress.queue_full_video(self, request_id, character, selection)

    def full_video_status(self, row):
        return discovery_progress.full_video_status(self, row)

    async def update_full_video(self, request_id, *, resume):
        return await discovery_progress.update_full_video(self, request_id, resume=resume)

    async def _start_full_video(self):
        return await discovery_progress.start_full_video(self)

    def _start_round(self, row, notifications_only):
        identity = row["id"]
        task = self.active[identity] = asyncio.create_task(self._execute(row, notifications_only))

        def finished(_):
            self.active.pop(identity, None)
            if self.store.one(
                "SELECT 1 FROM browsing_rounds WHERE status='waiting' "
                "AND account_id=? AND character_id=? AND character_version=? LIMIT 1",
                self.discovery.history_scope(),
            ):
                self.changed.set()

        task.add_done_callback(finished)

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
        local_interactions=False,
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
                request_id,
                self.runtime.character,
                selection,
                self.bilibili_account.display_account_id(),
            ):
                return {"id": existing["id"], "status": existing["status"], "reused": True, "preview": True}
        if automatic and not self.operations.accepting_work:
            raise ValueError("请先启动自主运营")
        if interaction_only:
            if not automatic or not self.operations.policy()["enabled"] or not self.account:
                raise ValueError("互动轮次尚未授权")
            config = self.effective() | {
                "interaction_only": True,
                "local_interactions": local_interactions,
                **({"inbox_only": True} if INBOX_RUNNER_ACTIVE.get() else {}),
            }
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
            and time.time() < self.store.get("browsing_next:" + self.account + ":" + self.runtime.character["id"], 0)
        ):
            return {"status": "deferred", "reason": "OC有界延后计划尚未到期"}
        if automatic and not self.operations.accepting_work:
            raise ValueError("自主运营已停止接纳新轮次")
        if config.get("autonomous_batch"):
            character = self.runtime.character
            scope = {
                "account_id": self.account,
                "character_id": character["id"],
                "character_version": character["version"],
                "config": config,
            }
            if not config.get("review_session_id") and (due := self.comment_window_due()):
                self.backlog_status()
                return {"status": "deferred", "reason": "评论小时额度已用完", "next_at": due}
            if not self.repo.batches.has_open_round(scope):
                if self._background_publications_pending(scope):
                    return {"status": "deferred", "reason": "上一轮配图或自动发送尚未结清"}
                if not self.page_capacity():
                    return {"status": "deferred", "reason": "评论积压达到高水位"}
        row, created = self.repo.create_round(
            self.account,
            self.runtime.character,
            config,
            self.operations.policy()["epoch"],
            preview,
            request_id=request_id,
        )
        if automatic and not notifications_only and not interaction_only:
            self.store.set("browsing_discovery_pending:" + self.account, True)
        if created:
            self._start_round(row, notifications_only)
        return {
            "id": row["id"],
            "status": row["status"],
            "reused": not created,
            "preview": bool(row["preview"]),
        }

    def send_permissions(self):
        return dispatch.send_permissions(self)

    def waiting_round(self):
        return dispatch.waiting_round(self)

    def operation_scope(self):
        return dispatch.operation_scope(self)

    def backlog_status(self, *, persist=True):
        return dispatch.backlog_status(self, persist=persist)

    def comment_window_due(self, *, now=None):
        return dispatch.comment_window_due(self, now=now)

    def _ready_approved_comments(self, *, now=None):
        return dispatch.ready_approved_comments(self, now=now)

    def _send_reserved(self):
        return dispatch.send_reserved(self)

    def _snapshot_comment_window(self):
        return dispatch.snapshot_comment_window(self)

    def _send_settled(self):
        return dispatch.send_settled(self)

    def review_counts(self, account, character):
        return dispatch.review_counts(self, account, character)

    def review_capacity(self, channel, row=None):
        return dispatch.review_capacity(self, channel, row)

    def page_capacity(self):
        return dispatch.page_capacity(self)

    def _background_publications_pending(self, scope):
        return background_publications_pending(self, scope)

    def _send_draft(self, item):
        return dispatch.send_draft(self, item)

    def _send_waiting_for_image(self, item):
        return dispatch.send_waiting_for_image(self, item)

    def queue_publication(self, identity, *, direct=None):
        return dispatch.queue_publication(self, identity, direct=direct)

    async def _send_automatic(self, item):
        return await dispatch.send_automatic(self, item)

    async def _send_approved(self, item):
        return await dispatch.send_approved(self, item)

    def _resume_reactions(self):
        return dispatch.resume_reactions(self)

    def resume_publication(self, row, intent, target):
        return dispatch.resume_publication(self, row, intent, target)

    def check_step(self, row):
        if self.operations.draining and not row["preview"]:
            raise asyncio.CancelledError("自主运营收尾，保留当前进度")

    def _check_round(self, row):
        current = self.runtime.character
        if current["id"] != row["character_id"] or current["version"] != row["character_version"]:
            raise ValueError("本轮角色版本已变化")
        if str(row["account_id"]) != str(self.account):
            raise ValueError("本轮账号身份已变化")
        if row["config"].get("full_video"):
            if row["operator_credential"] != self.credential_signature():
                raise ValueError("本轮账号凭证已变化")
        elif self.credential_signature() != self.ready_signature:
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
                    or row["config"].get("revisit_only")
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
        return await discovery_batches.execute(self, row, notifications_only)

    async def _sample_resources(self, metrics):
        return await discovery_batches.sample_resources(self, metrics)

    async def _discover(self, row):
        return await discovery_sources.discover(self, row)

    async def _discover_candidates(self, row, retries):
        return await discovery_sources.discover_candidates(self, row, retries)

    async def _select_batch_candidates(self, row, retries, candidates):
        return await discovery_sources.select_batch_candidates(self, row, retries, candidates)

    async def _plan_batch(self, row, candidates):
        return await discovery_sources.plan_batch(self, row, candidates)

    def _candidate_view(self, row, item):
        return discovery_sources.candidate_view(self, row, item)

    async def video_comment_context(self, row, bvid):
        if row.get("config", {}).get("autonomous_batch"):
            return await discovery_comment_reader.round_comment_page(self, row, bvid)
        return await discovery_comment_reader.video_comment_context(self, row, bvid)

    def commit_comment_context(self, context):
        return discovery_comment_reader.commit_comment_context(self, context)

    async def _resume_batch_publications(self, row, bvid):
        return await discovery_batches.resume_batch_publications(self, row, bvid)

    async def _process_discovery_video(self, row, candidate, metrics, semaphore):
        return await discovery_batches.process_discovery_video(self, row, candidate, metrics, semaphore)

    async def _run_discovery_video(self, row, candidate, metrics):
        return await discovery_batches.run_discovery_video(self, row, candidate, metrics)

    @staticmethod
    def filter_reason(item, config):
        return browsing_filter_reason(item, config)

    async def _prepare_task(self, identity, row, semaphore, metrics):
        return await discovery_progress.prepare_task(self, identity, row, semaphore, metrics)

    async def _prepare_attempt(self, identity, row, semaphore, metrics, owner):
        return await discovery_progress.prepare_attempt(self, identity, row, semaphore, metrics, owner)

    async def _heartbeat(self, identity, owner, generation):
        return await discovery_progress.heartbeat(self, identity, owner, generation)

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
        if not evidence or evidence["account_id"] != self.bilibili_account.display_account_id():
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
                fresh = await self._read(self.adapter.read_public_thread, event, reconciliation=True)
                if fresh.get("public_text") != original.get("public_text"):
                    raise ValueError("公开移交原文已变化或撤销")
            elif hasattr(self.adapter, "read_video_thread"):
                fresh = await self._read(self.adapter.read_video_thread, target, reconciliation=True)
            else:
                fresh = {"source": await self._read(self.adapter.read_video_reply, target, reconciliation=True)}
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
                items,
                target,
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

    async def _refresh_material_keys(self, keys, versions, *, allow_expired=False):
        result = []
        for key, version in zip(keys, versions, strict=True):
            try:
                material = self.evidence.load(key, allow_expired=allow_expired)
            except EvidenceUnavailable:
                if allow_expired:
                    raise
                source = self.store.one("SELECT bvid FROM browsing_materials WHERE id=?", (key,))
                if not source:
                    raise ValueError("原始来源标识已移除，不能恢复旧证据") from None
                key, material = await self._prepare_evidence(source["bvid"], self.effective(), force=True)
            else:
                old_meta = material["metadata"]
                fresh_meta = await self._read(self.adapter.video_metadata, old_meta["bvid"], reconciliation=True)
                if str(fresh_meta.get("aid")) != str(old_meta.get("aid")) or fresh_meta.get("description") != old_meta.get(
                    "description"
                ):
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
                "SELECT video_evidence FROM dm_drafts WHERE account_id=? AND platform_user_id=? "
                "AND character_id=? AND id<? AND video_evidence IS NOT NULL ORDER BY id DESC LIMIT 1",
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
        self._resume_reactions()
        discovery_paused = self.backlog_status()["discovery_paused"]
        if not row["config"].get("local_interactions"):
            await inbox_comment_events.run_interactions(self, row, metrics)
        if row["config"].get("inbox_only"):
            claimed = await self._process_events(row, metrics, inbox_only=True)
            if claimed and self.operations.accepting_work:
                self.interactions_changed.set()
            return
        claimed = await self._process_events(row, metrics)
        if (
            self.discovery.enabled
            and self.page_capacity()
            and (row["config"]["reply_enabled"] or row["config"].get("comment_like_enabled"))
        ):
            if not discovery_comment_cursor.resume_comment_page(
                self, row
            ) and not await discovery_comment_cursor.read_old_comment_page(self, row):
                await discovery_batches.process_comment_visits(self, row, metrics)
        elif (
            self.discovery.enabled
            and not self._ready_approved_comments()
            and not self.comment_window_due()
        ):
            discovery_comment_cursor.resume_comment_page(self, row)
        claimed += await self._process_events(row, metrics)
        if claimed and self.operations.accepting_work:
            self.interactions_changed.set()
        if discovery_paused and not self.backlog_status()["discovery_paused"]:
            self.changed.set()

    async def run_discovery_revisits_once(self) -> int:
        """Execute one Pipeline A comment revisit, old-page cursor, and `comment_visit` evaluation pass."""
        if (
            not self.operations.accepting_work
            or not self.discovery.enabled
            or self.operations.draining
            or self.discovery_tasks()
            or self.has_uncertain_writes()
        ):
            return 0
        if self.ready_signature != self.credential_signature() or not self.account:
            await self.preflight(for_automation=True)
        if self.blockers or not self.account:
            return 0
        config = self.effective()
        if not (config.get("reply_enabled") or config.get("comment_like_enabled")):
            return 0
        account, character_id, character_version = self.operation_scope()
        review = config.get("review_session_id", "")
        now = time.time()
        probe = {
            "account_id": account,
            "character_id": character_id,
            "character_version": character_version,
            "preview": False,
            "config": config,
        }
        can_pages = self.page_capacity()
        has_handoff = bool(
            self.store.one(
                "SELECT 1 FROM browsing_comment_page_handoffs "
                "WHERE account_id=? AND character_id=? AND review_session=? LIMIT 1",
                (account, character_id, review),
            )
        )
        has_events = bool(
            self.store.one(
                "SELECT 1 FROM browsing_events WHERE account_id=? AND kind='comment_visit' "
                "AND COALESCE(CASE WHEN json_valid(error) THEN json_extract(error,'$.next_at') END,0)<=? "
                "AND status IN ('received','source_verified','context_ready','reply_deferred') LIMIT 1",
                (account, now),
            )
        )
        has_pages = can_pages and bool(
            self.store.one(
                "SELECT 1 FROM browsing_comment_revisits "
                "WHERE account_id=? AND character_id=? AND character_version=? AND review_session=? "
                "AND page IS NOT NULL AND next_at<=? LIMIT 1",
                (account, character_id, character_version, review, now),
            )
            or self.repo.batches.pending_comment_visits(probe)
        )
        if not (
            has_events
            or has_pages
            or (has_handoff and (can_pages or (not self._ready_approved_comments() and not self.comment_window_due())))
        ):
            return 0
        round_cfg = (
            config
            | dict.fromkeys(("max_reads", "max_model_calls", "max_input_tokens", "max_output_tokens"), 0)
            | {"interaction_only": False, "revisit_only": True}
        )
        row, created = self.repo.create_round(
            self.account,
            self.runtime.character,
            round_cfg,
            self.operations.policy()["epoch"],
            False,
            source_pipeline="discovery",
        )
        if not created:
            return 0
        metrics = {
            "workers": round_cfg["workers"],
            "prepared": 0,
            "failures": 0,
            "viewed": 0,
            "lock_wait_seconds": 0,
            "preparation_seconds": 0,
            "cache_hits_before": self.evidence.hits,
            "merged_before": self.evidence.merged,
        }
        status, round_error = "completed", None
        async with self.operations.automatic_work() as admitted:
            if not admitted:
                self.repo.finish_round(row["id"], "paused", metrics)
                return 0
            self.store.execute(
                "UPDATE browsing_rounds SET status='running',updated_at=? WHERE id=?", (time.time(), row["id"])
            )
            row = self.repo.round(row["id"])
            lane = operation_lane.set("background")
            token = read_round.set(row["id"])
            checkpoint = operation_checkpoint.set(lambda: (self.check_step(row), self._check_round(row)))
            try:
                discovery_paused = self.backlog_status()["discovery_paused"]
                claimed = await self._process_events(row, metrics, revisit_only=True)
                if self.page_capacity():
                    if not discovery_comment_cursor.resume_comment_page(
                        self, row
                    ) and not await discovery_comment_cursor.read_old_comment_page(self, row):
                        await discovery_batches.process_comment_visits(self, row, metrics)
                elif not self._ready_approved_comments() and not self.comment_window_due():
                    discovery_comment_cursor.resume_comment_page(self, row)
                claimed += await self._process_events(row, metrics, revisit_only=True)
                if discovery_paused and not self.backlog_status()["discovery_paused"]:
                    self.changed.set()
                return claimed
            except asyncio.CancelledError:
                status = "paused"
                return 0
            except Exception as error:
                status, round_error = "failed", public_error(error)
                raise
            finally:
                self.repo.finish_round(row["id"], status, metrics, round_error)
                operation_checkpoint.reset(checkpoint)
                read_round.reset(token)
                operation_lane.reset(lane)

    async def _collect_own_comments(self, row, metrics):
        return await inbox_comment_events.collect_own_comments(self, row, metrics)

    async def _process_events(self, row, metrics, *, own_only=False, batch_only=False, inbox_only=False, revisit_only=False):
        return await inbox_comment_events.process_events(
            self, row, metrics, own_only=own_only, batch_only=batch_only, inbox_only=inbox_only, revisit_only=revisit_only
        )

    async def _event(self, event_row, row, metrics):
        return await inbox_comment_events.event(self, event_row, row, metrics)

    async def _event_unlocked(self, event_row, row, metrics, event, thread):
        return await inbox_comment_events.event_unlocked(self, event_row, row, metrics, event, thread)

    def tasks(self, status=None, *, round_id=None, task_id=None):
        return discovery_history.tasks(self, status, round_id=round_id, task_id=task_id)

    def rounds(self):
        return discovery_history.rounds(self)

    def intents(self):
        return discovery_history.intents(self)

    def public_comment_intent(self, identity):
        return discovery_history.public_comment_intent(self, identity)

    def inbox(self):
        return inbox_inbound.inbox(self)

    async def reread(self, task_id):
        task = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("视频任务不存在")
        return await self.run(
            preview=True,
            video=task["bvid"],
            comment_visit=json.loads(task["payload"]).get("task_kind") == "comment_visit",
        )

    async def open(self):
        return await lifecycle.open_service(self)

    async def close(self):
        return await lifecycle.close_service(self)


BrowsingService = OperationsService
