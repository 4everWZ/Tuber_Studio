"""An owner-approved, single-attempt handoff to the dynamics publisher."""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import Field, PositiveInt

from ....core.errors import PlatformRejected, SendLimitReached
from ....core.identity import runtime_identity
from ....core.models import StrictModel, content_hash
from ....infra.platforms.bilibili.client import BilibiliAccountExpired, _positive_id
from ....operations.facade import (
    control_policy,
    dynamic_character,
    dynamic_outgoing_payload,
    dynamic_publication_binding,
    dynamic_publication_images,
    dynamic_valid_sources,
    get_attempt,
    get_dynamic_post,
    reconcile_dynamic_post,
)
from ...tasks.runtime import ReviewBlocked, Tool, ToolWait

PUBLICATION_TOOL = "publish_dynamic_batch"
CONFIRM_SECONDS = 24 * 3600


class ExistingDraft(StrictModel):
    post_id: int = Field(strict=True, ge=1, le=9223372036854775807)


class DraftSelection(StrictModel):
    post_ids: list[PositiveInt] = Field(min_length=1)


_DRAFT = re.compile(
    r"(?:动态草稿|动态稿|草稿|动态(?=\s*(?:ID|编号|[#＃]))|post_id|draft\s+id)"
    r"\s*(?:ID|编号)?\s*(?:为|是)?\s*[:：=#＃]?\s*([1-9][0-9]*)(?![.\w-])", re.I | re.ASCII,
)


