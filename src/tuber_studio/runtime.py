"""Compose the business runtime and coordinate shared generation."""

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import psutil

from .agent.memory_organizer import MemoryOrganizer
from .agent_tools import build_default_tool_registry
from .api_providers import VisionService, create_llm
from .config import uses_broker_llm
from .context import ContextManager
from .context_delivery import comment_scope
from .daily_memory import DailyMemoryService
from .generation_meter import budgeted_call
from .live.scene import LiveScene
from .live.session import LiveSession
from .media_cache import MediaCache
from .memory import missing_slots
from .model_runtime.session import InferenceRuntime
from .models import OperationReply
from .operation_continuations import ContinuationCoordinator, content_hash
from .operation_generation import generate_with_recovery, hold_comment_body, reply_generation_binding
from .operation_identity import identity_messages, identity_snapshot
from .operation_text import operation_output_diagnostics
from .operation_turns import OperationTurns, operation_lane
from .operations.browsing.repository import BrowsingRepository
from .operations_control import OperationsControl
from .providers import VoiceService, public_error, reuse_turn_token_counts
from .relationships import Relationships
from .retention import INTERVAL_SECONDS, RetentionManager
from .runtime_modes import apply_mode_profile, profile_alignment_error
from .store import character_card, dump
from .video_vision_service import restore_admission


