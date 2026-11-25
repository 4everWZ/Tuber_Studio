"""Compose the business runtime and coordinate shared generation."""

import asyncio
import inspect
import json
import socket
import time
from contextlib import asynccontextmanager

import psutil

from ..agent.heartbeat import HeartbeatService
from ..agent.memory_organizer import MemoryOrganizer
from ..agent.skills import RemotionVideoSkill
from ..core.identity import identity_messages, identity_snapshot, operating_account_facts
from ..core.meter import budgeted_call
from ..core.models import OperationReply, Reply, canonical_voice_id, content_hash
from ..core.retention import INTERVAL_SECONDS, RetentionManager
from ..core.store import character_card, dump
from ..core.text import operation_output_diagnostics
from ..core.turns import operation_lane, operation_ready_at, operation_ticket
from ..infra.inference.api_providers import APIProvider, create_chat_llm, create_llm, create_operation_llm
from ..infra.inference.providers import VoiceService, public_error
from ..infra.inference.video_vision_service import restore_admission
from ..infra.media.cache import MediaCache
from ..live.facade import HANDOFF_PLATFORM, LiveScene, LiveSession, create_live_facade
from ..operations.channels.attachments.images import OperationImages
from ..operations.facade import browsing_visual_provider, business_timezone
from ..operations.kernel.continuations import ContinuationCoordinator
from ..operations.kernel.control import OperationsControl
from ..operations.kernel.generation import generate_with_recovery, hold_comment_body, reply_generation_binding
from ..operations.kernel.repository import BrowsingRepository
from ..operations.kernel.turns import OperationTurns
from ..persona.assets.meme_harvester import MemeHarvester
from ..persona.characters.front_persona import front_identity
from ..persona.characters.pets import PetAppearances
from ..persona.context.delivery import comment_scope
from ..persona.context.manager import ContextManager
from ..persona.context.tools import build_default_tool_registry
from ..persona.memory.core_memory import missing_slots
from ..persona.memory.daily import DailyMemoryService, MemoryChanged
from ..persona.memory.relationships import Relationships
from ..persona.memory.unified import UnifiedMemoryStore


def _comment_actor_formatter(event):
    from ..operations.kernel import inputs as kernel_inputs

    bound = kernel_inputs.bind_response_actors({"thread": event.get("comment_context", {})}, event["self_actor"])
    compact_fn = getattr(kernel_inputs, "compact_actor_context", None)
    if compact_fn is not None and "current_text_ref" in inspect.signature(compact_fn).parameters:
        actors = compact_fn(bound, current_text_ref="当前观众消息.text")
    else:
        actors = bound
    return actors, getattr(kernel_inputs, "RESPONSE_ACTOR_INSTRUCTION", "")


