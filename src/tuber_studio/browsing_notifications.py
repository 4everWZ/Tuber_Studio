"""Durable notification pages; release a complete scan in source order only.

The platform cursor is opaque and lives inside a versioned scan envelope in
the existing cursor column. A separate buffer links events to their scan;
the event payload and nine-column public inbox schema remain unchanged.
"""

import asyncio
import hashlib
import json
import math
import time
from uuid import uuid4

from .providers import public_error
from .store import dump


class NotificationInbox:
    def __init__(self, store, reader, adapter):
        self.store, self.reader, self.adapter = store, reader, adapter
        self.lock = asyncio.Lock()
        self.last = {"complete": True, "feeds": {}, "read_pages": 0, "buffered": 0, "released": 0}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS browsing_notification_buffer(
                event_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL,account_id TEXT NOT NULL,kind TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS browsing_notification_scan ON browsing_notification_buffer(scan_id);
        """)

    @staticmethod
    def _number(value):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or value >= 100_000_000_000
        ):
            raise ValueError("通知时间须为有效秒级时间，不接受毫秒或无效数值")
        return float(value)

    def _row(self, account, kind):
        return self.store.one("SELECT * FROM browsing_cursors WHERE account_id=? AND kind=?", (account, kind))

    def _archive(self, account, kind, reason):
        self.store.db.execute(
            "UPDATE browsing_events SET status='historical',error=? WHERE account_id=? AND kind=? "
            "AND status IN ('buffered','received','source_verified','context_ready')",
            (reason, account, kind),
        )
        self.store.db.execute(
            "DELETE FROM browsing_notification_buffer WHERE account_id=? AND kind=?", (account, kind)
        )

    def _start(self, account, kind, enabled, started):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self._row(account, kind)
            old = json.loads(row["cursor"]) if row and row["cursor"] else None
            is_envelope = isinstance(old, dict) and old.get("version") == 1
            changed_lifecycle = not is_envelope or (
                old.get("enabled_at") != enabled or old.get("process_started_at") != started
            )
            if changed_lifecycle:
                self._archive(account, kind, "启停／重启后旧通知只保留历史，不自动追发")
            if is_envelope and not changed_lifecycle and old.get("phase") in {"scanning", "blocked"}:
                return row, old
            boundary = max(
                int(enabled), int(started), int((old or {}).get("discard_before", 0)) if is_envelope else 0
            )
            baseline = float(row["watermark"]) if row else float(boundary)
            state = {
                "version": 1,
                "phase": "scanning",
                "scan_id": str(uuid4()),
                "page_cursor": None,
                "baseline": baseline,
                "latest": max(baseline, boundary),
                "initial": not bool(row and row["initialized"]),
                "cutoff": boundary,
                "enabled_at": enabled,
                "process_started_at": started,
                "discard_before": int(old.get("discard_before", 0)) if is_envelope else 0,
                "started_at": time.time(),
                "pages": 0,
                "seen_cursors": [],
            }
            self.store.db.execute(
                "INSERT INTO browsing_cursors(account_id,kind,watermark,cursor,initialized) VALUES(?,?,?,?,?) "
                "ON CONFLICT(account_id,kind) DO UPDATE SET cursor=excluded.cursor",
                (account, kind, baseline, dump(state), int(not state["initial"])),
            )
            return self._row(account, kind), state

    @staticmethod
    def _page(page, account, kind):
        if not isinstance(page, dict) or str(page.get("account_id")) != account:
            raise ValueError("通知账号范围不一致")
        items = page.get("items")
        if not isinstance(items, list) or len(items) > 100 or type(page.get("has_more")) is not bool:
            raise ValueError("通知页、完整性或事件数量无效")
        normalized = []
        for event in items:
            if (
                not isinstance(event, dict)
                or str(event.get("account_id")) != account
                or event.get("kind") != kind
                or not isinstance(event.get("event_id"), (str, int))
                or isinstance(event.get("event_id"), bool)
                or not 1 <= len(str(event["event_id"])) <= 100
            ):
                raise ValueError("通知不能证明当前账号、事件类型或原始身份")
            timestamp = NotificationInbox._number(event.get("timestamp"))
            payload = dump(event)
            if len(payload.encode()) > 128 * 1024:
                raise ValueError("通知原文超过单事件上限")
            identity = hashlib.sha256(dump([account, kind, str(event["event_id"])]).encode()).hexdigest()
            normalized.append((identity, timestamp, event, payload))
        cursor = page.get("cursor")
        if len(dump(cursor).encode()) > 16384:
            raise ValueError("通知分页游标过大")
        return normalized, cursor, page["has_more"]

    def _commit_page(self, account, kind, expected, state, page):
        events, page_cursor, has_more = self._page(page, account, kind)
        state = dict(state)
        cursor_key = dump(page_cursor)
        stalled = has_more and (
            page_cursor is None
            or cursor_key == dump(state["page_cursor"])
            or cursor_key in state["seen_cursors"]
        )
        state.update(
            page_cursor=page_cursor,
            pages=state["pages"] + 1,
            latest=max([state["latest"], *[event[1] for event in events]]),
        )
        if has_more and not stalled:
            state["seen_cursors"] = [*state["seen_cursors"], cursor_key]
        if stalled:
            state.update(phase="blocked", diagnostic="平台分页游标未前进，保留缓冲且停止重复读取")
        counters = {key: 0 for key in ("inserted", "duplicates", "released", "historical", "unsupported")}
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            current = self._row(account, kind)
            if current != expected:
                raise ValueError("通知扫描所有权已变化，旧分页结果未推进游标")
            for identity, timestamp, event, payload in events:
                supported = event.get("resource_type") in {"video", "dynamic"}
                inserted = self.store.db.execute(
                    "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        account,
                        kind,
                        str(event.get("comment_id") or ""),
                        timestamp,
                        "buffered" if supported else "unsupported",
                        payload,
                        None if supported else "该通知资源尚未支持，不自动猜测路由",
                        time.time(),
                    ),
                ).rowcount
                counters["inserted" if inserted else "duplicates"] += 1
                if inserted:
                    self.store.db.execute(
                        "INSERT INTO browsing_notification_buffer VALUES(?,?,?,?)",
                        (identity, state["scan_id"], account, kind),
                    )
            complete = not has_more
            if complete:
                buffered = self.store.all(
                    "SELECT e.id,e.timestamp,e.status FROM browsing_events e JOIN browsing_notification_buffer b "
                    "ON b.event_id=e.id WHERE b.scan_id=?",
                    (state["scan_id"],),
                )
                for event in buffered:
                    if event["status"] == "unsupported":
                        counters["unsupported"] += 1
                        continue
                    # Event IDs are the deduplication truth. Late, out-of-order
                    # new IDs within this authorization are not swallowed by a
                    # timestamp-only high-water mark, including same-second IDs.
                    fresh = not state["initial"] and event["timestamp"] >= state["cutoff"]
                    self.store.db.execute(
                        "UPDATE browsing_events SET status=? WHERE id=? AND status='buffered'",
                        ("received" if fresh else "historical", event["id"]),
                    )
                    counters["released" if fresh else "historical"] += 1
                self.store.db.execute(
                    "DELETE FROM browsing_notification_buffer WHERE scan_id=?", (state["scan_id"],)
                )
                state.update(phase="idle", page_cursor=None, scan_id=None, seen_cursors=[])
            watermark = state["latest"] if complete else current["watermark"]
            self.store.db.execute(
                "UPDATE browsing_cursors SET watermark=?,cursor=?,initialized=? WHERE account_id=? AND kind=?",
                (watermark, dump(state), int(bool(current["initialized"]) or complete), account, kind),
            )
        return self._row(account, kind), state, counters, complete

    def _stats(self, account, kind, row, state, pages=0, counters=None):
        pending = self.store.one(
            "SELECT COUNT(*) AS n FROM browsing_notification_buffer WHERE account_id=? AND kind=?",
            (account, kind),
        )["n"]
        complete = state["phase"] == "idle"
        age = max(0, time.time() - state["started_at"])
        diagnostic = state.get("diagnostic") or (
            "" if complete else "通知分页仍未完成；缓冲事件尚未进入自动回复，下一轮继续"
        )
        if not complete and (state["pages"] >= 6 or age >= 180):
            diagnostic += "；已跨多轮，请检查平台分页与读取预算"
        return {
            "complete": complete,
            "blocked": state["phase"] == "blocked",
            "pages": pages,
            "total_pages": state["pages"],
            "buffered": pending,
            "initialized": bool(row["initialized"]),
            "watermark": row["watermark"],
            "scan_age_seconds": age,
            "diagnostic": diagnostic,
            **(
                counters
                or {key: 0 for key in ("inserted", "duplicates", "released", "historical", "unsupported")}
            ),
        }

    async def sync(self, account_id, config, enabled_at, started_at):
        account = str(account_id or "")
        if not account or len(account) > 100:
            raise ValueError("同步通知需要已核验的账号身份")
        enabled = self._number(enabled_at or 0)
        started = self._number(started_at)
        maximum = config["max_pages"]
        if type(maximum) is not int or not 1 <= maximum <= 2:
            raise ValueError("每次通知同步最多读取1到2页")
        async with self.lock:
            report = {
                "account_id": account,
                "complete": True,
                "feeds": {},
                "read_pages": 0,
                "buffered": 0,
                "released": 0,
            }
            self.last = report
            feeds = [
                kind
                for kind, enabled in (
                    ("mention", config["mentions_enabled"]),
                    ("reply", config["replies_enabled"]),
                )
                if enabled
            ]
            if feeds:
                turn_key = "browsing_notification_turn:" + account
                turn = int(self.store.get(turn_key, 0)) % len(feeds)
                feeds = feeds[turn:] + feeds[:turn]
                self.store.set(turn_key, (turn + 1) % len(feeds))
            for index, kind in enumerate(feeds):
                row, state = self._start(account, kind, enabled, started)
                pages = 0
                remaining = maximum - report["read_pages"]
                allowance = max(1, remaining // (len(feeds) - index)) if remaining else 0
                totals = {
                    key: 0 for key in ("inserted", "duplicates", "released", "historical", "unsupported")
                }
                try:
                    while pages < allowance and state["phase"] != "blocked":
                        page = await self.reader(self.adapter.notifications, kind, state["page_cursor"])
                        row, state, counters, complete = self._commit_page(account, kind, row, state, page)
                        pages += 1
                        for key, value in counters.items():
                            totals[key] += value
                        if complete:
                            break
                except Exception as error:
                    report["complete"] = False
                    report["feeds"][kind] = self._stats(account, kind, row, state, pages, totals) | {
                        "error": public_error(error),
                        "diagnostic": "通知读取或持久化失败，游标未越过未保存事件",
                    }
                    raise
                stats = self._stats(account, kind, row, state, pages, totals)
                report["feeds"][kind] = stats
                report["complete"] &= stats["complete"]
                report["read_pages"] += pages
                report["buffered"] += stats["buffered"]
                report["released"] += stats["released"]
            return report

    def discard_pending(self, account_id=None):
        """A stop fences unfinished scans; it does not delete source audit rows."""
        rows = self.store.all(
            "SELECT * FROM browsing_cursors" + (" WHERE account_id=?" if account_id is not None else ""),
            (str(account_id),) if account_id is not None else (),
        )
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            for row in rows:
                self._archive(row["account_id"], row["kind"], "停止后旧通知只保留历史，不自动追发")
                state = json.loads(row["cursor"]) if row["cursor"] else {}
                if not isinstance(state, dict) or state.get("version") != 1:
                    state = {"version": 1}
                state.update(
                    phase="idle",
                    scan_id=None,
                    page_cursor=None,
                    seen_cursors=[],
                    discard_before=int(time.time()),
                )
                self.store.db.execute(
                    "UPDATE browsing_cursors SET cursor=? WHERE account_id=? AND kind=?",
                    (dump(state), row["account_id"], row["kind"]),
                )
