"""Daily episodic memory and community highlights consolidation.

Distills interesting anecdotes, jokes, and milestones from daily comments and
live streams into compact, grounded character memories for cross-channel banter.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)

DISTILLED_CATEGORIES = ("comment_anecdote", "live_highlight", "community_meme")
HIGHLIGHT_CATEGORIES = (*DISTILLED_CATEGORIES, "public_incident")
HIGHLIGHT_STATUSES = ("active", "pending_review", "archived")

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

DISTILLATION_PROMPT = (
    "从今天的互动中挑最多三件适合日后聊起的趣事，返回包含 highlights 数组的 JSON。"
    "每项填 category、headline、source_ref、quote、anecdote 和 tags。"
    "评论区趣事用 comment_anecdote，直播高光用 live_highlight，社群新梗用 community_meme。"
    "headline 用十五字以内，quote 摘录所选素材的连续原文。"
    "anecdote 以当前角色的第一人称，用三十到七十字聊聊素材中的事情。"
)


def desensitize_user(display_name: str | None) -> str:
    """Mask or sanitize user names to protect viewer privacy."""
    if not display_name or not isinstance(display_name, str):
        return "某位热心观众"
    clean = display_name.strip()
    # Strip explicit uid patterns if embedded like "user_123456"
    clean = re.sub(r"^u(?:ser)?[_-]?\d{4,}", "观众", clean, flags=re.I)
    if len(clean) > 20:
        clean = clean[:18] + "…"
    return clean or "某位热心观众"


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
        ]

    def confirm_operator_experience(self, character, request_id, fact, *, sources=None):
        source_ref = f"operator-public:{request_id}"
        previous = self.store.one(
            "SELECT id,quote FROM daily_highlights WHERE character_id=? AND character_version=? AND source_ref=?",
            (character["id"], character["version"], source_ref),
        )
        if previous:
            if previous["quote"] != fact:
                raise ValueError("这段对话已有确认的公开经历")
            return previous["id"]
        proof = self._source_proof("operator_note", source_ref, fact)
        if sources:
            proof["operation_sources"] = sources
        return self.record_highlight(
            character["id"], character["version"], self.business_date(time.time()), "community_meme",
            "号主确认的公开经历", fact, fact, source_ref=source_ref, source_evidence=proof,
        )

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

    def candidate_sources(self, date_str: str, character_id: str, limit: int = 15) -> list[dict[str, Any]]:
        """Collect potential highlight candidates from comments and conversations for a date."""
        candidates = []

        # 1. Fetch notable comments (high likes, interaction categories, or replied drafts)
        comment_rows = self.store.all(
            """
            SELECT c.video_id, c.comment_id, c.display_name, c.content, c.like_count, c.category,
                   d.status as draft_status, d.receipt as draft_receipt
            FROM comments c
            LEFT JOIN drafts d ON d.comment_id = c.id
            WHERE c.created_at LIKE ? OR date(c.imported_at, 'unixepoch', 'localtime') = ?
            ORDER BY c.like_count DESC, c.id DESC
            LIMIT ?
            """,
            (f"{date_str}%", date_str, limit),
        )
        for row in comment_rows:
            content = (row["content"] or "").strip()
            if not content or len(content) < 3:
                continue
            try:
                receipt = json.loads(row["draft_receipt"] or "null")
            except (TypeError, ValueError):
                receipt = None
            replied = (
                row["draft_status"] == "published"
                and isinstance(receipt, dict)
                and receipt.get("code", 0) == 0
                and bool(re.fullmatch(r"[1-9][0-9]{0,19}", str(receipt.get("rpid", ""))))
            )
            candidates.append(
                {
                    "source_type": "comment",
                    "source_ref": f"video:{row['video_id']}:comment:{row['comment_id']}",
                    "source_user": desensitize_user(row["display_name"]),
                    "content": content,
                    "like_count": row["like_count"] or 0,
                    "replied": replied,
                }
            )

        # 2. Fetch live conversation interactions for the date
        conv_rows = self.store.all(
            """
            SELECT session_id, event_text, reply_text, created_at
            FROM conversations
            WHERE character_id = ? AND date(created_at, 'unixepoch', 'localtime') = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (character_id, date_str, limit),
        )
        for row in conv_rows:
            user_msg = (row["event_text"] or "").strip()
            reply = (row["reply_text"] or "").strip()
            if not user_msg:
                continue
            candidates.append(
                {
                    "source_type": "live",
                    "source_ref": f"live:{row['session_id']}",
                    "source_user": "直播间观众",
                    "content": f"观众说：{user_msg} | 主播回：{reply}" if reply else user_msg,
                    "like_count": 0,
                    "replied": bool(reply),
                }
            )

        return candidates[:limit]

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
        if category not in DISTILLED_CATEGORIES:
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

    async def distill(
        self,
        character: dict[str, Any],
        date_str: str,
        provider: Any,
        *,
        candidates: list[dict[str, Any]] | None = None,
        auto_approve: bool = False,
    ) -> list[dict[str, Any]]:
        """Distill highlights from daily candidates using LLM with grounded quote validation."""
        character_id = character.get("id", "whale")
        character_version = character.get("version", 1)

        candidates = candidates if candidates is not None else self.candidate_sources(date_str, character_id)
        if not candidates:
            return []

        candidates_map = {c["source_ref"]: c for c in candidates}
        messages = [
            {"role": "system", "content": DISTILLATION_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "character": {key: character[key] for key in ("name", "core", "style") if key in character},
                        "sources": candidates,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        schema = {
            "type": "object",
            "required": ["highlights"],
            "properties": {
                "highlights": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "required": ["category", "headline", "source_ref", "quote", "anecdote"],
                        "properties": {
                            "category": {"type": "string", "enum": list(DISTILLED_CATEGORIES)},
                            "headline": {"type": "string", "maxLength": 30},
                            "source_ref": {"type": "string", "enum": list(candidates_map)},
                            "source_user": {"type": "string"},
                            "quote": {"type": "string"},
                            "anecdote": {"type": "string", "maxLength": 150},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
        }

        try:
            # Generate structured JSON
            if hasattr(provider, "generate_json"):
                data = await provider.generate_json(messages, schema, max_tokens=1024)
            else:
                raw_reply = await provider.summarize(messages, max_tokens=1024)
                # Parse JSON block from output
                match = re.search(r"\{.*\}", raw_reply, re.DOTALL)
                data = json.loads(match.group(0)) if match else {"highlights": []}
        except Exception as e:
            LOGGER.warning("Daily memory distillation LLM failed: %s", e)
            return []

        highlights = data.get("highlights", [])
        saved = []
        status = "active" if auto_approve else "pending_review"

        for item in highlights:
            quote = item.get("quote", "").strip()
            headline = item.get("headline", "").strip()
            anecdote = item.get("anecdote", "").strip()
            category = item.get("category", "comment_anecdote")
            if category not in DISTILLED_CATEGORIES:
                category = "comment_anecdote"
            source_ref = item.get("source_ref", "")
            candidate = candidates_map.get(source_ref)
            if not quote or not anecdote:
                continue
            if candidate is None:
                matches = [source for source in candidates if quote in source["content"]]
                if len(matches) != 1:
                    continue
                candidate = matches[0]
            elif quote not in candidate["content"]:
                continue
            source_ref = candidate["source_ref"]
            source_user = desensitize_user(candidate.get("source_user") or "观众")

            item_id = self.record_highlight(
                character_id=character_id,
                character_version=character_version,
                highlight_date=date_str,
                category=category,
                headline=headline or "社群趣事",
                quote=quote,
                anecdote=anecdote,
                source_ref=source_ref,
                source_user=source_user,
                tags=item.get("tags", []),
                status=status,
                source_evidence=self._source_proof("live" if candidate.get("source_type") == "live" else "viewer", source_ref, quote),
            )
            saved.append(
                {
                    **item,
                    "quote": quote,
                    "source_ref": source_ref,
                    "source_user": source_user,
                    "id": item_id,
                    "status": status,
                    "highlight_date": date_str,
                }
            )

        return saved

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
            if not proof:
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
            if not self._source_evidence(row):
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
    ) -> list[dict[str, Any]]:
        """List daily highlights for UI visualization and editing."""
        clauses = ["character_id = ?", "character_version = ?"]
        params: list[Any] = [character_id, character_version]
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
        if set(updates) & {"headline", "quote", "anecdote"} or updates.get("status") == "active":
            confirmed = self.store.one("SELECT * FROM daily_highlights WHERE id=?", (highlight_id,))
            if confirmed and confirmed["category"] != "public_incident":
                proof = self._source_proof("operator_note", confirmed["source_ref"], confirmed["quote"])
                previous = json.loads(confirmed.get("source_evidence") or "{}") or {}
                if previous.get("operation_sources"):
                    proof["operation_sources"] = previous["operation_sources"]
                self.store.execute("UPDATE daily_highlights SET source_evidence=? WHERE id=?",
                    (json.dumps(proof, ensure_ascii=False), highlight_id))
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
