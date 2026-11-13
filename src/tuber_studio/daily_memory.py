"""Source-backed long-term memories and stored community highlights."""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from .store import dump as dump_memory

ANECDOTE_CATEGORIES = ("comment_anecdote", "live_highlight", "community_meme")
HIGHLIGHT_CATEGORIES = (*ANECDOTE_CATEGORIES, "public_incident")
HIGHLIGHT_STATUSES = ("active", "pending_review", "archived")
MEMORY_CHANNELS = ("live", "comment", "dynamic")
HIGHLIGHT_ACCOUNT_FILTER = (
    "COALESCE(CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.operation_account_id') END,?)=?"
)


class MemoryChanged(ValueError):
    """A generated reply refers to memory that changed before delivery."""


RETROSPECTIVE_CUES = (
    "还记得",
    "记不记得",
    "记得吗",
    "还记不记得",
    "记得不",
    "忘记没",
    "有印象",
    "上次",
    "上回",
    "之前",
    "那天",
    "那次",
    "那时候",
    "当时",
    "曾经",
    "以前",
    "上周",
    "上个月",
    "前几天",
    "前段时间",
    "早前",
)
RETROSPECTIVE_PATTERN = re.compile(
    r"还?记得|记不记得|有印象|忘记没|上次|上回|之前|那天|那次|那时候|当时|曾经|以前|上周|上个月|前几天|前段时间|早前"
)

SCAFFOLDING_WORDS = {
    "你",
    "我",
    "他",
    "她",
    "它",
    "们",
    "还",
    "记得",
    "记不记得",
    "记得吗",
    "有印象",
    "忘记",
    "上次",
    "上回",
    "之前",
    "那天",
    "那次",
    "那时候",
    "当时",
    "曾经",
    "以前",
    "上周",
    "上个月",
    "前几天",
    "几天前",
    "十天前",
    "前段时间",
    "早前",
    "昨天",
    "今天",
    "明天",
    "有人",
    "观众",
    "弹幕",
    "说",
    "道",
    "问",
    "吗",
    "呢",
    "吧",
    "呀",
    "啊",
    "哦",
    "嗯",
    "什么",
    "怎么",
    "哪里",
    "哪个",
    "为什么",
    "是不是",
    "有没有",
    "能不能",
    "那件事",
    "那事",
    "事情",
    "关于",
    "大肥鱼",
    "肥鱼",
    "主播",
    "鲸鱼",
    "知道",
    "觉得",
    "不是",
    "就是",
    "那个",
    "这个",
}

