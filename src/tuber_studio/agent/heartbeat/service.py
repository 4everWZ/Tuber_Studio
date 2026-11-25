"""Always-on Heartbeat service coordinating ticks, curiosity research, and Aqua owner briefs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from ...core.identity import operating_account_facts, owner_address_label, without_model_identity
from ...core.models import StrictModel, content_hash
from ...core.store import dump
from ...core.time import day_bounds
from ...infra.web_client.fetch import fetch_excerpt
from ...infra.web_client.search import search_public_web
from ...operations.facade import business_timezone, heartbeat_pulse_snapshot
from ...persona.self_model import build_self_model_system_message
from .bus import AgentEventBus, HeartbeatEvent
from .state import (
    HeartbeatStateStore,
    evaluate_snapshot,
    is_in_quiet_hours,
    pending_summary_from_snapshot,
    sanitize_pulse_text,
    validate_hhmm,
)

logger = logging.getLogger(__name__)

HEARTBEAT_POLICY_KEY = "agent_heartbeat_policy"
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")
_BVID_ONLY_RE = re.compile(r"^(?:bv[0-9a-zA-Z]{6,}|av\d+)$", re.IGNORECASE)
_NOISE_LATIN = frozenset({"bilibili", "youtube", "hotmail", "http", "https", "www", "com", "video"})


class HeartbeatPolicy(StrictModel):
    """Runtime policy governing always-on ticks, curiosity research, quiet hours, and brief rate-limits."""

    enabled: bool = True
    tick_interval_seconds: float = Field(default=300.0, ge=1.0, le=86400.0)
    pulse_cooldown_seconds: float = Field(default=10800.0, ge=0.0, le=86400.0)
    event_cooldown_seconds: float = Field(default=900.0, ge=0.0, le=86400.0)
    max_pulses_per_day: int = Field(default=3, ge=0, le=200)
    max_unreplied_pulses: int = Field(default=1, ge=0, le=50)
    hot_topic_dedup_seconds: float = Field(default=604800.0, ge=0.0, le=2592000.0)
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:30"
    curiosity_research_enabled: bool = True
    curiosity_max_queries_per_tick: int = Field(default=2, ge=0, le=5)
    surface_to_chat: bool = True

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _validate_quiet_clock(cls, value: str) -> str:
        return validate_hhmm(value)


def _extract_research_query(topic: dict[str, Any]) -> str:
    title = str(topic.get("title") or "").strip()
    tags = [str(t).strip() for t in (topic.get("tags") or ()) if str(t).strip()]
    clean_title = re.sub(r"https?://\S+", " ", title)
    clean_title = re.sub(r"【[^】]{1,24}】|\[[^\]]{1,24}\]", " ", clean_title)
    clean_title = re.sub(r"[#｜|/\u200b-\u200d\ufe0f]+", " ", clean_title)
    clean_title = " ".join(clean_title.split())[:48].strip()
    if _BVID_ONLY_RE.match(clean_title):
        clean_title = ""
    filtered_tags = [
        t for t in tags
        if not _BVID_ONLY_RE.match(t) and len(t) >= 2 and t.lower() not in _NOISE_LATIN
    ]
    if filtered_tags and clean_title and filtered_tags[0] not in clean_title:
        candidate = f"{clean_title} {filtered_tags[0]}"[:64].strip()
    else:
        candidate = clean_title or (filtered_tags[0] if filtered_tags else "")
    return candidate if len(candidate) >= 4 else ""


def _is_relevant_research_page(query: str, title: str, excerpt: str) -> bool:
    """Reject unrelated foreign/spam pages whose language or keywords miss the query."""
    page_text = f"{title} {excerpt}".strip()
    if not page_text:
        return False
    page_lower = page_text.lower()
    latin_tokens = [tok.lower() for tok in _LATIN_ENTITY_RE.findall(query) if tok.lower() not in _NOISE_LATIN]
    matched_latin = any(tok in page_lower for tok in latin_tokens)
    cjk_runs = _CJK_CHAR_RE.findall(query)
    if cjk_runs:
        if not _CJK_CHAR_RE.search(page_text):
            return matched_latin
        query_cjk = "".join(cjk_runs)
        if len(query_cjk) >= 2:
            bigrams = {query_cjk[i : i + 2] for i in range(len(query_cjk) - 1)}
            return matched_latin or any(bg in page_text for bg in bigrams)
        return matched_latin or (query_cjk in page_text)
    return matched_latin if latin_tokens else True


class HeartbeatService:
    """Coordinates periodic ops health & hot-topic ticks, curiosity research, and Aqua owner briefs."""

    def __init__(
        self,
        runtime,
        store,
        *,
        bus: AgentEventBus | None = None,
        clock: Callable[[], float] | None = None,
        web_search_fn=None,
        web_fetch_fn=None,
        brief_generator=None,
        on_pulse=None,
    ) -> None:
        self.runtime = runtime
        self.store = store
        self._clock = clock or time.time
        self.state_store = HeartbeatStateStore(store)
        self.bus = bus or AgentEventBus(clock=self._clock, state_store=self.state_store)
        self._web_search_fn = web_search_fn or search_public_web
        self._web_fetch_fn = web_fetch_fn or fetch_excerpt
        self._brief_generator = brief_generator
        self.on_pulse = on_pulse
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._tick_lock = asyncio.Lock()
        self._closed = False
        self._last_tick_at: float | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_snapshot_synthetic = False
        self._install_storage()

    def _install_storage(self) -> None:
        with self.store.db:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_heartbeat_pulses(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT '',
                    character_id TEXT NOT NULL DEFAULT '',
                    character_version INTEGER NOT NULL DEFAULT 1,
                    kind TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    research_summary TEXT NOT NULL DEFAULT '',
                    sources TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    surfaced_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_heartbeat_pulses_lookup
                    ON agent_heartbeat_pulses(character_id, account_id, created_at DESC);
                """
            )

    def policy(self) -> HeartbeatPolicy:
        saved = self.store.get(HEARTBEAT_POLICY_KEY) or {}
        if not isinstance(saved, dict):
            return HeartbeatPolicy()
        try:
            return HeartbeatPolicy.model_validate(HeartbeatPolicy().model_dump() | saved)
        except Exception:
            return HeartbeatPolicy()

    def configure(self, patch: dict[str, Any]) -> HeartbeatPolicy:
        if not isinstance(patch, dict) or set(patch) - set(HeartbeatPolicy.model_fields):
            raise ValueError("未知的心跳配置字段")
        updated = HeartbeatPolicy.model_validate(self.policy().model_dump() | patch)
        self.store.set(HEARTBEAT_POLICY_KEY, updated.model_dump())
        self.wake()
        return updated

    def wake(self) -> None:
        self._wake.set()

    def start(self, *, initial_delay: float | None = None) -> None:
        self._closed = False
        if self._task is None or self._task.done():
            delay = self.policy().tick_interval_seconds if initial_delay is None else max(0.0, float(initial_delay))
            self._task = asyncio.create_task(self._loop(initial_delay=delay))

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self, *, initial_delay: float) -> None:
        delay = initial_delay
        while not self._closed:
            self._wake.clear()
            if delay > 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
            if self._closed:
                return
            policy = self.policy()
            if policy.enabled:
                with suppress(Exception):
                    await self.tick()
            delay = policy.tick_interval_seconds

    def _current_character(self) -> dict[str, Any]:
        return getattr(self.runtime, "character", None) or self.store.character()

    def _current_browsing(self):
        return getattr(self.runtime, "browsing", None)

    def _business_tz(self) -> str:
        browsing = self._current_browsing()
        return business_timezone(browsing) if browsing is not None else "Asia/Taipei"

    def _account_facts(self) -> dict[str, Any] | None:
        account_svc = getattr(self.runtime, "account", None)
        if account_svc is None or not hasattr(account_svc, "status"):
            return None
        return operating_account_facts(account_svc.status())

    async def research_topic(
        self,
        topic: str,
        *,
        bvid: str | None = None,
        bypass_cooldown: bool = False,
        character_id: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Run bounded curiosity-driven public web research for a trending video or topic."""
        cleaned_query = " ".join(str(topic or "").split())[:120]
        if not cleaned_query:
            return {"ok": False, "sources": [], "summary": "", "emitted": False}
        now = float(self._clock())
        cid = str(character_id if character_id is not None else (self._current_character().get("id") or ""))
        aid = str(account_id if account_id is not None else ((self._last_snapshot or {}).get("account_id") or ""))
        dedup_suffix = bvid or content_hash(cleaned_query)[:12]
        dedup_key = f"curiosity:{dedup_suffix}"
        policy = self.policy()
        if (
            bvid
            and not bypass_cooldown
            and policy.hot_topic_dedup_seconds > 0
            and self.state_store.is_dedup_active(cid, aid, dedup_key, now=now)
        ):
            return {
                "ok": False,
                "query": cleaned_query,
                "sources": [],
                "summary": "",
                "emitted": False,
                "suppressed_reason": "persisted_dedup",
            }

        sources: list[dict[str, str]] = []
        try:
            urls = await self._web_search_fn(cleaned_query)
        except Exception as exc:
            logger.debug("Curiosity search skipped: %s", type(exc).__name__)
            urls = []
        for url in list(urls or ())[:2]:
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            try:
                page = await self._web_fetch_fn(url)
            except Exception:
                continue
            if isinstance(page, dict) and page.get("excerpt"):
                page_title = str(page.get("title") or "").strip()
                page_excerpt = sanitize_pulse_text(str(page["excerpt"]), owner_label="你")[:260]
                if not _is_relevant_research_page(cleaned_query, page_title, page_excerpt):
                    continue
                sources.append(
                    {
                        "url": str(page.get("url") or url),
                        "title": (page_title or cleaned_query)[:120],
                        "excerpt": page_excerpt,
                    }
                )
        summary = f"顺手查了「{cleaned_query}」，看到相关介绍：{sources[0]['excerpt'][:140]}" if sources else ""
        event = HeartbeatEvent(
            kind="curiosity_research",
            source="curiosity",
            dedup_key=dedup_key,
            severity="info",
            title=f"延伸查证：{cleaned_query[:80]}",
            summary=summary or f"对「{cleaned_query}」做了公开资料检索。",
            payload={"query": cleaned_query, "bvid": bvid, "sources": sources},
            created_at=now,
        )
        emission = await self.bus.publish(
            event,
            cooldown_seconds=policy.event_cooldown_seconds,
            bypass_cooldown=bypass_cooldown,
        )
        if emission.accepted and policy.hot_topic_dedup_seconds > 0:
            self.state_store.record_dedup(
                cid,
                aid,
                dedup_key,
                kind="curiosity_research",
                digest=dedup_key,
                ttl_seconds=policy.hot_topic_dedup_seconds,
                now=now,
            )
        return {
            "ok": bool(sources),
            "query": cleaned_query,
            "sources": sources,
            "summary": summary,
            "emitted": emission.accepted,
            "event": event.model_dump(),
        }

    def _pulses_today_count(self, *, account_id: str, character_id: str) -> int:
        tz = self._business_tz()
        today = datetime.fromtimestamp(float(self._clock()), ZoneInfo(tz)).date()
        bounds = day_bounds(tz, today)
        row = self.store.one(
            "SELECT COUNT(*) AS n FROM agent_heartbeat_pulses "
            "WHERE character_id=? AND account_id=? AND severity!='urgent' AND created_at>=? AND created_at<?",
            (character_id, account_id, bounds["start_at"], bounds["next_at"]),
        )
        return int(row["n"]) if row else 0

    def _last_pulse_at(self, *, account_id: str, character_id: str) -> float:
        row = self.store.one(
            "SELECT MAX(created_at) AS ts FROM agent_heartbeat_pulses "
            "WHERE character_id=? AND account_id=? AND severity!='urgent'",
            (character_id, account_id),
        )
        return float(row["ts"]) if row and row["ts"] is not None else 0.0

    def _unreplied_pulses_count(self, *, account_id: str, character_id: str) -> int:
        last_owner_ts = 0.0
        if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_chat_turns'"):
            owner_row = self.store.one(
                "SELECT MAX(created_at) AS ts FROM operator_chat_turns "
                "WHERE character_id=? AND TRIM(COALESCE(message,'')) != '' "
                "AND COALESCE(json_extract(result, '$.assistant_only'), 0) = 0",
                (character_id,),
            )
            if owner_row and owner_row["ts"] is not None:
                last_owner_ts = float(owner_row["ts"])
        row = self.store.one(
            "SELECT COUNT(*) AS n FROM agent_heartbeat_pulses "
            "WHERE character_id=? AND account_id=? AND severity!='urgent' AND created_at>?",
            (character_id, account_id, last_owner_ts),
        )
        return int(row["n"]) if row else 0

    def _non_urgent_gate_reason(
        self,
        policy: HeartbeatPolicy,
        *,
        account_id: str,
        character_id: str,
        now: float,
    ) -> str | None:
        if is_in_quiet_hours(now, self._business_tz(), policy.quiet_hours_start, policy.quiet_hours_end):
            return "quiet_hours"
        last_at = self._last_pulse_at(account_id=account_id, character_id=character_id)
        if last_at and policy.pulse_cooldown_seconds > 0 and now - last_at < policy.pulse_cooldown_seconds:
            return "pulse_cooldown"
        if policy.max_pulses_per_day > 0:
            if self._pulses_today_count(account_id=account_id, character_id=character_id) >= policy.max_pulses_per_day:
                return "daily_pulse_limit"
        if policy.max_unreplied_pulses > 0:
            if self._unreplied_pulses_count(account_id=account_id, character_id=character_id) >= policy.max_unreplied_pulses:
                return "unread_cap"
        return None

    def _fallback_aqua_brief(
        self,
        char: dict[str, Any],
        events: list[HeartbeatEvent],
        research_sources: list[dict[str, str]],
    ) -> str:
        owner = owner_address_label(char)
        urgent = [e for e in events if e.severity == "urgent"]
        attention = [e for e in events if e.severity == "attention"]
        research = [e for e in events if e.kind == "curiosity_research" and e.payload.get("sources")]
        hot = [e for e in events if e.kind == "hot_topic"]
        parts: list[str] = []
        if urgent:
            parts.append(f"{owner}，先跟你说个急事：{urgent[0].summary}")
        elif attention:
            parts.append(f"{owner}，顺便跟你说一声：{attention[0].summary}")
        elif hot:
            top = hot[0]
            parts.append(f"{owner}，我刚刷到「{top.payload.get('title', top.title)}」，感觉还挺有意思的。")
        else:
            parts.append(f"{owner}，顺便提一下，{events[0].summary}")
        if research:
            src = research[0].payload["sources"][0]
            parts.append(f"我还顺手搜了点资料（{src.get('title') or '公开来源'}），大致是：{src.get('excerpt', '')[:100]}")
        elif len(events) > 1:
            extra = events[1]
            if extra.kind != "curiosity_research":
                parts.append(extra.summary)
        return sanitize_pulse_text(" ".join(parts), owner_label=owner)

    async def _compose_owner_brief(
        self,
        char: dict[str, Any],
        events: list[HeartbeatEvent],
        research_sources: list[dict[str, str]],
    ) -> str:
        owner = owner_address_label(char)
        facts = self._account_facts()
        has_urgent = any(e.severity == "urgent" for e in events)
        has_hot = any(e.kind in {"hot_topic", "curiosity_research"} for e in events)
        if has_urgent:
            task_intent = (
                f"把下面需要立刻处理的异常，用 Aqua 清晰直接的第一人称口吻告诉{owner}。"
                "控制在 1-2 句话内；只写已给材料里的事实，不输出 JSON、不吐原始堆栈或凭据。"
            )
        elif has_hot:
            task_intent = (
                f"用 Aqua 自然口语给{owner}发 1-2 句轻松分享，聊聊刚刷到的视频话题或顺手查到的公开资料；"
                "只写已给材料里的事实，不编造链接、不用「心跳巡检」「刚完成一轮巡检」这类机械套话、不输出 JSON。"
            )
        else:
            task_intent = (
                f"用 Aqua 自然口语轻声向{owner}提一句刚新增的待处理草稿或小动静，控制在 1-2 句话内；"
                "不用「心跳巡检」「刚完成一轮巡检」等机械套话，只写已给事实。"
            )
        sys_msg = build_self_model_system_message(
            char,
            channel="owner_chat",
            scene="后台主动简报",
            task_intent=task_intent,
            operating_account=facts,
            situation="后台出现了新的异常变化、待确认草稿或值得分享的视频话题。",
        )
        prompt_payload = {
            "owner_address": owner,
            "events": [
                {"kind": e.kind, "severity": e.severity, "title": e.title, "summary": e.summary}
                for e in events[:4]
            ],
            "verified_sources": [
                {"title": s.get("title", ""), "url": s.get("url", ""), "excerpt": s.get("excerpt", "")[:160]}
                for s in research_sources[:2]
            ],
        }
        messages = [sys_msg, {"role": "user", "content": dump(prompt_payload)}]

        if callable(self._brief_generator):
            try:
                cleaned = sanitize_pulse_text(await self._brief_generator(messages, char), owner_label=owner)
                if cleaned:
                    return cleaned
            except Exception:
                pass

        settings = getattr(self.runtime, "settings", None)
        has_key = bool(settings and callable(getattr(settings, "provider_key", None)) and settings.provider_key("omni"))
        provider = (
            self.runtime.foreground_chat_llm()
            if callable(getattr(self.runtime, "foreground_chat_llm", None))
            else getattr(self.runtime, "chat_llm", None) or getattr(self.runtime, "llm", None)
        )
        if has_key and provider is not None and callable(getattr(provider, "generate_text", None)):
            try:
                slot = (
                    self.runtime.offline_generation("background")
                    if callable(getattr(self.runtime, "offline_generation", None))
                    else asyncio.Lock()
                )
                async with asyncio.timeout(20), slot:
                    with without_model_identity():
                        raw = await provider.generate_text(messages, max_tokens=220)
                cleaned = sanitize_pulse_text(raw, owner_label=owner)
                if cleaned:
                    return cleaned
            except Exception:
                pass

        return self._fallback_aqua_brief(char, events, research_sources)

    def _persist_pulse(
        self,
        *,
        account_id: str,
        char: dict[str, Any],
        events: list[HeartbeatEvent],
        brief: str,
        research_sources: list[dict[str, str]],
    ) -> dict[str, Any]:
        ordered = sorted(events, key=lambda e: 0 if e.severity == "urgent" else 1)
        primary = ordered[0]
        now = float(self._clock())
        pulse_id = str(uuid4())
        research_summary = "；".join(
            f"{s.get('title', '')}：{s.get('excerpt', '')[:80]}" for s in research_sources[:2]
        )
        sources_payload = [
            {
                "title": s.get("title") or "公开网页来源",
                "entries": [{"kind": "web", "text": s.get("excerpt", ""), "href": s.get("url", "")}],
            }
            for s in research_sources[:3]
            if s.get("url")
        ]
        payload = {"event_ids": [e.event_id for e in ordered], "kinds": [e.kind for e in ordered]}
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO agent_heartbeat_pulses("
                "id,account_id,character_id,character_version,kind,dedup_key,severity,title,brief,"
                "research_summary,sources,payload,surfaced_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pulse_id,
                    account_id,
                    char["id"],
                    int(char.get("version") or 1),
                    primary.kind,
                    primary.dedup_key,
                    primary.severity,
                    primary.title,
                    brief,
                    research_summary,
                    dump(sources_payload),
                    dump(payload),
                    now,
                    now,
                ),
            )
        return {
            "id": pulse_id,
            "account_id": account_id,
            "character_id": char["id"],
            "character_version": int(char.get("version") or 1),
            "kind": primary.kind,
            "dedup_key": primary.dedup_key,
            "severity": primary.severity,
            "title": primary.title,
            "brief": brief,
            "research_summary": research_summary,
            "sources": sources_payload,
            "payload": payload,
            "surfaced_at": now,
            "created_at": now,
        }

    def _save_tick_state(
        self,
        *,
        char_id: str,
        account_id: str,
        persisted_state: dict[str, Any],
        eval_result: dict[str, Any],
        committed_faults: dict[str, str],
        committed_drafts: list[tuple[str, dict[str, Any]]],
        suppressions: list[dict[str, Any]],
        now: float,
    ) -> None:
        next_state = dict(persisted_state)
        active_faults = {
            k: v for k, v in dict(persisted_state.get("active_faults") or {}).items()
            if k not in eval_result["recovered_faults"]
        }
        active_faults.update(committed_faults)
        next_state["active_faults"] = active_faults
        next_state.update(eval_result["cleared_updates"])
        for kind_tag, fields in committed_drafts:
            if kind_tag == "dynamics":
                next_state["dynamics_pending_ids"] = fields["ids"]
                next_state["dynamics_pending_count"] = fields["count"]
                next_state["dynamics_latest_created_at"] = fields["latest_ts"]
            elif kind_tag == "dm":
                next_state["dm_pending_ids"] = fields["ids"]
                next_state["dm_pending_count"] = fields["count"]
                next_state["dm_latest_created_at"] = fields["latest_ts"]
            elif kind_tag == "inbox":
                next_state["inbox_pending_count"] = fields["count"]
        if suppressions:
            next_state["suppressions"] = [*list(next_state.get("suppressions") or []), *suppressions][-30:]
        self.state_store.save_state(char_id, account_id, next_state, now=now)

    async def tick(
        self,
        *,
        snapshot: dict[str, Any] | None = None,
        force_brief: bool = False,
        bypass_cooldown: bool = False,
    ) -> dict[str, Any]:
        """Run one unified heartbeat cycle: evaluate edge-triggered signals, gate non-urgent work, emit brief."""
        async with self._tick_lock:
            policy = self.policy()
            now = float(self._clock())
            self._last_tick_at = now
            self._last_snapshot_synthetic = snapshot is not None
            snap = snapshot if snapshot is not None else heartbeat_pulse_snapshot(
                self._current_browsing(), store=self.store
            )
            self._last_snapshot = snap
            char = self._current_character()
            char_id = str(char.get("id") or "")
            account_id = str(snap.get("account_id") or "")
            persisted_state = self.state_store.load_state(char_id, account_id)

            eval_res = evaluate_snapshot(
                self.state_store,
                snap,
                persisted_state,
                character_id=char_id,
                account_id=account_id,
                now=now,
                bypass_cooldown=bypass_cooldown,
                clear_bus_cooldown=self.bus.clear_cooldown,
            )
            suppressions: list[dict[str, Any]] = list(eval_res["suppressions"])
            fault_events: list[tuple[str, HeartbeatEvent]] = eval_res["fault_events"]
            non_urgent_items: list[tuple[str, HeartbeatEvent, Any]] = [
                ("attention", ev, None) for ev in eval_res["attention_events"]
            ] + [
                ("draft:" + tag, ev, fields) for tag, ev, fields in eval_res["draft_events"]
            ] + [
                ("hot_topic", ev, None) for ev in eval_res["hot_events"]
            ]

            has_urgent = bool(fault_events)
            gate_reason = (
                None
                if force_brief
                else self._non_urgent_gate_reason(policy, account_id=account_id, character_id=char_id, now=now)
            )

            if not has_urgent and gate_reason is not None and non_urgent_items:
                for _, ev, _ in non_urgent_items:
                    suppressions.append({
                        "at": now,
                        "reason": gate_reason,
                        "dedup_key": ev.dedup_key,
                        "kind": ev.kind,
                        "detail": "gated_before_research",
                    })
                self._save_tick_state(
                    char_id=char_id,
                    account_id=account_id,
                    persisted_state=persisted_state,
                    eval_result=eval_res,
                    committed_faults={},
                    committed_drafts=[],
                    suppressions=suppressions,
                    now=now,
                )
                return {
                    "ok": True,
                    "tick_at": now,
                    "emitted": 0,
                    "suppressed": [{"dedup_key": s["dedup_key"], "reason": s["reason"]} for s in suppressions],
                    "pulse": None,
                    "rate_limited": gate_reason,
                }

            emitted_events: list[HeartbeatEvent] = []
            committed_faults: dict[str, str] = {}
            committed_drafts: list[tuple[str, dict[str, Any]]] = []

            for fkey, ev in fault_events:
                res = await self.bus.publish(ev, cooldown_seconds=0.0, bypass_cooldown=True)
                if res.accepted:
                    emitted_events.append(ev)
                    committed_faults[fkey] = ev.dedup_key
                else:
                    suppressions.append({"at": now, "reason": res.reason, "dedup_key": ev.dedup_key, "kind": ev.kind, "detail": "bus"})

            allow_non_urgent = force_brief or gate_reason is None
            for tag, ev, meta in non_urgent_items:
                if not allow_non_urgent:
                    suppressions.append({"at": now, "reason": str(gate_reason), "dedup_key": ev.dedup_key, "kind": ev.kind, "detail": "gated_during_urgent"})
                    continue
                is_edge_draft = tag.startswith("draft:")
                res = await self.bus.publish(ev, cooldown_seconds=policy.event_cooldown_seconds, bypass_cooldown=bypass_cooldown or is_edge_draft)
                if res.accepted:
                    emitted_events.append(ev)
                    if is_edge_draft:
                        committed_drafts.append((tag.split(":", 1)[1], meta))
                else:
                    suppressions.append({"at": now, "reason": res.reason, "dedup_key": ev.dedup_key, "kind": ev.kind, "detail": "bus"})

            if not emitted_events:
                self._save_tick_state(
                    char_id=char_id,
                    account_id=account_id,
                    persisted_state=persisted_state,
                    eval_result=eval_res,
                    committed_faults=committed_faults,
                    committed_drafts=committed_drafts,
                    suppressions=suppressions,
                    now=now,
                )
                return {
                    "ok": True,
                    "tick_at": now,
                    "emitted": 0,
                    "suppressed": [{"dedup_key": s["dedup_key"], "reason": s["reason"]} for s in suppressions],
                    "pulse": None,
                    "rate_limited": None,
                }

            research_sources: list[dict[str, str]] = []
            if allow_non_urgent and policy.curiosity_research_enabled and policy.curiosity_max_queries_per_tick > 0:
                for hot in [e for e in emitted_events if e.kind == "hot_topic"][: policy.curiosity_max_queries_per_tick]:
                    query = _extract_research_query(hot.payload)
                    research = await self.research_topic(
                        query,
                        bvid=hot.payload.get("bvid"),
                        bypass_cooldown=bypass_cooldown,
                        character_id=char_id,
                        account_id=account_id,
                    )
                    if research.get("suppressed_reason") == "persisted_dedup":
                        suppressions.append({
                            "at": now,
                            "reason": "persisted_dedup",
                            "dedup_key": f"curiosity:{hot.payload.get('bvid') or ''}",
                            "kind": "curiosity_research",
                            "detail": query,
                        })
                    elif research.get("emitted"):
                        emitted_events.append(HeartbeatEvent.model_validate(research["event"]))
                    research_sources.extend(research.get("sources") or ())

            if policy.hot_topic_dedup_seconds > 0:
                for ev in emitted_events:
                    if ev.kind == "hot_topic":
                        self.state_store.record_dedup(
                            char_id,
                            account_id,
                            ev.dedup_key,
                            kind=ev.kind,
                            digest=ev.dedup_key,
                            ttl_seconds=policy.hot_topic_dedup_seconds,
                            now=now,
                        )

            brief = await self._compose_owner_brief(char, emitted_events, research_sources)
            pulse = self._persist_pulse(
                account_id=account_id,
                char=char,
                events=emitted_events,
                brief=brief,
                research_sources=research_sources,
            )
            self._save_tick_state(
                char_id=char_id,
                account_id=account_id,
                persisted_state=persisted_state,
                eval_result=eval_res,
                committed_faults=committed_faults,
                committed_drafts=committed_drafts,
                suppressions=suppressions,
                now=now,
            )
            if policy.surface_to_chat and callable(self.on_pulse):
                outcome = self.on_pulse(pulse)
                if asyncio.iscoroutine(outcome):
                    await outcome
            return {
                "ok": True,
                "tick_at": now,
                "emitted": len(emitted_events),
                "suppressed": [{"dedup_key": s["dedup_key"], "reason": s["reason"]} for s in suppressions],
                "pulse": pulse,
                "rate_limited": None,
            }

    def list_pulses(
        self,
        *,
        limit: int = 20,
        character_id: str | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if character_id:
            clauses.append("character_id=?")
            params.append(character_id)
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.store.all(
            f"SELECT * FROM agent_heartbeat_pulses {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, max(1, min(int(limit), 100))),
        )
        decoded: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for field, default in (("sources", []), ("payload", {})):
                try:
                    item[field] = json.loads(item.get(field) or json.dumps(default))
                except (TypeError, ValueError):
                    item[field] = default
            decoded.append(item)
        return decoded

    def latest_pulse(self, *, character_id: str | None = None) -> dict[str, Any] | None:
        items = self.list_pulses(limit=1, character_id=character_id)
        return items[0] if items else None

    def status(self, *, refresh_snapshot: bool = False) -> dict[str, Any]:
        running = bool(self._task and not self._task.done())
        now = float(self._clock())
        try:
            policy = self.policy()
            char_id = str(self._current_character().get("id") or "")
            recent_count = len(self.list_pulses(limit=20, character_id=char_id))
            snap = self._last_snapshot
            if (snap is None or (refresh_snapshot and not self._last_snapshot_synthetic)) and self._current_browsing() is not None:
                snap = heartbeat_pulse_snapshot(self._current_browsing(), store=self.store)
            account_id = str((snap or {}).get("account_id") or "")
            persisted = self.state_store.load_state(char_id, account_id)
            in_quiet = is_in_quiet_hours(now, self._business_tz(), policy.quiet_hours_start, policy.quiet_hours_end)
            unreplied = self._unreplied_pulses_count(account_id=account_id, character_id=char_id)
            pending_summary = pending_summary_from_snapshot(snap)
            recent_suppressions = list(reversed(persisted.get("suppressions") or []))[:20]
        except Exception:
            policy = HeartbeatPolicy(enabled=False)
            recent_count = 0
            in_quiet = False
            unreplied = 0
            pending_summary = pending_summary_from_snapshot(None)
            recent_suppressions = []
        return {
            "enabled": policy.enabled,
            "running": running,
            "closed": self._closed,
            "tick_interval_seconds": policy.tick_interval_seconds,
            "pulse_cooldown_seconds": policy.pulse_cooldown_seconds,
            "max_pulses_per_day": policy.max_pulses_per_day,
            "max_unreplied_pulses": policy.max_unreplied_pulses,
            "hot_topic_dedup_seconds": policy.hot_topic_dedup_seconds,
            "quiet_hours_start": policy.quiet_hours_start,
            "quiet_hours_end": policy.quiet_hours_end,
            "curiosity_research_enabled": policy.curiosity_research_enabled,
            "in_quiet_hours": in_quiet,
            "unreplied_pulses": unreplied,
            "pending_summary": pending_summary,
            "recent_suppressions": recent_suppressions,
            "last_tick_at": self._last_tick_at,
            "bus": self.bus.stats(),
            "recent_pulse_count": recent_count,
        }
