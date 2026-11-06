"""Compose the business runtime and coordinate shared generation."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager, nullcontext

import psutil

from .agent_tools import build_default_tool_registry
from .api_providers import VisionService, create_llm
from .context import ContextManager
from .daily_memory import DailyMemoryService
from .generation_meter import budgeted_call
from .inference_profiles import DAFEIYU_KEY, DAFEIYU_MODEL, GROUNDED_DAFEIYU_KEY, GROUNDED_DAFEIYU_MODEL
from .live.scene import LiveScene
from .live.session import LiveSession
from .media_cache import MediaCache
from .memory import missing_slots
from .model_hub import is_model_installed
from .model_runtime.session import InferenceRuntime
from .models import OperationReply
from .operation_continuations import ContinuationCoordinator, content_hash
from .operation_generation import generate_with_recovery, reply_generation_binding
from .operation_text import operation_output_diagnostics
from .operations.browsing.repository import BrowsingRepository
from .operations_control import OperationsControl
from .persona import uses_built_in_persona
from .providers import VoiceService, public_error, reuse_turn_token_counts
from .relationships import Relationships
from .retention import INTERVAL_SECONDS, RetentionManager
from .runtime_modes import apply_mode_profile, profile_alignment_error
from .video_vision_service import restore_admission


class Runtime:
    def __init__(self, settings, store, *, singing_provider=None, live_scene=None):
        self.settings, self.store = settings, store
        self.operations = OperationsControl(store)
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
        self.live = LiveSession(self, singing_provider=singing_provider, live_scene=live_scene)
        self.tool_registry = build_default_tool_registry(
            daily_memory=self.daily_memory,
            game_livestream=self.live.game_livestream,
            draw_and_guess=self.live.draw_and_guess,
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
        self.error = None
        self.cache = MediaCache(self)
        self.live.media.cache = self.cache
        self.voices.cache = self.cache
        self.inference = InferenceRuntime(self)

    @property
    def character(self):
        return self.store.character()

    @asynccontextmanager
    async def offline_generation(self):
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
        if profiles and (
            self.inference.status.get("mode") != "text"
            or self.inference.status.get("state") in {"error", "loading", "starting", "switching"}
            or profile_alignment_error(self, self.inference.status)
        ):
            return True
        return self.live.state in {"live", "paused"} and self.live.busy

    async def open(self):
        await self.live.online_songs.open()
        try:
            if self.settings.data.get("work_mode_profiles") and self.inference.client:
                observed = await self.inference.refresh()
                async with self.generation_lock:
                    await apply_mode_profile(self, observed["mode"])
                if observed.get("provider_alignment_error"):
                    await self.inference.refresh()
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
            "character": self.character,
            "persona": self.persona_status(),
            "session_id": self.live.session_id,
            "queue_length": len(self.live.queue),
            "overlay_connected": self.live.overlay is not None,
            "current_job": self.store.job(self.live.current_id) if self.live.current_id else None,
            "error": self.error,
            "last_played": self.live.last_played,
            "providers": {"llm": self.settings.data["llm"]["kind"], "tts": self.settings.data["tts"]["kind"]},
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

    def persona_status(self):
        node_model_state = "unknown"
        health = self.inference.status
        capabilities = self.inference.capabilities or {}
        data = self.settings.data
        operations = data.get("work_mode_profiles", {}).get("text", {}).get("llm", data["llm"])
        grounded = operations["model"] == GROUNDED_DAFEIYU_MODEL
        model_key = GROUNDED_DAFEIYU_KEY if grounded else DAFEIYU_KEY
        if self.inference.client and health.get("state") != "error":
            running = health.get("providers", {}).get(model_key, {}).get("running")
            if (
                health.get("state") == "active"
                and health.get("selected_provider") == model_key
                and running is True
            ):
                node_model_state = "active"
            elif not capabilities.get("monitoring_error"):
                profile = capabilities.get("profiles", {}).get(model_key, {})
                installed = profile.get("installed")
                if installed is True:
                    node_model_state = "installed"
                elif installed is False:
                    node_model_state = "not_installed"
        return {
            "source": "fine_tuned" if uses_built_in_persona(data) else "character_card",
            "configured_model": data["llm"]["model"],
            "fine_tuned_model": GROUNDED_DAFEIYU_MODEL if grounded else DAFEIYU_MODEL,
            "node_model_state": node_model_state,
            "local_model_installed": is_model_installed(self.settings.models),
        }

    @reuse_turn_token_counts
    async def generate_reply(self, scope, character, event, relationship=None, mode="live", *, context=None):
        """Generate once with source memory; lexical diagnostics never rewrite speech."""
        context = context or self.context
        evidence = context.memory.retrieve(scope, character, event["text"])
        messages = await context.prepare(scope, character, event, relationship, mode, evidence=evidence)
        operation = mode in {"comment_draft", "direct_message"}
        generate = (
            getattr(self.llm, "generate_operation", self.llm.generate) if operation else self.llm.generate
        )
        continuation = None
        if operation:
            maximum = self.settings.data["llm"].get("operations_max_tokens", 8192)
            binding = reply_generation_binding(scope, character, event, mode)
            identity = event["event_id"] + ":" + content_hash(binding)

            async def invoke(packet):
                return await budgeted_call(
                    self.llm,
                    lambda: generate(packet, character, event["event_id"], event["text"]),
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
                current = self.store.character()
                if (current["id"], current["version"]) != (
                    character["id"],
                    character["version"],
                ) or reply_generation_binding(scope, character, event, mode) != binding:
                    raise ValueError("生成期间角色或原始事件已变化")

            def diagnose(value):
                return (
                    "正文包含大量重复内容；结合表达意图选择保留、修订或暂缓"
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
                generation_context=nullcontext,
                context_data={
                    "event": event,
                    "relationship": relationship,
                    "memory_evidence": evidence,
                    "history": json.loads(context.existing(scope, character)["history"])
                    if context.existing(scope, character)
                    else [],
                },
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
                reply = await generate(messages, character, event["event_id"], event["text"])
        missing = missing_slots(reply.spoken_text, evidence)
        supported = [
            slot
            for slot in evidence["required_slots"]
            if any(
                fact["slot"] == slot for source in evidence["evidence"] for fact in source.get("facts", [])
            )
        ]
        audit = {
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
        if operation:
            audit["output"] = operation_output_diagnostics(reply.spoken_text) | {
                "finish_reason": getattr(self.llm, "last_finish_reason", None),
                "usage": dict(getattr(self.llm, "last_usage", {})),
            }
            if audit["output"]["needs_review"]:
                if continuation:
                    audit["output"] |= {
                        "diagnostic_needs_review": True,
                        "needs_review": False,
                        "agent_checked": True,
                    }
                else:
                    audit["warning"] = "长正文含大量重复段落，保留原文待审"
            audit["continuation"] = continuation
        return reply, audit

    async def control(self, action):
        return await self.live.control(action)

    async def submit(self, event, *, online_snapshot=None):
        return await self.live.submit(event, online_snapshot=online_snapshot)

    async def activate(self, character_id, version=None):
        return await self.live.activate(character_id, version)
