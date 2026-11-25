"""Persistent SQLite deduplication, edge-triggered state, signal evaluation, and quiet-hours helpers."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...core.models import content_hash
from ...core.store import dump
from .bus import HeartbeatEvent

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_RETIRED_LABEL = "\u5de5\u574a\u53f7\u4e3b"
_RAW_ERROR_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|ValidationError|pydantic|File \"[^\"]+\"|line \d+|"
    r"HTTPStatusError|ConnectError|TimeoutException|SESSDATA|bili_jct|sk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)

_DEFAULT_STATE: dict[str, Any] = {
    "active_faults": {},
    "dynamics_pending_ids": [],
    "dynamics_pending_count": 0,
    "dynamics_latest_created_at": 0.0,
    "dm_pending_ids": [],
    "dm_pending_count": 0,
    "dm_latest_created_at": 0.0,
    "inbox_pending_count": 0,
    "suppressions": [],
}


def validate_hhmm(value: str) -> str:
    """Validate an optional HH:MM 24-hour clock string (empty string disables)."""
    cleaned = str(value or "").strip()
    if cleaned == "":
        return ""
    if not _HHMM_RE.match(cleaned):
        raise ValueError("时间格式须为 HH:MM（00:00-23:59）或留空关闭")
    return cleaned


def _parse_minutes(hhmm: str) -> int | None:
    cleaned = str(hhmm or "").strip()
    if not _HHMM_RE.match(cleaned):
        return None
    hour, minute = cleaned.split(":", 1)
    return int(hour) * 60 + int(minute)


def is_in_quiet_hours(now: float, tz_name: str, start_hhmm: str, end_hhmm: str) -> bool:
    """Return True when `now` falls within [start_hhmm, end_hhmm) in `tz_name`."""
    start_min = _parse_minutes(start_hhmm)
    end_min = _parse_minutes(end_hhmm)
    if start_min is None or end_min is None or start_min == end_min:
        return False
    try:
        tz = ZoneInfo(tz_name or "Asia/Taipei")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Taipei")
    dt = datetime.fromtimestamp(float(now), tz)
    current_min = dt.hour * 60 + dt.minute
    if start_min < end_min:
        return start_min <= current_min < end_min
    return current_min >= start_min or current_min < end_min


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
    return " ".join(cleaned.split())[:600]


def is_auth_error(text: str | None) -> bool:
    low = str(text or "").lower()
    return bool(low and ("401" in low or "登录过期" in low or "登录已失效" in low or "expired" in low))


def is_risk_cooldown_error(text: str | None) -> bool:
    low = str(text or "").lower()
    return bool(low and ("412" in low or "风控" in low or "冷却" in low))


def calm_health_reason(raw_error: str | None, *, fallback: str) -> str:
    text = str(raw_error or "").strip()
    if not text:
        return fallback
    if is_auth_error(text):
        return "B站登录状态可能过期了，需要重新扫码确认。"
    if is_risk_cooldown_error(text) or "429" in text:
        return "B站读取暂时触发了平台冷却，正在避让等待恢复。"
    if "超时" in text or "timeout" in text.lower() or "连接" in text:
        return "上游服务刚刚连接不太稳，稍后会自动重试。"
    if _RAW_ERROR_RE.search(text):
        return fallback
    return sanitize_pulse_text(text, owner_label="你")[:140] or fallback


def pending_summary_from_snapshot(snap: dict[str, Any] | None) -> dict[str, int]:
    """Compute UI badge counters directly from a pulse snapshot."""
    snap = snap or {}
    inbox = snap.get("inbox") or {}
    dyn = snap.get("dynamics") or {}
    writes = snap.get("writes") or {}
    return {
        "dynamics_pending_review": int(dyn.get("pending_review") or 0),
        "dm_pending": int(inbox.get("dm_pending") or 0),
        "inbox_pending_events": int(inbox.get("pending_events") or 0),
        "uncertain_writes": int(writes.get("uncertain_count") or 0) + int(dyn.get("uncertain") or 0),
    }


def detect_pending_delta(
    ids: list[str],
    count: int,
    latest_ts: float,
    prev_ids: list[str],
    prev_count: int,
    prev_latest_ts: float,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Detect whether genuinely new pending drafts arrived compared to persisted state."""
    clean_ids = [str(item) for item in ids if str(item)]
    clean_count = max(0, int(count or 0))
    clean_latest = float(latest_ts or 0.0)
    if clean_ids:
        prev_set = {str(item) for item in prev_ids if str(item)}
        new_ids = [item for item in clean_ids if item not in prev_set]
        return (
            bool(new_ids),
            new_ids,
            {"ids": clean_ids, "count": max(clean_count, len(clean_ids)), "latest_ts": clean_latest},
        )
    if clean_count == 0:
        return False, [], {"ids": [], "count": 0, "latest_ts": 0.0}
    has_new = (
        int(prev_count or 0) == 0
        or (clean_latest > 0.0 and clean_latest > float(prev_latest_ts or 0.0))
        or clean_count > int(prev_count or 0)
    )
    return has_new, [], {"ids": [], "count": clean_count, "latest_ts": clean_latest}


