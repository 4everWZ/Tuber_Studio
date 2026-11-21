"""Source-backed long-term memories and stored community highlights."""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import memory_sources
from .cooperation import applicable_chat_memory, bound_statement
from .store import dump as dump_memory

ANECDOTE_CATEGORIES = ("comment_anecdote", "live_highlight", "community_meme")
HIGHLIGHT_CATEGORIES = (*ANECDOTE_CATEGORIES, "public_incident")
HIGHLIGHT_STATUSES = ("active", "pending_review", "archived")
MEMORY_CHANNELS = ("live", "comment", "dynamic")
_PUBLIC_PROJECTION_VERSION = 2
_OWNER_PROVENANCE_VERSION = 1
_ORIGINAL_PUBLIC_SOURCE_KINDS = {"comment", "subtitle"}
_PUBLIC_MEMORY_SOURCE = re.compile(
    r"https://(?:www\.bilibili\.com/video/(?P<bvid>BV[0-9A-Za-z]{10})/?"
    r"(?:\?(?:p|t)=[0-9]+(?:&(?:p|t)=[0-9]+)?)?"
    r"|t\.bilibili\.com/(?P<dynamic>[1-9][0-9]{0,19}))"
    r"(?:#reply(?P<comment>[1-9][0-9]{0,19}))?"
)
_MEMORY_CREDENTIAL = re.compile(
    r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"(?:api[ _-]?key|access[ _-]?token|password|secret|密码|密钥|SESSDATA|bili_jct)\s*(?:是|为|[=:：])\s*[^\s，。；]{4,}"
)
HIGHLIGHT_ACCOUNT_FILTER = (
    "COALESCE(CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.operation_account_id') END,?)=?"
)


class MemoryChanged(ValueError):
    """A generated reply refers to memory that changed before delivery."""


def contains_memory_credential(text):
    return bool(_MEMORY_CREDENTIAL.search(text or ""))


def _organized_note(source, decision):
    """Use the source wording. The classifier selects and labels evidence."""
    quote = source["text"].strip()
    if source.get("scene") == "live":
        return decision.note.strip()
    if "cooperation_rule" in source:
        return bound_statement(source["text"], source["cooperation_rule"]) or ""
    if len(quote) <= 150:
        return quote
    if source["kind"] == "experience":
        return quote[:149] + "…"
    excerpt = decision.note.strip()
    sentences = re.findall(r"[^。！？!?\n]+[。！？!?]*", quote)
    return excerpt if excerpt in {sentence.strip() for sentence in sentences} else ""


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


def front_topic_keywords(query: str) -> set[str]:
    return DailyMemoryService._extract_recall_keywords(_FRONT_FRAMING.sub(" ", query))


