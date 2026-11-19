"""Incremental, source-bound organization of operator preferences and public experiences."""

import asyncio
import json
import re
import time
from contextlib import suppress
from contextvars import ContextVar
from typing import Literal

import httpx
from pydantic import Field, create_model

from ..cooperation import RoutineReview, corroborates, locate_rule, with_scope_context
from ..daily_memory import contains_memory_credential
from ..generation_meter import budgeted_call
from ..memory_sources import install_chat_cleanup, memory_transaction
from ..models import StrictModel
from ..operation_continuations import content_hash
from ..operation_identity import without_model_identity
from ..operations.browsing.comment_timeline import comment_thread, resource_key
from ..providers import public_error
from ..store import dump

# Only the isolated replay installs a sink. Private turns never enter general application logs.
admission_receipt = ContextVar("private_memory_admission_receipt", default=None)
_STATEMENT_LIMIT = 150


class MemorySuggestion(StrictModel):
    action: Literal["add", "update", "skip"]
    existing_id: int | None
    headline: str = Field(max_length=40)
    note: str = Field(max_length=150)
    tags: list[str] = Field(max_length=5)
    summary_kind: Literal["preference", "decision"] = "preference"


class OwnerUtterance(StrictModel):
    meaning: Literal["持续合作方式", "当前事项", "社交交谈", "其他"]
    statement: str = Field(max_length=_STATEMENT_LIMIT, description="完整逐字摘录表达该意思的原句，包含下面有据的范围和做法，保留条件和否定。")
    scope_quote: str = Field(description="从 statement 摘出适用场合、时间或习惯，未说明填空。")
    practice_quote: str = Field(description="从 statement 摘出具体做法，与范围分别定位，未说明填空。")


class AgreementReview(RoutineReview):
    topic_quote: str = Field(max_length=40, description="从原话摘取代表约定主题的短语。")
    existing_id: int | None = Field(description="更正同一件事时选原条目 ID，否则 null。")
    summary_kind: Literal["preference", "decision"]


class ChangeOfPractice(StrictModel):
    relation: Literal["两种做法可并行", "新做法取代原做法", "无法确定"]
    common_reference: str = Field(description="两份原话中均可逐字定位的同一具体字段、主题名或被引用的旧值；找不到填空。")
    old_value: str = Field(description="旧约定中被取代的具体做法原文。")
    new_value: str = Field(description="新约定中对应的具体做法原文。")


def is_chat_turn(result):
    submission = result.get("submission") or {}
    if submission.get("video_selection") or result.get("card"):
        return False
    action = (submission.get("intent") or {}).get("action") or result.get("memory_intent")
    if action:
        return action == "chat"
    return True


