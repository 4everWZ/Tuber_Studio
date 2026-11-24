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

from pydantic import Field

from ...core.identity import operating_account_facts, owner_address_label, without_model_identity
from ...core.models import StrictModel, content_hash
from ...core.store import dump
from ...core.time import day_bounds
from ...infra.web_client.fetch import fetch_excerpt
from ...infra.web_client.search import search_public_web
from ...operations.facade import business_timezone, heartbeat_pulse_snapshot
from ...persona.self_model import build_self_model_system_message
from .bus import AgentEventBus, HeartbeatEvent

logger = logging.getLogger(__name__)

HEARTBEAT_POLICY_KEY = "agent_heartbeat_policy"
_RETIRED_LABEL = "\u5de5\u574a\u53f7\u4e3b"
_RAW_ERROR_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|ValidationError|pydantic|File \"[^\"]+\"|line \d+|"
    r"HTTPStatusError|ConnectError|TimeoutException|SESSDATA|bili_jct|sk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


class HeartbeatPolicy(StrictModel):
    """Runtime policy governing always-on ticks, curiosity research, and brief rate-limits."""

    enabled: bool = True
    tick_interval_seconds: float = Field(default=300.0, ge=1.0, le=86400.0)
    pulse_cooldown_seconds: float = Field(default=1800.0, ge=0.0, le=86400.0)
    event_cooldown_seconds: float = Field(default=900.0, ge=0.0, le=86400.0)
    max_pulses_per_day: int = Field(default=12, ge=0, le=200)
    curiosity_research_enabled: bool = True
    curiosity_max_queries_per_tick: int = Field(default=2, ge=0, le=5)
    surface_to_chat: bool = True


def _calm_health_reason(raw_error: str | None, *, fallback: str) -> str:
    text = str(raw_error or "").strip()
    if not text:
        return fallback
    if "401" in text or "登录" in text or "expired" in text.lower():
        return "B站登录状态可能过期了，需要重新扫码确认。"
    if "412" in text or "429" in text or "风控" in text or "冷却" in text:
        return "B站读取暂时触发了平台冷却，正在避让等待恢复。"
    if "超时" in text or "timeout" in text.lower() or "连接" in text:
        return "上游服务刚刚连接不太稳，稍后会自动重试。"
    if _RAW_ERROR_RE.search(text):
        return fallback
    return sanitize_pulse_text(text, owner_label="你")[:140] or fallback


