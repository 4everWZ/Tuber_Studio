"""Validated operation image references. Source and recipient are chosen by the domain."""

import asyncio
import hashlib
import json

from .comment_capabilities import video_top_level_images
from .operation_continuations import content_hash
from .operation_images import image_permissions
from .operations.browsing.thread_context import COMMENT_ID, VIDEO_ID
from .workstation_images import gateway_base

PENDING_IMAGE_STATES = ("queued", "deciding", "submitting", "running")
TEXT_ONLY_IMAGE = {"text_only": True}


def bound_comment(row):
    parent = row.get("platform_comment_id") or row.get("comment_id")
    return (
        row.get("platform", "bilibili") == "bilibili"
        and VIDEO_ID.fullmatch(str(row.get("video_id")))
        and all(
            COMMENT_ID.fullmatch(str(value))
            for value in (parent, row.get("thread_id"), row.get("platform_user_id"))
        )
    )


def image_scope(channel, row):
    if channel == "comments":
        if not bound_comment(row):
            raise ValueError("配图回复需要原 BV、楼层、父评论及对方 UID")
        source = [
            "reply",
            row["video_id"],
            row["thread_id"],
            row.get("platform_comment_id") or row["comment_id"],
            row["platform_user_id"],
        ]
    elif channel == "browsing":
        target = row["target"]
        source = [
            "video",
            target["bvid"],
            str(target["oid"]),
            str(target["root"]),
            str(target["parent"]),
            str(target.get("recipient") or ""),
        ]
    elif channel == "messages":
        source = ["message", row["source_kind"], str(row["source_id"]), row["platform_user_id"]]
    elif channel == "dynamics":
        source = ["dynamic", str(row["id"])]
    else:
        raise ValueError("未知配图渠道")
    channels = {
        "comments": "comment",
        "browsing": "comment",
        "messages": "direct_message",
        "dynamics": "dynamic",
    }
    return {
        "channel": channels[channel],
        "account_id": row["account_id"],
        "character_id": row["character_id"],
        "source_id": json.dumps([row["character_version"], *source], separators=(",", ":")),
    }


def workstation_image_decisions(runtime):
    if not hasattr(runtime, "operation_images"):
        return False
    try:
        gateway_base(runtime.settings)
    except ValueError:
        return False
    return True


def can_attach_comment_image(channel, row):
    if channel != "browsing" or row.get("kind") != "top_level":
        return False
    target = row["target"]
    return bool(
        row.get("account_id") and VIDEO_ID.fullmatch(str(target.get("bvid")))
        and video_top_level_images(target.get("resource_type") or target.get("type"),
                                   target.get("root"), target.get("parent"))
    )


def queue_image(runtime, channel, row, context, text, *, comment_card=None, require_choice=False):
    if not row.get("account_id") or not workstation_image_decisions(runtime):
        return None
    if channel in {"comments", "browsing"} and not can_attach_comment_image(channel, row):
        return None
    scope = image_scope(channel, row)
    options = image_permissions(runtime.store)
    if not (any(options) or comment_card or require_choice):
        return None
    if callable(context):
        context = context()
    key = content_hash([scope, row["character_version"], text, context, options, comment_card])
    job = runtime.operation_images.decide(
        scope, request_key=key, context=context, draft=text, character_version=row["character_version"],
        comment_card=comment_card,
    )
    return {"job_id": job["job_id"]}


def decision(runtime, channel, row, ref):
    if ref == TEXT_ONLY_IMAGE:
        return None
    if not ref or "job_id" not in ref:
        return ref
    scope = image_scope(channel, row)
    try:
        job = runtime.operation_images.status(scope, ref["job_id"])
    except LookupError:
        return {"job_id": ref["job_id"], "status": "failed", "error": "配图任务与当前草稿来源不符"}
    if job["status"] == "text_only":
        return None
    if job["status"] == "comment_card":
        return {"comment_card_sha256": job["comment_card_sha256"]}
    if job["status"] == "gallery":
        try:
            catalog = runtime.operation_images.catalog
            current = (image_permissions(runtime.store)[0] and catalog
                       and catalog.read(job["asset_id"], row["character_id"])["asset"]["sha256"]) == job["asset_sha256"]
        except (LookupError, ValueError, OSError):
            current = False
        if not current:
            if row["status"] == "pending_review" and not row.get("attempted_at"):
                runtime.operation_images.reselect(scope, job["job_id"])
            return {"job_id": job["job_id"], "status": "awaiting_choice", "error": "图库配图已变更，等待重新选图。"}
        return {"asset_id": job["asset_id"], "sha256": job["asset_sha256"]}
    if job["status"] != "ready":
        return {"job_id": job["job_id"], "status": job["status"], "error": job.get("error")}
    if not image_permissions(runtime.store)[1] and row["status"] in {"pending_review", "approved", "publishing"}:
        return {"job_id": job["job_id"], "status": "awaiting_choice",
                "error": "生成图片的权限已关闭，可以重新选图。"}
    try:
        artifact = runtime.operation_images.read(scope, job["artifact_id"])
    except LookupError:
        return {"job_id": job["job_id"], "status": "awaiting_choice", "error": "生成的图片暂时读不到。"}
    if artifact["character_version"] != row["character_version"]:
        return {"job_id": job["job_id"], "status": "failed", "error": "工件绑定的角色版本不同，请重新选图"}
    return {"artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"]}