class DailyMemoryService:
    def __init__(self, store):
        self.store = store
        if store is None:
            return
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(daily_highlights)")}
        for name, definition in (("incident_key", "TEXT"), ("public_sources", "TEXT NOT NULL DEFAULT '[]'"),
                                 ("source_evidence", "TEXT")):
            if name not in columns:
                store.execute(f"ALTER TABLE daily_highlights ADD COLUMN {name} {definition}")
        store.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS daily_incident_identity "
            "ON daily_highlights(character_id,character_version,incident_key) WHERE incident_key IS NOT NULL"
        )
        for row in store.all("SELECT * FROM daily_highlights WHERE source_evidence IS NULL AND category!='public_incident'"):
            proof = self._legacy_source_evidence(row)
            if proof:
                store.execute("UPDATE daily_highlights SET source_evidence=? WHERE id=?",
                              (json.dumps(proof, ensure_ascii=False), row["id"]))
        # Explicitly withdrawn public experiences share the legacy status. Only
        # promote automatically organized or source-verified distilled memories.
        store.execute(
            """UPDATE daily_highlights SET status='active' WHERE status='pending_review'
               AND CASE WHEN json_valid(source_evidence) THEN
                 (json_extract(source_evidence,'$.memory_kind') IN ('private','experience')
                  OR (json_extract(source_evidence,'$.kind') IN ('viewer','live')
                   AND category!='public_incident' AND source_ref NOT LIKE 'operator-public:%'))
               END"""
        )

    @staticmethod
    def _source_proof(kind, source_ref, quote):
        return {"version": 1, "kind": kind, "source_ref": source_ref, "quote": quote,
                "confirmed_by": "operator" if kind == "operator_note" else "runtime_event" if kind == "runtime_event" else "source_match"}

    def _legacy_source_evidence(self, row):
        ref, quote = row.get("source_ref") or "", row.get("quote") or ""
        if not quote:
            return None
        match = re.fullmatch(r"video:(.+):comment:(.+)", ref)
        if match:
            source = self.store.one("SELECT content FROM comments WHERE platform='bilibili' AND video_id=? AND comment_id=?", match.groups())
            if source and quote in source["content"]:
                return self._source_proof("viewer", ref, quote)
        elif ref.startswith("live:"):
            for source in self.store.all("SELECT event_text,reply_text FROM conversations WHERE session_id=? AND character_id=?",
                                         (ref.removeprefix("live:"), row.get("character_id"))):
                text = f"观众说：{source['event_text']} | 主播回：{source['reply_text']}" if source["reply_text"] else source["event_text"]
                if quote in text:
                    return self._source_proof("live", ref, quote)
        return None

    def _source_evidence(self, row):
        if row.get("category") == "public_incident":
            return {"kind": "operator_note"}
        try:
            proof = json.loads(row.get("source_evidence") or "null")
        except (TypeError, ValueError):
            proof = None
        if isinstance(proof, dict) and proof.get("version") == 1 and proof.get("kind") in {
            "viewer", "live", "runtime_event", "operator_note"
        } and (
            proof.get("source_ref") == (row.get("source_ref") or "") and proof.get("quote") == row.get("quote")
        ) and proof.get("confirmed_by") == {
            "viewer": "source_match", "live": "source_match", "runtime_event": "runtime_event", "operator_note": "operator"
        }.get(proof.get("kind")):
            return proof
        return self._legacy_source_evidence(row)

    def organized(self, scope, *, limit=100, item_id=None):
        rows = self.store.all(
            "SELECT * FROM daily_highlights WHERE character_id=? AND character_version=? "
            "AND CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.memory_kind') END IS NOT NULL "
            "AND CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.operation_account_id') END=? "
            "AND (? IS NULL OR id=?) "
            "ORDER BY updated_at DESC,id DESC LIMIT ?",
            (scope[1], scope[2], scope[0], item_id, item_id, limit),
        )
        result = []
        for row in rows:
            if proof := self._source_evidence(row):
                result.append(row | {"memory_kind": proof["memory_kind"], "source": proof["memory_source"],
                                     "channels": proof.get("channels", []), "proof": proof})
        return result

    def preference(self, scope):
        account, character, _ = scope
        return self.store.get(f"memory_preference:{account}:{character}", {"enabled": True, "revision": 0})

    def set_preference(self, scope, enabled):
        account, character, _ = scope
        current = self.preference(scope)
        if current["enabled"] != enabled:
            current = {"enabled": enabled, "revision": current["revision"] + 1}
            self.store.set(f"memory_preference:{account}:{character}", current)
        return current

    def save_organized(self, scope, source, decision, existing):
        key = source["key"]
        kind = source["kind"]
        rows = self.organized(scope)
        quote = source["text"]
        tags = sorted({tag.strip()[:24] for tag in decision.tags if tag.strip()})
        headline = decision.headline.strip()
        signature = re.sub(r"\W", "", headline).casefold()
        target = source.get("scene")
        for row in rows:
            if row["memory_kind"] != kind:
                continue
            if row["proof"].get("scene") != target:
                continue
            if row["source_ref"] == key or kind == "private" and (
                row["quote"] == quote or re.sub(r"\W", "", row["headline"]).casefold() == signature
            ):
                existing = row
                break
        if existing:
            latest = self.organized(scope, item_id=existing["id"])
            if not latest or latest[0]["status"] == "archived" or latest[0]["memory_kind"] != kind:
                return False
            existing = latest[0]
            if kind == "experience":
                # Keep the reviewed text and permissions, with the latest verified sources.
                proof = existing["proof"]
                previous = proof["memory_source"]
                first, *other = previous["references"]
                references = {item["href"]: item for item in [*other, *source["references"]]
                              if item["href"] != first["href"]}
                previous["references"] = [first, *list(references.values())[-7:]]
                self.store.db.execute("UPDATE daily_highlights SET source_evidence=?,updated_at=? WHERE id=?",
                                      (dump_memory(proof), time.time(), existing["id"]))
                return True
        if self.store.one(
            "SELECT 1 FROM memory_organization WHERE account=? AND character=? AND version=? AND "
            "source IN (?,?,?) AND status='deleted'",
            (*scope, key, "private:" + signature, f"deleted-preference:{decision.existing_id}"),
        ):
            return False
        source_ref = existing["source_ref"] if existing else key
        proof = self._source_proof("operator_note" if kind == "private" else "viewer", source_ref, quote)
        proof.update(memory_kind=kind, operation_account_id=scope[0], channels=[],
                     memory_source={"key": key, "at": source["at"], "references": source.get("references", []),
                                    "thread": source.get("anchor")})
        if kind == "private":
            previous = existing["proof"].get("preference_sources", [source_ref]) if existing else []
            proof["preference_sources"] = list(dict.fromkeys([*previous, key]))
        if target:
            proof["scene"] = target
            proof["channels"] = [target]
            proof["style_group"] = source["style_group"]
        at = time.time()
        fields = (headline, quote, decision.note.strip(), dump_memory(tags), dump_memory(proof), at)
        if existing:
            self.store.db.execute(
                "UPDATE daily_highlights SET headline=?,quote=?,anecdote=?,tags=?,source_evidence=?,updated_at=? WHERE id=?",
                (*fields, existing["id"]),
            )
        else:
            self.store.db.execute(
                "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,source_ref,source_user,"
                "status,created_at,headline,quote,anecdote,tags,source_evidence,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope[1], scope[2], self.business_date(source["at"]), "community_meme", source_ref,
                 "号主" if kind == "private" else "", "active", at, *fields),
            )
        return True

    def change_organized(self, scope, item_id, *, channels=None, headline=None, note=None, delete=False):
        rows = self.organized(scope, item_id=item_id)
        if not rows:
            return None
        row, = rows
        proof = row["proof"]
        if headline is not None and not headline.strip() or note is not None and not note.strip():
            raise ValueError("记忆标题和内容不能为空")
        with self.store.db:
            if delete:
                keys = {row["source_ref"], row["source"]["key"]}
                if row["memory_kind"] == "private":
                    keys.update(proof.get("preference_sources", []))
                    if not proof.get("scene"):
                        keys.add("private:" + re.sub(r"\W", "", row["headline"]).casefold())
                    keys.add(f"deleted-preference:{item_id}")
                self.store.db.executemany(
                    "INSERT INTO memory_organization VALUES(?,?,?,?,?,'deleted',?) "
                    "ON CONFLICT(account,character,version,source) DO UPDATE SET status='deleted',updated_at=excluded.updated_at",
                    [(*scope, key, "", time.time()) for key in keys],
                )
                self.store.db.execute("DELETE FROM daily_highlights WHERE id=?", (item_id,))
                if row["memory_kind"] == "private":
                    context_scope = "operator:" + dump_memory([scope[0], scope[1]])
                    self.store.db.execute(
                        "DELETE FROM context_history_audit WHERE window_key IN "
                        "(SELECT key FROM context_windows WHERE mode=? AND character_id=? AND character_version=?)",
                        (context_scope, scope[1], scope[2]),
                    )
                    self.store.db.execute("DELETE FROM context_windows WHERE mode=? AND character_id=? AND character_version=?",
                                          (context_scope, scope[1], scope[2]))
                    self.store.db.executemany(
                        "DELETE FROM grounded_memory WHERE scope=? AND character_id=? AND character_version=? "
                        "AND event_id=?",
                        [(context_scope, scope[1], scope[2], "operator:" + key.removeprefix("turn:"))
                         for key in keys if key.startswith("turn:")],
                    )
                return {"id": item_id, "deleted": True}
            if channels is not None:
                if row["memory_kind"] != "experience" or set(channels) - set(MEMORY_CHANNELS):
                    raise ValueError("这条记忆的渠道授权无效")
                proof["channels"] = [channel for channel in MEMORY_CHANNELS if channel in channels]
            if row["status"] != "active":
                proof["channels"] = []
            self.store.db.execute(
                "UPDATE daily_highlights SET headline=?,anecdote=?,source_evidence=?,updated_at=? WHERE id=?",
                (headline or row["headline"], note or row["anecdote"], dump_memory(proof), time.time(), item_id),
            )
        return self.organized(scope, item_id=item_id)[0]

    def live_preferences(self, scope):
        if not self.preference(scope)["enabled"]:
            return []
        account, character, version = scope
        rows = self.store.all(
            "SELECT id FROM daily_highlights WHERE character_id=? AND character_version=? AND status='active' "
            "AND CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.scene') END='live' "
            "AND json_extract(source_evidence,'$.operation_account_id')=? ORDER BY updated_at DESC,id DESC LIMIT 4",
            (character, version, account),
        )
        return [item for row in rows for item in self.organized(scope, item_id=row["id"])]

    def memory_match(self, row, text):
        return self._score_highlight_match(row, self._extract_recall_keywords(text), text)

    def recall_organized(self, scope, channel, text, *, resource=None, thread=None, participant=None):
        if not self.preference(scope)["enabled"]:
            return []
        matched = []
        for row in self.organized(scope):
            if row["status"] != "active" or channel != "operator" and channel not in row["channels"]:
                continue
            anchor = row["source"].get("thread") or {}
            same_thread = bool(thread and resource == anchor.get("resource") and str(thread) == anchor.get("root_id"))
            same_person = bool(participant and any(str(ref.get("author_uid")) == str(participant)
                               for ref in row["source"]["references"]))
            score = self.memory_match(row, text)
            if channel == "comment" and anchor and not same_thread:
                continue
            if same_thread or score >= 3:
                matched.append((20 * same_thread + 5 * same_person + score, row))
        relevant = [row for _, row in sorted(matched, key=lambda item: -item[0]) if not row["proof"].get("scene")]
        return (self.live_preferences(scope) + relevant)[:4] if channel == "live" else relevant[:3]

    def validate_organized(self, scope, channel, refs):
        for item in refs:
            rows = self.organized(scope, item_id=item["id"])
            if (not rows or rows[0]["updated_at"] != item["updated_at"] or rows[0]["status"] != "active"
                    or channel != "operator" and channel not in rows[0]["channels"]):
                raise MemoryChanged("生成使用的记忆已变更，请重试")

    @staticmethod
    def organized_reference(row):
        return {"id": row["id"], "updated_at": row["updated_at"]}

    def record_public_incident(
        self,
        character_id,
        character_version,
        *,
        incident_key,
        highlight_date,
        headline,
        anecdote,
        public_sources,
        tags=None,
    ):
        """Record an operator-confirmed event, never a model-generated personality rule."""
        if not isinstance(incident_key, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", incident_key
        ):
            raise ValueError("公开经历需要稳定事件编号")
        datetime.date.fromisoformat(highlight_date)
        if not isinstance(headline, str) or not 1 <= len(headline.strip()) <= 40:
            raise ValueError("公开经历标题须为1至40字")
        if not isinstance(anecdote, str) or not 1 <= len(anecdote.strip()) <= 150:
            raise ValueError("公开经历事实须为1至150字")
        if not isinstance(public_sources, list) or not 1 <= len(public_sources) <= 8:
            raise ValueError("公开经历须绑定1至8条公开来源")
        if tags is not None and (
            not isinstance(tags, list)
            or len(tags) > 12
            or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 40 for tag in tags)
        ):
            raise ValueError("经历关键词须为至多12个短词")
        sources, seen = [], set()
        for source in public_sources:
            if not isinstance(source, dict) or set(source) != {"bvid", "thread_id", "comment_id", "quote"}:
                raise ValueError("公开经历只接受公开视频、评论编号和原话")
            bvid, comment_id, quote = (source[key] for key in ("bvid", "comment_id", "quote"))
            thread_id = source["thread_id"]
            if (
                not isinstance(bvid, str)
                or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
                or not isinstance(comment_id, str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", comment_id)
                or not isinstance(thread_id, str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", thread_id)
                or not isinstance(quote, str)
                or not 1 <= len(quote.strip()) <= 2000
            ):
                raise ValueError("公开经历来源格式无效")
            identity = (bvid, comment_id)
            if identity in seen:
                raise ValueError("公开经历来源不能重复")
            seen.add(identity)
            sources.append(
                {
                    "bvid": bvid,
                    "thread_id": thread_id,
                    "comment_id": comment_id,
                    "quote": quote.strip(),
                    "url": f"https://www.bilibili.com/video/{bvid}#reply{comment_id}",
                }
            )
        existing = self.store.one(
            "SELECT id FROM daily_highlights WHERE character_id=? AND character_version=? AND incident_key=?",
            (character_id, character_version, incident_key),
        )
        if existing:
            return existing["id"]
        now = time.time()
        cursor = self.store.execute(
            "INSERT OR IGNORE INTO daily_highlights(character_id,character_version,highlight_date,category,headline,"
            "source_ref,source_user,quote,anecdote,tags,status,created_at,updated_at,incident_key,public_sources) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                character_id,
                character_version,
                highlight_date,
                "public_incident",
                headline.strip(),
                "incident:" + incident_key,
                "公开评论区",
                sources[0]["quote"],
                anecdote.strip(),
                json.dumps(tags or [], ensure_ascii=False),
                "active",
                now,
                now,
                incident_key,
                json.dumps(sources, ensure_ascii=False),
            ),
        )
        return (
            cursor.lastrowid
            if cursor.rowcount
            else self.store.one(
                "SELECT id FROM daily_highlights WHERE character_id=? AND character_version=? AND incident_key=?",
                (character_id, character_version, incident_key),
            )["id"]
        )

    def context(self, character, query_text="", *, video_id=None, thread_id=None, comment_id=None):
        """Recall factual experiences by their public floor, or relevant conversation text."""
        result = []
        query_words = self._extract_recall_keywords(query_text)
        sources = self.public_incident_sources(character["id"], character["version"])
        sources.extend(self.public_experience_sources(character["id"], character["version"]))
        for source in sources:
            linked = any(
                ref["bvid"] == str(video_id or "")
                and (
                    thread_id is not None
                    and ref.get("thread_id") == str(thread_id)
                    or comment_id is not None
                    and ref["comment_id"] == str(comment_id)
                )
                for ref in source["public_sources"]
            )
            row = self.store.one(
                "SELECT tags FROM daily_highlights WHERE id=?", (int(source["id"].split(":")[1]),)
            )
            tags = json.loads(row["tags"])
            relevant = any(tag and tag in query_text for tag in tags) or any(
                len(word) >= 2 and word in source["text"] for word in query_words
            )
            if linked or relevant:
                result.append(source)
        return result[:2]

    def public_experience_sources(self, character_id, character_version):
        rows = self.store.all(
            "SELECT * FROM daily_highlights WHERE character_id=? AND character_version=? AND status='active' "
            "AND (source_ref LIKE 'system:downtime:%' OR source_ref LIKE 'operator-public:%') "
            "ORDER BY highlight_date DESC,id DESC LIMIT 10",
            (character_id, character_version),
        )
        return [
            {"id": f"experience:{row['id']}", "type": proof["kind"], "fictional": False,
             "text": row["quote"], "public_sources": []}
            for row in rows if (proof := self._source_evidence(row))
            and proof["kind"] in {"runtime_event", "operator_note"}
            and not proof.get("operation_sources")
        ]

    def business_date(self, now):
        zone = self.store.get("browsing_policy", {}).get("business_timezone", "Asia/Taipei")
        return datetime.datetime.fromtimestamp(now, ZoneInfo(zone)).date().isoformat()

    def public_incident_sources(self, character_id, character_version):
        rows = self.store.all(
            "SELECT id,incident_key,headline,anecdote,public_sources FROM daily_highlights "
            "WHERE character_id=? AND character_version=? AND category='public_incident' AND status='active' "
            "ORDER BY highlight_date DESC,id DESC LIMIT 20",
            (character_id, character_version),
        )
        return [
            {
                "id": f"incident:{row['id']}",
                "type": "public_incident",
                "fictional": False,
                "text": row["headline"] + "：" + row["anecdote"],
                "incident_key": row["incident_key"],
                "public_sources": json.loads(row["public_sources"]),
            }
            for row in rows
        ]

    def record_highlight(
        self,
        character_id: str,
        character_version: int,
        highlight_date: str,
        category: str,
        headline: str,
        quote: str,
        anecdote: str,
        *,
        source_ref: str = "",
        source_user: str = "",
        tags: list[str] | None = None,
        status: str = "active",
        source_evidence: dict | None = None,
        operator_confirmed: bool = False,
    ) -> int:
        """Insert or record a verified daily highlight."""
        if category not in ANECDOTE_CATEGORIES:
            raise ValueError(f"Unsupported category: {category}")
        if status not in HIGHLIGHT_STATUSES:
            raise ValueError(f"Unsupported status: {status}")
        if not headline.strip() or not quote.strip() or not anecdote.strip():
            raise ValueError("Headline, quote and anecdote are required")

        now = time.time()
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        source_ref, quote = source_ref.strip()[:100], quote.strip()
        proof = self._source_proof("operator_note", source_ref, quote) if operator_confirmed else source_evidence
        if proof is None:
            proof = self._legacy_source_evidence({"source_ref": source_ref, "quote": quote, "character_id": character_id})
        cursor = self.store.execute(
            """
            INSERT INTO daily_highlights(
                character_id, character_version, highlight_date, category,
                headline, source_ref, source_user, quote, anecdote,
                tags, status, created_at, updated_at, source_evidence
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                character_id,
                character_version,
                highlight_date,
                category,
                headline.strip()[:40],
                source_ref.strip()[:100],
                source_user.strip()[:30],
                quote.strip(),
                anecdote.strip()[:150],
                tags_json,
                status,
                now,
                now,
                json.dumps(proof, ensure_ascii=False) if proof else None,
            ),
        )
        self.store.db.commit()
        return cursor.lastrowid

    def update_active_timestamp(self, current_time: float | None = None):
        """Update system last active timestamp."""
        now = current_time or time.time()
        self.store.set("system_runtime_heartbeat_at", now)

    def check_and_record_downtime(
        self,
        character_id: str,
        character_version: int,
        current_time: float | None = None,
    ) -> dict | None:
        """Inspect last active timestamp and record a downtime highlight if offline for >= 1 day."""
        now = current_time or time.time()
        last_active = self.store.get("system_runtime_heartbeat_at")
        self.update_active_timestamp(now)
        if last_active is None:
            return None
        try:
            last_active = float(last_active)
        except (ValueError, TypeError):
            return None

        gap_seconds = now - last_active
        if gap_seconds < 86400:
            return None

        days = max(1, int(gap_seconds // 86400))
        today_str = self.business_date(now)
        source_ref = f"system:downtime:{int(last_active)}"

        existing = self.store.one(
            "SELECT id FROM daily_highlights WHERE character_id=? AND character_version=? AND source_ref=?",
            (character_id, character_version, source_ref),
        )
        if existing:
            return None

        headline = f"工作台恢复运行（间隔约 {days} 天）"
        quote = f"本次启动距上次工作台运行记录约 {days} 天。"
        anecdote = quote
        tags = ["停机", "开机", "睡了好久", "小窝", "没开机", "好几天", "休眠", "去哪了", "失踪"]
        proof = {
            "version": 1,
            "kind": "runtime_event",
            "source_ref": source_ref,
            "quote": quote,
            "confirmed_by": "runtime_event",
        }
        highlight_id = self.record_highlight(
            character_id=character_id,
            character_version=character_version,
            highlight_date=today_str,
            category="community_meme",
            headline=headline,
            quote=quote,
            anecdote=anecdote,
            source_ref=source_ref,
            source_user="工作站饲养员",
            tags=tags,
            status="active",
            source_evidence=proof,
        )
        return {
            "id": highlight_id,
            "days": days,
            "headline": headline,
            "anecdote": anecdote,
        }

    def get_active_anecdotes(
        self,
        character_id: str,
        character_version: int,
        *,
        query_text: str = "",
        days: int = 3,
        limit: int = 2,
        max_tokens: int = 160,
        include_incidents: bool = True,
    ) -> list[dict[str, Any]]:
        """Retrieve bounded, active anecdotes for prompt injection.

        Strictly limits count and length to prevent context bloat.
        """
        today = datetime.date.today()
        cutoff = (today - datetime.timedelta(days=days)).isoformat()

        rows = self.store.all(
            """
            SELECT id, highlight_date, category, headline, source_user, quote, anecdote, tags,source_ref,source_evidence,character_id
            FROM daily_highlights
            WHERE character_id = ? AND character_version = ? AND status = 'active'
              AND highlight_date >= ?
            ORDER BY highlight_date DESC, id DESC
            LIMIT 20
            """,
            (character_id, character_version, cutoff),
        )
        if not rows:
            return []

        # Score and rank relevance if query_text is given
        scored = []
        for row in rows:
            proof = self._source_evidence(row)
            if not proof or proof.get("memory_kind"):
                continue
            if (row.get("source_ref") or "").startswith(("operator-public:", "system:downtime:")):
                continue  # Only the relevant public-experience path recalls these facts.
            if row["category"] == "public_incident" and not include_incidents:
                continue
            score = 0
            if query_text:
                q = query_text.lower()
                tags = json.loads(row["tags"] or "[]")
                for tag in tags:
                    if tag.lower() in q:
                        score += 3
                if row["headline"].lower() in q:
                    score += 2
                for word in ("昨天", "评论", "上次", "视频", "尾巴", "饭", "连跪"):
                    if word in q and (word in row["anecdote"] or word in row["headline"]):
                        score += 2
            if row["category"] == "public_incident" and query_text and score == 0:
                continue
            scored.append((score, row))

        # Sort by relevance score, then recent date
        scored.sort(key=lambda pair: (pair[0], pair[1]["highlight_date"]), reverse=True)

        selected = []
        total_chars = 0
        char_cap = max_tokens * 2  # Chinese characters roughly 1-2 tokens

        for _, row in scored:
            anecdote = row["anecdote"]
            if total_chars + len(anecdote) > char_cap and selected:
                break
            selected.append(
                {
                    "id": row["id"],
                    "date": row["highlight_date"],
                    "topic": row["headline"],
                    "memory": anecdote,
                    "source_role": self._source_evidence(row)["kind"],
                    "source_ref": row["source_ref"],
                }
            )
            total_chars += len(anecdote)
            if len(selected) >= limit:
                break

        return selected

    @staticmethod
    def is_retrospective_query(text: str) -> bool:
        """Fast check to see if user text asks about past memories or retrospective events."""
        if not text or not isinstance(text, str):
            return False
        return bool(RETROSPECTIVE_PATTERN.search(text))

    @staticmethod
    def _extract_recall_keywords(query: str) -> set[str]:
        """Extract meaningful Chinese phrases and alphanumeric keywords by stripping conversational scaffolding."""
        if not query or not isinstance(query, str):
            return set()
        clean = query
        for word in sorted(SCAFFOLDING_WORDS, key=len, reverse=True):
            clean = clean.replace(word, " ")

        tokens = re.findall(r"[\u4e00-\u9fff]{2,8}|[a-zA-Z0-9_]{2,16}", clean)
        keywords = set(tokens)
        for token in tokens:
            # Generate 2-gram and 3-gram sub-tokens for flexible matching
            if re.match(r"^[\u4e00-\u9fff]+$", token):
                for n in (2, 3):
                    for i in range(len(token) - n + 1):
                        keywords.add(token[i : i + n])
        return keywords

    @staticmethod
    def _score_highlight_match(row: dict[str, Any], keywords: set[str], query_text: str) -> int:
        """Score relevance of a highlight row against query keywords and full text."""
        score = 0
        tags = json.loads(row.get("tags") or "[]")
        headline = (row.get("headline") or "").strip()
        quote = (row.get("quote") or "").strip()
        anecdote = (row.get("anecdote") or "").strip()

        # 1. Tags match (highest semantic weight)
        q_lower = query_text.lower()
        for tag in tags:
            t_clean = tag.strip().lower()
            if not t_clean:
                continue
            if t_clean in q_lower:
                score += 5
            elif any(
                kw.lower() == t_clean or kw.lower() in t_clean or t_clean in kw.lower() for kw in keywords
            ):
                score += 4

        # 2. Headline, Quote, and Anecdote matching with multi-match accumulation
        fields = [
            (headline.lower(), 3),
            (quote.lower(), 2),
            (anecdote.lower(), 2),
        ]
        for field, weight in fields:
            if not field:
                continue
            matches = 0
            for kw in keywords:
                kw_l = kw.lower()
                if len(kw_l) >= 2 and kw_l in field:
                    matches += 1
                    if len(kw_l) >= 3:
                        score += 1  # Bonus for longer specific multi-char match
            score += min(matches * weight, 6)

        return score

    def recall_historical_highlight(
        self,
        character_id: str,
        character_version: int,
        query_text: str,
        *,
        exclude_ids: set[int] | None = None,
        max_chars: int = 70,
        min_score: int = 3,
    ) -> dict[str, Any] | None:
        """Passive fallback retrieval for archived or older highlights when user asks retrospective questions.

        Strictly returns at most 1 item (<60-70 chars) to prevent context bloat.
        Returns None if not a retrospective question or no high-confidence match found.
        """
        if not self.is_retrospective_query(query_text):
            return None

        keywords = self._extract_recall_keywords(query_text)
        if not keywords:
            return None

        rows = self.store.all(
            """
            SELECT id, highlight_date, category, headline, source_user, quote, anecdote, tags,source_ref,source_evidence,character_id
            FROM daily_highlights
            WHERE character_id = ? AND character_version = ?
              AND status IN ('active', 'archived')
            ORDER BY highlight_date DESC, id DESC
            LIMIT 100
            """,
            (character_id, character_version),
        )
        if not rows:
            return None

        exclude = exclude_ids or set()
        best_score = 0
        best_row = None

        for row in rows:
            if row["id"] in exclude:
                continue
            proof = self._source_evidence(row)
            if not proof or proof.get("operation_sources") or proof.get("memory_kind"):
                continue
            score = self._score_highlight_match(row, keywords, query_text)
            if score > best_score:
                best_score = score
                best_row = row

        if not best_row or best_score < min_score:
            return None

        anecdote = (best_row["anecdote"] or "").strip()
        if len(anecdote) > max_chars:
            anecdote = anecdote[: max_chars - 1] + "…"

        return {
            "id": best_row["id"],
            "date": best_row["highlight_date"],
            "headline": best_row["headline"],
            "memory": anecdote,
            "source_role": self._source_evidence(best_row)["kind"],
            "source_ref": best_row["source_ref"],
            "source_user": best_row["source_user"],
            "score": best_score,
            "formatted_clue": f"[{best_row['highlight_date']} {best_row['headline']}: {anecdote}]",
        }

    def list_highlights(
        self,
        character_id: str,
        character_version: int,
        *,
        date_str: str | None = None,
        status: str | None = None,
        limit: int = 50,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List daily highlights for UI visualization and editing."""
        clauses = ["character_id = ?", "character_version = ?"]
        params: list[Any] = [character_id, character_version]
        if account_id is not None:
            clauses.append(HIGHLIGHT_ACCOUNT_FILTER)
            params.extend((account_id, account_id))
        if date_str:
            clauses.append("highlight_date = ?")
            params.append(date_str)
        if status:
            clauses.append("status = ?")
            params.append(status)

        query = f"""
            SELECT id, character_id, character_version, highlight_date, category,
                   headline, source_ref, source_user, quote, anecdote, tags, status,
                   created_at, updated_at, incident_key, public_sources
            FROM daily_highlights
            WHERE {" AND ".join(clauses)}
              AND CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.memory_kind') END IS NULL
            ORDER BY highlight_date DESC, id DESC
            LIMIT ?
        """
        params.append(limit)
        rows = self.store.all(query, tuple(params))
        result = []
        for r in rows:
            item = dict(r)
            item["tags"] = json.loads(item["tags"] or "[]")
            item["public_sources"] = json.loads(item["public_sources"] or "[]")
            result.append(item)
        return result

    def update_highlight(self, highlight_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update headline, anecdote, tags or status of a highlight."""
        allowed = {"headline", "anecdote", "status", "category", "tags"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return None

        if "status" in filtered and filtered["status"] not in HIGHLIGHT_STATUSES:
            raise ValueError(f"Invalid status: {filtered['status']}")
        if "category" in filtered and filtered["category"] not in HIGHLIGHT_CATEGORIES:
            raise ValueError(f"Invalid category: {filtered['category']}")
        existing = self.store.one("SELECT category FROM daily_highlights WHERE id=?", (highlight_id,))
        if (
            "category" in filtered
            and existing
            and filtered["category"] != existing["category"]
            and ("public_incident" in (filtered["category"], existing["category"]))
        ):
            raise ValueError("公开经历分类与来源绑定，不可转换分类")
        if "tags" in filtered:
            filtered["tags"] = json.dumps(filtered["tags"], ensure_ascii=False)

        filtered["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [highlight_id]

        self.store.execute(f"UPDATE daily_highlights SET {set_clause} WHERE id = ?", tuple(values))
        self.store.db.commit()
        row = self.store.one("SELECT * FROM daily_highlights WHERE id = ?", (highlight_id,))
        if not row:
            return None
        res = dict(row)
        res["tags"] = json.loads(res["tags"] or "[]")
        return res

    def delete_highlight(self, highlight_id: int) -> bool:
        """Permanently remove a highlight entry."""
        cursor = self.store.execute("DELETE FROM daily_highlights WHERE id = ?", (highlight_id,))
        self.store.db.commit()
        return bool(cursor.rowcount)