def sanitize_pulse_text(text: str, *, owner_label: str = "你") -> str:
    """Strip retired labels, raw tracebacks, credential strings, and JSON wrappers."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = cleaned.replace(_RETIRED_LABEL, owner_label)
    cleaned = cleaned.replace("\u53f7\u4e3b", owner_label)
    cleaned = cleaned.replace("\u5de5\u574a", "\u8fd9\u8fb9")
    if _RAW_ERROR_RE.search(cleaned):
        lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not _RAW_ERROR_RE.search(line)]
        cleaned = " ".join(lines)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                for key in ("brief", "reply_text", "reply_markdown", "summary", "text", "content"):
                    if isinstance(obj.get(key), str) and obj[key].strip():
                        cleaned = obj[key].strip()
                        break
        except ValueError:
            pass
    cleaned = " ".join(cleaned.split())
    return cleaned[:600]


_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")
_BVID_ONLY_RE = re.compile(r"^(?:bv[0-9a-zA-Z]{6,}|av\d+)$", re.IGNORECASE)
_NOISE_LATIN = frozenset({"bilibili", "youtube", "hotmail", "http", "https", "www", "com", "video"})


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
    latin_tokens = [
        tok.lower() for tok in _LATIN_ENTITY_RE.findall(query) if tok.lower() not in _NOISE_LATIN
    ]
    matched_latin = any(tok in page_lower for tok in latin_tokens)

    cjk_runs = _CJK_CHAR_RE.findall(query)
    if cjk_runs:
        has_page_cjk = bool(_CJK_CHAR_RE.search(page_text))
        if not has_page_cjk:
            return matched_latin
        query_cjk = "".join(cjk_runs)
        if len(query_cjk) >= 2:
            bigrams = {query_cjk[i : i + 2] for i in range(len(query_cjk) - 1)}
            return matched_latin or any(bg in page_text for bg in bigrams)
        return matched_latin or (query_cjk in page_text)

    if latin_tokens:
        return matched_latin
    return True


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
        self.bus = bus or AgentEventBus(clock=self._clock)
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

    def _account_facts(self) -> dict[str, Any] | None:
        account_svc = getattr(self.runtime, "account", None)
        if account_svc is None or not hasattr(account_svc, "status"):
            return None
        return operating_account_facts(account_svc.status())

    def _signals_from_snapshot(self, snapshot: dict[str, Any]) -> list[HeartbeatEvent]:
        events: list[HeartbeatEvent] = []
        now = float(self._clock())

        disc = snapshot.get("discovery") or {}
        if snapshot.get("control_state") == "emergency":
            events.append(
                HeartbeatEvent(
                    kind="discovery_health",
                    source="discovery",
                    dedup_key="health:control:emergency",
                    severity="urgent",
                    title="自主运营处于紧急停止状态",
                    summary="所有自动探索与互动发送均已暂停，确认安全后可再解除。",
                    payload={"state": "emergency"},
                    created_at=now,
                )
            )
        elif disc.get("read_cooldown_until") and float(disc["read_cooldown_until"]) > now:
            events.append(
                HeartbeatEvent(
                    kind="discovery_health",
                    source="discovery",
                    dedup_key=f"health:discovery:cooldown:{int(float(disc['read_cooldown_until']) // 300)}",
                    severity="attention",
                    title="发现线触发平台读取冷却",
                    summary="探索发现暂停避让一会儿，但私信和评论回应还在正常守着。",
                    payload={"read_cooldown_until": disc["read_cooldown_until"]},
                    created_at=now,
                )
            )
        elif disc.get("error"):
            calm = _calm_health_reason(disc["error"], fallback="发现线刚才遇到了一点波动，已暂缓本轮。")
            events.append(
                HeartbeatEvent(
                    kind="discovery_health",
                    source="discovery",
                    dedup_key=f"health:discovery:err:{content_hash(calm)[:12]}",
                    severity="attention",
                    title="发现线状态提醒",
                    summary=calm,
                    payload={"state": disc.get("state", "blocked")},
                    created_at=now,
                )
            )

        inbox = snapshot.get("inbox") or {}
        if inbox.get("state") == "frozen":
            events.append(
                HeartbeatEvent(
                    kind="inbox_health",
                    source="inbox",
                    dedup_key="health:inbox:frozen",
                    severity="urgent",
                    title="互动收件箱因待核对提交已冻结",
                    summary="有一条平台发送回执结果待核对，已先暂停新的自动发送。",
                    payload={"pending_events": int(inbox.get("pending_events") or 0)},
                    created_at=now,
                )
            )
        elif int(inbox.get("pending_events") or 0) >= 3:
            count = int(inbox["pending_events"])
            events.append(
                HeartbeatEvent(
                    kind="inbox_health",
                    source="inbox",
                    dedup_key=f"health:inbox:backlog:{count // 3}",
                    severity="info",
                    title="评论区和互动消息有新动静",
                    summary=f"有 {count} 条公开互动等待查看或处理。",
                    payload={"pending_events": count},
                    created_at=now,
                )
            )

        if inbox.get("dm_error"):
            calm = _calm_health_reason(inbox["dm_error"], fallback="私信轮询刚才没能连上，稍后会再确认。")
            events.append(
                HeartbeatEvent(
                    kind="dm_health",
                    source="dm",
                    dedup_key=f"health:dm:err:{content_hash(calm)[:12]}",
                    severity="attention",
                    title="私信通道状态提醒",
                    summary=calm,
                    payload={"dm_pending": int(inbox.get("dm_pending") or 0)},
                    created_at=now,
                )
            )
        elif int(inbox.get("dm_pending") or 0) > 0:
            dm_count = int(inbox["dm_pending"])
            events.append(
                HeartbeatEvent(
                    kind="dm_health",
                    source="dm",
                    dedup_key=f"health:dm:pending:{dm_count}",
                    severity="info",
                    title="有待确认或未完结的私信草稿",
                    summary=f"当前有 {dm_count} 条私信草稿/待核对条目。",
                    payload={"dm_pending": dm_count},
                    created_at=now,
                )
            )

        dyn = snapshot.get("dynamics") or {}
        if int(dyn.get("uncertain") or 0) > 0:
            events.append(
                HeartbeatEvent(
                    kind="dynamics_health",
                    source="dynamics",
                    dedup_key=f"health:dynamics:uncertain:{int(dyn['uncertain'])}",
                    severity="urgent",
                    title="动态发送结果待核对",
                    summary="有一条动态提交还没拿到最终确认，记得去核对一下避免重复发。",
                    payload={"uncertain": int(dyn["uncertain"])},
                    created_at=now,
                )
            )
        elif int(dyn.get("pending_review") or 0) > 0:
            cnt = int(dyn["pending_review"])
            events.append(
                HeartbeatEvent(
                    kind="dynamics_health",
                    source="dynamics",
                    dedup_key=f"health:dynamics:review:{cnt}",
                    severity="info",
                    title="有新的动态草稿待查看",
                    summary=f"有 {cnt} 条公开动态草稿写好了，等你看过再发。",
                    payload={"pending_review": cnt},
                    created_at=now,
                )
            )

        for topic in snapshot.get("hot_topics") or ():
            bvid = str(topic.get("bvid") or "").strip()
            if not bvid:
                continue
            if not topic.get("popular") and not topic.get("summary"):
                continue
            title = str(topic.get("title") or bvid)[:120]
            events.append(
                HeartbeatEvent(
                    kind="hot_topic",
                    source="discovery",
                    dedup_key=f"hot_topic:{bvid}",
                    severity="info",
                    title=f"发现值得留意的视频话题：{title}",
                    summary=str(topic.get("summary") or f"刚看了《{title}》，觉得这个话题挺有意思。")[:300],
                    payload={
                        "bvid": bvid,
                        "title": title,
                        "tags": list(topic.get("tags") or ())[:6],
                        "popular": bool(topic.get("popular")),
                        "url": topic.get("url") or f"https://www.bilibili.com/video/{bvid}",
                    },
                    created_at=now,
                )
            )
        return events

    async def research_topic(
        self,
        topic: str,
        *,
        bvid: str | None = None,
        bypass_cooldown: bool = False,
    ) -> dict[str, Any]:
        """Run bounded curiosity-driven public web research for a trending video or topic."""
        cleaned_query = " ".join(str(topic or "").split())[:120]
        if not cleaned_query:
            return {"ok": False, "sources": [], "summary": ""}
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
        summary = (
            f"顺手查了「{cleaned_query}」，看到相关介绍：{sources[0]['excerpt'][:140]}"
            if sources
            else ""
        )
        dedup_suffix = bvid or content_hash(cleaned_query)[:12]
        event = HeartbeatEvent(
            kind="curiosity_research",
            source="curiosity",
            dedup_key=f"curiosity:{dedup_suffix}",
            severity="info",
            title=f"延伸查证：{cleaned_query[:80]}",
            summary=summary or f"对「{cleaned_query}」做了公开资料检索。",
            payload={"query": cleaned_query, "bvid": bvid, "sources": sources},
            created_at=float(self._clock()),
        )
        emission = await self.bus.publish(
            event,
            cooldown_seconds=self.policy().event_cooldown_seconds,
            bypass_cooldown=bypass_cooldown,
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
        browsing = self._current_browsing()
        tz = business_timezone(browsing) if browsing is not None else "Asia/Taipei"
        today = datetime.fromtimestamp(float(self._clock()), ZoneInfo(tz)).date()
        bounds = day_bounds(tz, today)
        row = self.store.one(
            "SELECT COUNT(*) AS n FROM agent_heartbeat_pulses "
            "WHERE character_id=? AND account_id=? AND created_at>=? AND created_at<?",
            (character_id, account_id, bounds["start_at"], bounds["next_at"]),
        )
        return int(row["n"]) if row else 0

    def _last_pulse_at(self, *, account_id: str, character_id: str) -> float:
        row = self.store.one(
            "SELECT MAX(created_at) AS ts FROM agent_heartbeat_pulses WHERE character_id=? AND account_id=?",
            (character_id, account_id),
        )
        return float(row["ts"]) if row and row["ts"] is not None else 0.0

    def _pulse_rate_limited(self, policy: HeartbeatPolicy, *, account_id: str, character_id: str) -> str | None:
        now = float(self._clock())
        last_at = self._last_pulse_at(account_id=account_id, character_id=character_id)
        if last_at and now - last_at < policy.pulse_cooldown_seconds:
            return "pulse_cooldown"
        if policy.max_pulses_per_day > 0:
            if self._pulses_today_count(account_id=account_id, character_id=character_id) >= policy.max_pulses_per_day:
                return "daily_pulse_limit"
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
            parts.append(f"{owner}，我这边刚盯到一件需要你看一下的事：{urgent[0].summary}")
        elif attention:
            parts.append(f"{owner}，顺便跟你说一声：{attention[0].summary}")
        elif hot:
            top = hot[0]
            parts.append(f"{owner}，我刚刷到「{top.payload.get('title', top.title)}」，感觉挺值得聊聊的。")
        else:
            parts.append(f"{owner}，我这边刚完成一轮巡检，{events[0].summary}")
        if research:
            src = research[0].payload["sources"][0]
            parts.append(f"我还顺手去搜了点资料（{src.get('title') or '公开来源'}），大致说的是：{src.get('excerpt', '')[:110]}")
        elif len(events) > 1 and not research:
            extra = events[1]
            if extra.kind not in {"curiosity_research"}:
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
        sys_msg = build_self_model_system_message(
            char,
            channel="owner_chat",
            scene="常驻心跳主动简报",
            task_intent=(
                f"把下面刚发生的值得注意的后台动静或热点查证，用 Aqua 自然干净的第一人称口吻写一条简短简报给{owner}。"
                "控制在 2-3 句话内；只写已给材料里的事实，绝不编造链接、不堆砌道具词、不输出 JSON、不吐原始报错。"
            ),
            operating_account=facts,
            situation="后台常驻心跳巡检发现新的状态变化或值得分享的话题。",
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
        messages = [
            sys_msg,
            {"role": "user", "content": dump(prompt_payload)},
        ]

        if callable(self._brief_generator):
            try:
                raw = await self._brief_generator(messages, char)
                cleaned = sanitize_pulse_text(raw, owner_label=owner)
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
        primary = events[0]
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
        payload = {
            "event_ids": [e.event_id for e in events],
            "kinds": [e.kind for e in events],
        }
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

    async def tick(
        self,
        *,
        snapshot: dict[str, Any] | None = None,
        force_brief: bool = False,
        bypass_cooldown: bool = False,
    ) -> dict[str, Any]:
        """Run one unified heartbeat cycle: gather signals, research hot topics, emit rate-limited brief."""
        async with self._tick_lock:
            policy = self.policy()
            now = float(self._clock())
            self._last_tick_at = now
            snap = snapshot if snapshot is not None else heartbeat_pulse_snapshot(
                self._current_browsing(), store=self.store
            )
            self._last_snapshot = snap
            char = self._current_character()
            account_id = str(snap.get("account_id") or "")

            candidate_events = self._signals_from_snapshot(snap)
            emitted_events: list[HeartbeatEvent] = []
            suppressed: list[dict[str, str]] = []

            for ev in candidate_events:
                res = await self.bus.publish(
                    ev,
                    cooldown_seconds=policy.event_cooldown_seconds,
                    bypass_cooldown=bypass_cooldown,
                )
                if res.accepted:
                    emitted_events.append(ev)
                else:
                    suppressed.append({"dedup_key": ev.dedup_key, "reason": res.reason})

            research_sources: list[dict[str, str]] = []
            if policy.curiosity_research_enabled and policy.curiosity_max_queries_per_tick > 0:
                hot_events = [e for e in emitted_events if e.kind == "hot_topic"]
                for hot in hot_events[: policy.curiosity_max_queries_per_tick]:
                    query = _extract_research_query(hot.payload)
                    research = await self.research_topic(
                        query,
                        bvid=hot.payload.get("bvid"),
                        bypass_cooldown=bypass_cooldown,
                    )
                    if research.get("emitted"):
                        ev = HeartbeatEvent.model_validate(research["event"])
                        emitted_events.append(ev)
                    research_sources.extend(research.get("sources") or ())

            if not emitted_events:
                return {
                    "ok": True,
                    "tick_at": now,
                    "emitted": 0,
                    "suppressed": suppressed,
                    "pulse": None,
                    "rate_limited": None,
                }

            limit_reason = (
                None
                if force_brief
                else self._pulse_rate_limited(policy, account_id=account_id, character_id=char["id"])
            )
            if limit_reason is not None:
                return {
                    "ok": True,
                    "tick_at": now,
                    "emitted": len(emitted_events),
                    "suppressed": suppressed,
                    "pulse": None,
                    "rate_limited": limit_reason,
                }

            brief = await self._compose_owner_brief(char, emitted_events, research_sources)
            pulse = self._persist_pulse(
                account_id=account_id,
                char=char,
                events=emitted_events,
                brief=brief,
                research_sources=research_sources,
            )
            if policy.surface_to_chat and callable(self.on_pulse):
                outcome = self.on_pulse(pulse)
                if asyncio.iscoroutine(outcome):
                    await outcome
            return {
                "ok": True,
                "tick_at": now,
                "emitted": len(emitted_events),
                "suppressed": suppressed,
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

    def status(self) -> dict[str, Any]:
        running = bool(self._task and not self._task.done())
        try:
            policy = self.policy()
            char_id = str(self._current_character().get("id") or "")
            recent_count = len(self.list_pulses(limit=20, character_id=char_id))
        except Exception:
            policy = HeartbeatPolicy(enabled=False)
            recent_count = 0
        return {
            "enabled": policy.enabled,
            "running": running,
            "closed": self._closed,
            "tick_interval_seconds": policy.tick_interval_seconds,
            "pulse_cooldown_seconds": policy.pulse_cooldown_seconds,
            "max_pulses_per_day": policy.max_pulses_per_day,
            "last_tick_at": self._last_tick_at,
            "bus": self.bus.stats(),
            "recent_pulse_count": recent_count,
        }