def image_choice(runtime, channel, row, raw, resolved):
    if raw == TEXT_ONLY_IMAGE:
        return "text_only"
    if resolved:
        if resolved.get("job_id"):
            return "awaiting_choice"
        return "image"
    if raw and "job_id" in raw:
        try:
            if runtime.operation_images.status(image_scope(channel, row), raw["job_id"])["status"] == "text_only":
                return "text_only"
        except LookupError:
            pass
    return None


def rebind_image(runtime, channel, row, text, *, retry=False):
    raw = row_ref(row)
    if not raw or "job_id" not in raw:
        if retry:
            raise ValueError("这份草稿没有可恢复的选图记录")
        return raw
    scope = image_scope(channel, row)
    material = runtime.operation_images.material(scope, raw["job_id"])
    result = queue_image(runtime, channel, row, material["context"], text, require_choice=True)
    if retry and result == raw:
        runtime.operation_images.reselect(scope, raw["job_id"])
    return result or raw


def image_decision_pending(runtime, channel, row, *, include_choice=True):
    ref = row_ref(row)
    if not ref or "job_id" not in ref:
        return False
    status = runtime.operation_images.status(image_scope(channel, row), ref["job_id"])["status"]
    if status in PENDING_IMAGE_STATES:
        return True
    return include_choice and "job_id" in (decision(runtime, channel, row, ref) or {})


async def wait_decision(runtime, channel, row, authorized):
    ref = row_ref(row)
    if not ref or "job_id" not in ref:
        return
    while authorized():
        if not image_decision_pending(runtime, channel, row, include_choice=False):
            return
        await asyncio.sleep(0.5)
    raise ValueError("等待配图期间原发送授权已停止")


def require_comment_attachment(channel, row, ref):
    if ref and ref != TEXT_ONLY_IMAGE and not can_attach_comment_image(channel, row):
        raise ValueError("仅视频一级评论可配图。已有附件可从草稿移除。")


def checked_image(catalog, ref, character, *, runtime=None, scope=None):
    if isinstance(ref, dict) and set(ref) == {"artifact_id", "sha256"} and runtime and scope:
        try:
            asset = runtime.operation_images.read(scope, ref["artifact_id"])
        except LookupError:
            raise ValueError("创作工件不属于当前草稿的来源与身份") from None
        if asset["sha256"] != ref["sha256"] or hashlib.sha256(asset["content"]).hexdigest() != ref["sha256"]:
            raise ValueError("创作工件内容已变化")
        return {
            "content": asset["content"],
            "mime_type": asset["mime_type"],
            "asset": ref | {"title": "创作配图"},
        }
    if not isinstance(ref, dict) or set(ref) != {"asset_id", "sha256"} or catalog is None:
        raise ValueError("配图须使用已登记的工件及内容指纹")
    image = catalog.read(ref["asset_id"], character)
    if image["asset"]["sha256"] != ref["sha256"]:
        raise ValueError("配图与审核的内容指纹不符")
    return image


def row_ref(row):
    target = row.get("target")
    ref = row.get("image_ref") or (target.get("image_ref") if isinstance(target, dict) else None)
    return json.loads(ref) if isinstance(ref, str) else ref


def image_payload(ref):
    if not ref:
        return {}
    if "job_id" in ref:
        raise ValueError(ref.get("error") or "配图待决定")
    key = "meme_id" if "asset_id" in ref else "artifact_id"
    return {key: ref.get("asset_id") or ref["artifact_id"], "asset_sha256": ref["sha256"]}


def comment_payload(row, runtime=None, channel="comments"):
    from .public_mentions import draft_mentions

    ref = row_ref(row)
    if runtime:
        ref = decision(runtime, channel, row, ref)
    mentions = draft_mentions(row).get("mentions")
    return {"text": row["text"]} | ({"mentions": mentions} if mentions else {}) | image_payload(ref)


def comment_image(catalog, payload, character, *, runtime=None, scope=None):
    if "meme_id" in payload:
        return checked_image(
            catalog, {"asset_id": payload["meme_id"], "sha256": payload["asset_sha256"]}, character
        )
    if "artifact_id" in payload:
        return checked_image(
            catalog,
            {"artifact_id": payload["artifact_id"], "sha256": payload["asset_sha256"]},
            character,
            runtime=runtime,
            scope=scope,
        )


def record_upload(store, channel, draft_id, ref, picture):
    store.execute(
        "INSERT INTO operation_image_uploads(channel,draft_id,asset_id,sha256,picture) VALUES(?,?,?,?,?)",
        (
            channel,
            str(draft_id),
            ref.get("asset_id") or ref["artifact_id"],
            ref["sha256"],
            json.dumps(picture),
        ),
    )


def uploads(store, channel, draft_id):
    return [
        row | {"picture": json.loads(row["picture"])}
        for row in store.all(
            "SELECT asset_id,sha256,picture FROM operation_image_uploads WHERE channel=? AND draft_id=? ORDER BY id",
            (channel, str(draft_id)),
        )
    ]
