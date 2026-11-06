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

LOGGER = logging.getLogger(__name__)

HIGHLIGHT_CATEGORIES = ("comment_anecdote", "live_highlight", "community_meme")
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

DISTILLATION_PROMPT = """你叫{name}，是一只活泼机灵、爱吐槽又有点傲娇的深海鲸鱼娘虚拟主播。
请审阅今天在社群评论区与直播间发生的真实互动素材，从中提炼出 2 到 3 个最生动、最值得当作日常谈资的【每日趣事与社群高光】。

【输入素材列表】
{candidates_json}

【提炼规则】
1. 真实溯源（严禁虚构事实）：每个趣事必须绑定一条具体素材，quote 必须完全一字不差摘录自素材 content，不得凭空捏造观众没说过的话。
2. 角色自述（anecdote）：必须用{name}典型的第一人称口吻进行生动、富有趣味的生活感吐槽或回忆（每条 30~70 字，例如：“昨天在视频底下被吐槽尾巴太胖游得慢，气得我昨晚多盛了一碗热白饭！”）。
3. 严格精炼：避免长篇大论，方便后续作为轻量谈资随口提及。
4. 分类规范：category 限用 comment_anecdote（评论区趣事）、live_highlight（直播战报/高光）、community_meme（社群新梗）。

请以 JSON 格式输出，格式如下：
{{
  "highlights": [
    {{
      "category": "comment_anecdote",
      "headline": "简明标题（15字以内）",
      "source_ref": "素材对应的source_ref",
      "source_user": "观众昵称（不含敏感UID）",
      "quote": "从content中一字不差摘录的关键句子",
      "anecdote": "{name}第一人称视角的人设自述吐槽台词",
      "tags": ["关键词1", "关键词2"]
    }}
  ]
}}
"""


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

    def candidate_sources(self, date_str: str, character_id: str, limit: int = 15) -> list[dict[str, Any]]:
        """Collect potential highlight candidates from comments and conversations for a date."""
        candidates = []

        # 1. Fetch notable comments (high likes, interaction categories, or replied drafts)
        comment_rows = self.store.all(
            """
            SELECT c.video_id, c.comment_id, c.display_name, c.content, c.like_count, c.category,
                   d.status as draft_status, d.text as draft_text
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
            candidates.append(
                {
                    "source_type": "comment",
                    "source_ref": f"video:{row['video_id']}:comment:{row['comment_id']}",
                    "source_user": desensitize_user(row["display_name"]),
                    "content": content,
                    "like_count": row["like_count"] or 0,
                    "replied": bool(row["draft_status"] in ("published", "approved")),
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
    ) -> int:
        """Insert or record a verified daily highlight."""
        if category not in HIGHLIGHT_CATEGORIES:
            raise ValueError(f"Unsupported category: {category}")
        if status not in HIGHLIGHT_STATUSES:
            raise ValueError(f"Unsupported status: {status}")
        if not headline.strip() or not quote.strip() or not anecdote.strip():
            raise ValueError("Headline, quote and anecdote are required")

        now = time.time()
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        cursor = self.store.execute(
            """
            INSERT INTO daily_highlights(
                character_id, character_version, highlight_date, category,
                headline, source_ref, source_user, quote, anecdote,
                tags, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            ),
        )
        self.store.db.commit()
        return cursor.lastrowid

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
        name = character.get("name", "大飞鱼")

        candidates = candidates if candidates is not None else self.candidate_sources(date_str, character_id)
        if not candidates:
            return []

        candidates_map = {c["source_ref"]: c for c in candidates}
        prompt = DISTILLATION_PROMPT.format(
            name=name,
            candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        )

        messages = [
            {"role": "system", "content": "你是一个严格按照 JSON 格式输出结构化摘要的助手。"},
            {"role": "user", "content": prompt},
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
                        "required": ["category", "headline", "quote", "anecdote"],
                        "properties": {
                            "category": {"type": "string", "enum": list(HIGHLIGHT_CATEGORIES)},
                            "headline": {"type": "string", "maxLength": 30},
                            "source_ref": {"type": "string"},
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
            if category not in HIGHLIGHT_CATEGORIES:
                category = "comment_anecdote"
            source_ref = item.get("source_ref", "")
            source_user = item.get("source_user", "")

            # Relaxed progressive attribution: link to candidate if available
            candidate = candidates_map.get(source_ref)
            if not candidate and quote:
                candidate = next(
                    (c for c in candidates if quote in c["content"] or c["content"] in quote), None
                )
            if not candidate and candidates:
                candidate = candidates[0]

            source_ref_val = candidate["source_ref"] if candidate else ""
            source_user_val = desensitize_user(
                source_user or (candidate.get("source_user") if candidate else "观众")
            )
            quote_val = quote or (candidate["content"][:60] if candidate else "")
            if not anecdote:
                continue

            item_id = self.record_highlight(
                character_id=character_id,
                character_version=character_version,
                highlight_date=date_str,
                category=category,
                headline=headline or "社群趣事",
                quote=quote_val,
                anecdote=anecdote,
                source_ref=source_ref_val,
                source_user=source_user_val,
                tags=item.get("tags", []),
                status=status,
            )
            saved.append({**item, "id": item_id, "status": status, "highlight_date": date_str})

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
    ) -> list[dict[str, Any]]:
        """Retrieve bounded, active anecdotes for prompt injection.

        Strictly limits count and length to prevent context bloat.
        """
        today = datetime.date.today()
        cutoff = (today - datetime.timedelta(days=days)).isoformat()

        rows = self.store.all(
            """
            SELECT id, highlight_date, category, headline, source_user, quote, anecdote, tags
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
            SELECT id, highlight_date, category, headline, source_user, quote, anecdote, tags
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
                   created_at, updated_at
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
