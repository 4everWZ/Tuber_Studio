"""Public-only Roast Radio & Viewer Debate show service for Tuber Studio live."""

from __future__ import annotations

import json
import random
import re
import unicodedata
from typing import Any, Literal

_FALLBACK_TOPICS: list[dict[str, str]] = [
    {
        "topic": "深夜刷到离谱科技测评却忍不住看完",
        "detail": "标题写着改变世界，结果拆开里面是十块钱塑料壳，偏偏评论区还都在玩梗。",
    },
    {
        "topic": "半夜煮白饭配老干妈算不算顶级夜宵",
        "detail": "明明知道睡前碳水会困，但电饭煲跳闸那一下香味飘出来根本顶不住。",
    },
    {
        "topic": "买了一堆外设结果只是用来逛B站",
        "detail": "客制化键盘买回来天天敲的字还没有弹幕多，纯属给桌面增加电子手办。",
    },
    {
        "topic": "写代码前先整理半小时歌单和桌面",
        "detail": "仪式感拉满，真正干活五分钟，找BGM找了四十分钟。",
    },
]

_FALLBACK_ROAST_TEMPLATES: tuple[str, ...] = (
    "说到「{topic}」，你们是不是也干过这种离谱事？表面上冷静得一批，其实手已经诚实地双击点赞了，别装了。",
    "关于「{topic}」这事，本小姐必须锐评一句：槽点多到我小本本都记不下了，但偏偏又让人想再看两眼，真是拿你们没办法。",
    "刚才翻到「{topic}」，看完我直接沉默三秒——到底是谁给的自信这么搞的啦？不过讲真，还挺下饭的。",
)

_ROAST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spoken_text": {"type": "string"},
    },
    "required": ["spoken_text"],
    "additionalProperties": False,
}


def _normalize_vote_text(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text or "").strip().casefold()
    cleaned = re.sub(r"[\s！!？?。.,，、~～:：]+", "", cleaned)
    return cleaned


