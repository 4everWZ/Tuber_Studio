"""Operator dynamic drafting, presentation metadata, and continuation edits."""

import json
import time
from datetime import datetime
from uuid import uuid4

from ....core.identity import identity_messages, runtime_identity
from ....core.meter import budgeted_json
from ....core.models import content_hash
from ....core.text import detect_reasoning_leak, operation_output_diagnostics
from ....infra.platforms.bilibili.dynamics import read_dynamic_caption, validate_text
from ...kernel.feedback import FeedbackPublication
from ...kernel.generation import generate_with_recovery
from ..mentions import MENTION_INSTRUCTION, MENTION_REQUESTS, bind_mentions, rebase_mentions
from .evaluator import dynamic_decision_messages
from .materials import source_links
from .repository import _content_hash, _has_legacy_chat_lineage, _json
from .turns import evaluate_native_dynamic, revise_native_dynamic_caption, should_use_native_dynamic_turns


def _safe_rebase_mentions(text: str, mentions: list[dict]) -> list[dict]:
    kept = [item for item in (mentions or []) if item.get("name") and f"@{item['name']}" in text]
    return rebase_mentions(text, kept) if kept else []


class DynamicsDrafterMixin:
    def _valid_sources(self, row):
        if _has_legacy_chat_lineage(row):
            return False
        char = self._character()
        sources = row["sources"]
        if row.get("manual") and len(sources) == 1 and sources[0].get("type") == "operator_topic":
            decision = self.store.one("SELECT sources FROM dynamic_decisions WHERE id=?", (row["decision_id"],))
            return (
                (row["character_id"], row["character_version"]) == (char["id"], char["version"])
                and decision is not None
                and json.loads(decision["sources"]) == sources
            )
        dependencies = [item for source in sources for item in source.get("memory_dependencies", [])]
        dependencies.extend(json.loads(row.get("context_sources") or "[]"))
        originals = [{key: value for key, value in source.items() if key != "memory_dependencies"} for source in sources]
        bound_ids = [s["id"] for s in originals + dependencies]
        current = {s["id"]: s for s in self._sources(bound_ids=bound_ids)}

        def unchanged(source):
            fresh = current.get(source["id"])
            if not fresh:
                return False
            if source["type"] == "watched_video" and source.get("material_version"):
                return fresh.get("material_version") == source["material_version"]
            return all(fresh.get(key) == value for key, value in source.items() if key != "observed_at")

        return (
            row["character_id"] == char["id"]
            and row["character_version"] == char["version"]
            and all(unchanged(source) for source in originals + dependencies)
        )

    async def create_operator_draft(self, character, request_id, topic, *, account=""):
        source = {"id": f"operator-topic:{request_id}", "type": "operator_topic",
                  "text": topic or "选一个你自己想分享的话题"}
        sources = [source]
        async with self.evaluation_lock:
            existing = self.store.one(
                "SELECT id, character_id, character_version, sources, account_id FROM dynamic_posts "
                "WHERE manual=1 AND source_ids=?",
                (_json([source["id"]]),),
            )
            if existing:
                if (
                    (existing["character_id"], existing["character_version"])
                    != (character["id"], character["version"])
                    or json.loads(existing["sources"]) != sources
                    or existing["account_id"] != account
                ):
                    raise ValueError("请求已用于其他动态草稿")
                return self._post(existing["id"])
            scope = self.runtime.browsing.discovery.history_scope
            if str(scope()[0] or "") != account:
                raise ValueError("账号已切换，请重新交接动态素材")
            identity = runtime_identity(self.runtime, character, account or None)
            if should_use_native_dynamic_turns(self):
                async with self.runtime.offline_generation("background"):
                    data, _ = await evaluate_native_dynamic(
                        self,
                        char=character,
                        evaluation_account=account,
                        recent_posts=[],
                        related_posts=[],
                        public_sources=sources,
                        unified_public=[],
                        current_time=datetime.now().astimezone().isoformat(),
                        topic=topic or "选一个你自己想分享的话题",
                        manual=True,
                    )
                current = self._character()
                if (current["id"], current["version"]) != (character["id"], character["version"]) or (
                    runtime_identity(self.runtime, current, account or None) != identity
                ) or str(scope()[0] or "") != account:
                    raise ValueError("生成期间账号或角色已变化")
                if data["action"] != "post":
                    raise ValueError("本次没有可保存的动态正文")
                if source["id"] not in data["source_ids"]:
                    data["source_ids"] = [source["id"], *data["source_ids"]]
            else:
                messages = dynamic_decision_messages(
                    character, [], sources, current_time=datetime.now().astimezone().isoformat()
                )
                messages.append({
                    "role": "system",
                    "content": ("根据 operator_topic 写一条动态草稿，选 post 并引用这项来源。" if topic else
                                "从你自己感兴趣的话题写一条动态草稿，选 post。"),
                })
                async with self.runtime.offline_generation("background"):
                    result = await budgeted_json(
                        self.runtime.operation_llm, identity_messages(messages, identity), self.decision_schema(sources),
                        max_tokens=512,
                    )
                current = self._character()
                if (current["id"], current["version"]) != (character["id"], character["version"]) or (
                    runtime_identity(self.runtime, current, account or None) != identity
                ) or str(scope()[0] or "") != account:
                    raise ValueError("生成期间账号或角色已变化")
                data = self._parse(result, sources)
                if data["action"] != "post" or data["source_ids"] != [source["id"]]:
                    raise ValueError("本次没有可保存的动态正文")
            data["presentation"] = await self._presentation(data, sources)
            if str(scope()[0] or "") != account or self._character() != current:
                raise ValueError("核对提及期间账号或角色已变化")
            saved = self._save_decision(character, True, data, sources, generation_identity=identity,
                                        schedule=False, account_id=account)
            return self._post(saved["post_id"])

    async def propose_dynamic_edit(self, character, request_id, identifier, instruction):
        adapter = self.account.adapter
        account = str(await self.account.account_id())
        if self.store.one("SELECT 1 FROM dynamic_edits WHERE request_id=?", (request_id,)):
            previous = self.dynamic_edit(request_id, account, character["id"])
            if previous["platform_id"] != identifier:
                raise ValueError("这次修订请求已绑定另一条动态")
            return previous
        post = self.store.one(
            "SELECT id FROM dynamic_posts WHERE status='published' AND account_id=? AND character_id=?"
            " AND json_extract(receipt,'$.dynamic_id')=?",
            (account, character["id"], identifier),
        )
        if not post:
            raise ValueError("找不到当前账号和角色从工作台发布的这条动态")
        row = self._post(post["id"])
        original = await read_dynamic_caption(adapter, identifier, account)
        if not self._valid_sources(row):
            raise ValueError("原动态的公开来源已变化")
        if should_use_native_dynamic_turns(self):
            async with self.runtime.offline_generation("background"):
                raw_caption, mentions, pending = await revise_native_dynamic_caption(self, row, original, instruction)
            if adapter is not self.account.adapter or self._post(row["id"]) != row or not self._valid_sources(row):
                raise ValueError("账号、动态或公开来源已变化")
            caption = self._edit_caption(row, raw_caption)
            if pending:
                raise ValueError("请在编辑要求中提供要 @ 的 B 站 UID 和当前昵称")
            if not mentions and original.get("mentions"):
                mentions = rebase_mentions(caption, original.get("mentions", []))
        else:
            async with self.runtime.offline_generation("background"):
                result = await budgeted_json(self.runtime.operation_llm, [
                    {"role": "system", "content": (
                        "按号主的要求改写一条已经发布的动态。只输出可直接使用的正文。"
                        "原来的 @ 保留正文中的写法。只有号主要求改提及时才填 mentions，空列表表示全部移除。"
                        + MENTION_INSTRUCTION
                    )},
                    {"role": "user", "content": _json({
                        "instruction": instruction, "original": original["text"], "original_mentions": original.get("mentions", []),
                        "public_sources": [
                            {key: value for key, value in source.items() if key != "memory_dependencies"}
                            for source in row["sources"]
                        ],
                    })},
                ], {
                    "type": "object", "additionalProperties": False, "required": ["caption"],
                    "properties": {"caption": {"type": "string", "minLength": 1}, "mentions": MENTION_REQUESTS},
                }, max_tokens=1024)
            if not isinstance(result, dict) or "caption" not in result or set(result) - {"caption", "mentions"}:
                raise ValueError("模型未返回完整的动态修订")
            if adapter is not self.account.adapter or self._post(row["id"]) != row or not self._valid_sources(row):
                raise ValueError("账号、动态或公开来源已变化")
            caption = self._edit_caption(row, result["caption"])
            if "mentions" in result:
                mentions, pending = await bind_mentions(adapter, caption, result["mentions"], original.get("mentions", []))
                if pending:
                    raise ValueError("请在编辑要求中提供要 @ 的 B 站 UID 和当前昵称")
            else:
                mentions = rebase_mentions(caption, original.get("mentions", []))
        if adapter is not self.account.adapter or self._post(row["id"]) != row:
            raise ValueError("核对提及期间账号或动态已变化")
        self.store.execute(
            "INSERT INTO dynamic_edits(request_id,post_id,account_id,character_id,platform_id,original,caption,outgoing,status,mentions)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (request_id, row["id"], account, character["id"], identifier, _json(original),
             caption, self._outgoing_text(row, caption), "draft", _json(mentions)),
        )
        return self.dynamic_edit(request_id, account, character["id"])

    async def create_manual_dynamic_edit(
        self,
        post_id: int,
        caption: str,
        *,
        include_source_links: bool | None = None,
        include_comment_card: bool = False,
        request_id: str | None = None,
    ):
        adapter = self.account.adapter
        account = str(await self.account.account_id())
        character = self._character()
        row = self._post(post_id)
        if row["status"] != "published" or not (row.get("receipt") or {}).get("dynamic_id"):
            raise ValueError("只有已发布成功的动态才能原地编辑")
        if row.get("account_id") and row["account_id"] != account:
            raise ValueError("该动态属于其他账号，请先切换账号")
        if (row["character_id"], row["character_version"]) != (character["id"], character["version"]):
            raise ValueError("角色或版本已切换，无法直接原地编辑")
        if not self._valid_sources(row):
            raise ValueError("原动态的公开来源已变化")
        if include_comment_card:
            card = row.get("comment_card") or row.get("source_comment_card") or self._comment_card(
                row["sources"], json.loads(row.get("generation_identity") or "null")
            )
            if not card:
                raise ValueError("这条动态没有关联的原评论卡片")
        identifier = str(row["receipt"]["dynamic_id"])
        req_id = request_id or str(uuid4())
        original = await read_dynamic_caption(adapter, identifier, account)
        link_ids = None
        if include_source_links is True:
            existing_links = (row.get("presentation") or {}).get("link_source_ids")
            link_ids = list(existing_links) if existing_links else [
                s["id"] for s in row["sources"] if source_links(s)
            ][:2]
        elif include_source_links is False:
            link_ids = []
        cleaned = self._edit_caption(row, caption, link_source_ids=link_ids)
        if leak := detect_reasoning_leak(cleaned):
            raise ValueError(leak)
        outgoing = self._outgoing_text(row, cleaned, link_source_ids=link_ids)
        mentions = _safe_rebase_mentions(cleaned, original.get("mentions", []))
        self.store.execute(
            "INSERT INTO dynamic_edits(request_id,post_id,account_id,character_id,platform_id,original,caption,outgoing,status,mentions,link_source_ids,include_comment_card)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                req_id, row["id"], account, character["id"], identifier, _json(original),
                cleaned, outgoing, "draft", _json(mentions),
                _json(link_ids) if link_ids is not None else None,
                int(include_comment_card),
            ),
        )
        return self.dynamic_edit(req_id, account, character["id"])

    def redraft_post(self, post_id: int, *, text: str | None = None, include_source_links: bool = True):
        row = self._post(post_id)
        char = self._character()
        if (row["character_id"], row["character_version"]) != (char["id"], char["version"]):
            raise ValueError("角色或版本已切换，请重新评估起草")
        active_account = self._source_account()
        target_account = row.get("account_id") or active_account or ""
        if active_account and row.get("account_id") and active_account != row["account_id"]:
            raise ValueError("请先切换到原动态所属账号")
        if row["sources"] and not self._valid_sources(row):
            raise ValueError("原动态的公开来源已变化，请重新评估起草")
        content = validate_text(text if text is not None else row["content"])
        if leak := detect_reasoning_leak(content):
            raise ValueError(leak)
        link_ids = (
            [s["id"] for s in row["sources"] if source_links(s)][:2]
            if include_source_links
            else []
        )
        mentions = _safe_rebase_mentions(content, row.get("mentions", []))
        has_comment_card = any(
            (s.get("type") or s.get("kind")) in {"published_reply", "public_incident", "public_comment"}
            for s in row["sources"]
        )
        data = {
            "action": "post",
            "reason": f"基于动态 #{post_id} 重新编辑草稿",
            "content": content,
            "source_ids": list(row["source_ids"]),
            "next_evaluation_seconds": self.policy()["evaluate_interval_seconds"],
            "evaluation_account_id": target_account,
            "illustration": "comment_card" if has_comment_card else "none",
            "presentation": {
                "link_source_ids": link_ids,
                "mentions": mentions,
                "pending_mentions": [],
                "points": [{"summary": content[:60], "source_ids": list(row["source_ids"])}] if row["source_ids"] else [],
            },
        }
        identity = runtime_identity(self.runtime, char, target_account or None)
        saved = self._save_decision(
            char,
            True,
            data,
            row["sources"],
            generation_identity=identity,
            schedule=False,
            account_id=target_account,
            decide_image=False,
        )
        return self._post(saved["post_id"])

    async def _presentation(self, data, sources):
        participants = [person for source in sources for person in source.get("participants", [])]
        mentions, pending = await bind_mentions(self.account.adapter, data["content"], data["mentions"], participants)
        ambiguous = [item for item in pending if item.get("candidates")]
        demoted = [item for item in pending if not item.get("candidates")]
        presentation = {
            "link_source_ids": data["link_source_ids"],
            "mentions": mentions,
            "pending_mentions": ambiguous,
            "points": data["points"],
        }
        if demoted:
            presentation["demoted_mentions"] = demoted
        return presentation

    async def _refresh_post_identity(self, post_id, automatic, epoch):
        refresh = getattr(self.runtime, "refresh_identity", None)
        if refresh is None:
            return
        row = self._post(post_id)
        if row["status"] != ("pending_review" if automatic else "approved") or row.get("attempted_at"):
            return
        policy = self.policy()
        if automatic and (
            not (policy["enabled"] and policy["auto_publish"])
            or row["manual"]
            or self._automatic_post_id != post_id
        ):
            raise ValueError("未授权自动发布此动态")
        if not policy["credentials_configured"]:
            raise ValueError("发布动态需要先配置账号登录凭证")
        character = self.store.character(row["character_id"], row["character_version"])
        account = row.get("account_id") or getattr(self.account, "display_account_id", lambda: "")() or None
        self.operations.assert_allowed(
            automatic=automatic,
            channel="dynamics",
            account_id=account,
            character=character,
            epoch=epoch,
        )
        if not self._valid_sources(row):
            raise ValueError("人物版本或公开来源已变化，需重新评估")
        identity = await refresh(character, account)
        previous = json.loads(row.get("generation_identity") or "null")
        if previous and previous.get("identity_version") == identity["identity_version"]:
            return
        original = {key: row.get(key) for key in ("status", "content", "sources", "meme_id", "account_id", "presentation")}

        def guard():
            fresh = self._post(post_id)
            if any(fresh.get(key) != value for key, value in original.items()) or not self._valid_sources(
                fresh
            ):
                raise ValueError("身份更新期间动态正文或来源已变化")
            if (
                runtime_identity(self.runtime, character, account)["identity_version"]
                != identity["identity_version"]
            ):
                raise ValueError("生成期间账号身份再次变化")
            self.operations.assert_allowed(
                automatic=automatic,
                channel="dynamics",
                account_id=account,
                character=character,
                epoch=epoch,
            )

        messages = identity_messages(
            dynamic_decision_messages(
                character,
                self._recent_public_posts(character),
                row["sources"],
                current_time=datetime.now().astimezone().isoformat(),
            ),
            identity,
        )
        maximum = self.settings.data["omni"].get("operations_max_tokens", 8192)

        async def invoke(packet):
            return await budgeted_json(
                self.runtime.operation_llm, packet, self.decision_schema(row["sources"]), max_tokens=maximum
            )

        async with self.runtime.offline_generation("background"):
            guard()
            data, audit = await generate_with_recovery(
                self.runtime,
                channel="generation.dynamics",
                identity=f"identity-refresh:{post_id}:{identity['identity_version']}",
                binding={
                    "task": "generation",
                    "post_id": post_id,
                    "character": {key: character[key] for key in ("id", "version")},
                    "source_sha256": content_hash(row["sources"]),
                    "self_identity": identity,
                },
                character=character,
                messages=messages,
                generate=invoke,
                validate=lambda value: self._parse(value, row["sources"]),
                max_tokens=maximum,
                guard=guard,
                diagnose=lambda value: "正文包含大量重复内容，保留原文待审"
                if value["action"] == "post" and operation_output_diagnostics(value["content"])["needs_review"]
                else None,
                context_data={"public_sources": row["sources"], "reason": "当前账号身份资料已更新"},
            )
            guard()
            if data["action"] != "post":
                self._status(post_id, "rejected", data["reason"])
                raise ValueError("身份更新后模型决定暂不发布：" + data["reason"])
            selected = [source for source in row["sources"] if source["id"] in data["source_ids"]]
            if row.get("presentation") is not None:
                try:
                    mentions = rebase_mentions(data["content"], row["mentions"])
                except ValueError:
                    self._status(post_id, "pending_review", "身份更新后的正文需要重新确认提及")
                    raise
                presentation = row["presentation"] | {"mentions": mentions}
                if data["points"]:
                    presentation["points"] = data["points"]
                if presentation.get("link_source_ids") is not None:
                    presentation["link_source_ids"] = [item for item in presentation["link_source_ids"]
                                                       if item in data["source_ids"]]
                # An approved target can only change through the draft editor.
                if data["mentions"] and data["mentions"] != [
                    {"label": "@" + item["name"], "account": item["uid"]} for item in mentions
                ]:
                    self._status(post_id, "pending_review", "身份更新后的正文需要重新确认提及")
                    raise ValueError("请重新确认动态的提及对象")
            else:
                presentation = None
            self.store.execute(
                "UPDATE dynamic_posts SET content=?,content_hash=?,reason=?,sources=?,source_ids=?,"
                "generation_identity=?,comment_card_snapshot=?,presentation=?,updated_at=?,error=NULL WHERE id=?",
                (
                    data["content"],
                    _content_hash(data["content"]),
                    data["reason"],
                    _json(selected),
                    _json(data["source_ids"]),
                    _json(identity),
                    _json(self._comment_card(selected, identity) if row["comment_card"] or row.get("source_comment_card") else None),
                    _json(presentation) if presentation is not None else None,
                    time.time(),
                    post_id,
                ),
            )
            self.store.execute(
                "UPDATE dynamic_decisions SET content=?,reason=?,sources=?,source_ids=? WHERE id=?",
                (
                    data["content"],
                    data["reason"],
                    _json(selected),
                    _json(data["source_ids"]),
                    row["decision_id"],
                ),
            )
            if audit:
                self.store.execute("UPDATE dynamic_decisions SET generation_continuation_id=? WHERE id=?",
                                   (audit["id"], row["decision_id"]))
                if audit["state"] == "awaiting_review":
                    self._status(post_id, "pending_review", "正文诊断尚待审核")

    def _apply_revision(self, post_id, prepared, text):
        prepared.guard()
        text = validate_text(text)
        row = self._post(post_id)
        presentation = row.get("presentation")
        if presentation is not None:
            presentation = presentation | {"mentions": rebase_mentions(text, row["mentions"]), "points": []}
        self.store.execute(
            "UPDATE dynamic_posts SET content=?,content_hash=?,presentation=?,status='pending_review',error=NULL,updated_at=? WHERE id=?",
            (text, _content_hash(text), _json(presentation) if presentation is not None else None, time.time(), post_id),
        )

    async def resume_continuation(self, post_id, continuation_id):
        if str(post_id).startswith("generation:"):
            identity = str(post_id).split(":", 1)[1]
            decision = self.store.one("SELECT * FROM dynamic_decisions WHERE id=?", (identity,))
            if not decision or decision.get("generation_continuation_id") != continuation_id:
                raise ValueError("生成恢复任务与运营决定不匹配")
            row = self.runtime.continuations.get(continuation_id)
            if row["binding"]["character"] != {key: self._character()[key] for key in ("id", "version")}:
                raise ValueError("生成恢复任务的角色已变化")
            self.runtime.continuations.resume(row["id"], binding=row["binding"])
            result = await self.evaluate(manual=True)
            return {
                "status": result["status"],
                "decision": result,
                "continuation": result.get("continuation"),
            }
        return await FeedbackPublication(self.runtime, self.store, self.operations).resume(
            "dynamics",
            post_id,
            continuation_id,
            prepare=lambda: self._publication_context(post_id, False, None),
            apply_revision=lambda prepared, text: self._apply_revision(post_id, prepared, text),
            reconcile=lambda token, prepared: self._reconcile(post_id),
        )