class DailyMemoryService:
    def __init__(self, store):
        self.store = store
        self.private_owner = None
        if store is None:
            return
        store.db.create_function("memory_title_key", 1, self._title_key, deterministic=True)
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
        memory_sources.migrate(self)

    def _upgrade_pending(self):
        self.store.db.execute(
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

    @staticmethod
    def _title_key(title):
        return re.sub(r"\W", "", title).casefold()

    def organized(self, scope, *, limit=100, item_id=None, unclassified=False, recall=None, exact_title=None,
                  candidate_for=None, memory_kind=None, material_query=None):
        private_unclassified = (
            "COALESCE(json_extract(source_evidence,'$.memory_kind'),'')='private' "
            "AND json_extract(source_evidence,'$.scene') IS NULL "
            "AND json_extract(source_evidence,'$.summary_kind') IS NULL "
            "AND json_extract(source_evidence,'$.inspection_only') IS NULL"
        )
        query = ""
        owner = self.private_owner if not recall or recall[0] == "operator" else None
        parameters = [scope[1], self.private_owner, scope[2], scope[0], owner, item_id, item_id,
                      int(unclassified), exact_title, exact_title]
        if candidate_for:
            key, kind, signature, quote = candidate_for
            query = "AND (source_ref=? OR (?='private' AND (quote=? OR memory_title_key(headline)=?))) "
            parameters.extend((key, kind, quote, signature))
        if memory_kind is not None:
            query += "AND json_extract(source_evidence,'$.memory_kind')=? "
            parameters.append(memory_kind)
        if material_query is not None:
            clause, values = material_query.sql(
                "headline || ' ' || quote || ' ' || anecdote || ' ' || tags", "created_at",
                "json_extract(source_evidence,'$.memory_source.thread.resource')",
                "json_extract(source_evidence,'$.memory_source.thread.root_id')",
            )
            query += "AND status='active' " + clause + " "
            parameters.extend(values)
        if recall is not None:
            channel, keywords, resource, thread = recall
            document = ("lower(headline || ' ' || quote || ' ' || anecdote || ' ' || tags)" if channel == "operator"
                        else "lower(quote || ' ' || json_extract(source_evidence,'$.memory_source.references'))")
            alternatives = [f"instr({document},?)>0" for _ in keywords]
            parameters.extend(word.lower() for word in keywords)
            if thread and resource:
                alternatives.append("(json_extract(source_evidence,'$.memory_source.thread.resource')=? "
                                    "AND json_extract(source_evidence,'$.memory_source.thread.root_id')=?)")
                parameters.extend((resource, str(thread)))
            if not alternatives:
                return []
            query += "AND status='active' AND (" + " OR ".join(alternatives) + ") "
        rows = self.store.all(
            "SELECT * FROM daily_highlights WHERE character_id=? AND CASE WHEN json_valid(source_evidence) THEN "
            "(json_extract(source_evidence,'$.private_owner') IS NULL OR owner_principal=?) AND "
            "json_extract(source_evidence,'$.memory_kind') IS NOT NULL AND ((character_version=? AND "
            "json_extract(source_evidence,'$.operation_account_id')=?) OR (owner_principal=? "
            "AND json_extract(source_evidence,'$.memory_kind')='private' AND json_extract(source_evidence,'$.scene') IS NULL)) END "
            "AND (? IS NULL OR id=?) "
            f"AND CASE WHEN json_valid(source_evidence) THEN ({private_unclassified}) = ? END "
            "AND (? IS NULL OR headline=?) "
            + query +
            "ORDER BY updated_at DESC,id DESC LIMIT ?",
            (*parameters, limit),
        )
        result = []
        for row in rows:
            if proof := self._source_evidence(row):
                if any(contains_memory_credential(row[key]) for key in ("quote", "headline", "anecdote")):
                    continue
                source = proof["memory_source"]
                if proof["memory_kind"] == "private" and proof.get("owner_edit_at", 0) > source["at"]:
                    # Older settings edits replaced only the note. Attribute its current
                    # wording to the owner edit, never to the old chat or audio transcript.
                    at = proof["owner_edit_at"]
                    source = {"key": memory_sources.edited_source_key(row["id"], at), "at": at, "references": []}
                    if row["headline"] in row["quote"] and row["headline"] not in row["anecdote"]:
                        row = row | {"headline": row["anecdote"][:40]}
                    row = row | {"quote": row["anecdote"]}
                public = self._public_memory_projection(proof, source)
                channels = []
                if row["status"] == "active":
                    if proof.get("scene") == "live":
                        channels = ["live"]
                    elif public and not proof.get("owner_only"):
                        channels = list(MEMORY_CHANNELS)
                result.append(row | {"memory_kind": proof["memory_kind"], "source": source,
                                     "channels": channels, "public_memory": public, "proof": proof})
        return result

    @staticmethod
    def _public_memory_projection(proof, source):
        """A receipt proves delivery. Only an original source can describe the experience."""
        if proof["memory_kind"] != "experience" or proof["kind"] != "viewer" or not proof.get("operation_account_id"):
            return None
        anchor = source.get("thread") or {}
        source_kind = source.get("kind")
        if source_kind is not None and source_kind not in _ORIGINAL_PUBLIC_SOURCE_KINDS:
            return None
        for ref in source.get("references", []):
            text = ref.get("text") or ""
            match = _PUBLIC_MEMORY_SOURCE.fullmatch(ref.get("href") or "")
            if (not match or text != proof["quote"] or not text.strip() or contains_memory_credential(text)
                    or ref.get("kind") == "published_reply"):
                continue
            resource = match["bvid"] or f"dynamic:{match['dynamic']}"
            comment = match["comment"]
            if anchor and (resource != anchor.get("resource") or comment != str(anchor.get("comment_id"))):
                continue
            if ref.get("comment_id") and str(ref["comment_id"]) != comment:
                continue
            # Old video-only snapshots cannot distinguish captions from generated vision.
            if not comment and source_kind != "subtitle":
                continue
            if comment and str(ref.get("author_uid") or "") == proof["operation_account_id"]:
                continue
            excerpt = " ".join(text.split())
            if len(excerpt) > 150:
                excerpt = excerpt[:149] + "…"
            return {"text": f"「{excerpt}」", "references": [
                {"href": match[0], "text": text, "comment_id": comment,
                 "author_uid": ref.get("author_uid"), "author_name": ref.get("author_name")},
            ]}
        return None

    def summary(self, scope, *, organized=None):
        if organized is None:
            organized = self.organized(scope)
        groups = {"preference": [], "decision": [], "experience": []}
        for row in organized:
            if row["status"] != "active" or row["proof"].get("inspection_only"):
                continue
            if (row := self.owner_projection(row)) is None:
                continue
            kind = "experience" if row["memory_kind"] == "experience" else row["proof"].get("summary_kind", "preference")
            notes = groups[kind]
            if len(notes) < 3 and all(item["text"] != row["anecdote"] for item in notes):
                notes.append({"id": row["id"], "text": row["anecdote"], "visibility": self.memory_visibility(row)})
        return [{"kind": kind, "notes": notes} for kind, notes in groups.items() if notes]

    @staticmethod
    def owner_projection(row):
        """Recall verified wording without replacing ambiguous legacy entries in management."""
        proof = row["proof"]
        if proof.get("owner_provenance") == _OWNER_PROVENANCE_VERSION or proof.get("scene") == "live":
            return row
        explicit_note = proof.get("owner_edit_at")
        if row["memory_kind"] == "experience" and not explicit_note and not row["public_memory"]:
            return None
        original = row["quote"].strip()
        tags = [tag for tag in json.loads(row["tags"] or "[]") if tag and tag in original]
        headline = row["headline"]
        if not proof.get("owner_headline_confirmed") and headline not in original:
            headline = next(iter(tags), original)[:40]
        note = original if len(original) <= 150 else original[:149] + "…"
        return row | {"headline": headline, "anecdote": row["anecdote"] if explicit_note else note,
                      "tags": dump_memory(tags)}

    @staticmethod
    def memory_visibility(row):
        if "live" in row["channels"] and row["proof"].get("scene") == "live":
            return "live_rule"
        return "shared" if row["channels"] else "owner"

    def resolve_legacy_private(self, scope, row, decision, *, reviewed_chat=False, rule=None):
        current = self.organized(scope, item_id=row["id"], unclassified=True)
        if not current or current[0]["updated_at"] != row["updated_at"]:
            return
        note = bound_statement(row["quote"], rule) if rule else ""
        if not decision or decision.action == "skip" or not note or not decision.headline.strip():
            # Preserve the original chat and prevent this source from being saved again.
            proof = row["proof"]
            for key in {*proof.get("preference_sources", []), row["source_ref"], row["source"]["key"]}:
                self.store.db.execute(
                    "INSERT INTO memory_organization VALUES(?,?,?,?,?,'skipped',?) "
                    "ON CONFLICT(account,character,version,source) DO UPDATE SET status='skipped',updated_at=excluded.updated_at "
                    "WHERE memory_organization.status!='deleted'",
                    (*scope, key, "", time.time()),
                )
            if reviewed_chat:
                self.store.db.execute(
                    "UPDATE daily_highlights SET source_evidence=json_set(source_evidence,'$.inspection_only',1),"
                    "updated_at=? WHERE id=?", (time.time(), row["id"]),
                )
            else:
                self.store.db.execute("DELETE FROM daily_highlights WHERE id=?", (row["id"],))
            return
        proof = row["proof"] | {"summary_kind": decision.summary_kind, "owner_provenance": _OWNER_PROVENANCE_VERSION,
                                "cooperation_rule": rule}
        self.store.db.execute(
            "UPDATE daily_highlights SET headline=?,anecdote=?,tags=?,source_evidence=?,updated_at=? WHERE id=?",
            (decision.headline.strip(), note, dump_memory(decision.tags), dump_memory(proof),
             time.time(), row["id"]),
        )

    def preference(self, scope):
        account, character, _ = scope
        default = {"enabled": True, "revision": 0}
        legacy = self.store.get(f"memory_preference:{account}:{character}", default)
        if not self.private_owner:
            return legacy
        principal = self.store.get(f"owner_memory_preference:{self.private_owner}:{character}", default)
        return {"enabled": principal["enabled"] and legacy["enabled"],
                "revision": principal["revision"] + legacy["revision"]}

    def set_preference(self, scope, enabled):
        account, character, _ = scope
        legacy = f"memory_preference:{account}:{character}"
        owner = f"owner_memory_preference:{self.private_owner}:{character}" if self.private_owner else legacy
        keys = [owner]
        if enabled and self.private_owner:
            # A legacy account-specific opt-out survives until the owner explicitly
            # enables memory while viewing that account.
            keys.append(legacy)
        with memory_sources.memory_transaction(self.store.db):
            for key in keys:
                self.store.db.execute("INSERT OR IGNORE INTO kv VALUES(?,?)",
                                      (key, dump_memory({"enabled": True, "revision": 0})))
                self.store.db.execute(
                    "UPDATE kv SET value=json_set(value,'$.enabled',json(?),"
                    "'$.revision',json_extract(value,'$.revision')+1) "
                    "WHERE key=? AND json_extract(value,'$.enabled')!=?",
                    (dump_memory(enabled), key, enabled),
                )
            return self.preference(scope)

    def save_organized(self, scope, source, decision, existing):
        db = self.store.db
        with memory_sources.memory_transaction(db):
            db.execute("UPDATE daily_highlights SET id=id WHERE 0")
            return self._save_organized(scope, source, decision, existing)

    def _save_organized(self, scope, source, decision, existing):
        key = source["key"]
        kind = source["kind"]
        if kind not in {"private", "experience"}:
            return False
        quote = source["text"]
        note = _organized_note(source, decision)
        if (not note or kind == "experience" and source.get("source_kind") not in _ORIGINAL_PUBLIC_SOURCE_KINDS
                or any(contains_memory_credential(text) for text in (quote, decision.headline, note, *decision.tags))):
            return False
        tags = sorted({tag.strip()[:24] for tag in decision.tags if tag.strip()})
        headline = decision.headline.strip()
        signature = self._title_key(headline)
        target = source.get("scene")
        owner = source.get("private_owner") if (
            kind == "private" and not target
            and source.get("private_owner") == self.private_owner
        ) else None
        if source.get("private_owner") and source["private_owner"] != self.private_owner:
            return False
        if kind == "private" and (turn := memory_sources.original_turn(self.store.db, scope[1], key)):
            origin = (turn["account"], scope[1], turn["character_version"])
            if (turn["message"] != quote or turn["owner"] and turn["owner"] != self.private_owner
                    or origin != scope and not (owner and owner == turn["owner"])):
                return False
            scope = origin
        if self.source_deleted(scope, key, owner):
            return False
        if existing:
            current = self.organized(scope, item_id=existing["id"])
            if not current or current[0]["revision"] != existing["revision"]:
                return False
        if kind == "private":
            previous = self.store.one(
                "SELECT s.item_id,s.quote FROM memory_source_aliases a JOIN memory_sources s USING(item_id,source_key) "
                "JOIN daily_highlights h ON h.id=s.item_id WHERE a.alias=? AND s.source_key=? AND s.character=? "
                "AND h.owner_principal IS ? AND json_extract(h.source_evidence,'$.scene') IS ? "
                "AND (? IS NOT NULL OR (s.account=? AND s.version=?))",
                (key, key, scope[1], owner, target, owner, scope[0], scope[2]),
            )
            if previous:
                return previous["quote"] == quote and (not existing or previous["item_id"] == existing["id"])
        if existing and (existing["proof"].get("private_owner") != owner or existing["proof"].get("scene") != target):
            return False
        matches = [] if existing else self.organized(scope, limit=-1, candidate_for=(key, kind, signature, quote))
        for row in matches:
            if row["memory_kind"] != kind:
                continue
            if source.get("cooperation_rule") and row["quote"] != quote:
                # A shared topic label cannot override the organizer's verified correction target.
                continue
            if row["proof"].get("scene") != target or row["proof"].get("private_owner") != owner:
                continue
            existing = row
            break
        if existing:
            latest = self.organized(scope, item_id=existing["id"])
            if not latest or latest[0]["status"] == "archived" or latest[0]["memory_kind"] != kind:
                return False
            existing = latest[0]
            if kind == "private" and source["at"] <= existing["proof"].get("owner_edit_at", 0):
                return False
            if owner and source["at"] < existing["source"]["at"]:
                return False
            if kind == "experience":
                # Keep the reviewed text and permissions, with the latest verified sources.
                proof = existing["proof"]
                previous = proof["memory_source"]
                if key != existing["source_ref"] or not previous.get("references"):
                    return False
                incoming = {"kind": source["source_kind"], "references": source.get("references", []),
                            "thread": source.get("anchor")}
                projection = self._public_memory_projection(proof | {"quote": quote}, incoming)
                if scope[0] and not projection:
                    return False
                if projection and quote != existing["quote"]:
                    # A thread groups several original comments. The permalink identifies each quote.
                    memory_sources.record_source(self.store.db, existing["id"], scope,
                                                 projection["references"][0]["href"], quote,
                                                 projection=projection, thread=incoming["thread"])
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
        proof.update(memory_kind=kind, operation_account_id=scope[0], owner_provenance=_OWNER_PROVENANCE_VERSION,
                     memory_source={"key": key, "at": source["at"], "references": source.get("references", []),
                                    "thread": source.get("anchor")})
        if kind == "experience":
            proof["memory_source"]["kind"] = source["source_kind"]
            if scope[0] and not self._public_memory_projection(proof, proof["memory_source"]):
                return False
        if kind == "private":
            if owner:
                proof["private_owner"] = owner
                proof["memory_source"]["scope"] = list(scope)
                if existing:
                    proof["operation_account_id"] = existing["proof"]["operation_account_id"]
            proof["summary_kind"] = decision.summary_kind
            if source.get("owner_confirmed"):
                proof["owner_confirmed"] = True
            if "cooperation_rule" in source:
                proof["cooperation_rule"] = source["cooperation_rule"]
            previous = existing["proof"].get("preference_sources", [source_ref]) if existing else []
            proof["preference_sources"] = list(dict.fromkeys([*previous, key]))
        if target:
            proof["scene"] = target
            proof["style_group"] = source["style_group"]
        at = time.time()
        fields = (headline, quote, note, dump_memory(tags), dump_memory(proof), at)
        if existing:
            item_id = existing["id"]
            self.store.db.execute(
                "UPDATE daily_highlights SET headline=?,quote=?,anecdote=?,tags=?,source_evidence=?,updated_at=? WHERE id=?",
                (*fields, existing["id"]),
            )
            if kind == "private":
                self._invalidate_previous_turn(existing)
        else:
            cursor = self.store.db.execute(
                "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,source_ref,source_user,"
                "status,created_at,headline,quote,anecdote,tags,source_evidence,updated_at,owner_principal) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope[1], scope[2], self.business_date(source["at"]), "community_meme", source_ref,
                 "号主" if kind == "private" else "", "active", at, *fields, owner),
            )
            item_id = cursor.lastrowid
        memory_sources.record_source(self.store.db, item_id, scope, key, quote,
                                     projection=self._public_memory_projection(proof, proof["memory_source"]),
                                     thread=source.get("anchor"))
        return True

    def source_deleted(self, scope, key, owner):
        return bool(self.store.one(
            "SELECT 1 FROM memory_source_tombstones WHERE owner=? AND character=? AND source_key=? "
            "UNION ALL SELECT 1 FROM memory_organization WHERE account=? AND character=? AND version=? "
            "AND source=? AND status='deleted' LIMIT 1", (owner, scope[1], key, *scope, key),
        ))

    def history_turn_available(self, scope, key, owner):
        return not (self.source_deleted(scope, key, owner) or self.store.one(
            "SELECT 1 FROM superseded_private_turns WHERE character=? AND account=? AND version=? AND source_key=?",
            (scope[1], scope[0], scope[2], key)))

    def _invalidate_previous_turn(self, row):
        source = row["proof"]["memory_source"]
        origin = source.get("scope") or (row["proof"]["operation_account_id"], row["character_id"], row["character_version"])
        memory_sources.invalidate_turn(self.store.db, origin, source["key"])
        memory_sources.invalidate_outdated_reads(self.store.db, row["id"])

    def lookup_source(self, scope, channel, source_key):
        """Return exact, currently authorized original quotes. An absent source yields []."""
        if channel not in ("operator", *MEMORY_CHANNELS) or not self.preference(scope)["enabled"]:
            return []
        matches = self.store.all(
            "SELECT s.item_id,s.source_key,s.quote FROM memory_source_aliases a JOIN memory_sources s "
            "USING(item_id,source_key) JOIN daily_highlights h ON h.id=s.item_id "
            "WHERE a.alias=? AND h.character_id=? AND h.status='active' AND "
            "((h.owner_principal IS NULL AND h.character_version=? AND "
            "json_extract(h.source_evidence,'$.operation_account_id')=?) OR h.owner_principal=?) "
            "ORDER BY s.item_id DESC,s.source_key",
            (source_key, scope[1], scope[2], scope[0], self.private_owner if channel == "operator" else None),
        )
        result = []
        for match in matches:
            rows = self.organized(scope, item_id=match["item_id"])
            if not rows or rows[0]["status"] != "active" or contains_memory_credential(match["quote"]):
                continue
            row, = rows
            if channel != "operator":
                public = row["public_memory"]
                if (channel not in row["channels"] or not public or match["source_key"] != row["source"]["key"] or
                        not any(ref["text"] == match["quote"] for ref in public["references"])):
                    continue
            result.append({"id": row["id"], "revision": row["revision"],
                           "source_key": match["source_key"], "quote": match["quote"]})
        return result

    def change_organized(self, scope, item_id, *, owner_only=None, headline=None, note=None, delete=False,
                         expected_revision=None):
        db = self.store.db
        with memory_sources.memory_transaction(db):
            # Acquire the write lock before reading visibility and the displayed revision.
            db.execute("UPDATE daily_highlights SET id=id WHERE id=?", (item_id,))
            return self._change_organized(scope, item_id, owner_only, headline, note, delete, expected_revision)

    def _change_organized(self, scope, item_id, owner_only, headline, note, delete, expected_revision):
        rows = self.organized(scope, item_id=item_id)
        if not rows:
            return None
        row, = rows
        if expected_revision is not None and row["revision"] != expected_revision:
            raise MemoryChanged("记忆版本已变化，请重新读取")
        proof = row["proof"]
        if headline is not None and not headline.strip() or note is not None and not note.strip():
            raise ValueError("记忆标题和内容不能为空")
        if any(contains_memory_credential(text) for text in (headline, note)):
            raise ValueError("记忆不能保存凭据")
        if delete:
            self._forget_sources(scope, row)
            self.store.db.execute("DELETE FROM daily_highlights WHERE id=?", (item_id,))
            return {"id": item_id, "deleted": True}
        if owner_only is not None:
            if not row["public_memory"] or row["status"] != "active":
                raise ValueError("只有已保存且有公开来源的经历可以切换共享")
            proof["owner_only"] = owner_only
        if note is not None and row["memory_kind"] == "private" and note != row["anecdote"]:
            self._invalidate_previous_turn(row)
            at = time.time()
            key = "settings:edit:" + uuid4().hex
            if (not headline or headline == row["headline"]) and row["headline"] in row["quote"] and row["headline"] not in note:
                headline = note[:40]
            proof.update(quote=note, owner_confirmed=True, owner_edit_at=at,
                         memory_source={"key": key, "at": at, "scope": list(scope), "references": []})
            proof.pop("cooperation_rule", None)
            memory_sources.record_source(self.store.db, item_id, scope, key, note)
        elif note is not None and row["memory_kind"] != "private":
            proof["owner_edit_at"] = time.time()
        if headline is not None:
            proof["owner_headline_confirmed"] = True
        proof.pop("channels", None)
        self.store.db.execute(
            "UPDATE daily_highlights SET headline=?,quote=?,anecdote=?,source_evidence=?,updated_at=? WHERE id=?",
            (headline or row["headline"], proof["quote"], note or row["anecdote"], dump_memory(proof), time.time(), item_id),
        )
        return self.organized(scope, item_id=item_id)[0]

    def _forget_sources(self, scope, row):
        db = self.store.db
        keys = set(row["proof"].get("preference_sources", [])) | {row["source_ref"], row["source"]["key"]}
        owner = row["owner_principal"]
        origins = {}
        for source in self.store.all("SELECT * FROM memory_sources WHERE item_id=?", (row["id"],)):
            origins[source["source_key"]] = (source["account"], source["character"], source["version"])
        legacy_scope = (row["proof"]["operation_account_id"], scope[1], row["character_version"])
        for key in keys | origins.keys():
            if owner:
                db.execute("INSERT OR IGNORE INTO memory_source_tombstones VALUES(?,?,?)", (owner, scope[1], key))
                if key not in origins:
                    continue  # Stable key is known, historical account is not.
            origin = origins.get(key, legacy_scope)
            db.execute("INSERT INTO memory_organization VALUES(?,?,?,?,?,'deleted',?) "
                       "ON CONFLICT(account,character,version,source) DO UPDATE SET status='deleted',updated_at=excluded.updated_at",
                       (*origin, key, "", time.time()))
            if row["memory_kind"] == "private":
                memory_sources.invalidate_turn(db, origin, key)
        if row["memory_kind"] == "private":
            memory_sources.invalidate_outdated_reads(db, row["id"])

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

    def recall_organized(self, scope, channel, text, *, resource=None, thread=None, participant=None,
                         specific_experiences=False, manual_inspection=False, memory_kind=None, limit=3):
        if not (manual_inspection and channel == "operator") and not self.preference(scope)["enabled"]:
            return []
        matched = []
        keywords = (front_topic_keywords(text) if channel == "operator" and not manual_inspection
                    else self._extract_recall_keywords(text))
        search = sorted(keywords, key=lambda word: (-len(word), word))[:18]
        candidates = self.organized(scope, recall=(channel, search, resource, thread), memory_kind=memory_kind, limit=-1)
        keys = set(re.findall(r"BV[A-Za-z0-9]{10}|(?:turn|event|thread):[A-Za-z0-9_:/#.-]+", text))
        if resource:
            keys.add(f"thread:{resource}:{thread}" if thread else resource)
        if participant:
            keys.add(str(participant))
        exact = {item["id"] for key in keys for item in self.lookup_source(scope, channel, key)}
        present = {row["id"] for row in candidates}
        for item_id in exact - present:
            candidates.extend(self.organized(scope, item_id=item_id, memory_kind=memory_kind))
        for row in candidates:
            if row["status"] != "active" or channel != "operator" and channel not in row["channels"]:
                continue
            if channel == "operator" and (row := self.owner_projection(row)) is None:
                continue
            if channel == "operator" and not manual_inspection and not applicable_chat_memory(row):
                continue
            anchor = row["source"].get("thread") or {}
            same_thread = bool(thread and resource == anchor.get("resource") and str(thread) == anchor.get("root_id"))
            same_person = bool(participant and any(str(ref.get("author_uid")) == str(participant)
                               for ref in row["source"]["references"]))
            subject = row["headline"] + row["anecdote"]
            if channel != "operator":
                public = row["public_memory"]
                if not public:
                    continue  # Scene rules are added independently below.
                subject = public["text"]
                row = {key: row[key] for key in ("id", "revision", "updated_at", "memory_kind", "public_memory")} | {
                    "headline": "公开经历", "anecdote": subject, "quote": subject, "tags": "[]", "proof": {},
                    "source": {"references": public["references"]},
                }
            score = self._score_highlight_match(row, keywords, text)
            if channel == "comment" and anchor and not same_thread:
                continue
            exact_match = row["id"] in exact
            if specific_experiences and row["memory_kind"] == "experience" and not same_thread and not exact_match:
                if row["headline"] not in text and not any(len(word) >= 3 and word in subject for word in keywords):
                    continue
            if same_thread or exact_match or score >= 3:
                matched.append((20 * same_thread + 10 * exact_match + 5 * same_person + score, row))
        relevant = [row for _, row in sorted(matched, key=lambda item: -item[0]) if not row["proof"].get("scene")]
        if channel == "live":
            rules = [{key: row[key] for key in ("id", "revision", "updated_at", "headline", "anecdote")}
                     for row in self.live_preferences(scope)]
            return (rules + relevant)[:4]
        return relevant[:limit]

    def validate_organized(self, scope, channel, refs):
        for item in refs:
            rows = self.organized(scope, item_id=item["id"])
            if (not rows or rows[0]["updated_at"] != item["updated_at"] or rows[0]["status"] != "active"
                    or "revision" in item and rows[0]["revision"] != item["revision"]
                    or channel != "operator" and channel not in rows[0]["channels"]):
                raise MemoryChanged("生成使用的记忆已变更，请重试")
            row, = rows
            if (channel != "operator" and row["public_memory"] and len(row["source"]["references"]) > 1
                    and item.get("public_projection") != _PUBLIC_PROJECTION_VERSION):
                raise MemoryChanged("生成使用的记忆已变更，请重试")

    @staticmethod
    def organized_reference(row):
        return {"id": row["id"], "updated_at": row["updated_at"], "revision": row["revision"],
                "public_projection": _PUBLIC_PROJECTION_VERSION}

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

    def public_experience_sources(self, character_id, character_version, *, material_query=None, bound_ids=()):
        where, args = material_query.sql("headline||' '||quote", "created_at", None) if material_query else ("", ())
        rows = self.store.all(
            "SELECT * FROM daily_highlights WHERE character_id=? AND character_version=? AND status='active' "
            "AND (source_ref LIKE 'system:downtime:%' OR source_ref LIKE 'operator-public:%') "
            "AND ('experience:'||id IN (SELECT value FROM json_each(?)) OR (1" + where + ")) "
            "ORDER BY ('experience:'||id IN (SELECT value FROM json_each(?))) DESC,highlight_date DESC,id DESC LIMIT 10",
            (character_id, character_version, dump_memory(bound_ids), *args, dump_memory(bound_ids)),
        )
        return [
            {"id": f"experience:{row['id']}", "type": proof["kind"], "fictional": False,
             "text": row["quote"], "public_sources": [], "observed_at": row["created_at"]}
            for row in rows if (proof := self._source_evidence(row))
            and proof["kind"] in {"runtime_event", "operator_note"}
            and not proof.get("operation_sources")
        ]

    def business_date(self, now):
        zone = self.store.get("browsing_policy", {}).get("business_timezone", "Asia/Taipei")
        return datetime.datetime.fromtimestamp(now, ZoneInfo(zone)).date().isoformat()

    def public_incident_sources(self, character_id, character_version, *, material_query=None, bound_ids=()):
        where, args = material_query.sql(
            "headline||' '||COALESCE(public_sources,'')", "created_at",
            "json_extract(public_sources,'$[0].bvid')", "json_extract(public_sources,'$[0].thread_id')",
        ) if material_query else ("", ())
        rows = self.store.all(
            "SELECT id,incident_key,public_sources,created_at,updated_at FROM daily_highlights "
            "WHERE character_id=? AND character_version=? AND category='public_incident' AND status='active' "
            "AND ('incident:'||id IN (SELECT value FROM json_each(?)) OR (1" + where + ")) "
            "ORDER BY ('incident:'||id IN (SELECT value FROM json_each(?))) DESC,highlight_date DESC,id DESC LIMIT 20",
            (character_id, character_version, dump_memory(bound_ids), *args, dump_memory(bound_ids)),
        )
        result = []
        for row in rows:
            sources = [ref for ref in json.loads(row["public_sources"] or "[]")
                       if _PUBLIC_MEMORY_SOURCE.fullmatch(ref.get("url") or "")
                       and ref.get("quote") and not contains_memory_credential(ref["quote"])]
            if not sources:
                continue
            result.append({
                "id": f"incident:{row['id']}",
                "type": "public_incident",
                "fictional": False,
                "text": "；".join(f"「{ref['quote'][:150]}」" for ref in sources[:2]),
                "incident_key": row["incident_key"],
                "public_sources": sources,
                "memory_version": row["updated_at"],
                "observed_at": row["created_at"],
            })
        return result

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
            """,
            (character_id, character_version),
        )
        if not rows:
            return None

        exclude = exclude_ids or set()
        best_score = 0
        best_row = None

        for row in rows:
            if row["id"] in exclude or row["category"] == "public_incident":
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