def _table_exists(store: Any, name: str) -> bool:
    if store is None or not hasattr(store, "all"):
        return False
    try:
        rows = store.all(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return bool(rows)
    except Exception:
        return False


def _decode_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class RoastRadioService:
    """Strictly public-only video/memory roast & viewer debate radio."""

    def __init__(self):
        self.state: Literal["idle", "roast", "debate"] = "idle"
        self.mode: Literal["roast", "debate"] = "roast"
        self.topic: str = ""
        self.side_a: str = "正方"
        self.side_b: str = "反方"
        self.votes: dict[str, Literal["a", "b"]] = {}
        self.votes_a: int = 0
        self.votes_b: int = 0
        self.last_roast: str = ""
        self.source_bvid: str | None = None
        self.recent_topics: list[str] = []

    def is_active(self) -> bool:
        return self.state in {"roast", "debate"}

    def pick_public_watch_topic(self, store: Any) -> dict[str, Any] | None:
        """Pick a roastable topic strictly from public watched videos or scope='public' memories."""
        if store is None or not hasattr(store, "all"):
            return None

        candidates: list[dict[str, Any]] = []

        if _table_exists(store, "browsing_video_progress"):
            rows = store.all(
                "SELECT bvid, material, updated_at FROM browsing_video_progress "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            for row in rows:
                bvid = str(row.get("bvid") or "").strip()
                material = _decode_json(row.get("material"))
                meta = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
                title = str(meta.get("title") or "").strip()
                if not bvid or not title:
                    continue
                tags = [str(t).strip() for t in (meta.get("tags") or ()) if str(t).strip()][:5]
                desc = str(meta.get("description") or "").strip()[:160]
                candidates.append(
                    {
                        "topic": title,
                        "source_bvid": bvid,
                        "source_kind": "watched_video",
                        "title": title,
                        "tags": tags,
                        "summary": desc,
                        "updated_at": float(row.get("updated_at") or 0.0),
                    }
                )

        if _table_exists(store, "memories"):
            rows = store.all(
                "SELECT id, scope, content, tags, updated_at FROM memories "
                "WHERE scope='public' AND active=1 ORDER BY updated_at DESC LIMIT 20"
            )
            for row in rows:
                # Defense-in-depth: never allow non-public scope even from a mocked store.
                if row.get("scope") != "public":
                    continue
                content = str(row.get("content") or "").strip()
                if not content:
                    continue
                tags_raw = row.get("tags")
                if isinstance(tags_raw, str):
                    try:
                        parsed_tags = json.loads(tags_raw)
                        tags = [str(t).strip() for t in parsed_tags if str(t).strip()] if isinstance(parsed_tags, list) else []
                    except (TypeError, ValueError):
                        tags = []
                elif isinstance(tags_raw, list):
                    tags = [str(t).strip() for t in tags_raw if str(t).strip()]
                else:
                    tags = []
                candidates.append(
                    {
                        "topic": content[:120],
                        "source_bvid": None,
                        "source_kind": "public_memory",
                        "title": content[:80],
                        "tags": tags[:5],
                        "summary": content[:160],
                        "updated_at": float(row.get("updated_at") or 0.0),
                    }
                )

        if not candidates:
            return None

        candidates.sort(key=lambda item: item["updated_at"], reverse=True)
        fresh = [c for c in candidates if c["topic"] not in self.recent_topics[-5:]] or candidates
        picked = dict(fresh[0])
        picked.pop("updated_at", None)
        return picked

    def start_debate(
        self,
        *,
        topic: str,
        side_a: str = "正方",
        side_b: str = "反方",
        trigger_user: str = "主播",
    ) -> dict[str, Any]:
        """Start a viewer debate topic with two votable sides."""
        cleaned_topic = (topic or "").strip() or "今天夜宵吃白饭还是面条"
        self.state = "debate"
        self.mode = "debate"
        self.topic = cleaned_topic
        self.side_a = (side_a or "").strip() or "正方"
        self.side_b = (side_b or "").strip() or "反方"
        self.votes = {}
        self.votes_a = 0
        self.votes_b = 0
        self.last_roast = ""
        self.source_bvid = None
        self.recent_topics = (self.recent_topics + [cleaned_topic])[-10:]
        return {
            "status": "started",
            "mode": "debate",
            "topic": self.topic,
            "side_a": self.side_a,
            "side_b": self.side_b,
            "trigger_user": trigger_user,
            "spoken_text": (
                f"{trigger_user}把辩论台支起来了！今天题面是「{self.topic}」——"
                f"支持「{self.side_a}」扣1，站「{self.side_b}」扣2，别光看热闹赶紧站队。"
            ),
        }

    def record_vote(
        self,
        *,
        user_id: str | None,
        display_name: str,
        text: str,
    ) -> dict[str, Any] | None:
        """Record a deduplicated viewer vote (`1`/`2`/`正方`/`反方`/side label)."""
        if self.state != "debate":
            return None
        norm = _normalize_vote_text(text)
        if not norm:
            return None

        voter_key = str(user_id).strip() if user_id else f"name:{display_name}"
        if voter_key in self.votes:
            return None

        norm_a = _normalize_vote_text(self.side_a)
        norm_b = _normalize_vote_text(self.side_b)
        a_tokens = {"1", "选1", "投1", "支持1", "站1", "1号", "正方", "支持正方", "站正方", norm_a, f"支持{norm_a}", f"站{norm_a}"}
        b_tokens = {"2", "选2", "投2", "支持2", "站2", "2号", "反方", "支持反方", "站反方", norm_b, f"支持{norm_b}", f"站{norm_b}"}

        if norm in a_tokens and norm not in b_tokens:
            side: Literal["a", "b"] = "a"
            self.votes[voter_key] = "a"
            self.votes_a += 1
            side_label = self.side_a
        elif norm in b_tokens and norm not in a_tokens:
            side = "b"
            self.votes[voter_key] = "b"
            self.votes_b += 1
            side_label = self.side_b
        else:
            return None

        return {
            "side": side,
            "side_label": side_label,
            "votes_a": self.votes_a,
            "votes_b": self.votes_b,
            "user_id": user_id,
            "display_name": display_name,
        }

    async def generate_roast_segment(
        self,
        llm: Any,
        store: Any,
        *,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """Produce a sharp civil tsundere Aqua roast/commentary segment from public topics."""
        explicit_topic = (topic or "").strip()
        picked = None
        if explicit_topic:
            chosen_topic = explicit_topic
            source_bvid = None
            detail = ""
            mode: Literal["roast", "debate"] = "roast"
        elif self.state == "debate" and self.topic:
            chosen_topic = self.topic
            source_bvid = None
            detail = f"当前投票 {self.side_a}:{self.votes_a} vs {self.side_b}:{self.votes_b}"
            mode = "debate"
        else:
            picked = self.pick_public_watch_topic(store)
            if picked:
                chosen_topic = picked["topic"]
                source_bvid = picked.get("source_bvid")
                detail = picked.get("summary") or ""
            else:
                fresh = [t for t in _FALLBACK_TOPICS if t["topic"] not in self.recent_topics[-4:]] or _FALLBACK_TOPICS
                fb = random.choice(fresh)
                chosen_topic = fb["topic"]
                source_bvid = None
                detail = fb["detail"]
            mode = "roast"

        self.recent_topics = (self.recent_topics + [chosen_topic])[-10:]

        spoken_text = ""
        if llm is not None and hasattr(llm, "generate_json"):
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是虚拟主播Aqua（毒舌但有礼貌、爱护观众的傲娇小鲸鱼）。"
                        "请针对给定公开话题写一段2-3句的锐评/吐槽电台口播（spoken_text），"
                        "机智有梗、不脏话、不人身攻击、不捏造隐私。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"模式：{mode}\n公开话题：{chosen_topic}\n补充背景：{detail or '无'}",
                },
            ]
            try:
                raw = await llm.generate_json(messages, _ROAST_SCHEMA, max_tokens=220)
                if isinstance(raw, dict):
                    spoken_text = str(raw.get("spoken_text") or "").strip()
            except Exception:
                spoken_text = ""

        if not spoken_text:
            spoken_text = random.choice(_FALLBACK_ROAST_TEMPLATES).format(topic=chosen_topic)

        if mode == "roast":
            self.state = "roast"
            self.mode = "roast"
            self.topic = chosen_topic
            self.source_bvid = source_bvid
        self.last_roast = spoken_text

        return {
            "spoken_text": spoken_text,
            "topic": chosen_topic,
            "source_bvid": source_bvid,
            "mode": mode,
        }

    def stop(self) -> dict[str, Any]:
        """Stop the active roast or debate show."""
        prev_mode = self.mode if self.is_active() else "idle"
        prev_topic = self.topic
        votes_a, votes_b = self.votes_a, self.votes_b
        self.state = "idle"
        return {
            "status": "stopped",
            "mode": prev_mode,
            "topic": prev_topic,
            "votes_a": votes_a,
            "votes_b": votes_b,
            "spoken_text": (
                f"本期「{prev_topic}」吐槽电台先收麦啦，感谢各位大聪明捧场。"
                if prev_topic
                else "吐槽电台先收麦啦。"
            ),
        }

    def get_overlay_state(self) -> dict[str, Any]:
        """Return OBS/UI overlay state for Roast Radio & Debate."""
        return {
            "active": self.is_active(),
            "mode": self.mode,
            "topic": self.topic,
            "side_a": self.side_a,
            "side_b": self.side_b,
            "votes_a": self.votes_a,
            "votes_b": self.votes_b,
            "last_roast": self.last_roast,
            "source_bvid": self.source_bvid,
        }
