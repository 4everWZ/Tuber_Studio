"""Organized memory notes, public/owner projections, and organizer synchronization."""

from __future__ import annotations

import json
import re
import time
from uuid import uuid4

from ....core.store import dump as dump_memory
from .. import sources as memory_sources
from ..cooperation import applicable_chat_memory, bound_statement
from .models import (
    _ORIGINAL_PUBLIC_SOURCE_KINDS,
    _OWNER_PROVENANCE_VERSION,
    _PUBLIC_MEMORY_SOURCE,
    _PUBLIC_PROJECTION_VERSION,
    MEMORY_CHANNELS,
    MemoryChanged,
    contains_memory_credential,
)
from .scoring import extract_recall_keywords, front_topic_keywords, score_highlight_match


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


class DailyMemoryOrganizerBridge:
    def __init__(self, repo):
        self.repo = repo

    @property
    def store(self):
        return self.repo.store

    @property
    def private_owner(self):
        return self.repo.private_owner

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
            if proof := self.repo._source_evidence(row):
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
        signature = self.repo._title_key(headline)
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
        if self.repo.source_deleted(scope, key, owner):
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
        proof = self.repo._source_proof("operator_note" if kind == "private" else "viewer", source_ref, quote)
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
                self.repo._invalidate_previous_turn(existing)
        else:
            cursor = self.store.db.execute(
                "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,source_ref,source_user,"
                "status,created_at,headline,quote,anecdote,tags,source_evidence,updated_at,owner_principal) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope[1], scope[2], self.repo.business_date(source["at"]), "community_meme", source_ref,
                 "号主" if kind == "private" else "", "active", at, *fields, owner),
            )
            item_id = cursor.lastrowid
        memory_sources.record_source(self.store.db, item_id, scope, key, quote,
                                     projection=self._public_memory_projection(proof, proof["memory_source"]),
                                     thread=source.get("anchor"))
        return True

    def lookup_source(self, scope, channel, source_key):
        """Return exact, currently authorized original quotes. An absent source yields []."""
        if channel not in ("operator", *MEMORY_CHANNELS) or not self.repo.preference(scope)["enabled"]:
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
            self.repo._forget_sources(scope, row)
            self.store.db.execute("DELETE FROM daily_highlights WHERE id=?", (item_id,))
            return {"id": item_id, "deleted": True}
        if owner_only is not None:
            if not row["public_memory"] or row["status"] != "active":
                raise ValueError("只有已保存且有公开来源的经历可以切换共享")
            proof["owner_only"] = owner_only
        if note is not None and row["memory_kind"] == "private" and note != row["anecdote"]:
            self.repo._invalidate_previous_turn(row)
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

    def live_preferences(self, scope):
        if not self.repo.preference(scope)["enabled"]:
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
        return score_highlight_match(row, extract_recall_keywords(text), text)

    def recall_organized(self, scope, channel, text, *, resource=None, thread=None, participant=None,
                         specific_experiences=False, manual_inspection=False, memory_kind=None, limit=3):
        if not (manual_inspection and channel == "operator") and not self.repo.preference(scope)["enabled"]:
            return []
        matched = []
        keywords = (front_topic_keywords(text) if channel == "operator" and not manual_inspection
                    else extract_recall_keywords(text))
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
            score = score_highlight_match(row, keywords, text)
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