def requested_draft_ids(goal):
    return {int(value) for value in _DRAFT.findall(goal) if len(value) <= 19}


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

    def snapshot(row, *, allow_manual=False):
        bound(row)
        if (row["status"] not in {"pending_review", "approved"} or row["attempted_at"] is not None
                or row.get("continuation") or not dynamic_valid_sources(dynamics, row)):
            raise ValueError("草稿已有尝试、来源失效或不在可审核状态")
        decision = dynamics.store.one("SELECT sources FROM dynamic_decisions WHERE id=? AND post_id=? AND action='post'",
                                      (row["decision_id"], row["id"]))
        if decision is None or json.loads(decision["sources"]) != row["sources"]:
            raise ValueError("无法核对草稿的原始公开来源")
        sources = []
        if row.get("pending_mentions"):
            raise ValueError("请先选择这条动态要 @ 的账号")
        for source in row["sources"]:
            kind = source["type"]
            if allow_manual and row["manual"] and kind == "operator_topic":
                continue
            if kind not in {"published_reply", "public_incident", "watched_video", "public_memory", "public_comment", "own_reflection"} or (
                    kind not in {"public_memory", "own_reflection"} and not (source.get("source_url") or source.get("public_sources"))):
                raise ValueError("这条限定发布路径只能使用已核实的公开出处")
            item = {key: source[key] for key in ("id", "type", "text", "source_url", "public_sources", "receipt_id")
                    if key in source}
            if kind == "public_memory":
                item["fictional"] = source["fictional"]
            sources.append(item)
        character = dynamic_character(dynamics)
        binding = dynamic_publication_binding(dynamics, row, row["account_id"], character)
        payload = dynamic_outgoing_payload(dynamics, row, character)
        attachment = " · 含配图" if "asset_sha256" in payload or "comment_card_sha256" in payload else ""
        return {"target": f"B站 UID {row['account_id']} 的公开动态 · 草稿 #{row['id']} · "
                          f"{character['name']} v{row['character_version']}{attachment}",
                "post_id": row["id"], "revision": row["updated_at"], "status": row["status"],
                "scope": tasks.scope(), "binding": binding, "sources": sources,
                "policy_epoch": control_policy(dynamics)["epoch"],
                "source_version": content_hash(row["sources"]), "payload": payload,
                "draft_text": row["content"], "text": payload["text"]}

    def review(args):
        return snapshot(get_dynamic_post(dynamics, args.post_id))

    async def inspect(request, args):
        approval = review(args)
        return {key: approval[key] for key in ("post_id", "revision", "binding", "text", "sources", "target", "status")}

    async def reconcile(step_id, approval, *, allow_read):
        post_id = approval["post_id"]
        if tasks.scope() != approval["scope"]:
            return pending("原授权范围已变化")
        duplicate = await reused_delivery(approval, allow_read=allow_read)
        if duplicate is not None:
            return duplicate
        row = dynamics.store.one("SELECT send_payload FROM dynamic_posts WHERE id=?", (post_id,))
        saved = json.loads(row["send_payload"] or "null") if row else None
        if (not saved or saved.get("agent_step") != step_id or saved.get("binding") != approval["binding"]
                or {key: value for key, value in saved["payload"].items() if key != "prepared_images"} != approval["payload"]):
            return pending("没有与本次审批唯一匹配的原提交")
        token = saved["token"]
        attempt = get_attempt(dynamics, token)
        if not attempt or attempt["binding"] != approval["binding"]:
            return pending("原发送账本绑定无法核实")
        state = attempt["resolved_status"]
        post = get_dynamic_post(dynamics, post_id)
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
                        or dynamics.account.display_account_id() != approval["binding"]["account_id"]
                        or dynamics.store.one("SELECT send_payload FROM dynamic_posts WHERE id=?", (post_id,)) != row):
                    raise ValueError("对账期间原提交或授权已变化")

            await reconcile_dynamic_post(dynamics, post_id, guard=guard)
            return await reconcile(step_id, approval, allow_read=False)
        return pending("原回执尚未核实，不再发送")

    async def reused_delivery(approval, *, allow_read):
        post = get_dynamic_post(dynamics, approval["post_id"])
        if not post.get("duplicate_of"):
            return None
        bound(post)
        account = approval["binding"]["account_id"]
        if tasks.scope() != approval["scope"] or dynamics.account.display_account_id() != account:
            return pending("原授权范围已变化")
        original_id = post["duplicate_of"]
        if post["status"] != "published" and allow_read:
            await reconcile_dynamic_post(dynamics, original_id)
            post = get_dynamic_post(dynamics, post["id"])
        if post["status"] == "published" and post["receipt"]:
            return {"status": "published", "post_id": post["id"], "receipt": post["receipt"],
                    "duplicate_of": original_id, "target": approval["target"], "text": approval["text"]}
        return pending("相同内容的原提交待核对")

    def approval_post(row):
        item = snapshot(row, allow_manual=True)
        payload = item["payload"]
        digest = payload.get("comment_card_sha256") or payload.get("asset_sha256")
        return item | {"image_hashes": payload.get("comment_card_page_sha256") or ([digest] if digest else [])}

    def review_batch(args):
        scope = tasks.scope()
        posts, excluded = [], []
        for key in args.post_ids:
            item = {"post_id": key, "text": ""}
            try:
                row = get_dynamic_post(dynamics, key)
                bound(row)
                item["text"] = row["content"]
                posts.append(approval_post(row))
            except ValueError as error:
                excluded.append(batch_item_result(item, "not_sent", str(error)))
        if not posts:
            raise ReviewBlocked({"status": "failed", "items": excluded})
        actor = getattr(dynamics.runtime, "account", None)
        profile = (actor.status().get("profile") or {}) if actor else {}
        name = profile.get("name") if str(profile.get("uid")) == scope[0] else None
        return {"kind": "dynamic_publication", "account_id": scope[0], "account_name": name,
                "target": f"B 站公开动态 · {name + ' · ' if name else ''}UID {scope[0]}",
                "expires_at": time.time() + CONFIRM_SECONDS, "posts": posts, "excluded": excluded, "blocked": excluded}

    def select_batch(approval, selected):
        available = {item["post_id"]: item for item in approval["posts"]}
        if not selected or len(selected) != len(set(selected)) or not set(selected) <= available.keys():
            raise ValueError("请从本次确认窗选择要发布的草稿。")
        still_valid, changed, blocked = [], [], list(approval.get("blocked", []))
        for key in selected:
            prior = available[key]
            try:
                current = approval_post(get_dynamic_post(dynamics, key))
            except ValueError as error:
                blocked.append(batch_item_result(prior, "not_sent", str(error)))
                continue
            if current != prior:
                changed.append(current)
            else:
                still_valid.append(prior)
        posts = sorted([*still_valid, *changed], key=lambda item: item["post_id"])
        confirmed = [item["post_id"] for item in still_valid]
        if changed:
            approval = approval | {"posts": posts, "carried_ids": confirmed, "blocked": blocked,
                                   "expires_at": time.time() + CONFIRM_SECONDS}
            return approval, True
        return approval | {"posts": posts, "selected_ids": confirmed, "blocked": blocked}, False

    def current_identity(row, character, account):
        if not getattr(dynamics.runtime, "refresh_identity", None):
            return True
        original = json.loads(row.get("generation_identity") or "{}")
        return original.get("identity_version") == runtime_identity(dynamics.runtime, character, account)["identity_version"]

    def batch_item_result(item, status, reason=None, receipt=None, write_token=None):
        value = {"post_id": item["post_id"], "text": item["text"], "status": status}
        if write_token:
            value["write_token"] = write_token
        if reason:
            value["reason"] = reason
        if receipt:
            try:
                number = _positive_id(receipt.get("dynamic_id") or receipt.get("dynamic_id_str") or receipt.get("id_str"))
            except ValueError:
                return value | {"status": "unconfirmed", "reason": "原回执没有可核对的动态编号"}
            value["number"] = number
            if receipt.get("url") == f"https://t.bilibili.com/{number}":
                value["href"] = receipt["url"]
        return value

    async def send_batch_item(request, item):
        post_id = item["post_id"]
        identity = str(uuid5(UUID(request.id), str(post_id)))
        row = dynamics.store.one("SELECT status,error,send_payload FROM dynamic_posts WHERE id=?", (post_id,))
        saved = json.loads(row["send_payload"] or "null") if row else None
        reason = None
        if not saved and row and row["status"] in {"pending_review", "approved"}:
            try:
                def check(fresh, *, publishing=False):
                    bound(fresh)
                    character = dynamic_character(dynamics)
                    policy = control_policy(dynamics)
                    if (request.scope != tasks.scope() or policy["emergency"]
                            or fresh["attempted_at"] is not None or fresh.get("continuation")
                            or fresh["content"] != item["draft_text"]
                            or content_hash(fresh["sources"]) != item["source_version"]
                            or dynamic_outgoing_payload(dynamics, fresh, character) != item["payload"]
                            or not current_identity(fresh, character, item["binding"]["account_id"])
                            or (fresh["status"] != "publishing" if publishing else
                                (fresh["status"], fresh["updated_at"]) != (item["status"], item["revision"]))):
                        raise ValueError("草稿、账号或发布授权已变化，请重新核对")

                submission = ScopedSubmission(identity, item["binding"], item["payload"], check)
                await dynamics.publish(post_id, expected_text=item["draft_text"], submission=submission)
            except PlatformRejected as error:
                reason = str(error)
            except SendLimitReached:
                raise
            except ValueError as error:
                reason = str(error)
            except Exception:
                reason = "发布服务暂时不可用"
            fresh = dynamics.store.one("SELECT status,error,send_payload FROM dynamic_posts WHERE id=?", (post_id,))
            saved = json.loads(fresh["send_payload"] or "null") if fresh else None
            if not saved and fresh and fresh["status"] == "publishing":
                return batch_item_result(item, "unconfirmed", "正等待原提交记录")
        if not saved or saved.get("agent_step") != identity:
            prior = (request.checkpoint or {}).get("items", {}).get(str(post_id))
            reused = await reused_delivery(item, allow_read=bool(prior and prior["status"] == "unconfirmed"))
            if isinstance(reused, ToolWait):
                return batch_item_result(item, "unconfirmed", reused.checkpoint["reason"])
            if reused is not None:
                return batch_item_result(item, "published", "已沿用相同动态的原回执", receipt=reused["receipt"]) | {
                    "duplicate_of": reused["duplicate_of"]
                }
            return batch_item_result(item, "not_sent", reason or "草稿状态已变化，这次没有提交")
        # The first observation after a send reports uncertainty before doing
        # any additional platform reads. Later passes only reconcile this ID.
        prior = (request.checkpoint or {}).get("items", {}).get(str(post_id))
        try:
            result = await reconcile(identity, item, allow_read=bool(prior and prior["status"] == "unconfirmed"))
        except Exception:
            return batch_item_result(item, "unconfirmed", "暂时无法核对原提交")
        if isinstance(result, ToolWait):
            return batch_item_result(item, "unconfirmed", result.checkpoint["reason"], write_token=saved["token"])
        if result["status"] == "published":
            return batch_item_result(item, "published", receipt=result["receipt"], write_token=saved["token"])
        return batch_item_result(item, "not_sent", reason or (row or {}).get("error") or result["error"],
                                 write_token=saved["token"])

    async def publish_batch(request, args):
        approval = request.approval
        checkpoint = request.checkpoint or {}
        items = dict(checkpoint.get("items", {}))
        if not approval["posts"]:
            return {"status": "failed", "items": approval["blocked"]}
        selected = set(approval["selected_ids"])
        posts = [item for item in approval["posts"] if item["post_id"] in selected]
        async def reconcile_pending():
            for item in posts:
                key = str(item["post_id"])
                if key in items and items[key]["status"] == "unconfirmed":
                    items[key] = await send_batch_item(request, item)

        next_item = next((item for item in posts if str(item["post_id"]) not in items), None)
        if next_item:
            try:
                account = await dynamics.account.account_id() if dynamics.policy()["credentials_configured"] else None
            except BilibiliAccountExpired:
                account = None
            except ValueError:
                return ToolWait(checkpoint | {"items": items, "safe_to_pause": not reconciliation_pending(items),
                                              "message": "暂时无法核验发布账号，正在等待。"}, 30)
            if account is None:
                return ToolWait(checkpoint | {"items": items, "safe_to_pause": not reconciliation_pending(items),
                                "message": "发布前需要登录 B 站。",
                                "interaction": {"kind": "login", "target": approval["target"], "href": "#accounts"}}, 10)
            if str(account) != approval["account_id"]:
                return ToolWait(checkpoint | {"items": items, "message": "请登录原确认窗口中的 B 站账号。",
                                "interaction": {"kind": "login", "target": approval["target"], "href": "#accounts"}}, 10)
            try:
                items[str(next_item["post_id"])] = await send_batch_item(request, next_item)
            except SendLimitReached as limit:
                await reconcile_pending()
                delay = max(1, limit.next_at - time.time())
                if reconciliation_pending(items):
                    delay = min(delay, 30)
                wait = batch_wait(items, len(posts), delay=delay, blocked=approval["blocked"])
                return ToolWait(wait.checkpoint | {"message": wait.checkpoint["message"] + str(limit) + "，额度恢复后继续。"},
                                wait.delay)
            if len(items) < len(posts) or reconciliation_pending(items):
                return batch_wait(items, len(posts), blocked=approval["blocked"])
        else:
            await reconcile_pending()
            if reconciliation_pending(items):
                return batch_wait(items, len(posts), delay=30, blocked=approval["blocked"])
        return {"status": "settled", "items": [*items.values(), *approval["blocked"]]}

    def batch_wait(items, count, delay=0, *, blocked=()):
        uncertain = reconciliation_pending(items)
        progress = publication_progress([*items.values(), *blocked], count + len(blocked))
        return ToolWait({"items": items, "message": progress, "safe_to_pause": not uncertain,
                         **({"status": "needs_reconciliation", "reason": "正在核对原平台提交"} if uncertain else {})},
                        delay or (3 if uncertain and len(items) == count else 0))

    async def stop_batch(identity, checkpoint):
        rows = checkpoint.get("items", {})
        results = []
        for item in checkpoint.get("posts", []):
            key = str(item["post_id"])
            if item["post_id"] not in checkpoint.get("selected_ids", []):
                continue
            if key in rows and rows[key]["status"] != "unconfirmed":
                results.append(rows[key])
                continue
            step_id = str(uuid5(UUID(identity), key))
            row = dynamics.store.one("SELECT send_payload FROM dynamic_posts WHERE id=?", (item["post_id"],))
            saved = json.loads(row["send_payload"] or "null") if row else None
            if saved and saved.get("agent_step") == step_id:
                result = await reconcile(step_id, item, allow_read=True)
                if isinstance(result, ToolWait):
                    raise ValueError("原提交尚待核对")
                results.append(batch_item_result(item, result["status"] if result["status"] == "published" else "not_sent",
                                                 result.get("error"), result.get("receipt")))
            else:
                results.append(batch_item_result(item, "not_sent", "已取消"))
        return {"status": "cancelled", "items": results}

    async def publish(request, args):
        approval = request.approval
        if not approval or approval["post_id"] != args.post_id or request.scope != approval["scope"]:
            return pending("缺少本次目标的明确审批")
        try:
            if not request.checkpoint:
                def check(row, *, publishing=False):
                    bound(row)
                    policy = control_policy(dynamics)
                    if (tasks.scope() != request.scope or policy["emergency"]
                            or dynamics.account.display_account_id() != approval["binding"]["account_id"]
                            or row.get("continuation")
                            or row["id"] != args.post_id or row["content"] != approval["draft_text"]
                            or not current_identity(row, dynamic_character(dynamics), approval["binding"]["account_id"])
                            or row["attempted_at"] is not None or content_hash(row["sources"]) != approval["source_version"]
                            or (row["status"] != "publishing" if publishing else
                                row["status"] != approval["status"] or row["updated_at"] != approval["revision"])):
                        raise ValueError("本次审批已失效")

                def _comparable(item):
                    return {key: value for key, value in item.items() if key != "policy_epoch"}

                if _comparable(review(args)) != _comparable(approval):
                    return pending("草稿版本、账号、角色或公开来源已变化，原审批无效")
                if str(await dynamics.account.account_id()) != approval["binding"]["account_id"]:
                    return pending("无法核验原账号")
                if refresh := getattr(dynamics.runtime, "refresh_identity", None):
                    identity = await refresh(dynamic_character(dynamics), approval["binding"]["account_id"])
                    original = json.loads(get_dynamic_post(dynamics, args.post_id)["generation_identity"] or "{}")
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

    tasks.register("inspect_dynamic_draft", Tool("只读指定 ID 的现有动态草稿、目标及公开出处。",
                   ExistingDraft, inspect, responses_only=True, accepts_goal=named_in_goal))
    tasks.register("publish_dynamic_draft", Tool("提议发布指定 ID 的现有动态草稿，须审批实际正文和账号。",
                   ExistingDraft, publish, stop, review, responses_only=True, accepts_goal=named_in_goal,
                   checkpoint_submission=True))
    tasks.register(PUBLICATION_TOOL, Tool("从已绑定的草稿快照逐条发布",
                   DraftSelection, publish_batch, stop_batch, review_batch, checkpoint_submission=True,
                   select_approval=select_batch, fixed_only=True))