def collect_urgent_faults(snapshot: dict[str, Any], now: float) -> dict[str, HeartbeatEvent]:
    """Extract all currently active urgent faults keyed by stable fault key."""
    faults: dict[str, HeartbeatEvent] = {}
    disc = snapshot.get("discovery") or {}
    inbox = snapshot.get("inbox") or {}
    dyn = snapshot.get("dynamics") or {}
    writes = snapshot.get("writes") or {}

    if snapshot.get("control_state") == "emergency":
        faults["fault:control:emergency"] = HeartbeatEvent(
            kind="discovery_health",
            source="discovery",
            dedup_key="health:control:emergency",
            severity="urgent",
            title="自主运营处于紧急停止状态",
            summary="所有自动探索与互动发送都已暂停，确认安全后可再解除。",
            payload={"state": "emergency"},
            created_at=now,
        )

    auth_error = snapshot.get("account_error")
    if not auth_error:
        for candidate_err in (disc.get("error"), inbox.get("dm_error"), dyn.get("error")):
            if is_auth_error(candidate_err):
                auth_error = candidate_err
                break
    if auth_error:
        faults["fault:account:auth"] = HeartbeatEvent(
            kind="discovery_health",
            source="account",
            dedup_key="health:account:auth",
            severity="urgent",
            title="B站账号凭证异常",
            summary=calm_health_reason(str(auth_error), fallback="B站登录状态需要重新扫码确认。"),
            payload={"account_error": str(auth_error)[:160]},
            created_at=now,
        )

    cooldown_until = disc.get("read_cooldown_until")
    if (cooldown_until and float(cooldown_until) > now) or is_risk_cooldown_error(disc.get("error")):
        faults["fault:discovery:cooldown"] = HeartbeatEvent(
            kind="discovery_health",
            source="discovery",
            dedup_key="health:discovery:cooldown",
            severity="urgent",
            title="发现线触发平台读取冷却",
            summary="B站读取触发了平台风控冷却，已先避让等待恢复；私信和评论回应还在正常守着。",
            payload={"read_cooldown_until": cooldown_until},
            created_at=now,
        )

    if inbox.get("state") == "frozen":
        faults["fault:inbox:frozen"] = HeartbeatEvent(
            kind="inbox_health",
            source="inbox",
            dedup_key="health:inbox:frozen",
            severity="urgent",
            title="互动收件箱因待核对提交已冻结",
            summary="有一条平台发送回执结果待核对，已先暂停新的自动发送。",
            payload={"pending_events": int(inbox.get("pending_events") or 0)},
            created_at=now,
        )

    if int(dyn.get("uncertain") or 0) > 0:
        faults["fault:dynamics:uncertain"] = HeartbeatEvent(
            kind="dynamics_health",
            source="dynamics",
            dedup_key="health:dynamics:uncertain",
            severity="urgent",
            title="动态发送结果待核对",
            summary="有一条动态提交还没拿到最终确认，先别重复发，方便时去核对一下。",
            payload={"uncertain": int(dyn["uncertain"])},
            created_at=now,
        )

    if int(writes.get("uncertain_count") or 0) > 0:
        faults["fault:writes:uncertain"] = HeartbeatEvent(
            kind="inbox_health",
            source="writes",
            dedup_key="health:writes:uncertain",
            severity="urgent",
            title="有发送回执结果待核对",
            summary="有一次平台发送还没拿到最终回执，先确认结果再继续更稳妥。",
            payload={"uncertain_count": int(writes["uncertain_count"])},
            created_at=now,
        )
    return faults


