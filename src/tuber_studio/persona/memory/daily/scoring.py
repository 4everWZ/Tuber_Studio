"""Retrospective query cues and relevance scoring for daily highlights."""

from __future__ import annotations

import json
import re
from typing import Any

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
    "主播",
    "知道",
    "觉得",
    "不是",
    "就是",
    "那个",
    "这个",
}

# Topic-free framing in casual questions and standing preferences. Remove whole
# phrases before expanding Chinese n-grams, so their boundaries cannot match.
_FRONT_FRAMING = re.compile(
    r"平时|平常|通常|一般|以后|现在|每次|总是|喜欢|习惯|自己|我们|咱俩|咱们|"
    r"可以|一下|讲讲|聊聊|聊一下|是什么|怎么样|什么样|感觉|你好|您好|早上好|晚上好|"
    r"\b(?:the|and|are|was|you|your|what|how|usually|like|would|about|with)\b",
    re.I,
)


def is_retrospective_query(text: str) -> bool:
    """Fast check to see if user text asks about past memories or retrospective events."""
    if not text or not isinstance(text, str):
        return False
    return bool(RETROSPECTIVE_PATTERN.search(text))


def extract_recall_keywords(query: str) -> set[str]:
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


def score_highlight_match(row: dict[str, Any], keywords: set[str], query_text: str) -> int:
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


def front_topic_keywords(query: str) -> set[str]:
    return extract_recall_keywords(_FRONT_FRAMING.sub(" ", query))
