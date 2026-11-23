"""Public-source collection, recall-assisted evaluation, and autonomous dynamic loop."""

import asyncio
import hashlib
import json
import re
import time
from contextlib import AsyncExitStack
from datetime import datetime

from ....core.config import operation_profile
from ....core.errors import SendLimitReached
from ....core.identity import identity_messages, runtime_identity
from ....core.meter import budgeted_json
from ....core.models import content_hash
from ....core.text import MAX_OPERATION_TEXT_BYTES, operation_character, operation_output_diagnostics
from ....infra.platforms.bilibili.dynamics import validate_text
from ....persona.assets.illustration_policy import illustration_policy
from ....persona.assets.meme_selection import select_meme
from ....persona.characters.front_persona import expressive_persona
from ....persona.context.delivery import memory_source_allowed
from ....persona.memory import recall as recall_memory
from ...kernel.generation import generate_with_recovery
from ..attachments import workstation_image_decisions
from ..mentions import MENTION_INSTRUCTION, MENTION_REQUESTS, mention_requests
from .materials import MaterialFilter, public_reads, reflections, source_links
from .repository import (
    MAX_SAFE_INTEGER,
    _content_hash,
    _has_legacy_chat_lineage,
    _json,
)


def dynamic_decision_messages(character, recent_posts, public_sources, *, current_time):
    return [
        {
            "role": "system",
            "content": (
                "用自己的口吻和关注你的人分享。今天想聊什么、想聊多长，由你决定。"
                "public_incident是公开经历和原评论，published_reply是来评和你当时发出的回复，"
                "public_memory是角色故事摘录，runtime_event是运行记录，operator_note是号主确认的经历。"
                "随想、创作、兴趣、日常观察、角色小故事或公开互动都可以独立成稿，篇幅随话题。"
                "看过的视频、读到的公开评论和自己的心情都能单独分享，也可以自然地一起聊。"
                "几条素材在讲同一件事时可以合成一条。points 记录这次聊到的各个要点和对应的 source_ids。"
                "参考相关待发稿和已分享记录。后来只换说法重复同一个点就先等，有新发现或进展可以接着分享。"
                "source_ids 写实际用过的素材，随想可留空。想附具体链接才填 link_source_ids。"
                + MENTION_INSTRUCTION +
                "分享时选post，content写动态正文。暂缓时选wait，content留空。"
                "reason说明决定，next_evaluation_seconds是下次评估间隔。"
            ),
        },
        {
            "role": "user",
            "content": _json(
                {
                    "character": expressive_persona(operation_character(character)),
                    "current_time": current_time,
                    "recent_public_posts": recent_posts,
                    "public_sources": public_sources,
                }
            ),
        },
    ]