class Runtime:
    def __init__(self, settings, store, *, singing_provider=None, live_scene=None):
        self.settings, self.store = settings, store
        self.operation_images = OperationImages(settings, store)
        self.pets = PetAppearances(settings, store)
        self.operations = OperationsControl(store)
        self.browsing_quotas = self.operations.browsing_quotas
        self.browsing_repository = BrowsingRepository(store)
        self.continuations = ContinuationCoordinator(
            store, self.operations, parent_repository=self.browsing_repository
        )
        self.relationships = Relationships(store)
        self._chat_llm_explicit = False
        self._default_chat_llm_tracking = False
        self.llm = create_llm(settings)
        self.chat_llm = create_chat_llm(settings)
        self.operation_llm = create_operation_llm(settings)
        self._default_llm = self.llm
        self._default_llm_client = getattr(self.llm, "client", None)
        self._default_llm_uses_model_card = getattr(self.llm, "uses_model_card", None)
        self._default_chat_llm = self.chat_llm
        self._default_chat_client = getattr(self.chat_llm, "client", None)
        self._default_chat_uses_model_card = getattr(self.chat_llm, "uses_model_card", None)
        self._default_chat_llm_tracking = True
        self.voices = VoiceService(settings)
        self.daily_memory = DailyMemoryService(store)
        self.unified_memory = UnifiedMemoryStore(store)
        # Startup auto-migration of legacy daily_highlights/grounded_memory rows is disabled
        # so Layer 1 Saved Memories stay distilled and legacy tables are queried via Layer 2 recall.
        self.memory_organizer = MemoryOrganizer(self)
        self.heartbeat = HeartbeatService(self, store)
        self.remotion_skill = RemotionVideoSkill(self, store)
        self.live = LiveSession(
            self,
            singing_provider=singing_provider,
            live_scene=live_scene,
            timezone_provider=lambda: business_timezone(getattr(self, "browsing", None)),
        )
        self.live_facade = create_live_facade(self.live)
        self.visual_provider = None
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
            actor_formatter=_comment_actor_formatter,
        )
        self.operation_context = ContextManager(
            settings, store, self.operation_llm, daily_memory=self.daily_memory,
            game_livestream=self.live.game_livestream, draw_and_guess=self.live.draw_and_guess,
            tool_registry=self.tool_registry,
            actor_formatter=_comment_actor_formatter,
        )
        self.retention = RetentionManager(settings, store)
        self.retention_worker = None
        self.connection_probe = None
        self.admission_restore = None
        self.retention_error = None
        self.generation_lock = asyncio.Lock()
        self.operation_generation_lock = asyncio.Lock()
        self.operation_turns = OperationTurns(self.operation_generation_lock, lambda: False)
        self._conversation_turns = {}
        self.error = None
        self.cache = MediaCache(
            settings,
            store,
            music_ids_provider=self._protected_music_ids,
            active_speech_provider=self._protected_speech_files,
            browsing_protected_provider=self.browsing_repository.protected_materials,
            video_protected_provider=self._protected_video_jobs,
            song_blob_resolver=self.live.media.blob,
        )
        self.live.media.cache = self.cache
        self.voices.cache = self.cache
        self.meme_harvester = MemeHarvester(
            settings,
            store,
            catalog=self.pets.catalog,
            cache=self.cache,
            runtime=self,
        )
        self.cache.meme_inbox_evicted_callback = self.meme_harvester.on_cache_evicted

    @property
    def character(self):
        return self.store.persona_defaults(self.store.character())

    @property
    def llm(self):
        return self._llm

    @llm.setter
    def llm(self, provider):
        self._llm = provider
        provider.identity_resolver = self.identity_snapshot

    @property
    def chat_llm(self):
        return self._chat_llm

    @chat_llm.setter
    def chat_llm(self, provider):
        self._chat_llm = provider
        provider.identity_resolver = self.identity_snapshot
        if getattr(self, "_default_chat_llm_tracking", True):
            self._chat_llm_explicit = True

    def replace_providers(self, *, llm=None, chat_llm=None, operation_llm=None):
        self._default_chat_llm_tracking = False
        try:
            if llm is not None:
                self.llm = llm
                self._default_llm = llm
                self._default_llm_client = getattr(llm, "client", None)
                self._default_llm_uses_model_card = getattr(llm, "uses_model_card", None)
            if chat_llm is not None:
                self.chat_llm = chat_llm
                self._default_chat_llm = chat_llm
                self._default_chat_client = getattr(chat_llm, "client", None)
                self._default_chat_uses_model_card = getattr(chat_llm, "uses_model_card", None)
                self._chat_llm_explicit = False
            if operation_llm is not None:
                self.operation_llm = operation_llm
        finally:
            self._default_chat_llm_tracking = True

    def foreground_chat_llm(self):
        patched = (
            "generate",
            "generate_json",
            "generate_assistant_json",
            "generate_operator_json",
            "generate_text_stream",
            "capacity_window",
        )
        chat_dict = getattr(self.chat_llm, "__dict__", {})
        if (
            self._chat_llm_explicit
            or self.chat_llm is not self._default_chat_llm
            or getattr(self.chat_llm, "client", None) is not getattr(self, "_default_chat_client", None)
            or getattr(self.chat_llm, "uses_model_card", None) != getattr(self, "_default_chat_uses_model_card", None)
            or any(key in chat_dict for key in patched)
        ):
            return self.chat_llm
        llm_dict = getattr(self.llm, "__dict__", {})
        if (
            not isinstance(self.llm, APIProvider)
            or self.llm is not self._default_llm
            or getattr(self.llm, "client", None) is not getattr(self, "_default_llm_client", None)
            or getattr(self.llm, "uses_model_card", None) != getattr(self, "_default_llm_uses_model_card", None)
            or any(key in llm_dict for key in patched)
        ):
            return self.llm
        return self.chat_llm

    @property
    def operation_llm(self):
        return self._operation_llm

    @operation_llm.setter
    def operation_llm(self, provider):
        self._operation_llm = provider
        provider.identity_resolver = self.identity_snapshot
        if hasattr(self, "operation_context"):
            self.operation_context.provider = provider

    def _protected_music_ids(self):
        live = self.live
        identities = {
            song[field]
            for song in live.media.songs()
            for field in ("audio", "vocal", "accompaniment")
            if song.get(field)
        }
        active = ([live.current_id] if live.current_id else []) + live.queue
        for identity in active:
            job = self.store.job(identity)
            if job and job.get("media"):
                song = json.loads(job["media"])["song"]
                identities.update(
                    song[field] for field in ("audio", "vocal", "accompaniment") if song.get(field)
                )
        for task in live.online_songs.tasks.values():
            if task.done() and not task.cancelled() and task.exception() is None:
                identities.add(task.result()["song"]["audio"])
        return identities

    def _protected_speech_files(self):
        speech = set()
        for identity in (
            [self.live.current_id] if self.live.current_id else []
        ) + self.live.queue:
            job = self.store.job(identity)
            if job and job.get("audio_ref"):
                speech.add(job["audio_ref"].rsplit("/", 1)[-1])
        return speech

    def _protected_video_jobs(self):
        protected = set(getattr(getattr(self, "remotion_skill", None), "active_job_ids", ()))
        if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='remotion_video_jobs'"):
            rows = self.store.all(
                "SELECT id FROM remotion_video_jobs WHERE status IN ('rendering', 'publishing')"
            )
            protected.update(str(row["id"]) for row in rows if row.get("id"))
        return protected

    def identity_snapshot(self, character=None, account_id=None):
        from ..infra.platforms.bilibili.account import cached_account_actor

        account = getattr(self, "account", None) or getattr(
            getattr(self, "browsing", None), "bilibili_account", None
        )
        credentials = account.credentials if account else {}
        if account_id is None and account is not None:
            account_id = account.display_account_id() or None
        return identity_snapshot(
            cached_account_actor(self.store, credentials, account_id), character or self.character
        )

    def identity_messages(self, messages, character=None, account_id=None):
        return identity_messages(messages, self.identity_snapshot(character, account_id))

    async def refresh_identity(self, character=None, account_id=None):
        """Share one remote profile read before sends, never certify stale cache."""
        account = getattr(self, "account", None)
        if account is None or not hasattr(getattr(account, "adapter", None), "account_profile"):
            return self.identity_snapshot(character, account_id)
        from ..infra.platforms.bilibili.account import signature

        credential = signature(account.credentials)
        pending = getattr(self, "_identity_refresh", None)
        if pending is None or pending[0] != credential or pending[1].done():
            pending = (credential, asyncio.create_task(account.refresh()))
            self._identity_refresh = pending
        observed = await asyncio.shield(pending[1])
        if (
            signature(account.credentials) != credential
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
        lane = lane or operation_lane.get() or "background"
        if lane == "chat":
            raise ValueError("前台聊天须使用前台推理入口")
        ticket = await self.operation_turns.acquire(lane, operation_ready_at.get())
        token = operation_ticket.set((self.operation_turns, ticket))
        try:
            yield
        finally:
            operation_ticket.reset(token)
            self.operation_turns.release(ticket)

    @asynccontextmanager
    async def chat_routing_generation(self):
        ticket = await self.operation_turns.acquire("chat")
        token = operation_ticket.set((self.operation_turns, ticket))
        try:
            yield
        finally:
            operation_ticket.reset(token)
            self.operation_turns.release(ticket)

    @asynccontextmanager
    async def chat_text_generation(self):
        ticket = await self.operation_turns.acquire("chat")
        token = operation_ticket.set((self.operation_turns, ticket))
        try:
            async with self.operator_generation():
                yield
        finally:
            operation_ticket.reset(token)
            self.operation_turns.release(ticket)

    def operator_generation(self):
        return self.generation_lock

    def generation_active(self):
        return self.generation_lock.locked() or self.operation_generation_lock.locked()

    def operations_waiting(self):
        return self.live.state in {"live", "paused"} and self.live.busy

    async def open(self):
        self.operation_images.start()
        char = self.character
        self.daily_memory.check_and_record_downtime(char["id"], char["version"])
        await self.live.online_songs.open()
        self.schedule_connection_probe()
        if getattr(self, "browsing", None):
            self.admission_restore = asyncio.create_task(self._restore_visual_admission())
            self.visibility_restore = asyncio.create_task(self._recheck_pending_visibility())
        await self.cleanup_retention(automatic=True)
        self.retention_worker = asyncio.create_task(self._retention_loop())
        self.live.start_worker()
        self.heartbeat.start(initial_delay=self.heartbeat.policy().tick_interval_seconds)

    @staticmethod
    async def _probe_foreground(provider):
        try:
            result = await provider.health()
        except Exception as error:
            return {"available": False, "error": public_error(error)}
        return {"available": result["available"], "context_tokens": result.get("server_context_tokens")}

    def schedule_connection_probe(self):
        if self.connection_probe and not self.connection_probe.done():
            self.connection_probe.cancel()
        self.connection_probe = asyncio.create_task(self._probe_foreground(self.llm))

    async def check_foreground_connection(self):
        if self.connection_probe is None or self.connection_probe.done():
            self.schedule_connection_probe()
        while True:
            probe = self.connection_probe
            try:
                result = await asyncio.shield(probe)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() or probe is self.connection_probe:
                    raise
                continue
            if probe is self.connection_probe:
                return result

    async def _restore_visual_admission(self):
        try:
            await restore_admission(self)
        except Exception:
            pass

    async def _recheck_pending_visibility(self):
        try:
            browsing = getattr(self, "browsing", None)
            if browsing is not None and hasattr(getattr(browsing, "publication", None), "recheck_pending_visibility"):
                await browsing.publication.recheck_pending_visibility()
        except Exception:
            pass

    async def close(self):
        await self.heartbeat.close()
        await self.operation_images.close()
        tasks = [
            task
            for task in (self.connection_probe, self.admission_restore, getattr(self, "visibility_restore", None))
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.retention_worker:
            self.retention_worker.cancel()
            await asyncio.gather(self.retention_worker, return_exceptions=True)
        await self.control("stop")
        await self.live.online_songs.close()
        await self.live.stop_worker()
        await self.llm.close()
        await self.chat_llm.close()
        await self.operation_llm.close()
        await self.live.media.singing.close()
        await self.voices.close()
        self.daily_memory.update_active_timestamp()

    async def cleanup_retention(self, before=None, *, automatic=False):
        if automatic and self.retention.days is None:
            return None
        if self.live.busy or self.generation_active():
            if automatic:
                return None
            raise ValueError("正在生成或播放，请暂停并等待当前任务结束后清理")
        async with self.generation_lock, self.operation_generation_lock:
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
        character = self.character
        voice_id = canonical_voice_id(character["voice_profile"])
        automation = None
        if (browsing := getattr(self, "browsing", None)) and getattr(browsing, "operations", None) is not None:
            control = browsing.operations.policy()
            effective = browsing.effective()
            account_id = browsing.account or control["account_id"]
            cooldown_until = browsing.sends.comments_cooldown_until(account_id)
            automation = {
                "state": browsing.control_state(control),
                "enabled": bool(control["enabled"]),
                "emergency": bool(control["emergency"]),
                "epoch": int(control["epoch"]),
                "inbox_enabled": bool(control["enabled"] and effective.get("inbox_enabled", False)),
                "discovery_enabled": bool(control["enabled"] and effective.get("discovery_enabled", False)),
                "comment_cooldown_until": cooldown_until if cooldown_until > time.time() else None,
                "visibility_risk": browsing.sends.visibility_risk_status(account_id),
                "active_agent_tasks": len(self.tasks.activity()) if getattr(self, "tasks", None) else 0,
            }
        return {
            "state": self.live.state,
            "muted": self.live.muted,
            "character": character_card(self.store.web_character(character)) | {"version": character["version"]},
            "session_id": self.live.session_id,
            "queue_length": len(self.live.queue),
            "overlay_connected": self.live.overlay is not None,
            "current_job": self.store.job(self.live.current_id) if self.live.current_id else None,
            "error": self.error,
            "last_played": self.live.last_played,
            "providers": {
                "llm": "omni",
                "llm_model": self.settings.data["omni"].get("model"),
                "llm_route": "direct",
                "tts": next((voice["provider"] for voice in self.settings.voices() if voice["id"] == voice_id), None),
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
            "deployment": {
                "mode": "service",
                "business_data": str(self.settings.runtime),
                "business_host": socket.gethostname(),
                "inference_url": self.settings.data["omni"].get("base_url"),
            },
            "playback": self.live.playback,
            "operations_waiting": self.operations_waiting(),
            "automation": automation,
            "heartbeat": self.heartbeat.status(),
            "video_production": self.remotion_skill.status(),
        }

    async def generate_reply(self, scope, character, event, relationship=None, mode="live", *, context=None):
        """Generate once with source memory; lexical diagnostics never rewrite speech."""
        if mode == "live" and event.get("platform") == HANDOFF_PLATFORM:
            return await self._generate_handoff_reply(character, event)
        operation = mode in {"comment_draft", "direct_message"}
        context = context or (self.operation_context if operation else self.context)
        provider = self.operation_llm if operation else self.llm
        evidence = context.memory.retrieve(scope, character, event["text"])
        channel = {"operator_chat": "operator", "live": "live", "comment_draft": "comment",
                   "direct_message": "direct_message"}.get(mode)
        memory_scope = self.memory_organizer.scope() if channel and hasattr(self, "browsing") else None
        organized = []
        if memory_scope and memory_scope[1:] != (character["id"], character["version"]):
            memory_scope = None
        if memory_scope:
            resource = event.get("video_id") or event.get("bvid")
            thread = event.get("thread_id")
            if channel == "direct_message":
                if event.get("account_id") and str(event["account_id"]) != memory_scope[0]:
                    memory_scope = None
            elif channel == "comment":
                account, _, resource, thread = comment_scope(scope) or (None, None, None, None)
                if account != memory_scope[0]:
                    memory_scope = None
            elif channel == "live" and event.get("account_id") and str(event["account_id"]) != memory_scope[0]:
                memory_scope = None
            if memory_scope and channel != "direct_message":
                organized = self.daily_memory.recall_organized(
                    memory_scope, channel, event["text"], resource=resource, thread=thread,
                    participant=event.get("platform_user_id") or event.get("user_id"),
                )
        memory_refs = [self.daily_memory.organized_reference(row) for row in organized]
        preference = self.daily_memory.preference(memory_scope) if memory_scope else None
        memory_audit = {"organized_memory": memory_refs, "memory_scope": memory_scope, "memory_channel": channel}

        def remember_dependency():
            if preference:
                memory_audit["memory_preference_revision"] = preference["revision"]

        if memory_refs:
            remember_dependency()
        extra = [{"role": "user", "kind": "context", "content": "与当前话题相关的记忆：" + dump([
            {"note": row["anecdote"]} for row in organized
        ])}] if organized else []
        unified_scope = "owner" if mode == "operator_chat" else "public"
        unified_items = self.unified_memory.for_prompt(unified_scope, character_id=character["id"], limit=15)
        if unified_items:
            extra.append(
                {
                    "role": "system",
                    "kind": "context",
                    "content": "已保存的长期记忆（自然承接背景即可，勿生硬复读）：\n"
                    + "\n".join(f"- {item['content']}" for item in unified_items),
                }
            )
        messages = await context.prepare(
            scope, character, event, relationship, mode, evidence=evidence, extra=extra,
            daily_memory_enabled=preference["enabled"] if preference else not hasattr(self, "browsing"),
            on_memory=remember_dependency,
            operating_account=operating_account_facts(self.account.status()) if mode == "operator_chat" and hasattr(self, "account") else None,
        )
        if mode == "comment_draft":
            from ..operations.channels.mentions import MENTION_INSTRUCTION

            messages.append({"role": "system", "content": MENTION_INSTRUCTION})
        elif mode == "direct_message" and event.get("share_reflection"):
            from ..operations.channels.dynamics.materials import REFLECTION_INSTRUCTION

            messages.append({"role": "system", "content": REFLECTION_INSTRUCTION})
        generation_identity = self.identity_snapshot(character, event.get("account_id"))
        if mode != "operator_chat":
            messages = identity_messages(messages, generation_identity)
        generate = getattr(provider, "generate_operation", provider.generate) if operation else provider.generate
        continuation = None
        if operation:
            maximum = self.settings.data["omni"].get("operations_max_tokens", 8192)
            source_binding = reply_generation_binding(scope, character, event, mode)
            binding = dict(source_binding)
            binding["self_identity"] = generation_identity
            if memory_refs:
                binding["organized_memory"] = memory_refs
            if "memory_preference_revision" in memory_audit:
                binding["memory_preference_revision"] = memory_audit["memory_preference_revision"]
            profile_revision = self.relationships.event_profile_revision(character["id"], event)
            if profile_revision is not None:
                binding["profile_revision"] = profile_revision
            identity = event["event_id"] + ":" + content_hash(binding)

            async def invoke(packet, *, web_fetch=None):
                options = {"web_fetch": web_fetch} if web_fetch is not None else {}
                return await budgeted_call(
                    provider,
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
                self.validate_memory_delivery(memory_audit)
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
                visual = browsing_visual_provider(getattr(self, "browsing", None), self)
                scene_reply = getattr(visual, "generate_live_reply", None)
                if scene.image and scene_reply:
                    reply = await scene_reply(scene, messages, self.settings.data["omni"], event["event_id"])
                if reply is None and scene.current(self.live.session_id):
                    messages = scene.annotate(messages)
            if reply is None:
                reply = (
                    await self.live.generate_text_reply(generate, messages, character, event)
                    if mode == "live"
                    else await generate(messages, character, event["event_id"], event["text"])
                )
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
        if "memory_preference_revision" in memory_audit:
            audit.update(memory_audit)
        if operation:
            audit["output"] = operation_output_diagnostics(reply.spoken_text) | {
                "finish_reason": getattr(provider, "last_finish_reason", None),
                "usage": dict(getattr(provider, "last_usage", {})),
            }
            if audit["output"]["needs_review"]:
                audit["warning"] = "长正文含大量重复段落，保留原文待审"
            if continuation and continuation["feedback"].get("kind") == "body_integrity":
                audit["output"]["needs_review"] = True
                audit["warning"] = continuation["feedback"]["message"]
            audit["continuation"] = continuation
        return reply, audit

    async def _generate_handoff_reply(self, character, event):
        scope = self.memory_organizer.scope()
        account = event["metadata"]["_handoff_account"]
        if scope != (account, character["id"], character["version"]) or event["room_id"] != self.live.session_id:
            raise ValueError("交接所属的账号、角色或直播场次已切换")
        preference = self.daily_memory.preference(scope)
        style = self.daily_memory.live_preferences(scope)
        text = event["text"]
        messages = [
            {"role": "system", "content": "以当前角色口吻向直播观众讲一句。根据交接文字说结论。"
             "使用 spoken_text、emotion、animation_id 和 reply_to 输出。\n" + front_identity(character)
             + "\n角色表达：" + dump(character["style"])},
            {"role": "user", "content": dump({"交接文字": text, "直播偏好": [row["anecdote"] for row in style]})},
        ]
        # Provider wrappers otherwise resolve and append the active account.
        messages = identity_messages(messages, identity_snapshot({}, character))
        if generate_json := getattr(self.llm, "generate_live_json", None):
            schema = Reply.model_json_schema()
            schema["properties"]["reply_to"]["items"] = {"type": "string", "enum": [event["event_id"]]}
            schema["properties"]["reply_to"]["maxItems"] = 1
            value = await generate_json(messages, schema, max_tokens=180)
            reply = Reply.model_validate(value)
            if any(reference != event["event_id"] for reference in reply.reply_to):
                raise ValueError("模型引用了其他事件")
        else:
            generate = getattr(self.llm, "generate_live", self.llm.generate)
            reply = await generate(messages, character, event["event_id"], text)
        audit = {"handoff": True, "memory_scope": scope, "memory_channel": "live",
                 "organized_memory": [self.daily_memory.organized_reference(row) for row in style]}
        if style:
            audit["memory_preference_revision"] = preference["revision"]
        self.validate_memory_delivery(audit)
        return reply, audit

    def validate_memory_delivery(self, audit, *, historical=False):
        if not audit or not (audit.get("organized_memory") or "memory_preference_revision" in audit or audit.get("handoff")):
            return
        scope = tuple(audit["memory_scope"])
        if scope != self.memory_organizer.scope():
            raise ValueError("记忆所属账号或角色已切换，请重试")
        preference = self.daily_memory.preference(scope)
        if not historical and (audit.get("organized_memory") or "memory_preference_revision" in audit) and (
            not preference["enabled"] or preference["revision"] != audit.get("memory_preference_revision", 0)
        ):
            raise MemoryChanged("生成使用的记忆设置已变更，请重试")
        self.daily_memory.validate_organized(scope, audit["memory_channel"], audit.get("organized_memory", []))

    async def control(self, action):
        return await self.live.control(action)

    async def submit(self, event, *, online_snapshot=None):
        return await self.live.submit(event, online_snapshot=online_snapshot)

    async def activate(self, character_id, version=None):
        return await self.live.activate(character_id, version)