def publication_progress(items, total):
    items = tuple(items)
    counts = {status: sum(item["status"] == status for item in items)
              for status in ("published", "not_sent", "unconfirmed")}
    labels = {"published": "已发", "not_sent": "没发", "unconfirmed": "待核对"}
    parts = [f"{label} {counts[status]} 条" for status, label in labels.items() if counts[status]]
    waiting = total - sum(counts.values())
    if waiting:
        parts.append(f"{waiting} 条尚未提交")
    return "，".join(parts) + "。" if parts else "还没有发布。"


def reconciliation_pending(items):
    return any(item["status"] == "unconfirmed" for item in items.values())


def approval_image(dynamics, task, step_id, post_id, index):
    step = task["steps"][-1] if task["steps"] else {}
    approval = step.get("approval") or {}
    if (task["status"] != "awaiting_approval" or step.get("id") != step_id
            or step["revision"] != task["revision"] or approval.get("kind") != "dynamic_publication"):
        raise LookupError(post_id)
    item = next((item for item in approval["posts"] if item["post_id"] == post_id), None)
    if item is None or not 0 <= index < len(item["image_hashes"]):
        raise LookupError(post_id)
    row = get_dynamic_post(dynamics, post_id)
    if [row["account_id"], row["character_id"], row["character_version"]] != task["scope"][:3]:
        raise LookupError(post_id)
    images = dynamic_publication_images(dynamics, row)
    if index >= len(images) or hashlib.sha256(images[index]["content"]).hexdigest() != item["image_hashes"][index]:
        raise LookupError(post_id)
    return images[index]