class DynamicsEvaluatorMixin:
    def _live_busy(self):
        return bool(self.is_live_busy())

    def _character(self):
        return self.store.character()

    def _summary_model(self):
        fields = ("base_url", "model", "enable_thinking", "structured_output", "context_source", "prompt_token_endpoint")
        active = getattr(self.runtime.operation_llm, "config", {})
        active = active if isinstance(active, dict) else {}
        profile = operation_profile(self.settings.data["omni"])
        identity = {
            "settings": {k: profile.get(k) for k in fields},
            "provider": {k: active.get(k) for k in fields},
            "implementation": type(self.runtime.operation_llm).__module__ + "." + type(self.runtime.operation_llm).__qualname__,
        }
        return hashlib.sha256(_json(identity).encode()).hexdigest()

    def _summary_sources(self):
        sources, size = [], 0
        for source in self._sources():
            length = len(_json(source))
            if len(sources) < 8 and size + length <= 6000:
                sources.append(source)
                size += length
        return sorted(sources, key=lambda source: source["id"])

    def summary_status(self):
        return self.memory_summary.status(
            self._character(),
            [source for source in self._summary_sources() if source["type"] == "public_memory"],
            self.policy()["memory_summary_max_tokens"],
            self._summary_model(),
        )

    def _memory_rows(self, *, query=None, bound_ids=(), authorized_only=False):
        if not self.store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='grounded_memory'"):
            return []
        char = self._character()
        where, args = (query or MaterialFilter()).sql("g.text", "g.created_at", None)
        rows = self.store.all(
            "SELECT g.* FROM grounded_memory g " + (
                "JOIN dynamic_memory_public m ON m.memory_id=g.id " if authorized_only else ""
            ) + "WHERE character_id=? AND character_version=? "
            "AND role='assistant' AND kind='role_story' AND fictional=1 "
            "AND scope NOT LIKE 'dm:%' AND scope NOT LIKE 'operator:%' "
            "AND ('memory:'||g.id IN (SELECT value FROM json_each(?)) OR (1" + where + ")) "
            "ORDER BY ('memory:'||g.id IN (SELECT value FROM json_each(?))) DESC,g.id DESC LIMIT 200",
            (char["id"], char["version"], _json(bound_ids), *args, _json(bound_ids)),
        )
        account = runtime_identity(self.runtime, char).get("account_uid") or ""
        return [row for row in rows if memory_source_allowed(self.store, row, account_id=account)]

    @staticmethod
    def _memory_fingerprint(row):
        return hashlib.sha256(
            _json(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "scope",
                        "character_id",
                        "character_version",
                        "event_id",
                        "role",
                        "kind",
                        "fictional",
                        "text",
                        "created_at",
                    )
                }
            ).encode()
        ).hexdigest()

    def list_memories(self, limit=100):
        result = []
        for row in self._memory_rows()[: self._limit(limit, 200)]:
            mark = self.store.one(
                "SELECT fingerprint FROM dynamic_memory_public WHERE memory_id=?", (row["id"],)
            )
            result.append(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "scope",
                        "event_id",
                        "role",
                        "kind",
                        "text",
                        "created_at",
                        "character_id",
                        "character_version",
                    )
                }
                | {
                    "fictional": True,
                    "public": bool(mark and mark["fingerprint"] == self._memory_fingerprint(row)),
                }
            )
        return result

    def mark_memory(self, memory_id, public_bool):
        if type(public_bool) is not bool or type(memory_id) is not int:
            raise ValueError("记忆ID与公开开关无效")
        row = next((row for row in self._memory_rows() if row["id"] == memory_id), None)
        if row is None:
            raise ValueError("只能标记当前人物版本的虚构角色记忆")
        if public_bool:
            self.store.execute(
                "INSERT OR REPLACE INTO dynamic_memory_public VALUES(?,?,?)",
                (memory_id, self._memory_fingerprint(row), time.time()),
            )
        else:
            self.store.execute("DELETE FROM dynamic_memory_public WHERE memory_id=?", (memory_id,))
        return next(item for item in self.list_memories(200) if item["id"] == memory_id)

    def _memory_preference(self):
        organizer = getattr(self.runtime, "memory_organizer", None)
        scope = organizer.scope() if organizer and getattr(self.runtime, "browsing", None) else None
        return scope, self.daily_memory.preference(scope) if scope else {"enabled": True, "revision": 0}

    def _source_account(self):
        browsing = getattr(self.runtime, "browsing", None)
        discovery = getattr(browsing, "discovery", None)
        return str(getattr(self.account, "display_account_id", lambda: "")()
                   or (discovery.history_scope()[0] if discovery else getattr(browsing, "account", "")) or "")

    def _sources(self, *, bound_ids=(), query=None):
        char = self._character()
        browsing = getattr(self.runtime, "browsing", None)
        account_id = self._source_account()
        query = query or MaterialFilter()
        scope, preference = self._memory_preference()
        result = []
        if preference["enabled"]:
            result = self.daily_memory.public_incident_sources(char["id"], char["version"],
                                                                material_query=query, bound_ids=bound_ids)
            result.extend(self.daily_memory.public_experience_sources(char["id"], char["version"],
                                                                     material_query=query, bound_ids=bound_ids))
        if scope and preference["enabled"]:
            recall = ("dynamic", list(query.terms), query.resource, query.root) if query.terms or query.resource else None
            rows = self.daily_memory.organized(scope, recall=recall, limit=100)
            for identity in bound_ids:
                if identity.startswith("organized:") and identity[10:].isdecimal():
                    rows.extend(self.daily_memory.organized(scope, item_id=int(identity[10:])))
            for row in {row["id"]: row for row in rows}.values():
                if row["status"] != "active" or "dynamic" not in row["channels"] or not row["public_memory"]:
                    continue
                anchor = row["source"].get("thread") or {}
                public = row["public_memory"]
                references = public["references"]
                if not references:
                    continue
                result.append({"id": f"organized:{row['id']}", "type": "public_incident", "fictional": False,
                               "text": public["text"], "memory_version": row["updated_at"],
                               "observed_at": row["created_at"],
                               "source_url": references[0]["href"], "public_sources": [
                                   {"bvid": anchor["resource"], "thread_id": anchor["root_id"],
                                    "comment_id": ref["comment_id"], "quote": ref["text"]} for ref in references
                               ] if anchor.get("resource", "").startswith("BV") else []})
        if preference["revision"]:
            for source in result:
                source["memory_preference_revision"] = preference["revision"]
        has_account = any(row[1] == "account_id" for row in self.store.db.execute("PRAGMA table_info(drafts)"))
        scoped = bool(account_id and has_account)
        legacy_time = "d.updated_at"
        legacy_filter, legacy_args = query.sql("d.text||' '||c.content", legacy_time, "c.video_id")
        rows = self.store.all(
            "SELECT d.id,d.text,d.receipt,d.updated_at,c.video_id,c.comment_id,c.content FROM drafts d "
            "JOIN comments c ON c.id=d.comment_id WHERE d.status='published' AND c.platform='bilibili' "
            "AND json_extract(d.answer_check,'$.organized_memory') IS NULL "
            "AND d.character_id=? AND d.character_version=? " + (
                "AND (d.account_id=? OR d.account_id='' AND json_extract(d.receipt,'$.account_id')=?) " if scoped else ""
            ) + ("AND d.account_id='' " if browsing and has_account and not scoped else "")
            + "AND ('reply:'||d.id IN (SELECT value FROM json_each(?)) "
            "OR 'reply:'||json_extract(d.receipt,'$.rpid') IN (SELECT value FROM json_each(?)) OR (1" + legacy_filter
            + ")) ORDER BY ('reply:'||d.id IN (SELECT value FROM json_each(?)) "
            "OR 'reply:'||json_extract(d.receipt,'$.rpid') IN (SELECT value FROM json_each(?))) DESC,d.id DESC LIMIT 100",
            (char["id"], char["version"], *((account_id, account_id) if scoped else ()),
             _json(bound_ids), _json(bound_ids), *legacy_args, _json(bound_ids), _json(bound_ids)),
        )
        for row in rows:
            try:
                receipt = json.loads(row["receipt"] or "null")
                # A copied/imported row without an actual platform receipt is not public evidence.
                if not isinstance(receipt, dict) or not re.fullmatch(
                    r"[1-9][0-9]{0,19}", str(receipt.get("rpid", ""))
                ):
                    continue
            except (ValueError, TypeError):
                continue
            result.append(
                {
                    "id": f"reply:{row['id']}",
                    "type": "published_reply",
                    "text": row["text"],
                    "fictional": True,
                    "source_url": f"https://www.bilibili.com/video/{row['video_id']}",
                    "original_comment": {"id": row["comment_id"], "text": row["content"]},
                    "receipt_id": str(receipt["rpid"]),
                    "observed_at": row["updated_at"],
                }
            )
        material = list(reflections(self.store, account_id, char["id"], char["version"], bound_ids)) if account_id else []
        if browsing:
            if account_id:
                material.extend(public_reads(self.store, account_id, char["id"], bound_ids, query))
                reply_filter, reply_args = query.sql(
                    "text||' '||COALESCE(json_extract(target,'$.source_text'),'')", "updated_at",
                    "COALESCE(json_extract(target,'$.resource'),json_extract(target,'$.bvid'))",
                    "COALESCE(json_extract(target,'$.root'),json_extract(target,'$.root_id'))",
                )
                for row in self.store.all(
                    "SELECT id,text,target,receipt,updated_at FROM browsing_intents WHERE kind='reply_public' AND status='published' "
                    "AND json_extract(target,'$.memory_audit') IS NULL "
                    "AND account_id=? AND character_id=? AND character_version=? "
                    "AND ('reply:browsing:'||id IN (SELECT value FROM json_each(?)) "
                    "OR 'reply:'||json_extract(receipt,'$.rpid') IN (SELECT value FROM json_each(?)) OR (1" + reply_filter
                    + ")) ORDER BY ('reply:browsing:'||id IN (SELECT value FROM json_each(?)) "
                    "OR 'reply:'||json_extract(receipt,'$.rpid') IN (SELECT value FROM json_each(?))) DESC,updated_at DESC LIMIT 100",
                    (account_id, char["id"], char["version"], _json(bound_ids), _json(bound_ids), *reply_args,
                     _json(bound_ids), _json(bound_ids)),
                ):
                    target, receipt = json.loads(row["target"]), json.loads(row["receipt"] or "null")
                    if not isinstance(receipt, dict) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(receipt.get("rpid"))) or not (
                        re.fullmatch(r"BV[0-9A-Za-z]{10}", str(target.get("bvid"))) and target.get("source_text")
                    ):
                        continue
                    result.append({
                        "id": f"reply:browsing:{row['id']}", "type": "published_reply", "text": row["text"],
                        "fictional": False, "source_url": f"https://www.bilibili.com/video/{target['bvid']}",
                        "original_comment": {"id": target.get("parent"), "text": target["source_text"]},
                        "receipt_id": str(receipt["rpid"]),
                        "observed_at": row["updated_at"],
                        "reference": {"resource": target["bvid"], "thread_id": target.get("root") or target.get("root_id")},
                    })
        for row in self._memory_rows(query=query, bound_ids=bound_ids, authorized_only=True) if preference["enabled"] else ():
            mark = self.store.one(
                "SELECT fingerprint FROM dynamic_memory_public WHERE memory_id=?", (row["id"],)
            )
            if mark and mark["fingerprint"] == self._memory_fingerprint(row):
                result.append(
                    {
                        "id": f"memory:{row['id']}",
                        "type": "public_memory",
                        "text": row["text"],
                        "fictional": True,
                        "scope": row["scope"],
                        "event_id": row["event_id"],
                        "observed_at": row.get("created_at"),
                        **({"memory_preference_revision": preference["revision"]} if preference["revision"] else {}),
                    }
                )
        return [source for _, source in sorted(material, key=lambda item: -item[0])] + result

    @staticmethod
    def _limit(value, maximum=100):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("列表数量超出范围")
        return value

    def _recent_public_posts(self, character):
        _, preference = self._memory_preference()
        account = getattr(self.account, "display_account_id", lambda: "")()
        return [
            {"content": post["content"], "published_at": post["published_at"]}
            for post in self.list_posts("published", 5)
            if (post["character_id"], post["character_version"]) == (character["id"], character["version"])
            and (not account or post["account_id"] == account)
            and not _has_legacy_chat_lineage(post)
            and not json.loads(post.get("context_sources") or "[]")
            and not any(
                source.get("memory_version") or source.get("memory_dependencies")
                or (source["id"].startswith(("memory:", "experience:", "incident:")) and (
                    not preference["enabled"]
                    or source.get("memory_preference_revision", 0) != preference["revision"]
                ))
                for source in post["sources"]
            )
        ]

    def _related_posts(self, character, account, sources):
        if not sources or not account:
            return []
        ids = _json([source["id"] for source in sources])
        resources = _json(sorted({link.partition("#")[0] for source in sources for link in source_links(source)}))
        history = []
        for shared in (False, True):
            candidates = self.store.all(
                "SELECT * FROM dynamic_posts p WHERE account_id=? AND character_id=? "
                "AND status IN ('pending_review','approved','publishing','publish_uncertain','published') "
                "AND COALESCE(status='published',0)=? "
                "AND (status!='published' OR json_extract(receipt,'$.dynamic_id') IS NOT NULL) "
                "AND EXISTS (SELECT 1 FROM json_each(p.sources) s WHERE "
                "json_extract(s.value,'$.id') IN (SELECT value FROM json_each(?)) OR "
                "substr(json_extract(s.value,'$.source_url') || '#', 1, "
                "instr(json_extract(s.value,'$.source_url') || '#','#')-1) IN (SELECT value FROM json_each(?)) OR "
                "EXISTS (SELECT 1 FROM json_each(json_extract(s.value,'$.public_sources')) r "
                "WHERE 'https://www.bilibili.com/video/'||json_extract(r.value,'$.bvid') "
                "IN (SELECT value FROM json_each(?)))) ORDER BY updated_at DESC LIMIT 12",
                (account, character["id"], int(shared), ids, resources, resources),
            )
            found = 0
            for row in candidates:
                row = self._decode(row)
                if not self._valid_sources(row | {"character_version": character["version"]}):
                    continue
                history.append({
                    "status": "shared" if shared else "pending", "content": row["content"][:600],
                    "source_ids": row["source_ids"], "points": (row.get("presentation") or {}).get("points", [])[:8],
                })
                found += 1
                if found == 4:
                    break
        return history

    @staticmethod
    def decision_schema(sources):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "reason", "content", "source_ids", "points", "next_evaluation_seconds"],
            "properties": {
                "action": {"type": "string", "enum": ["post", "wait"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                "content": {"type": "string"},
                "source_ids": {
                    "type": "array",
                    "maxItems": len(sources),
                    "items": {"type": "string", **({"enum": [source["id"] for source in sources]} if sources else {})},
                },
                "link_source_ids": {"type": "array", "items": {"type": "string"}},
                "mentions": MENTION_REQUESTS,
                "points": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                           "required": ["text", "source_ids"], "properties": {
                               "text": {"type": "string", "minLength": 1, "maxLength": 300},
                               "source_ids": {"type": "array", "items": {"type": "string"}},
                           }}},
                "next_evaluation_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            },
        }

    @staticmethod
    def _parse(data, sources):
        if not isinstance(data, dict) or len(_json(data).encode("utf-8")) > MAX_OPERATION_TEXT_BYTES + 16384:
            raise ValueError("模型决定格式无效")
        required = {
            "action",
            "reason",
            "content",
            "source_ids",
            "next_evaluation_seconds",
        }
        if not required <= data.keys() or data.keys() - required - {"link_source_ids", "illustration", "mentions", "points"}:
            raise ValueError("模型决定字段无效")
        if data["action"] not in {"post", "wait"}:
            raise ValueError("模型决定动作无效")
        if not isinstance(data["reason"], str) or not 1 <= len(data["reason"]) <= 500:
            raise ValueError("模型决定理由无效")
        ids = data["source_ids"]
        allowed = {s["id"] for s in sources}
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("模型来源列表无效")
        if not set(ids) <= allowed:
            raise ValueError("模型引用了未授权来源")
        links = data.get("link_source_ids", [])
        linkable = {source["id"] for source in sources if source["id"] in ids and source_links(source)}
        if not isinstance(links, list) or any(not isinstance(item, str) or item not in linkable for item in links):
            raise ValueError("附加链接须选自用到的公开素材")
        illustration = data.get("illustration")
        if illustration not in (None, "none", "comment_card", "picture"):
            raise ValueError("模型配图选择无效")
        mentions = mention_requests(data.get("mentions", []))
        points = data.get("points", [])
        if not isinstance(points, list) or any(
            not isinstance(point, dict) or set(point) != {"text", "source_ids"}
            or not isinstance(point["text"], str) or not 1 <= len(point["text"].strip()) <= 300
            or not isinstance(point["source_ids"], list)
            or any(not isinstance(item, str) or item not in ids for item in point["source_ids"])
            for point in points
        ):
            raise ValueError("分享要点须关联本次用到的素材")
        seconds = data["next_evaluation_seconds"]
        if type(seconds) is not int or not 1 <= seconds <= MAX_SAFE_INTEGER:
            raise ValueError("模型下次评估间隔无效")
        if data["action"] == "post":
            data["content"] = validate_text(data["content"])
        elif data["content"] != "" or links or mentions or points or illustration not in (None, "none"):
            raise ValueError("等待决定不得包含待发布正文")
        return data | {"link_source_ids": links, "illustration": illustration, "mentions": mentions, "points": points}

    def _save_decision(
        self, char, manual, data, sources, error=None, *, generation_identity=None, schedule=True, account_id=None,
        decide_image=True
    ):
        now = time.time()
        selected = [s for s in sources if s["id"] in data["source_ids"]]
        selected_ids = {s["id"] for s in selected}
        memory_dependencies = [
            s for s in sources if s["id"] not in selected_ids
            and (s.get("memory_version") or s["id"].startswith(("memory:", "experience:", "incident:")))
        ]
        if selected and memory_dependencies:
            selected[0] = selected[0] | {"memory_dependencies": memory_dependencies}
            memory_dependencies = []
        model = str(self.settings.data["omni"]["responses_model"])
        next_at = now + data["next_evaluation_seconds"]
        presentation = data.get("presentation")
        owner = (account_id if account_id is not None else
                 getattr(self.account, "display_account_id", lambda: "")() or "")
        shared_choice = bool(owner and decide_image and workstation_image_decisions(self.runtime))
        if shared_choice:
            presentation = (presentation or {}) | {"image_choice": "shared"}
        card_selected = shared_choice or presentation is None or data.get("illustration") == "comment_card"
        with self.store.db:
            decision_id = self.store.db.execute(
                "INSERT INTO dynamic_decisions(character_id,character_version,model,manual,action,reason,content,"
                "source_ids,sources,next_evaluation_at,created_at,status,error,points) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    char["id"],
                    char["version"],
                    model,
                    int(manual),
                    data["action"],
                    data["reason"],
                    data["content"],
                    _json(data["source_ids"]),
                    _json(selected),
                    next_at,
                    now,
                    data["action"],
                    error,
                    _json(presentation["points"]) if presentation is not None and "points" in presentation else None,
                ),
            ).lastrowid
            if "evaluation_account_id" in data:
                self.store.db.execute("UPDATE dynamic_decisions SET evaluation_account_id=? WHERE id=?",
                                      (data["evaluation_account_id"], decision_id))
            if data["action"] == "post":
                post_id = self.store.db.execute(
                    "INSERT INTO dynamic_posts(decision_id,character_id,character_version,model,content,reason,"
                    "source_ids,sources,status,created_at,updated_at,content_hash,manual,account_id,comment_card_snapshot,context_sources) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        char["id"],
                        char["version"],
                        model,
                        data["content"],
                        data["reason"],
                        _json(data["source_ids"]),
                        _json(selected),
                        "pending_review",
                        now,
                        now,
                        _content_hash(data["content"]),
                        int(manual),
                        owner,
                        _json(self._comment_card(selected, generation_identity) if card_selected else None),
                        _json(memory_dependencies),
                    ),
                ).lastrowid
                self.store.db.execute("UPDATE dynamic_decisions SET post_id=? WHERE id=?", (post_id, decision_id))
                self.store.db.execute(
                    "UPDATE dynamic_posts SET generation_identity=?,presentation=? WHERE id=?",
                    (_json(generation_identity or runtime_identity(self.runtime, char)),
                     _json(presentation) if presentation is not None else None, post_id),
                )
        if data["action"] == "post" and shared_choice:
            self._post(post_id)
        if schedule:
            self._schedule(data["next_evaluation_seconds"], error)
        return next(d for d in self.list_decisions(100) if d["id"] == decision_id)

    async def evaluate(self, manual=False, *, topic=""):
        async with self.evaluation_lock, AsyncExitStack() as reservations:
            policy = self.policy()
            if not manual and (
                not policy["enabled"]
                or max(policy["next_evaluation_at"] or 0, policy["backoff_until"] or 0) > time.time()
            ):
                return {"status": "skipped", "reason": "运营已关闭或尚未到评估时间"}
            char = self._character()
            generation_identity = runtime_identity(self.runtime, char)
            evaluation_account = self._source_account()
            tasks = getattr(self.runtime, "tasks", None)
            evaluation_scope = tasks.scope() if tasks else None
            base = {
                "action": "wait",
                "reason": "暂缓动态评估",
                "content": "",
                "source_ids": [],
                "next_evaluation_seconds": policy["evaluate_interval_seconds"],
                "evaluation_account_id": evaluation_account,
            }
            if not manual and self._live_busy():
                base["reason"] = "直播队列繁忙，延后动态评估"
                return self._save_decision(char, manual, base, [])
            if not manual:
                blocked = None
                try:
                    self._quota()
                except SendLimitReached as error:
                    blocked = error
                browsing = getattr(self.runtime, "browsing", None)
                if policy["auto_publish"] and browsing:
                    shared = reservations.enter_context(browsing.sends.preparation_slot(
                        self.operations.policy()["account_id"], "dynamics",
                        {"max_per_day": policy["max_posts_per_day"]},
                        retry_seconds=policy["evaluate_interval_seconds"],
                        min_interval=policy["min_post_interval_seconds"],
                    ))
                    if shared and (not blocked or shared.next_at > blocked.next_at):
                        blocked = shared
                if blocked:
                    base.update(reason=str(blocked), next_evaluation_seconds=max(1, blocked.next_at - time.time()))
                    return self._save_decision(char, manual, base, [])
            current_time = datetime.now().astimezone().isoformat()
            memory_preference = self._memory_preference()
            messages = dynamic_decision_messages(
                char,
                self._recent_public_posts(char),
                [],
                current_time=current_time,
            )
            messages = identity_messages(messages, generation_identity)
            selected, selection_failed = None, False
            try:
                sources = recall_memory.search_public(self, recall_memory.RecallQuery())["items"]
                async with self.runtime.offline_generation("background"):
                    if not manual and self._live_busy():
                        base["reason"] = "直播队列繁忙，延后动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not manual and not self.policy()["enabled"]:
                        base["reason"] = "运营已停止，取消动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not self._valid_sources(
                        {"character_id": char["id"], "character_version": char["version"], "sources": sources}
                    ):
                        base["reason"] = "人物版本或公开来源已变化，取消动态评估"
                        return self._save_decision(char, manual, base, [])
                    model_identity = self._summary_model()

                    recall = None

                    def authorized(*, include_enabled=True):
                        return (
                            self._character()["id"] == char["id"]
                            and self._character()["version"] == char["version"]
                            and self._source_account() == evaluation_account
                            and (not tasks or tasks.scope() == evaluation_scope)
                            and self._valid_sources({"character_id": char["id"], "character_version": char["version"],
                                                     "sources": list(recall.sources.values()) if recall else sources})
                            and self._memory_preference() == memory_preference
                            and self._summary_model() == model_identity
                            and runtime_identity(self.runtime, self._character())["identity_version"]
                            == generation_identity["identity_version"]
                            and (not include_enabled or manual or self.policy()["enabled"])
                        )

                    def guard():
                        if not authorized(include_enabled=False):
                            raise ValueError("动态取材来源或授权已变化")

                    if topic:
                        sources = recall_memory.search_public(self, recall_memory.RecallQuery(topic=topic))["items"]
                    originals = recall_memory.originals(sources)
                    public_sources = [recall_memory.model_material(source, comment_texts=originals) for source in sources]
                    if not manual and self._live_busy():
                        base["reason"] = "直播队列繁忙，延后动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not authorized():
                        raise ValueError("运营记忆来源或授权已变化")
                    payload = json.loads(messages[1]["content"])
                    unified_store = getattr(self.runtime, "unified_memory", None)
                    unified_public = unified_store.for_prompt("public", character_id=char["id"], limit=15) if unified_store is not None else []
                    if unified_public:
                        payload["unified_public_memories"] = [item["content"] for item in unified_public]
                    payload["public_sources"] = public_sources
                    account = getattr(self.account, "display_account_id", lambda: "")()
                    payload["related_posts"] = self._related_posts(char, account, sources)
                    for post in payload["related_posts"] + payload["recent_public_posts"]:
                        post["content"] = recall_memory.trim_repeated_quotes(post["content"], originals)
                        for point in post.get("points", []):
                            point["text"] = recall_memory.trim_repeated_quotes(point["text"], originals)
                    messages[1]["content"] = _json(payload)
                    maximum = self.settings.data["omni"].get("operations_max_tokens", 8192)
                    binding = {
                        "task": "generation",
                        "character": {"id": char["id"], "version": char["version"]},
                        "source_sha256": content_hash(public_sources),
                        "self_identity": generation_identity,
                        "evaluation_slot": current_time if manual else policy["next_evaluation_at"],
                        "epoch": self.operations.policy()["epoch"],
                    }

                    async def invoke_model(packet, schema):
                        return await budgeted_json(self.runtime.operation_llm, packet, schema, max_tokens=maximum)

                    async def invoke(packet, *, web_fetch=None):
                        schema = self.decision_schema(public_sources)
                        if web_fetch is not None:
                            return await web_fetch.complete(packet, schema, invoke_model)
                        return await invoke_model(packet, schema)

                    def diagnose(value):
                        return (
                            "正文包含大量重复内容，保留原文待审"
                            if value["action"] == "post"
                            and operation_output_diagnostics(value["content"])["needs_review"]
                            else None
                        )

                    data, generation_continuation = await generate_with_recovery(
                        self.runtime,
                        channel="generation.dynamics",
                        identity=content_hash(binding),
                        binding=binding,
                        character=char,
                        messages=messages,
                        generate=invoke,
                        validate=lambda value: self._parse(value, public_sources),
                        max_tokens=maximum,
                        guard=guard,
                        diagnose=diagnose,
                        context_data={"public_sources": sources},
                        allow_web_fetch=True,
                    )
                    data["presentation"] = await self._presentation(data, public_sources)
                    if not authorized(include_enabled=False):
                        raise ValueError("动态素材或账号已变化")
                    if (
                        data["action"] == "post" and data["illustration"] == "picture"
                        and illustration_policy(self.store).use_memes and self.memes and not workstation_image_decisions(self.runtime)
                    ):
                        try:
                            selected = await select_meme(
                                self.runtime,
                                self.memes,
                                char,
                                {"text": data["content"]},
                                data["content"],
                                mode="dynamic",
                            )
                        except Exception:
                            selection_failed = True
                        if not illustration_policy(self.store).use_memes:
                            selected = None
                        if not authorized(include_enabled=False):
                            raise ValueError("选图期间运营来源或授权已变化")
                data["next_evaluation_seconds"] = max(
                    data["next_evaluation_seconds"], policy["evaluate_interval_seconds"]
                )
                data["evaluation_account_id"] = evaluation_account
                if self._character()["id"] != char["id"] or self._character()["version"] != char["version"]:
                    raise ValueError("人物版本已变化")
                output_review = (
                    data["action"] == "post"
                    and operation_output_diagnostics(data["content"])["needs_review"]
                )
                decision = self._save_decision(
                    char,
                    manual,
                    data,
                    sources,
                    "长正文含大量重复段落，保留原文待审" if output_review else None,
                    generation_identity=generation_identity,
                    decide_image=not selected and not selection_failed,
                )
                if generation_continuation:
                    self.store.execute(
                        "UPDATE dynamic_decisions SET generation_continuation_id=? WHERE id=?",
                        (generation_continuation["id"], decision["id"]),
                    )
                if output_review and decision["post_id"]:
                    self.store.execute(
                        "UPDATE dynamic_posts SET error=? WHERE id=?",
                        ("长正文含大量重复段落，保留原文待审", decision["post_id"]),
                    )
                if selected and decision["post_id"]:
                    try:
                        self.review(decision["post_id"], "edit", meme_id=selected["asset_id"])
                    except ValueError:
                        selection_failed = True
                if selection_failed and decision["post_id"]:
                    error = "配图选择失败，正文已保存"
                    self.store.execute(
                        "UPDATE dynamic_posts SET error=? WHERE id=?", (error, decision["post_id"])
                    )
                    self.store.execute(
                        "UPDATE dynamic_decisions SET error=? WHERE id=?", (error, decision["id"])
                    )
                decision = next(d for d in self.list_decisions(100) if d["id"] == decision["id"])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                continuation = getattr(error, "continuation", None)
                if continuation:
                    base.update(
                        action="wait" if continuation["state"] in {"waiting", "abandoned"} else "error",
                        reason="本次运营生成已保留检查点",
                        next_evaluation_seconds=policy["evaluate_interval_seconds"],
                    )
                    saved = self._save_decision(char, manual, base, [], str(error))
                    self.store.execute(
                        "UPDATE dynamic_decisions SET status=?,generation_continuation_id=? WHERE id=?",
                        (error.continuation["state"], error.continuation["id"], saved["id"]),
                    )
                    return next(item for item in self.list_decisions(100) if item["id"] == saved["id"])
                base.update(action="error", reason="模型评估未返回合法完整决定", next_evaluation_seconds=3600)
                return self._save_decision(char, manual, base, [], "模型评估失败，请检查模型服务与输出格式")
            if (
                not manual
                and decision["post_id"]
                and self.policy()["enabled"]
                and self.policy()["auto_publish"]
                and not selection_failed
                and not output_review
                and not data["presentation"]["pending_mentions"]
            ):
                self._automatic_post_id = decision["post_id"]
                try:
                    if browsing := getattr(self.runtime, "browsing", None):
                        browsing.sends.enqueue("dynamics", decision["post_id"])
                    else:
                        await self.publish(decision["post_id"], automatic=True)
                except ValueError:
                    pass  # Durable post status/schedule is the actionable outcome.
                finally:
                    self._automatic_post_id = None
                decision = next(d for d in self.list_decisions(100) if d["id"] == decision["id"])
            return decision

    def _schedule(self, seconds, error=None):
        now = time.time()
        self.store.set(
            "dynamic_schedule",
            {
                "next_evaluation_at": now + seconds,
                "backoff_until": now + seconds if error else None,
                "last_error": error,
            },
        )

    async def open(self):
        if self.worker is None or self.worker.done():
            self.closed = False
            self.worker = asyncio.create_task(self._loop())

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None

    async def pause(self):
        running = self.worker is not None and not self.worker.done()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None
        if running and not self.closed:
            await self.open()

    async def _loop(self):
        while not self.closed:
            self.changed.clear()
            policy = self.policy()
            if policy["enabled"]:
                try:
                    async with self.operations.automatic_work() as admitted:
                        if admitted:
                            browsing = getattr(self.runtime, "browsing", None)
                            if browsing and not policy["auto_publish"] and not browsing.review_capacity("dynamics"):
                                self._schedule(policy["evaluate_interval_seconds"], "动态审核积压")
                            else:
                                await self.evaluate()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._schedule(3600, "动态评估服务异常，已延后重试")
            policy = self.policy()
            due = max(policy["next_evaluation_at"] or 0, policy["backoff_until"] or 0)
            delay = max(1, due - time.time()) if policy["enabled"] else 86400
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=delay)
            except TimeoutError:
                pass
