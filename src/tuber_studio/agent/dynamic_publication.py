"""An owner-approved, single-attempt handoff to the dynamics publisher."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import Field

from ..models import StrictModel
from ..operation_continuations import content_hash
from .task_runtime import Tool, ToolWait


class ExistingDraft(StrictModel):
    post_id: int = Field(strict=True, ge=1, le=9223372036854775807)


_DRAFT = re.compile(
    r"(?:动态草稿|动态稿|草稿|动态(?=\s*(?:ID|编号|[#＃]))|post_id|draft\s+id)"
    r"\s*(?:ID|编号)?\s*(?:为|是)?\s*[:：=#＃]?\s*([1-9][0-9]*)(?![.\w-])", re.I | re.ASCII,
)


def named_in_goal(goal, args):
    return set(_DRAFT.findall(goal)) == {str(args.post_id)}


def pending(reason):
    return ToolWait({"submission_started": True, "status": "needs_reconciliation", "reason": reason}, 60)


@dataclass(frozen=True)
class ScopedSubmission:
    step_id: str
    binding: dict
    payload: dict
    check: Callable


def register_dynamic_publication(tasks, dynamics):
    def bound(row):
        account, character, version, *_ = tasks.scope()
        if (not account or (row["account_id"], row["character_id"], row["character_version"]) !=
                (account, character, version)):
            raise ValueError("无法确认原账号和角色版本")

    def snapshot(row):
        bound(row)
        if (row["status"] not in {"pending_review", "approved"} or row["attempted_at"] is not None
                or row.get("continuation") or not row["sources"] or not dynamics._valid_sources(row)):
            raise ValueError("草稿已有尝试、来源失效或不在可审核状态")
        sources = []
        for source in row["sources"]:
            if (source["type"] not in {"published_reply", "public_incident"} or
                    not (source.get("source_url") or source.get("public_sources"))):
                raise ValueError("这条限定发布路径只接收带公开出处的草稿")
            sources.append({key: source[key] for key in ("id", "type", "text", "source_url", "public_sources", "receipt_id")
                            if key in source})
        character = dynamics._character()
        binding = dynamics._publication_binding(row, row["account_id"], character)
        payload = dynamics._outgoing(row, character)
        attachment = " · 含配图" if len(payload) > 1 else ""
        return {"target": f"B站 UID {row['account_id']} 的公开动态 · 草稿 #{row['id']} · "
                          f"{character['name']} v{row['character_version']}{attachment}",
                "post_id": row["id"], "revision": row["updated_at"], "status": row["status"],
                "scope": tasks.scope(), "binding": binding, "sources": sources,
                "policy_epoch": dynamics.operations.policy()["epoch"],
                "source_version": content_hash(row["sources"]), "payload": payload,
                "draft_text": row["content"], "text": payload["text"]}

    def review(args):
        return snapshot(dynamics._post(args.post_id))

    async def inspect(request, args):
        approval = review(args)
        return {key: approval[key] for key in ("post_id", "revision", "binding", "text", "sources", "target", "status")}

    async def reconcile(step_id, approval, *, allow_read):
        post_id = approval["post_id"]
        if tasks.scope() != approval["scope"]:
            return pending("原授权范围已变化")
        row = dynamics.store.one("SELECT send_payload FROM dynamic_posts WHERE id=?", (post_id,))
        saved = json.loads(row["send_payload"] or "null") if row else None
        if (not saved or saved.get("agent_step") != step_id or saved.get("binding") != approval["binding"]
                or {key: value for key, value in saved["payload"].items() if key != "prepared_images"} != approval["payload"]):
            return pending("没有与本次审批唯一匹配的原提交")
        token = saved["token"]
        attempt = dynamics.operations.get_attempt(token)
        if not attempt or attempt["binding"] != approval["binding"]:
            return pending("原发送账本绑定无法核实")
        state = attempt["resolved_status"]
        post = dynamics._post(post_id)
        bound(post)
        receipt = attempt["resolution_receipt"] or attempt["receipt"]
        if state == "published" and receipt and post["status"] == "published" and receipt == post["receipt"]:
            return {"status": "published", "post_id": post_id, "write_token": token, "receipt": receipt,
                    "target": approval["target"], "text": approval["text"]}
        if state in {"rejected", "cancelled"}:
            return {"error": "原提交已被拒绝或取消，未重新发送。", "status": state, "write_token": token}
        coordinator = getattr(dynamics.runtime, "continuations", None)
        original = coordinator.find("dynamics", post_id) if coordinator else None
        if (allow_read and original and original["state"] == "needs_reconciliation"
                and original["binding"] == saved["binding"] and original["feedback"].get("write_token") == token
                and original["feedback"].get("sent_payload") == saved["payload"]):
            def guard(original_row, original_attempt):
                if (tasks.scope() != approval["scope"] or original_attempt["token"] != token
                        or original_row["binding"] != approval["binding"]
                        or dynamics.comments.display_account_id() != approval["binding"]["account_id"]
                        or dynamics.store.one("SELECT send_payload FROM dynamic_posts WHERE id=?", (post_id,)) != row):
                    raise ValueError("对账期间原提交或授权已变化")

            await dynamics._reconcile(post_id, guard=guard)
            return await reconcile(step_id, approval, allow_read=False)
        return pending("原回执尚未核实，不再发送")

    async def publish(request, args):
        approval = request.approval
        if not approval or approval["post_id"] != args.post_id or request.scope != approval["scope"]:
            return pending("缺少本次目标的明确审批")
        try:
            if not request.checkpoint:
                def check(row, *, publishing=False):
                    bound(row)
                    policy = dynamics.operations.policy()
                    if (tasks.scope() != request.scope or policy["emergency"] or policy["epoch"] != approval["policy_epoch"]
                            or dynamics.comments.display_account_id() != approval["binding"]["account_id"]
                            or row.get("continuation")
                            or row["id"] != args.post_id or row["content"] != approval["draft_text"]
                            or row["attempted_at"] is not None or content_hash(row["sources"]) != approval["source_version"]
                            or (row["status"] != "publishing" if publishing else
                                row["status"] != approval["status"] or row["updated_at"] != approval["revision"])):
                        raise ValueError("本次审批已失效")

                if review(args) != approval:
                    return pending("草稿版本、账号、角色或公开来源已变化，原审批无效")
                if str(await dynamics.comments.account_id()) != approval["binding"]["account_id"]:
                    return pending("无法核验原账号")
                if refresh := getattr(dynamics.runtime, "refresh_identity", None):
                    identity = await refresh(dynamics._character(), approval["binding"]["account_id"])
                    original = json.loads(dynamics._post(args.post_id)["generation_identity"] or "{}")
                    if not original.get("identity_version") or original["identity_version"] != identity["identity_version"]:
                        return pending("生成时的账号身份已变化，须重新审核草稿")
                submission = ScopedSubmission(request.id, approval["binding"], approval["payload"], check)
                try:
                    await dynamics.publish(args.post_id, expected_text=approval["draft_text"], submission=submission)
                except Exception:
                    return await reconcile(request.id, approval, allow_read=False)
            return await reconcile(request.id, approval, allow_read=bool(request.checkpoint))
        except Exception:
            return pending("无法安全核对原提交或授权")

    async def stop(identity, checkpoint):
        if not checkpoint.get("submission_started"):
            return {"status": "cancelled"}
        result = await reconcile(identity, checkpoint, allow_read=True)
        if isinstance(result, ToolWait):
            raise ValueError("原提交尚待核对")
        return result

    tasks.register("inspect_dynamic_draft", Tool("只读号主明确指定 ID 的现有动态草稿、目标及公开出处。",
                   ExistingDraft, inspect, responses_only=True, accepts_goal=named_in_goal))
    tasks.register("publish_dynamic_draft", Tool("提议发布号主明确指定 ID 的现有动态草稿，须审批实际正文和账号。",
                   ExistingDraft, publish, stop, review, responses_only=True, accepts_goal=named_in_goal,
                   checkpoint_submission=True))