class Runtime:
    def __init__(self, settings, store, *, singing_provider=None, live_scene=None):
        self.settings, self.store = settings, store
        self.operations = OperationsControl(store)
        self.browsing_quotas = self.operations.browsing_quotas
        self.browsing_repository = BrowsingRepository(store)
        self.continuations = ContinuationCoordinator(
            store, self.operations, parent_repository=self.browsing_repository
        )
        self.relationships = Relationships(store)
        self.llm = create_llm(settings)
        self.voices = VoiceService(settings)
        self.vision = VisionService(
            settings.data.get("vision", {"kind": "disabled"}), settings.endpoint_key("vision")
        )
        self.daily_memory = DailyMemoryService(store)
        self.memory_organizer = MemoryOrganizer(self)
        self.live = LiveSession(self, singing_provider=singing_provider, live_scene=live_scene)
        self.tool_registry = build_default_tool_registry(
            daily_memory=self.daily_memory,
            game_livestream=self.live.game_livestream,
            draw_and_guess=self.live.draw_and_guess,
            live_session=self.live,
        )
        self.context = ContextManager(
            settings,
            store,
            self.llm,
            daily_memory=self.daily_memory,
            game_livestream=self.live.game_livestream,
            draw_and_guess=self.live.draw_and_guess,
            tool_registry=self.tool_registry,
        )
        self.retention = RetentionManager(settings, store)
        self.retention_worker = None
        self.retention_error = None
        self.generation_lock = asyncio.Lock()
        self.operation_turns = OperationTurns(self.generation_lock, self.operations_waiting)
        self._conversation_turns = {}
        self.error = None
        self.cache = MediaCache(self)
        self.live.media.cache = self.cache
        self.voices.cache = self.cache
        self.inference = InferenceRuntime(self)

    @property
    def character(self):
        return self.store.character()

    @property
    def llm(self):
        return self._llm

    @llm.setter
    def llm(self, provider):
        self._llm = provider
        provider.identity_resolver = self.identity_snapshot

    def identity_snapshot(self, character=None, account_id=None):
        from .bilibili_account import cached_account_actor

        account = getattr(self, "account", None)
        comments = account.comments if account else getattr(getattr(self, "browsing", None), "comments", None)
        credentials = comments.credentials if comments else {}
        if account_id is None and comments is not None:
            account_id = comments.display_account_id() or None
        return identity_snapshot(
            cached_account_actor(self.store, credentials, account_id), character or self.character
        )

    def identity_messages(self, messages, character=None, account_id=None):
        return identity_messages(messages, self.identity_snapshot(character, account_id))

    async def refresh_identity(self, character=None, account_id=None):
        """Share one remote profile read before sends, never certify stale cache."""
        account = getattr(self, "account", None)
        if account is None or not hasattr(getattr(account.comments, "adapter", None), "account_profile"):
            return self.identity_snapshot(character, account_id)
        from .bilibili_account import signature

        credential = signature(account.comments.credentials)
        pending = getattr(self, "_identity_refresh", None)
        if pending is None or pending[0] != credential or pending[1].done():
            pending = (credential, asyncio.create_task(account.refresh()))
            self._identity_refresh = pending
        observed = await asyncio.shield(pending[1])
        if (
            signature(account.comments.credentials) != credential
            or observed.get("state") != "logged_in"
            or observed.get("error")
            or not observed.get("profile")
        ):
            raise ValueError("发出前无法核验当前账号资料，请稍后重试")
        actual = str(observed["profile"]["uid"])
        if account_id is not None and actual != str(account_id):
            raise ValueError("发出前当前账号已变化，保留原账号的来源与草稿")
        return self.identity_snapshot(character, actual)

    @asynccontextmanager
    async def conversation_turn(self, scope):
        """Keep one discussion ordered while unrelated discussions can prepare.

        The yielded flag tells a caller whether its prepared context waited for
        another turn and therefore needs refreshing before generation.
        """
        entry = self._conversation_turns.get(scope)
        if entry is None:
            entry = {"lock": asyncio.Lock(), "users": 0}
            self._conversation_turns[scope] = entry
        entry["users"] += 1
        waited = entry["users"] > 1
        try:
            async with entry["lock"]:
                yield waited
        finally:
            entry["users"] -= 1
            if not entry["users"]:
                del self._conversation_turns[scope]

    @asynccontextmanager
    async def offline_generation(self, lane=None):
        lane = lane or operation_lane.get()
        if lane:
            await self.operation_turns.acquire(lane)
            try:
                yield
            finally:
                self.generation_lock.release()
            return
        while True:
            while self.operations_waiting():
                await asyncio.sleep(0.2)
            await self.generation_lock.acquire()
            if self.operations_waiting():
                self.generation_lock.release()
                continue
            try:
                yield
            finally:
                self.generation_lock.release()
            return

    def operations_waiting(self):
        profiles = self.settings.data.get("work_mode_profiles")
        node = self.inference.status
        if profiles and (
            node.get("mode") != "text"
            or node.get("state") in {"loading", "starting", "switching"}
            or (node.get("state") == "error" and uses_broker_llm(self.settings.data))
            or profile_alignment_error(self, node)
        ):
            return True
        return self.live.state in {"live", "paused"} and self.live.busy

    async def open(self):
        char = self.character
        self.daily_memory.check_and_record_downtime(char["id"], char["version"])
        await self.live.online_songs.open()
        if self.settings.data.get("work_mode_profiles") and self.inference.client:
            try:
                observed = await self.inference.refresh()
                async with self.generation_lock:
                    await apply_mode_profile(self, observed["mode"])
                if observed.get("provider_alignment_error"):
                    await self.inference.refresh()
            except Exception as error:
                self.inference.status = self.inference.status | {
                    "state": "error", "active": None, "error": public_error(error),
                }
        try:
            await self.llm.health()
            self.error = None
        except Exception as error:
            self.error = public_error(error)
        if getattr(self, "browsing", None):
            try:
                await restore_admission(self)
            except Exception:
                pass
        await self.cleanup_retention(automatic=True)
        self.retention_worker = asyncio.create_task(self._retention_loop())
        self.live.start_worker()
        if self.settings.data.get("inference_broker", {}).get("enabled"):
            self.inference.start_observer()

    async def close(self):
        if self.retention_worker:
            self.retention_worker.cancel()
            await asyncio.gather(self.retention_worker, return_exceptions=True)
        await self.control("stop")
        await self.live.online_songs.close()
        await self.inference.stop_observer()
        await self.live.stop_worker()
        await self.llm.close()
        await self.live.media.singing.close()
        await self.voices.close()
        await self.vision.close()
        await self.inference.close()
        self.daily_memory.update_active_timestamp()

    async def cleanup_retention(self, before=None, *, automatic=False):
        if automatic and self.retention.days is None:
            return None
        if self.live.busy or self.generation_lock.locked():
            if automatic:
                return None
            raise ValueError("正在生成或播放，请暂停并等待当前任务结束后清理")
        async with self.generation_lock:
            result = self.retention.cleanup(before, manual=not automatic)
            self.retention_error = None
            if self.live.last_played and not self.store.job(self.live.last_played["id"]):
                self.live.last_played = None
            return result

    async def _retention_loop(self):
        while True:
            await asyncio.sleep(60)
            self.daily_memory.update_active_timestamp()
            last = (self.retention.last_result or {}).get("completed_at", 0)
            if time.time() - last >= INTERVAL_SECONDS:
                try:
                    await self.cleanup_retention(automatic=True)
                except Exception as error:
                    self.retention_error = public_error(error)

    def status(self):
        memory = psutil.virtual_memory()
        return {
            "state": self.live.state,
            "muted": self.live.muted,
            "character": character_card(self.character),
            "session_id": self.live.session_id,
            "queue_length": len(self.live.queue),
            "overlay_connected": self.live.overlay is not None,
            "current_job": self.store.job(self.live.current_id) if self.live.current_id else None,
            "error": self.error,
            "last_played": self.live.last_played,
            "providers": {
                "llm": self.settings.data["llm"]["kind"],
                "llm_model": self.settings.data["llm"].get("model"),
                "live_thinking_budget_supported": (
                    getattr(self.llm, "live_thinking_budget_supported", None)
                    if self.settings.data["llm"]["kind"] == "llama_cpp" else False
                ),
                "llm_route": "node" if uses_broker_llm(self.settings.data) else "direct",
                "tts": self.settings.data["tts"]["kind"],
            },
            "resources": {
                "cpu_percent": psutil.cpu_percent(),
                "ram_used_mb": round(memory.used / 2**20),
                "ram_total_mb": round(memory.total / 2**20),
                "process_mb": round(psutil.Process().memory_info().rss / 2**20),
                "gpu": "未接入GPU采样；Jetson可用tegrastats查看",
            },
            "timings": self.live.timings,
            "context": self.context.status(),
            "inference": self.inference.status,
            "inference_node": self.inference.capabilities,
            "deployment": {
                "mode": self.settings.data.get("inference_broker", {}).get("deployment", "local"),
                "business_data": str(self.settings.runtime),
                "business_host": socket.gethostname(),
                "inference_url": self.settings.data.get("inference_broker", {}).get("base_url"),
            },
            "playback": self.live.playback,
            "operations_waiting": self.operations_waiting(),
        }

    @reuse_turn_token_counts
    async def generate_reply(self, scope, character, event, relationship=None, mode="live", *, context=None):
        """Generate once with source memory; lexical diagnostics never rewrite speech."""
        context = context or self.context
        evidence = context.memory.retrieve(scope, character, event["text"])
        channel = {"operator_chat": "operator", "live": "live", "comment_draft": "comment"}.get(mode)
        memory_scope = self.memory_organizer.scope() if channel and hasattr(self, "browsing") else None
        organized = []
        if memory_scope and memory_scope[1:] == (character["id"], character["version"]):
            resource = event.get("video_id") or event.get("bvid")
            thread = event.get("thread_id")
            if channel == "comment":
                account, _, resource, thread = comment_scope(scope) or (None, None, None, None)
                if account != memory_scope[0]:
                    memory_scope = None
            elif channel == "live" and event.get("account_id") and str(event["account_id"]) != memory_scope[0]:
                memory_scope = None
            if memory_scope:
                organized = self.daily_memory.recall_organized(
                    memory_scope, channel, event["text"], resource=resource, thread=thread,
                    participant=event.get("platform_user_id") or event.get("user_id"),
                )
        memory_refs = [self.daily_memory.organized_reference(row) for row in organized]
        extra = [{"role": "user", "kind": "context", "content": "与当前话题相关的现行记忆（偏好以此处最新版本为准）：" + dump([
            {"topic": row["headline"], "note": row["anecdote"]} for row in organized
        ])}] if organized else []
        messages = await context.prepare(scope, character, event, relationship, mode, evidence=evidence, extra=extra)
        generation_identity = self.identity_snapshot(character, event.get("account_id"))
        messages = identity_messages(messages, generation_identity)
        operation = mode in {"comment_draft", "direct_message"}
        generate = (
            getattr(self.llm, "generate_operation", self.llm.generate) if operation else self.llm.generate
        )
        continuation = None
        if operation:
            maximum = self.settings.data["llm"].get("operations_max_tokens", 8192)
            source_binding = reply_generation_binding(scope, character, event, mode)
            binding = dict(source_binding)
            binding["self_identity"] = generation_identity
            if memory_refs:
                binding["organized_memory"] = memory_refs
            profile_revision = self.relationships.event_profile_revision(character["id"], event)
            if profile_revision is not None:
                binding["profile_revision"] = profile_revision
            identity = event["event_id"] + ":" + content_hash(binding)

            async def invoke(packet, *, web_fetch=None):
                options = {"web_fetch": web_fetch} if web_fetch is not None else {}
                return await budgeted_call(
                    self.llm,
                    lambda: generate(packet, character, event["event_id"], event["text"], **options),
                    packet,
                    max_tokens=maximum,
                )

            def validate(value):
                reply = OperationReply.model_validate(
                    value.model_dump() if hasattr(value, "model_dump") else value
                )
                if any(reference != event["event_id"] for reference in reply.reply_to):
                    raise ValueError("模型引用了其他事件")
                return reply

            def guard():
                self.validate_memory_delivery({"organized_memory": memory_refs, "memory_scope": memory_scope,
                                               "memory_channel": channel})
                current = self.store.character()
                if (
                    (current["id"], current["version"], current.get("_edit_identity"))
                    != (character["id"], character["version"], character.get("_edit_identity"))
                    or reply_generation_binding(scope, character, event, mode) != source_binding
                    or self.relationships.event_profile_revision(character["id"], event) != profile_revision
                ):
                    raise ValueError("生成期间角色或原始事件已变化")

            def diagnose(value):
                if mode == "comment_draft":
                    hold_comment_body(value, value.spoken_text, recipient_name=event.get("display_name") or "")
                return (
                    "正文包含大量重复内容，保留原文待审"
                    if operation_output_diagnostics(value.spoken_text)["needs_review"]
                    else None
                )

            reply, continuation = await generate_with_recovery(
                self,
                channel="generation." + mode,
                identity=identity,
                binding=binding,
                character=character,
                messages=messages,
                generate=invoke,
                validate=validate,
                max_tokens=maximum,
                guard=guard,
                diagnose=diagnose,
                context_data={
                    "event": event,
                    "relationship": relationship,
                    "memory_evidence": evidence,
                    "history": context.history_for_model(scope, character),
                },
                allow_web_fetch=True,
            )
        else:
            scene = (
                self.live.live_scene(self.live.session_id, event)
                if mode == "live" and self.live.live_scene
                else None
            )
            reply = None
            if isinstance(scene, LiveScene) and scene.current(self.live.session_id):
                visual = self.browsing.visual_provider
                scene_reply = getattr(visual, "generate_live_reply", None)
                if scene.image and scene_reply:
                    reply = await scene_reply(scene, messages, self.settings.data["llm"], event["event_id"])
                if reply is None and scene.current(self.live.session_id):
                    messages = scene.annotate(messages)
            if reply is None:
                reply = (
                    await self.live.generate_text_reply(generate, messages, character, event)
                    if mode == "live"
                    else await generate(messages, character, event["event_id"], event["text"])
                )
        memory_audit = {"organized_memory": memory_refs, "memory_scope": memory_scope, "memory_channel": channel}
        self.validate_memory_delivery(memory_audit)
        missing = missing_slots(reply.spoken_text, evidence)
        supported = [
            slot
            for slot in evidence["required_slots"]
            if any(
                fact["slot"] == slot for source in evidence["evidence"] for fact in source.get("facts", [])
            )
        ]
        audit = {
            "generation_identity": generation_identity,
            "sources": evidence["evidence"],
            "required_slots": evidence["required_slots"],
            "supported_slots": supported,
            "method": "lexical_coverage",
            "status": "not_applicable" if not supported else "needs_review" if missing else "lexical_match",
            "semantic_verdict": None,
            "missing": missing,
            "retried": False,
            "warning": None,
            "memory_kind": "derived_reply" if evidence["evidence"] else "role_story",
        }
        if missing:
            labels = {"location": "地点", "person": "人物", "item": "物品", "quantity": "数量"}
            audit["warning"] = "待核对：" + "、".join(labels.get(slot, slot) for slot in missing)
        if memory_refs:
            audit.update(memory_audit)
        if operation:
            audit["output"] = operation_output_diagnostics(reply.spoken_text) | {
                "finish_reason": getattr(self.llm, "last_finish_reason", None),
                "usage": dict(getattr(self.llm, "last_usage", {})),
            }
            if audit["output"]["needs_review"]:
                audit["warning"] = "长正文含大量重复段落，保留原文待审"
            if continuation and continuation["feedback"].get("kind") == "body_integrity":
                audit["output"]["needs_review"] = True
                audit["warning"] = continuation["feedback"]["message"]
            audit["continuation"] = continuation
        return reply, audit

    def validate_memory_delivery(self, audit):
        if not audit or not audit.get("organized_memory"):
            return
        scope = tuple(audit["memory_scope"])
        if scope != self.memory_organizer.scope():
            raise ValueError("记忆所属账号或角色已切换，请重试")
        self.daily_memory.validate_organized(scope, audit["memory_channel"], audit["organized_memory"])

    async def control(self, action):
        return await self.live.control(action)

    async def submit(self, event, *, online_snapshot=None):
        return await self.live.submit(event, online_snapshot=online_snapshot)

    async def activate(self, character_id, version=None):
        return await self.live.activate(character_id, version)