class HeartbeatStateStore:
    """SQLite-backed deduplication windows and edge-triggered state scoped by (character_id, account_id)."""

    def __init__(self, store) -> None:
        self.store = store
        self._install_storage()

    def _install_storage(self) -> None:
        with self.store.db:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_heartbeat_dedup(
                    scope_key TEXT PRIMARY KEY,
                    dedup_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    last_emitted_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_heartbeat_dedup_expires
                    ON agent_heartbeat_dedup(expires_at);
                CREATE TABLE IF NOT EXISTS agent_heartbeat_state(
                    scope_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def state_scope_key(character_id: str, account_id: str) -> str:
        """Build a collision-free scope key for `(character_id, account_id)` state."""
        return dump([str(character_id or ""), str(account_id or "")])

    @staticmethod
    def dedup_scope_key(character_id: str, account_id: str, dedup_key: str) -> str:
        """Build a collision-free scope key for `(character_id, account_id, dedup_key)`."""
        return dump([str(character_id or ""), str(account_id or ""), str(dedup_key)])

    def is_dedup_active(
        self,
        character_id: str,
        account_id: str,
        dedup_key: str,
        *,
        now: float | None = None,
        digest: str | None = None,
    ) -> bool:
        """Return True if an unexpired dedup record exists for `(character_id, account_id, dedup_key)`."""
        current = time.time() if now is None else float(now)
        scope = self.dedup_scope_key(character_id, account_id, dedup_key)
        row = self.store.one("SELECT digest, expires_at FROM agent_heartbeat_dedup WHERE scope_key=?", (scope,))
        if not row or float(row["expires_at"]) <= current:
            return False
        if digest is not None and str(row["digest"]) != str(digest):
            return False
        return True

    def record_dedup(
        self,
        character_id: str,
        account_id: str,
        dedup_key: str,
        *,
        kind: str,
        digest: str = "",
        ttl_seconds: float,
        now: float | None = None,
    ) -> None:
        """Persist or refresh a deduplication entry until `now + ttl_seconds`."""
        current = time.time() if now is None else float(now)
        ttl = float(ttl_seconds)
        scope = self.dedup_scope_key(character_id, account_id, dedup_key)
        with self.store.db:
            if ttl <= 0:
                self.store.db.execute("DELETE FROM agent_heartbeat_dedup WHERE scope_key=?", (scope,))
                return
            self.store.db.execute(
                "INSERT OR REPLACE INTO agent_heartbeat_dedup("
                "scope_key, dedup_key, kind, digest, last_emitted_at, expires_at"
                ") VALUES(?,?,?,?,?,?)",
                (scope, str(dedup_key), str(kind), str(digest), current, current + ttl),
            )

    def clear_dedup(
        self,
        character_id: str | None = None,
        account_id: str | None = None,
        dedup_key: str | None = None,
    ) -> None:
        """Delete one or all persisted dedup records."""
        with self.store.db:
            if dedup_key is not None and character_id is not None and account_id is not None:
                scope = self.dedup_scope_key(character_id, account_id, dedup_key)
                self.store.db.execute("DELETE FROM agent_heartbeat_dedup WHERE scope_key=?", (scope,))
            elif dedup_key is not None:
                self.store.db.execute("DELETE FROM agent_heartbeat_dedup WHERE dedup_key=?", (str(dedup_key),))
            else:
                self.store.db.execute("DELETE FROM agent_heartbeat_dedup")

    def load_state(self, character_id: str, account_id: str) -> dict[str, Any]:
        """Load persisted edge-triggered state for `(character_id, account_id)`."""
        scope = self.state_scope_key(character_id, account_id)
        row = self.store.one("SELECT state_json FROM agent_heartbeat_state WHERE scope_key=?", (scope,))
        state = {
            key: (dict(val) if isinstance(val, dict) else list(val) if isinstance(val, list) else val)
            for key, val in _DEFAULT_STATE.items()
        }
        if not row or not row["state_json"]:
            return state
        try:
            parsed = json.loads(row["state_json"])
        except (TypeError, ValueError):
            return state
        if not isinstance(parsed, dict):
            return state
        if isinstance(parsed.get("active_faults"), dict):
            state["active_faults"] = dict(parsed["active_faults"])
        for list_key in ("dynamics_pending_ids", "dm_pending_ids"):
            if isinstance(parsed.get(list_key), list):
                state[list_key] = [str(x) for x in parsed[list_key] if str(x)]
        for int_key in ("dynamics_pending_count", "dm_pending_count", "inbox_pending_count"):
            if isinstance(parsed.get(int_key), (int, float)):
                state[int_key] = max(0, int(parsed[int_key]))
        for float_key in ("dynamics_latest_created_at", "dm_latest_created_at"):
            if isinstance(parsed.get(float_key), (int, float)):
                state[float_key] = float(parsed[float_key])
        if isinstance(parsed.get("suppressions"), list):
            state["suppressions"] = [item for item in parsed["suppressions"] if isinstance(item, dict)][-30:]
        return state

    def save_state(
        self,
        character_id: str,
        account_id: str,
        state: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Persist edge-triggered state for `(character_id, account_id)`."""
        current = time.time() if now is None else float(now)
        normalized = dict(_DEFAULT_STATE) | dict(state)
        normalized["suppressions"] = [
            item for item in (normalized.get("suppressions") or []) if isinstance(item, dict)
        ][-30:]
        scope = self.state_scope_key(character_id, account_id)
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO agent_heartbeat_state(scope_key, state_json, updated_at) VALUES(?,?,?)",
                (scope, dump(normalized), current),
            )
        return normalized


def evaluate_snapshot(
    state_store: HeartbeatStateStore,
    snapshot: dict[str, Any],
    persisted_state: dict[str, Any],
    *,
    character_id: str,
    account_id: str,
    now: float,
    bypass_cooldown: bool,
    clear_bus_cooldown=None,
) -> dict[str, Any]:
    """Evaluate snapshot against persisted state into edge-triggered faults, drafts, hot topics, and suppressions."""
    suppressions: list[dict[str, Any]] = []
    current_faults = collect_urgent_faults(snapshot, now)
    prev_faults = dict(persisted_state.get("active_faults") or {})
    fault_events: list[tuple[str, HeartbeatEvent]] = []
    for fkey, ev in current_faults.items():
        if bypass_cooldown or fkey not in prev_faults:
            fault_events.append((fkey, ev))
        else:
            suppressions.append({
                "at": now,
                "reason": "no_state_delta",
                "dedup_key": ev.dedup_key,
                "kind": ev.kind,
                "detail": "fault_still_active",
            })
    recovered_faults = [k for k in prev_faults if k not in current_faults]
    for rkey in recovered_faults:
        old_dedup = prev_faults[rkey]
        if callable(clear_bus_cooldown) and isinstance(old_dedup, str) and old_dedup:
            clear_bus_cooldown(old_dedup)

    disc = snapshot.get("discovery") or {}
    inbox = snapshot.get("inbox") or {}
    dyn = snapshot.get("dynamics") or {}
    attention_events: list[HeartbeatEvent] = []
    for err_val, kind, source, title, fallback, extra in (
        (
            disc.get("error") if not is_risk_cooldown_error(disc.get("error")) else None,
            "discovery_health",
            "discovery",
            "发现线状态提醒",
            "发现线刚才遇到了一点波动，已暂缓本轮。",
            {"state": disc.get("state", "blocked")},
        ),
        (
            inbox.get("dm_error"),
            "dm_health",
            "dm",
            "私信通道状态提醒",
            "私信轮询刚才没能连上，稍后会再确认。",
            {"dm_pending": int(inbox.get("dm_pending") or 0)},
        ),
        (
            dyn.get("error"),
            "dynamics_health",
            "dynamics",
            "动态通道状态提醒",
            "动态草稿通道刚才遇到一点波动，稍后再确认。",
            {},
        ),
    ):
        if err_val and not is_auth_error(err_val):
            calm = calm_health_reason(err_val, fallback=fallback)
            attention_events.append(
                HeartbeatEvent(
                    kind=kind,
                    source=source,
                    dedup_key=f"health:{source}:err:{content_hash(calm)[:12]}",
                    severity="attention",
                    title=title,
                    summary=calm,
                    payload=extra,
                    created_at=now,
                )
            )

    draft_events: list[tuple[str, HeartbeatEvent, dict[str, Any]]] = []
    cleared_updates: dict[str, Any] = {}

    for tag, ids, count, latest_ts, p_ids, p_cnt, p_ts, kind, key_prefix, title, msg_fn in (
        (
            "dynamics",
            list(dyn.get("pending_ids") or ()),
            int(dyn.get("pending_review") or 0),
            float(dyn.get("pending_latest_created_at") or 0.0),
            list(persisted_state.get("dynamics_pending_ids") or ()),
            int(persisted_state.get("dynamics_pending_count") or 0),
            float(persisted_state.get("dynamics_latest_created_at") or 0.0),
            "dynamics_health",
            "health:dynamics:review",
            "有新的动态草稿待查看",
            lambda n: f"有 {n} 条公开动态草稿写好了，有空看一眼再定。",
        ),
        (
            "dm",
            list(inbox.get("dm_pending_ids") or ()),
            int(inbox.get("dm_pending") or 0),
            float(inbox.get("dm_latest_created_at") or 0.0),
            list(persisted_state.get("dm_pending_ids") or ()),
            int(persisted_state.get("dm_pending_count") or 0),
            float(persisted_state.get("dm_latest_created_at") or 0.0),
            "dm_health",
            "health:dm:pending",
            "有新的私信草稿或待处理消息",
            lambda n: f"刚多出待确认的私信条目，现在一共 {n} 条。",
        ),
    ):
        has_new, new_ids, next_fields = detect_pending_delta(ids, count, latest_ts, p_ids, p_cnt, p_ts)
        if has_new or (bypass_cooldown and next_fields["count"] > 0):
            cnt = next_fields["count"]
            draft_events.append((
                tag,
                HeartbeatEvent(
                    kind=kind,
                    source=tag,
                    dedup_key=f"{key_prefix}:{content_hash(next_fields)[:12]}",
                    severity="info",
                    title=title,
                    summary=msg_fn(cnt),
                    payload={"count": cnt, "new_ids": new_ids, "pending_ids": next_fields["ids"]},
                    created_at=now,
                ),
                next_fields,
            ))
        else:
            prefix = "dynamics" if tag == "dynamics" else "dm"
            cleared_updates[f"{prefix}_pending_ids"] = next_fields["ids"]
            cleared_updates[f"{prefix}_pending_count"] = next_fields["count"]
            cleared_updates[f"{prefix}_latest_created_at"] = next_fields["latest_ts"]
            if next_fields["count"] > 0:
                suppressions.append({
                    "at": now,
                    "reason": "no_state_delta",
                    "dedup_key": key_prefix,
                    "kind": kind,
                    "detail": f"pending={next_fields['count']}",
                })

    inbox_cnt = int(inbox.get("pending_events") or 0)
    prev_inbox_cnt = int(persisted_state.get("inbox_pending_count") or 0)
    if inbox_cnt >= 3 and (bypass_cooldown or prev_inbox_cnt < 3 or inbox_cnt > prev_inbox_cnt):
        draft_events.append((
            "inbox",
            HeartbeatEvent(
                kind="inbox_health",
                source="inbox",
                dedup_key=f"health:inbox:backlog:{inbox_cnt}",
                severity="info",
                title="评论区和互动消息有新动静",
                summary=f"有 {inbox_cnt} 条公开互动等待查看或处理。",
                payload={"pending_events": inbox_cnt},
                created_at=now,
            ),
            {"count": inbox_cnt},
        ))
    else:
        cleared_updates["inbox_pending_count"] = inbox_cnt
        if inbox_cnt >= 3:
            suppressions.append({
                "at": now,
                "reason": "no_state_delta",
                "dedup_key": "health:inbox:backlog",
                "kind": "inbox_health",
                "detail": f"pending_events={inbox_cnt}",
            })

    hot_events: list[HeartbeatEvent] = []
    for topic in snapshot.get("hot_topics") or ():
        bvid = str(topic.get("bvid") or "").strip()
        if not bvid or (not topic.get("popular") and not topic.get("summary")):
            continue
        dedup_key = f"hot_topic:{bvid}"
        if not bypass_cooldown and state_store.is_dedup_active(character_id, account_id, dedup_key, now=now):
            suppressions.append({
                "at": now,
                "reason": "persisted_dedup",
                "dedup_key": dedup_key,
                "kind": "hot_topic",
                "detail": f"bvid={bvid}",
            })
            continue
        title = str(topic.get("title") or bvid)[:120]
        hot_events.append(
            HeartbeatEvent(
                kind="hot_topic",
                source="discovery",
                dedup_key=dedup_key,
                severity="info",
                title=f"刚刷到个有意思的视频：{title}",
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

    return {
        "current_faults": current_faults,
        "recovered_faults": recovered_faults,
        "fault_events": fault_events,
        "attention_events": attention_events,
        "draft_events": draft_events,
        "cleared_updates": cleared_updates,
        "hot_events": hot_events,
        "suppressions": suppressions,
    }
