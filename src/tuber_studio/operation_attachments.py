"""Validated operation image references. Source and recipient are chosen by the domain."""

import asyncio
import hashlib
import json

from .operation_continuations import content_hash
from .operations.browsing.thread_context import COMMENT_ID, VIDEO_ID
from .workstation_images import gateway_base


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


def queue_image(runtime, channel, row, context, text):
    if not row.get("account_id") or not hasattr(runtime, "operation_images"):
        return None
    try:
        gateway_base(runtime.settings)
    except ValueError:
        return None
    scope = image_scope(channel, row)
    if callable(context):
        context = context()
    key = content_hash([scope, row["character_version"], text, context])
    job = runtime.operation_images.decide(
        scope, request_key=key, context=context, draft=text, character_version=row["character_version"]
    )
    return {"job_id": job["job_id"]}


def decision(runtime, channel, row, ref):
    if not ref or "job_id" not in ref:
        return ref
    scope = image_scope(channel, row)
    try:
        job = runtime.operation_images.status(scope, ref["job_id"])
    except LookupError:
        return {"job_id": ref["job_id"], "status": "failed", "error": "配图任务与当前草稿来源不符"}
    if job["status"] == "text_only":
        return None
    if job["status"] != "ready":
        return {"job_id": job["job_id"], "status": job["status"], "error": job.get("error")}
    artifact = runtime.operation_images.read(scope, job["artifact_id"])
    if artifact["character_version"] != row["character_version"]:
        return {"job_id": job["job_id"], "status": "failed", "error": "工件绑定的角色版本不同，请重新选图"}
    return {"artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"]}


async def wait_decision(runtime, channel, row, authorized):
    ref = row_ref(row)
    if not ref or "job_id" not in ref:
        return
    scope = image_scope(channel, row)
    while authorized():
        status = runtime.operation_images.status(scope, ref["job_id"])["status"]
        if status in {"text_only", "ready", "failed"}:
            return
        await asyncio.sleep(0.5)
    raise ValueError("等待配图期间原发送授权已停止")


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
        raise ValueError("配图决定尚未完成或失败，可在草稿中查看或移除")
    key = "meme_id" if "asset_id" in ref else "artifact_id"
    return {key: ref.get("asset_id") or ref["artifact_id"], "asset_sha256": ref["sha256"]}


def comment_payload(row, runtime=None, channel="comments"):
    ref = row_ref(row)
    if runtime:
        ref = decision(runtime, channel, row, ref)
    return {"text": row["text"]} | image_payload(ref)


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