class MemoryOrganizer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.worker = None
        self.status = "waiting"
        self.error = None
        self.status_scope = None
        self.next_run = 0

    def start(self):
        if not self.worker or self.worker.done():
            install_chat_cleanup(self.store.db)
            self.worker = asyncio.create_task(self._loop())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker

    def setting_changed(self):
        self.next_run = 0
        if self.status == "disabled" and self.runtime.daily_memory.preference(self.scope())["enabled"]:
            self.status = "idle"
        self.schedule()

    def schedule(self):
        self.changed.set()

    def state(self):
        scope = self.scope()
        preference = self.runtime.daily_memory.preference(scope)
        counts = self.store.all(
            "SELECT status,COUNT(*) AS count FROM memory_organization WHERE account=? AND character=? AND version=? "
            "AND source NOT LIKE 'thread:%' AND status IN ('saved','skipped','invalid') GROUP BY status", scope,
        )
        status, error = ((self.status, self.error) if self.status_scope == scope else ("idle", None))
        return {"status": status if preference["enabled"] else "disabled", "error": error,
                "processed": sum(row["count"] for row in counts),
                "invalid": sum(row["count"] for row in counts if row["status"] == "invalid")}

    def scope(self):
        account, character, version = self.runtime.browsing.discovery.history_scope()
        return str(account or ""), character, version

    async def _loop(self):
        while True:
            try:
                delay = max(1, min(60, self.next_run - time.monotonic())) if self.next_run else 60
                await asyncio.wait_for(self.changed.wait(), timeout=delay)
            except TimeoutError:
                pass
            self.changed.clear()
            if time.monotonic() < self.next_run:
                continue
            try:
                processed = await self.run_once()
            except (ValueError, TimeoutError, httpx.HTTPError) as error:
                self.error = public_error(error)
                self.status = "retry"
                self.next_run = time.monotonic() + 120
            else:
                if processed:
                    self.error = None
                self.next_run = time.monotonic() + (15 if processed else 60)

    def _source_rows(self, scope):
        # The original operator text is the only eligible private source.
        account, character, version = scope
        private = self.store.all(
            "SELECT t.request_id,t.message,t.created_at,t.result,t.character_version FROM operator_chat_turns t "
            "WHERE t.character_id=? AND COALESCE(json_extract(t.result,'$.owner_source')=?,1) "
            "AND ((t.character_version=? AND json_extract(t.result,'$.account_id')=?) "
            "OR (json_extract(t.result,'$.owner_source')=? AND NOT EXISTS (SELECT 1 FROM kv WHERE "
            "key='memory_preference:'||json_extract(t.result,'$.account_id')||':'||t.character_id "
            "AND json_extract(value,'$.enabled')=0))) "
            "AND COALESCE(json_extract(t.result,'$.memory_intent'),'chat')='chat' "
            "AND NOT EXISTS (SELECT 1 FROM memory_source_tombstones d WHERE d.owner=json_extract(t.result,'$.owner_source') "
            "AND d.character=t.character_id AND d.source_key='turn:'||t.request_id) "
            "AND NOT EXISTS (SELECT 1 FROM memory_organization p WHERE p.account=json_extract(t.result,'$.account_id') "
            "AND p.character=t.character_id AND p.version=t.character_version AND p.source='turn:'||t.request_id) "
            "ORDER BY t.created_at,t.request_id LIMIT 4",
            (character, self.runtime.daily_memory.private_owner, version, account, self.runtime.daily_memory.private_owner),
        )
        for turn in private:
            saved = json.loads(turn["result"])
            origin = (saved["account_id"], character, turn["character_version"])
            yield {"key": "turn:" + turn["request_id"], "kind": "private", "text": turn["message"],
                   "at": turn["created_at"], "revision": turn["request_id"],
                   "origin": origin, "preference": self.runtime.daily_memory.preference(origin),
                   "temporary": not is_chat_turn(saved) or contains_memory_credential(turn["message"])}
        if not account:
            return
        public = self.store.all(
            "WITH receipts AS ("
            "SELECT 'reply:'||id AS identity,target,updated_at FROM browsing_intents "
            "WHERE account_id=? AND character_id=? AND character_version=? AND kind='reply_public' AND status='published' "
            "AND json_extract(receipt,'$.rpid') IS NOT NULL AND json_extract(target,'$.memory_audit') IS NULL "
            "UNION ALL SELECT 'manual:'||d.id,json_object('bvid',c.video_id,'root',c.thread_id,'parent',c.comment_id),"
            "d.updated_at FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE d.account_id=? AND d.character_id=? "
            "AND d.character_version=? AND d.status='published' AND c.platform='bilibili' "
            "AND json_extract(d.receipt,'$.rpid') IS NOT NULL AND json_extract(d.answer_check,'$.organized_memory') IS NULL"
            ") SELECT identity,target,updated_at FROM receipts r WHERE NOT EXISTS (SELECT 1 FROM memory_organization p "
            "WHERE p.account=? AND p.character=? AND p.version=? AND p.source=r.identity) "
            "ORDER BY updated_at,identity LIMIT 2", (*scope, *scope, *scope),
        )
        for row in public:
            target = json.loads(row["target"])
            anchor = {"resource": resource_key(target), "root_id": str(target.get("root") or ""),
                      "comment_id": str(target.get("parent") or "")}
            source = self._public_source(scope, anchor, row["updated_at"])
            if source and not self.store.one(
                "SELECT 1 FROM memory_organization WHERE account=? AND character=? AND version=? "
                "AND source=? AND (revision=? OR status='deleted')", (*scope, source["key"], source["revision"]),
            ):
                source["progress_key"] = row["identity"]
            else:
                source = {"key": row["identity"], "kind": "unavailable", "revision": row["identity"]}
            yield source

    def _public_source(self, scope, anchor, at):
        if not anchor["resource"] or not anchor["root_id"] or not anchor["comment_id"]:
            return None
        thread = comment_thread(self.store, anchor["resource"], anchor["root_id"],
                                account_id=scope[0], character_id=scope[1])
        source = next((item for item in thread["comments"] if item["comment_id"] == anchor["comment_id"]
                       and item["author_uid"] and item["author_uid"] != scope[0] and item["text"] and item["href"]), None)
        if not source:
            return None
        reply = next((item for item in reversed(thread["comments"]) if item["parent_comment_id"] == source["comment_id"]
                      and item["publication"] == "confirmed" and item["text"] and item["href"]), None)
        if not reply:
            return None
        fields = ("comment_id", "author_uid", "author_name", "text", "href")
        references = [{field: item[field] for field in fields} | {"kind": kind}
                      for item, kind in ((source, "comment"), (reply, "published_reply"))]
        return {"key": f"thread:{anchor['resource']}:{anchor['root_id']}", "kind": "experience",
                "source_kind": "comment", "text": source["text"], "references": references, "anchor": anchor, "at": at,
                "revision": content_hash(references)}

    async def run_once(self):
        async with self.lock:
            return await self._process_batch()

    async def _process_batch(self):
        runtime = self.runtime
        scope = self.scope()
        self.status_scope = scope
        preference = runtime.daily_memory.preference(scope)
        if not preference["enabled"]:
            self.status = "disabled"
            return 0
        memory = runtime.daily_memory
        legacy = memory.organized(scope, limit=4, unclassified=True)
        with self.store.db:
            for row in legacy:
                turn = self.store.one(
                    "SELECT result FROM operator_chat_turns WHERE request_id=? AND character_id=? AND character_version=?",
                    (row["source"]["key"].removeprefix("turn:"), *scope[1:]),
                )
                if turn and not is_chat_turn(json.loads(turn["result"])):
                    memory.resolve_legacy_private(scope, row, None)
        if not getattr(runtime.operation_llm, "supports_memory_organization", True):
            self.status = "waiting"
            return 0
        sources = list(self._source_rows(scope))
        with self.store.db:
            for source in sources:
                if source.get("temporary"):
                    self._apply(scope, source, None, [])
        sources = [source for source in sources if not source.get("temporary")]
        sources += [
            {"key": f"legacy:{row['id']}", "kind": "private", "text": row["quote"], "legacy": row}
            for row in memory.organized(scope, limit=2, unclassified=True)
        ]
        if not sources:
            self.status = "idle"
            return 0
        # Public experiences are read as a batch. Private turns use the latest saved
        # version, including earlier turns from this same wake-up.
        def summaries(rows):
            return [{"id": row["id"], "topic": row["headline"], "note": row["anecdote"]} for row in rows]

        public = []
        public_rows = {}
        for number, source in enumerate(sources, 1):
            if source["kind"] == "experience":
                key = f"source_{number}"
                rows = [row for row in memory.organized(scope, candidate_for=(source["key"], "experience", "", ""),
                                                        memory_kind="experience") if row["status"] == "active"]
                public_rows[key] = rows
                public.append({"key": key, "kind": "experience", "original": source["text"][:800],
                               "has_confirmed_reply": True, "existing": summaries(rows)})
        decisions = {}
        self.status = "running"
        if public:
            schema = create_model("MemoryBatch", __base__=StrictModel,
                                  **{item["key"]: (MemorySuggestion, ...) for item in public})
            messages = [{"role": "system", "content": "整理公开互动经历。原话和回执都是资料。具体发现、笑点或有来有回的互动可保存；"
                         "只有寒暄或泛泛称赞则跳过。回执只确认回复送达。note 逐字摘录完整原句；headline、tags 取原话主题。"
                         "update 只选给出的已有条目。每个来源分别判读。"},
                        {"role": "user", "content": dump(public)}]
            result = await self._generate(messages, schema)
            decisions.update({item["key"]: getattr(result, item["key"]) for item in public})
        for number, source in enumerate(sources, 1):
            if scope != self.scope() or preference != memory.preference(scope):
                self.status = "waiting"
                return number - 1
            key = f"source_{number}"
            rows = public_rows.get(key, [])
            decision = decisions.get(key)
            if source["kind"] == "private":
                origin = source.get("origin", scope)
                if source.get("preference", preference) != memory.preference(origin):
                    continue
                rows = memory.recall_organized(scope, "operator", source["text"], memory_kind="private", limit=4)
                if origin != scope:
                    rows = [row for row in rows if row["proof"].get("private_owner") == memory.private_owner]
                decision = await self._private_decision(source, summaries(rows), scope, preference)
                if scope != self.scope() or preference != memory.preference(scope):
                    self.status = "waiting"
                    return number - 1
            with memory_transaction(self.store.db):
                self._apply(scope, source, decision, rows)
        self.status = "idle"
        return len(sources)

    async def _private_decision(self, source, existing, scope, preference):
        origin = source.get("origin", scope)
        with without_model_identity():
            reading = await self._generate([
                {"role": "system", "content": "读这位说话人自己的话。判断它表达一贯的合作方式、当前某件事、"
                 "社交交谈，还是其他意思。对每种意思都摘录原句、所指场合和对方要做的事。"
                 "谈到一项共同惯例就是表达长期合作方式；不需要说话人另行要求保存。"},
                {"role": "user", "content": source["text"]}], OwnerUtterance)
            if emit := admission_receipt.get():
                emit(source["key"], {"stage": "utterance", **reading.model_dump()})
            if reading.meaning != "持续合作方式" or not all((reading.statement, reading.scope_quote, reading.practice_quote)):
                return None
            original = source["text"]
            rule = locate_rule(original, reading)
            if rule:
                # A comma-delimited excerpt can omit the subject or condition of a change.
                # Have the existing reviewer judge the full sentence against the original.
                start, end = rule["statement_span"]
                boundaries = "。！？.!?\n"
                while start and original[start - 1] not in boundaries:
                    start -= 1
                while end < len(original) and original[end - 1] not in boundaries:
                    end += 1
                sentence = original[start:end].strip()
                if sentence != reading.statement:
                    rule = None
                    if len(sentence) <= _STATEMENT_LIMIT:
                        reading = reading.model_copy(update={"statement": sentence})
                        rule = locate_rule(original, reading)
            if not rule:
                if emit:
                    emit(source["key"], {"stage": "rejected_unbound_proposal"})
                return None
            rule = with_scope_context(source["text"], rule)
            if (scope != self.scope() or preference != self.runtime.daily_memory.preference(scope)
                    or source.get("preference", preference) != self.runtime.daily_memory.preference(origin)):
                return None
            review = await self._generate([
                {"role": "system", "content": "只按 original 判断说话人本人的持续合作约定。核对 scope_quote 是否说明跨次适用，"
                 "practice_quote 是否是可执行做法，两者是否是同一约定且 statement 保留条件和否定。一次邀约、"
                 "转述与玩笑按原意判。existing 仅用于定位更正。不同的做法可以并存，即使都与写作有关。只有本句取代了"
                 "原约定的同一具体做法才选对应 ID，新增做法填 null。"},
                {"role": "user", "content": dump({"original": source["text"], **reading.model_dump(exclude={"meaning"}),
                 "scope_quote": source["text"][slice(*rule["scope_span"])],
                 "existing": existing})}], AgreementReview)
        accepted = corroborates(source["text"], rule, review)
        if emit:
            emit(source["key"], {"stage": "review", "bound": rule, "judgment": review.model_dump(), "accepted": accepted})
        if not accepted:
            return None
        source["cooperation_rule"] = rule
        target = next((row for row in existing if row["id"] == review.existing_id), None)
        update = target and await self._replaces(source["key"], target["note"], reading.statement)
        headline = review.topic_quote.strip()
        if not headline or headline not in reading.statement:
            headline = reading.statement[:40]
        return MemorySuggestion(action="update" if update else "add", existing_id=target["id"] if update else None,
                                headline=headline, note=reading.statement, tags=[],
                                summary_kind=review.summary_kind)

    async def _replaces(self, key, existing, incoming):
        if not existing:
            return False
        with without_model_identity():
            change = await self._generate([
                {"role": "system", "content": "比较两条协作约定。判断是否能在同一份成品或同一次工作同时做到。"
                 "若可以，则两种做法可并行。若新要求明确废止旧做法，或对同一事项给出了彼此不相容的取值，"
                 "才是取代。分别从两句原话中逐字摘出相应做法。"},
                {"role": "user", "content": dump({"original": existing, "new": incoming})}], ChangeOfPractice)
        reference = change.common_reference.strip()
        replaces = bool(change.relation == "新做法取代原做法" and re.search(r"\w", reference)
                        and reference in existing and reference in incoming
                        and change.old_value.strip() and change.old_value in existing
                        and change.new_value.strip() and change.new_value in incoming)
        if emit := admission_receipt.get():
            emit(key, {"stage": "correction_target", "judgment": change.model_dump(), "replaces": replaces})
        return replaces

    async def _generate(self, messages, result_type):
        async with self.runtime.offline_generation("background"), asyncio.timeout(60):
            provider = self.runtime.operation_llm
            generate = getattr(provider, "generate_operator_json", provider.generate_json)
            schema = result_type.model_json_schema()
            value = await budgeted_call(provider, lambda: generate(messages, schema, max_tokens=1000),
                                        messages, max_tokens=1000)
            return result_type.model_validate(value)

    def _apply(self, scope, source, decision, supplied):
        if "origin" in source:
            scope = source["origin"]
            if source["preference"] != self.runtime.daily_memory.preference(scope):
                return
        if decision and decision.action != "skip":
            original = (source.get("cooperation_rule") or {}).get("statement", source["text"]).strip()
            tags = [tag.strip() for tag in decision.tags if tag.strip() and tag.strip() in original]
            label = decision.headline.strip()
            if not label or label not in original:
                label = next(iter(tags), original)[:40]
            decision = decision.model_copy(update={"tags": tags, "headline": label})
        if legacy := source.get("legacy"):
            self.runtime.daily_memory.resolve_legacy_private(scope, legacy, decision, reviewed_chat=True,
                                                            rule=source.get("cooperation_rule"))
            return
        if source["kind"] == "private":
            current = self.store.one(
                "SELECT message,result FROM operator_chat_turns WHERE request_id=? AND character_id=? "
                "AND character_version=? AND json_extract(result,'$.account_id')=?",
                (source["key"].removeprefix("turn:"), *scope[1:], scope[0]),
            )
            if not current or current["message"] != source["text"]:
                return
            saved = json.loads(current["result"])
            if self.runtime.daily_memory.source_deleted(scope, source["key"], saved.get("owner_source")):
                return
            if not is_chat_turn(saved):
                decision = None
            elif saved.get("private") and saved.get("owner_source"):
                source = source | {"private_owner": saved["owner_source"]}
        elif source["kind"] == "experience":
            current = self._public_source(scope, source["anchor"], source["at"])
            if not current or current["revision"] != source["revision"]:
                return
        history = self.store.one("SELECT revision,status FROM memory_organization WHERE account=? AND character=? "
                                 "AND version=? AND source=?", (*scope, source["key"]))
        deleted = history and history["status"] == "deleted"
        processed = history and history["revision"] == source["revision"]
        if (deleted or processed) and not source.get("progress_key"):
            return
        status = "skipped"
        if decision and decision.action != "skip" and not (deleted or processed):
            existing = next((row for row in supplied if row["id"] == decision.existing_id), None)
            if (not source["text"].strip() or not decision.note.strip() or not decision.headline.strip()
                    or decision.action == "update" and not existing):
                status = "invalid"
            elif self.runtime.daily_memory.save_organized(scope, source, decision, existing):
                status = "saved"
        self.store.db.execute(
            "INSERT INTO memory_organization VALUES(?,?,?,?,?,?,?) ON CONFLICT(account,character,version,source) "
            "DO UPDATE SET revision=excluded.revision,status=excluded.status,updated_at=excluded.updated_at",
            (*scope, source.get("progress_key", source["key"]), source["revision"], status, time.time()),
        )
        if source["kind"] == "private" and (emit := admission_receipt.get()):
            emit(source["key"], {"stage": "source_ledger", "status": status})
        if source["kind"] == "experience" and not (deleted or processed):
            self.store.db.execute(
                "INSERT INTO memory_organization VALUES(?,?,?,?,?,?,?) ON CONFLICT(account,character,version,source) "
                "DO UPDATE SET revision=excluded.revision,status=excluded.status,updated_at=excluded.updated_at",
                (*scope, source["key"], source["revision"], status, time.time()),
            )
