"""Owner-selected replies handed to the existing comment and inbox publishers."""

import json
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from pydantic import Field

from ..core.models import StrictModel, content_hash
from ..core.store import dump
from ..infra.inference.providers import public_error
from ..operations.facade import (
    COMMENT_ID,
    VIDEO_ID,
    comment_draft_source,
    comment_payload,
    comment_publication_binding,
    control_policy,
    create_feedback_publication,
    dm_draft_source,
    dm_message_source,
    dm_outgoing_payload,
    dm_publication_binding,
    dm_reply_window,
    get_attempt,
    reconcile_attempt,
    reconcile_channel_draft,
)
from .dynamic_publication import pending
from .task_runtime import Tool, ToolWait

LocalID = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]


class ReplyTarget(StrictModel):
    channel: Literal["comments", "messages"]
    draft_id: LocalID
    source_ids: list[LocalID] = Field(min_length=1, max_length=30)


class ReplySelection(StrictModel):
    items: list[ReplyTarget] = Field(min_length=1, max_length=100)
    epoch: int = Field(strict=True, ge=1)


class SelectionRef(StrictModel):
    selection_id: UUID


class SelectedReply(SelectionRef):
    item: int = Field(strict=True, ge=1, le=100)


class ReplyOperations:
    def __init__(self, tasks, comments, messages):
        self.tasks, self.store = tasks, tasks.store
        self.comments, self.messages = comments, messages
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_reply_selections(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, policy_epoch INTEGER NOT NULL, items TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_reply_attempts(
                step_id TEXT PRIMARY KEY, approval_hash TEXT NOT NULL, token TEXT UNIQUE NOT NULL, domain_attempt_id INTEGER);
        """)
        if "domain_attempt_id" not in {row["name"] for row in self.store.all("PRAGMA table_info(agent_reply_attempts)")}:
            self.store.execute("ALTER TABLE agent_reply_attempts ADD COLUMN domain_attempt_id INTEGER")

    def snapshot(self, target, scope, *, publishing=False):
        channel, identity = target["channel"], target["draft_id"]
        service = self.comments if channel == "comments" else self.messages
        row = (
            comment_draft_source(service, identity)
            if channel == "comments"
            else dm_draft_source(self.store, identity)
        )
        if not row or not scope[0] or [row[k] for k in ("account_id", "character_id", "character_version")] != scope[:3]:
            raise ValueError("草稿没有绑定当前账号和角色版本")
        allowed = {"publishing"} if publishing else {"approved", "copied", "pending_review"}
        if row["status"] not in allowed or not row["text"].strip():
            raise ValueError("草稿已处理或不能审核")
        recipient = row["platform_user_id"]
        if not COMMENT_ID.fullmatch(str(recipient)) or recipient == scope[0]:
            raise ValueError("回复需要明确的对方 UID")
        character = self.store.character(row["character_id"], row["character_version"])
        if channel == "comments":
            source = self.store.one("SELECT platform FROM comments WHERE id=?", (row["comment_id"],))
            if (source["platform"] != "bilibili" or not VIDEO_ID.fullmatch(row["video_id"] or "")
                    or not all(COMMENT_ID.fullmatch(str(row[k])) for k in ("thread_id", "platform_comment_id"))
                    or target["source_ids"] != [row["comment_id"]]):
                raise ValueError("所选来源与原视频评论不符")
            binding = comment_publication_binding(service, row, scope[0], character)
            sources = [row["platform_comment_id"]]
            source_hash = binding["source"]["content_sha256"]
            destination = f"公开评论 {row['video_id']} 楼 {row['thread_id']}"
            payload = comment_payload(row, service.runtime)
        else:
            if row["source_kind"] != "message" or row["platform"] != "bilibili":
                raise ValueError("须选择由实际来信产生的私信草稿")
            source = dm_message_source(service, "message", row["source_id"], character)
            binding = dm_publication_binding(service, row, source, scope[0], character)
            expected = binding["source"].get("reply_incoming_ids") or [row["source_id"]]
            ids = target["source_ids"]
            if len(set(ids)) != len(ids) or set(expected) != set(ids) or row["source_id"] not in ids:
                raise ValueError("草稿的回复来源必须与选择集合一致")
            conversation = dm_reply_window(service, row, source)
            if not set(ids).issubset(conversation.refs.values()):
                raise ValueError("所选来信已处理或不属于当前会话")
            rows = self.store.all(
                "SELECT id,message_id,content FROM dm_inbox WHERE platform='bilibili' AND account_id=? "
                "AND character_id=? AND platform_user_id=? AND id IN (SELECT value FROM json_each(?)) ORDER BY id",
                (scope[0], scope[1], recipient, dump(ids)),
            )
            if len(rows) != len(ids):
                raise ValueError("所选来信不属于同一账号、角色和收件人")
            sources = [item["message_id"] for item in rows]
            source_hash = content_hash(rows)
            destination = "私信"
            payload = dm_outgoing_payload(service, row, character)
        return {**target, "binding": binding, "payload": payload, "source_keys": sources, "source_hash": source_hash,
                "audit_hash": content_hash(row["answer_check"]), "status": row["status"], "revision": row["updated_at"],
                "target": f"账号 {scope[0]} · {character['name']} v{scope[2]} · {destination} → UID {recipient}"
                          f" · 来源 {', '.join(sources)}",
                "text": payload.get("text") or f"表情 {payload['meme_id']} ({payload['asset_sha256']})"}

    def select(self, selection):
        scope = self.tasks.scope()
        if not self.tasks.identity or scope[-1] != selection.epoch:
            raise ValueError("账号或角色授权已变化")
        items, seen = [], set()
        for target in selection.items:
            keys = {(target.channel, value) for value in target.source_ids}
            if seen & keys:
                raise ValueError("同一来源只能选择一次")
            seen.update(keys)
            items.append(self.snapshot(target.model_dump(), scope))
        identity = str(uuid4())
        self.store.execute("INSERT INTO agent_reply_selections VALUES(?,?,?,?)",
                           (identity, dump(scope), control_policy(self.comments)["epoch"], dump(items)))
        return {"selection_id": identity, "items": [{"item": index, "target": item["target"], "text": item["text"]}
                                                    for index, item in enumerate(items, 1)]}

    def selection(self, args):
        scope = self.tasks.scope()
        row = self.store.one("SELECT * FROM agent_reply_selections WHERE id=? AND scope=?",
                             (str(args.selection_id), dump(scope)))
        policy = control_policy(self.comments)
        if not row or policy["emergency"] or row["policy_epoch"] != policy["epoch"]:
            raise ValueError("所选范围已失效")
        return scope, row["policy_epoch"], json.loads(row["items"])

    def review(self, args):
        scope, epoch, items = self.selection(args)
        if args.item > len(items):
            raise ValueError("条目不在选择集合内")
        item = items[args.item - 1]
        target = {key: item[key] for key in ReplyTarget.model_fields}
        if self.snapshot(target, scope) != item:
            raise ValueError("所选草稿或来源已变化，请重新选择")
        return item | {"scope": scope, "epoch": epoch, **args.model_dump(mode="json")}

    async def inspect(self, request, args):
        _, _, items = self.selection(args)
        return {"items": [{"item": index, "target": item["target"]} for index, item in enumerate(items, 1)]}

    def current(self, approval):
        policy = control_policy(self.comments)
        return (self.tasks.identity.grant_current(approval["scope"]) and not policy["emergency"]
                and policy["epoch"] == approval["epoch"]
                and self.store.one("SELECT 1 FROM agent_reply_selections WHERE id=? AND scope=?",
                                   (approval["selection_id"], dump(approval["scope"]))))

    async def recover(self, step_id, approval, *, read):
        link = self.store.one("SELECT * FROM agent_reply_attempts WHERE step_id=?", (step_id,))
        if not link:
            return {"status": "not_submitted", "error": "该步骤没有取得发送令牌，请重新核对所选草稿。"}
        service = self.comments if approval["channel"] == "comments" else self.messages
        token = link["token"]
        attempt = get_attempt(service, token)
        if not attempt or link["approval_hash"] != content_hash(approval) or attempt["binding"] != approval["binding"]:
            return pending("原审批与发送令牌不匹配")
        if not self.tasks.identity.grant_current(approval["scope"]):
            return pending("原账号或角色授权已变化")
        is_comment = approval["channel"] == "comments"
        attempts = "publish_attempts" if is_comment else "dm_attempts"
        identity = approval["draft_id"]

        def original_draft():
            row = (
                comment_draft_source(service, identity)
                if is_comment
                else dm_draft_source(self.store, identity)
            )
            if (not row or [row[k] for k in ("account_id", "character_id", "character_version")] != approval["scope"][:3]
                    or content_hash(row["answer_check"]) != approval["audit_hash"]):
                return None
            account = approval["scope"][0]
            character = approval["binding"]["character"]
            if is_comment:
                binding = comment_publication_binding(service, row, account, character)
            elif row["source_kind"] == "message":
                source = dm_message_source(service, "message", row["source_id"], character)
                binding = dm_publication_binding(service, row, source, account, character)
            else:
                return None
            if is_comment:
                payload = comment_payload(row, service.runtime)
            elif row.get("message_kind") == "image" and approval["payload"].get("message_kind") == "image":
                # A disabled or retired catalog image can still have a confirmed historical receipt.
                payload = approval["payload"] | {"meme_id": row.get("meme_id")}
            else:
                payload = dm_outgoing_payload(service, row, character)
            if binding != approval["binding"] or payload != approval["payload"]:
                return None
            return row

        draft = original_draft()
        domain = self.store.one(f"SELECT id,status,receipt FROM {attempts} WHERE draft_id=? ORDER BY id DESC LIMIT 1",
                                (identity,))
        if not draft or not domain or domain["id"] != link["domain_attempt_id"]:
            return pending("原草稿与领域发送记录无法唯一匹配")
        receipt = json.loads(draft["receipt"] or "null")
        receipt_id = "rpid" if is_comment else "msg_key"
        if (draft["status"] == domain["status"] == "published" and receipt and receipt == json.loads(domain["receipt"] or "null")
                and receipt.get(receipt_id)):
            if attempt["resolved_status"] == "uncertain" and read:
                attempt = reconcile_attempt(
                    service, token, account_id=approval["scope"][0], binding=approval["binding"], receipt=receipt
                )
            if attempt["resolved_status"] == "published" and (attempt["resolution_receipt"] or attempt["receipt"]) == receipt:
                return {"status": "published", "write_token": token, "receipt": receipt, "target": approval["target"],
                        "message": f"{approval['target']} · 平台回执 {receipt[receipt_id]}"}
        if attempt["resolved_status"] in {"rejected", "cancelled"}:
            return {"status": attempt["resolved_status"], "error": "原发送未被平台接受", "write_token": token}
        if read and draft["status"] == "publish_uncertain":
            def guard(row, original):
                if (not self.tasks.identity.grant_current(approval["scope"]) or not original or original["token"] != token
                        or row["binding"] != approval["binding"] or row["feedback"]["sent_payload"] != approval["payload"]
                        or not original_draft() or self.store.one(
                            f"SELECT MAX(id) AS id FROM {attempts} WHERE draft_id=?", (identity,))["id"] != domain["id"]):
                    raise ValueError("待核对记录与原提交不符")

            feedback = create_feedback_publication(service)
            continuation = feedback.coordinator.find(approval["channel"], identity)
            if not continuation and attempt["resolved_status"] in {"published", "uncertain"}:
                continuation = feedback.record_unknown(approval["channel"], identity, token, approval["binding"],
                                                       approval["payload"], "原提交中断，按发送账本核对")
            if continuation and continuation["state"] == "needs_reconciliation":
                guard(continuation, get_attempt(service, continuation["feedback"]["write_token"]))
                await reconcile_channel_draft(service, identity, guard=guard)
                return await self.recover(step_id, approval, read=False)
        return pending("原提交尚待回执核对")

    async def run(self, request, args):
        approval = request.approval
        if (not approval or approval["scope"] != request.scope or approval["selection_id"] != str(args.selection_id)
                or approval["item"] != args.item):
            return {"error": "本步骤缺少对应条目的审批"}
        if request.checkpoint:
            return await self.recover(request.id, approval, read=True)
        submission = ReviewedReply(self, request.id, approval)
        reason = None
        try:
            submission.check()
            if approval["channel"] == "comments":
                result = await self.comments.publish(approval["draft_id"], submission=submission)
                if result.get("status") == "operator_replied":
                    return {"status": "operator_replied", "target": approval["target"], "message": "已核实号主回复，跳过本条。"}
            else:
                await self.messages.send(approval["draft_id"], submission=submission)
        except ValueError as error:
            reason = public_error(error)
        except Exception:
            reason = "领域发送中断"
        outcome = await self.recover(request.id, approval, read=False)
        if reason and isinstance(outcome, dict) and outcome["status"] == "not_submitted":
            outcome["error"] = reason
        return outcome

    async def stop(self, identity, checkpoint):
        if not checkpoint.get("submission_started"):
            return {"status": "cancelled"}
        approval = {key: value for key, value in checkpoint.items() if key not in {"submission_started", "reason"}}
        result = await self.recover(identity, approval, read=True)
        if isinstance(result, ToolWait):
            raise ValueError("原提交仍待核对")
        return result


class ReviewedReply:
    def __init__(self, operations, step_id, approval):
        self.operations, self.step_id, self.approval = operations, step_id, approval

    def check(self, *, publishing=False):
        approval = self.approval
        if not self.operations.current(approval):
            raise ValueError("原范围或审批已失效")
        target = {key: approval[key] for key in ReplyTarget.model_fields}
        actual = self.operations.snapshot(target, approval["scope"], publishing=publishing)
        if any(value != approval[key] for key, value in actual.items()
               if not publishing or key not in {"status", "revision"}):
            raise ValueError("审批后的目标、正文或来源已变化")

    def admit(self, token, binding, payload, domain_attempt_id):
        self.check(publishing=True)
        if binding != self.approval["binding"] or payload != self.approval["payload"]:
            raise ValueError("实际发送目标或正文与审批不符")
        self.operations.store.db.execute(
            "INSERT INTO agent_reply_attempts(step_id,approval_hash,token,domain_attempt_id) VALUES(?,?,?,?)",
            (self.step_id, content_hash(self.approval), token, domain_attempt_id),
        )


def register_reply_operations(app, tasks, comments, messages, require_admin):
    operations = ReplyOperations(tasks, comments, messages)
    def named(goal, args):
        return str(args.selection_id) in goal

    tasks.register("inspect_reply_selection", Tool("查看号主在目标中提供的回复选择编号和条目范围。",
                   SelectionRef, operations.inspect, accepts_goal=named, responses_only=True))
    tasks.register("submit_selected_reply", Tool("仅提交号主选择集合中的一条评论或私信，审批显示对象和实际正文。",
                   SelectedReply, operations.run, operations.stop, operations.review, accepts_goal=named,
                   responses_only=True, checkpoint_submission=True))
    path = "/api/agent/reply-selections"
    deps = [Depends(require_admin)]

    @app.get(path, dependencies=deps)
    async def current_scope():
        account, character, version, _, _, epoch = tasks.scope()
        return {"account": account, "character": character, "version": version, "epoch": epoch}

    @app.post(path, dependencies=deps)
    async def select(body: ReplySelection):
        try:
            return operations.select(body)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post(path + "/{selection_id}/delegate", dependencies=deps)
    async def delegate(selection_id: UUID):
        identity = str(selection_id)
        row = tasks.store.one("SELECT items FROM agent_reply_selections WHERE id=? AND scope=?",
                              (identity, dump(tasks.scope())))
        if not row:
            raise HTTPException(409, "选择已撤回或账号、角色授权已变化")
        count = len(json.loads(row["items"]))
        goal = f"处理我选定的 {count} 条回复（选择编号 {identity}）。只处理所选来源，逐条展示对象和正文，等我批准。"
        try:
            task = tasks.create(identity, goal, source_key=["reply_selection", identity])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"task_id": task["id"]}

    @app.delete(path + "/{selection_id}", dependencies=deps)
    async def revoke(selection_id: UUID):
        deleted = tasks.store.execute("DELETE FROM agent_reply_selections WHERE id=? AND scope=?",
                                      (str(selection_id), dump(tasks.scope())))
        if not deleted.rowcount:
            raise HTTPException(404, "找不到这份选择")
        return {"revoked": True}
