"""Inbox comment/mention/own-content processing with atomic OperationClaims preemption."""

import asyncio
import json
import time
from contextlib import ExitStack

from ...core.errors import SendLimitReached
from ...core.models import content_hash
from ...core.store import dump
from ...core.turns import operation_lane, operation_ready_at
from ...infra.inference.providers import public_error
from ...infra.platforms.bilibili.client import BilibiliAccountExpired, PlatformReadError
from ..channels.comments.materials import prepare_comment_materials
from ..channels.comments.scanner import category, low_context_skip_reason
from ..channels.comments.timeline import (
    comment_source,
    comment_thread_context,
    confirmed_bot_comment_ids,
    observe_thread,
    operator_reply,
    read_scoped_thread,
    resource_key,
    source_discussion,
)
from ..channels.direct_messages.outreach import handoff_comment
from ..kernel.control import canonical_intent
from ..kernel.models import ResponseDecision
from ..kernel.quotas import DailyReplyLimitReached, day_window
from ..kernel.read_gate import PIPELINE_READ
from ..kernel.repository import eligible_event_heads, event_scope


def _claims_manager(browsing):
    ops = getattr(browsing, "operations", None)
    return getattr(ops, "claims", None) if ops is not None else None


def _resource_ids_for_event(event, thread=None) -> tuple[str, list[str]]:
    target = (thread or {}).get("target") or {}
    resource_type = str(
        (thread or {}).get("resource_type")
        or target.get("resource_type")
        or event.get("resource_type")
        or ("video" if (event.get("bvid") or target.get("bvid")) else "dynamic")
    )
    candidates = [
        event.get("bvid"),
        event.get("oid"),
        target.get("bvid"),
        target.get("oid"),
    ]
    ids = list(dict.fromkeys(str(v) for v in candidates if v not in (None, "")))
    return resource_type, ids


def _acquire_inbox_claims(browsing, row, event, *, thread=None):
    """Acquire atomic Inbox floor leases + comment claims (preempting any uncommitted Discovery lease)."""
    claims = _claims_manager(browsing)
    if claims is None:
        return []
    account_id = str(row["account_id"])
    character_id = str(row["character_id"])
    target = (thread or {}).get("target") or {}
    source = (thread or {}).get("source") or {}
    comment_id = str(source.get("comment_id") or event.get("comment_id") or target.get("parent") or "")
    root_id = str(target.get("root") or event.get("root") or comment_id)
    if root_id == "0":
        root_id = comment_id
    if not comment_id or not root_id:
        return []
    resource_type, resource_ids = _resource_ids_for_event(event, thread)
    handles = []
    for rid in resource_ids:
        floor = claims.acquire_floor_lease(
            account_id=account_id,
            character_id=character_id,
            resource_type=resource_type,
            resource_id=rid,
            root_id=root_id,
            owner_pipeline="inbox",
            ttl_seconds=240.0,
        )
        if floor["acquired"]:
            handles.append((claims, floor["claim_key"], floor["lease_token"]))
        comment = claims.claim_comment(
            account_id=account_id,
            character_id=character_id,
            resource_type=resource_type,
            resource_id=rid,
            root_id=root_id,
            target_comment_id=comment_id,
            owner_pipeline="inbox",
            ttl_seconds=180.0,
        )
        if comment["acquired"]:
            handles.append((claims, comment["claim_key"], comment["lease_token"]))
        elif floor["acquired"] and comment.get("reason") != "inbox_floor_active":
            _release_inbox_claims(handles)
            return False
    return handles


def _release_inbox_claims(handles) -> None:
    if not handles or handles is False:
        return
    for claims, claim_key, lease_token in reversed(handles):
        claims.release(claim_key, lease_token=lease_token)


def _has_inflight_evidence(browsing, bvid: str) -> bool:
    evidence = getattr(browsing, "evidence", None)
    if evidence is None:
        return False
    has_inflight = getattr(evidence, "has_inflight", None)
    if callable(has_inflight):
        return bool(has_inflight(bvid))
    pending = getattr(evidence, "pending", None)
    if not isinstance(pending, dict):
        return False
    return any(
        isinstance(key, tuple)
        and len(key) >= 2
        and isinstance(key[1], tuple)
        and len(key[1]) >= 2
        and key[1][1] == bvid
        for key in pending
    )


async def reuse_or_prepare_light_evidence(
    browsing,
    row,
    bvid: str,
    *,
    wait_inflight_only: bool = True,
):
    """Reuse watched material, valid cached material, or same-BV single-flight light evidence without `start_watch`."""
    if not bvid:
        return None
    saved = browsing.repo.watch_material(row, bvid)
    if saved is not None:
        return await browsing.evidence._store(saved)
    evidence = getattr(browsing, "evidence", None)
    if evidence is None:
        return None
    languages = row["config"].get("subtitle_languages")
    cached = evidence.cached_material(bvid, languages=languages)
    if cached is not None:
        return cached
    if not wait_inflight_only or _has_inflight_evidence(browsing, bvid):
        try:
            return await evidence.prepare_light(bvid, row["config"], task=row.get("id"))
        except Exception:
            return None
    return None


async def run_interactions(browsing, row, metrics):
    """Collect own-content comments and notification feeds under `PIPELINE_READ='inbox'`."""
    token = PIPELINE_READ.set("inbox")
    try:
        public_enabled = row["config"]["reply_enabled"] or row["config"].get("comment_like_enabled")
        if hasattr(browsing.adapter, "own_uploads") and public_enabled:
            try:
                await browsing._collect_own_comments(row, metrics)
            except BilibiliAccountExpired:
                raise
            except (ValueError, TimeoutError) as error:
                metrics["own_upload_error"] = public_error(error)
                metrics["failures"] += 1
        if hasattr(browsing.adapter, "own_dynamics") and public_enabled:
            try:
                await collect_own_dynamics(browsing, row, metrics)
            except BilibiliAccountExpired:
                raise
            except (ValueError, TimeoutError) as error:
                metrics["own_dynamic_error"] = public_error(error)
                metrics["failures"] += 1
        try:
            await browsing.sync_notifications(row)
        except BilibiliAccountExpired:
            raise
        except (ValueError, TimeoutError) as error:
            metrics["notification_error"] = public_error(error)
            metrics["failures"] += 1
    finally:
        PIPELINE_READ.reset(token)


async def collect_own_comments(browsing, row, metrics):
    """Collect recent comments on the account's own uploaded videos into `browsing_events`."""
    account = row["account_id"]
    cutoff = max(int(browsing.started_at), int(browsing.operations.policy()["enabled_at"] or 0))
    for video in await browsing._read(browsing.adapter.own_uploads, account):
        try:
            result = await browsing._read(browsing.adapter.fetch, video["bvid"], newest=True)
        except BilibiliAccountExpired:
            raise
        except (ValueError, TimeoutError) as error:
            metrics["own_upload_comment_error"] = public_error(error)
            metrics["failures"] += 1
            continue
        browsing._check_round(row)
        enqueue_own_comments(
            browsing,
            account,
            cutoff,
            {"resource_type": "video", "oid": video["aid"], "bvid": video["bvid"]},
            result["items"],
        )


async def collect_own_dynamics(browsing, row, metrics):
    """Collect recent comments on the account's own dynamics into `browsing_events`."""
    account = row["account_id"]
    cutoff = max(int(browsing.started_at), int(browsing.operations.policy()["enabled_at"] or 0))
    try:
        targets = await browsing._read(browsing.adapter.own_dynamics, account)
    except BilibiliAccountExpired:
        raise
    except PlatformReadError as error:
        if error.kind != "restricted" or not hasattr(browsing.adapter, "own_dynamic_target"):
            raise
        metrics["own_dynamic_error"] = public_error(error)
        metrics["failures"] += 1
        receipts = browsing.store.all(
            "SELECT DISTINCT CAST(json_extract(receipt,'$.dynamic_id') AS TEXT) AS dynamic_id FROM dynamic_posts "
            "WHERE account_id=? AND status='published' AND json_extract(receipt,'$.dynamic_id') IS NOT NULL "
            "ORDER BY published_at DESC, id DESC LIMIT 3",
            (account,),
        )
        targets = [{"dynamic_id": receipt["dynamic_id"]} for receipt in receipts]
    for target in targets:
        try:
            if "oid" not in target:
                target = await browsing._read(browsing.adapter.own_dynamic_target, account, target["dynamic_id"])
            items = await browsing._read(browsing.adapter.fetch_dynamic_comments, target)
        except BilibiliAccountExpired:
            raise
        except (ValueError, TimeoutError) as error:
            metrics["own_dynamic_comment_error"] = public_error(error)
            metrics["failures"] += 1
            continue
        browsing._check_round(row)
        enqueue_own_comments(browsing, account, cutoff, target, items)


def enqueue_own_comments(browsing, account, cutoff, target, items):
    if harvester := getattr(browsing.runtime, "meme_harvester", None):
        try:
            harvester.enqueue_comment_attachments(
                items, bvid=target.get("bvid"), dynamic_id=target.get("dynamic_id")
            )
        except Exception:
            pass
    for comment in items:
        timestamp = int(comment["created_at"])
        author = str(comment["platform_user_id"])
        if timestamp < cutoff or author == account or category(comment["content"], comment.get("attachments") or []) == "spam":
            continue
        comment_id = str(comment["comment_id"])
        identity = "own:" + content_hash([account, target["resource_type"], str(target["oid"]), comment_id])
        event = target | {
            "event_id": identity,
            "kind": "own_comment",
            "account_id": account,
            "sender_uid": author,
            "sender_name": comment["display_name"],
            "root": str(comment["thread_id"]),
            "parent": comment_id,
            "comment_id": comment_id,
            "timestamp": timestamp,
            "source_text": comment["content"],
        }
        browsing.store.execute(
            "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,'received',?,NULL,?)",
            (identity, account, "own_comment", comment_id, timestamp, dump(event), time.time()),
        )


async def read_event_thread(browsing, event):
    """Read thread items and original comment for an event."""
    if event["kind"] != "comment_visit":
        return await read_scoped_thread(
            browsing.store,
            browsing.adapter.read_public_thread,
            event,
            event,
            invoke=browsing._read,
        )
    target = {key: event[key] for key in ("type", "resource_type", "oid", "bvid", "root", "parent")}
    if hasattr(browsing.adapter, "read_video_thread"):
        thread = await read_scoped_thread(
            browsing.store,
            browsing.adapter.read_video_thread,
            target,
            event,
            invoke=browsing._read,
        )
        thread = {"target": target} | thread
        thread["source"] = {"comment_id": target["parent"]} | thread["source"]
    else:
        item = next(item for item in event["items"] if str(item["comment_id"]) == target["parent"])
        thread = {"target": target, "source": comment_source(item), "items": event["items"]}
    return thread | {
        "account_id": event["account_id"],
        "resource_type": "video",
        "resource_author_uid": event.get("resource_author_uid"),
    }


def settle_aliases(browsing, event_row, row, target, source):
    """Settle duplicate pending notification / visit rows pointing to the same public comment."""
    if row["preview"]:
        return
    browsing.store.execute(
        "UPDATE browsing_events SET status='skipped',error='同一公开评论已处理' WHERE account_id=? "
        "AND status IN ('received','reply_deferred') AND id<>? AND json_extract(payload,'$.resource_type')=? "
        "AND CAST(json_extract(payload,'$.oid') AS TEXT)=? AND CAST(json_extract(payload,'$.parent') AS TEXT)=? "
        "AND CAST(json_extract(payload,'$.sender_uid') AS TEXT)=? AND (kind<>'comment_visit' OR "
        "(json_extract(payload,'$.character_id')=? AND json_extract(payload,'$.review_session_id') IS ?))",
        (
            row["account_id"],
            event_row["id"],
            target.get("resource_type") or ("video" if target.get("bvid") else "dynamic"),
            str(target["oid"]),
            str(source["comment_id"]),
            str(source["author_uid"]),
            row["character_id"],
            row["config"].get("review_session_id"),
        ),
    )
    if event_row["kind"] != "comment_visit":
        comment_id = str(source.get("comment_id") or "")
        if comment_id and target.get("bvid"):
            browsing.store.execute(
                "UPDATE browsing_events SET status='skipped',error='同一公开评论已由收件箱优先处理' "
                "WHERE account_id=? AND id<>? AND kind='comment_visit' "
                "AND status IN ('received','source_verified','context_ready','reply_deferred') "
                "AND CAST(json_extract(payload,'$.comment_id') AS TEXT)=? "
                "AND (json_extract(payload,'$.bvid')=? OR CAST(json_extract(payload,'$.oid') AS TEXT)=?)",
                (row["account_id"], event_row["id"], comment_id, target["bvid"], str(target.get("oid") or "")),
            )


async def decide_event(browsing, event_row, row, metrics, event, thread, slots):
    """Evaluate one comment/mention/reply event with shared ResponseEngine + claim/quota enforcement."""
    config = row["config"]
    source, target = thread["source"], dict(thread["target"])
    target["parent"] = str(source["comment_id"])
    if (
        str(thread["account_id"]) != str(row["account_id"])
        or str(source["author_uid"]) == str(row["account_id"])
    ):
        raise ValueError("账号来源无效、本人循环或广告互动")

    source_pipeline = "discovery" if event_row["kind"] == "comment_visit" else "inbox"
    if source_pipeline == "discovery":
        claims = _claims_manager(browsing)
        if claims is not None:
            for rid in [v for v in (target.get("bvid"), target.get("oid")) if v not in (None, "")]:
                if claims.has_inbox_priority(
                    account_id=str(row["account_id"]),
                    character_id=str(row["character_id"]),
                    resource_type=target.get("resource_type") or ("video" if target.get("bvid") else "dynamic"),
                    resource_id=str(rid),
                    root_id=str(target.get("root") or source["comment_id"]),
                ):
                    browsing.store.execute(
                        "UPDATE browsing_events SET status='skipped',error='同一公开评论已由收件箱优先处理' WHERE id=?",
                        (event_row["id"],),
                    )
                    return None

    resource = target.get("bvid") or "dynamic:" + str(target["oid"])
    canonical = canonical_intent("bilibili", resource, source["comment_id"], row["character_id"])
    items_list = thread.get("items", [])
    if not row["preview"] and items_list and (harvester := getattr(browsing.runtime, "meme_harvester", None)):
        try:
            harvester.enqueue_comment_attachments(
                items_list,
                bvid=target.get("bvid"),
                dynamic_id=target.get("dynamic_id"),
            )
        except Exception:
            pass
    with browsing.store.db:
        observe_thread(
            browsing.store,
            row,
            resource_key(target),
            items_list,
            via=event_row["kind"],
            resource_author_uid=str(row["account_id"])
            if event_row["kind"] == "own_comment"
            else thread.get("resource_author_uid"),
        )
    existing = browsing.publication.source_intent(row, canonical)
    matched = operator_reply(items_list, target, row["account_id"])
    if matched and (not existing or existing["status"] != "published"):
        browsing.store.execute(
            "UPDATE browsing_events SET status='operator_replied',source_key=?,"
            "payload=json_set(payload,'$.account_reply',json(?),'$.bvid',?,'$.dynamic_id',?),error=NULL WHERE id=?",
            (
                canonical,
                dump(comment_source(matched) | {"created_at": matched.get("created_at")}),
                target.get("bvid"),
                target.get("dynamic_id"),
                event_row["id"],
            ),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return None
    if existing:
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='同源回应意图已存在' WHERE id=?",
            (event_row["id"],),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return None
    if browsing.publication.has_manual_draft(row, resource, source["comment_id"]):
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='同源草稿已存在' WHERE id=?",
            (event_row["id"],),
        )
        return None
    source_row = next((r for r in items_list if str(r.get("comment_id")) == str(source["comment_id"])), None)
    if source_row is None:
        source_row = {
            "comment_id": source["comment_id"],
            "thread_id": target["root"],
            "platform_user_id": source["author_uid"],
            "display_name": source.get("author_name") or source.get("display_name") or event.get("sender_name", ""),
            "content": source["text"],
        }
    checked_source = comment_source(source_row)
    if (
        checked_source["text"] != source["text"]
        or checked_source["author_uid"] != str(source["author_uid"])
        or (event.get("sender_uid") and str(source["author_uid"]) != str(event["sender_uid"]))
    ):
        raise ValueError("评论原文或作者已变化，未使用旧候选")
    if category(source["text"], source_row.get("attachments") or []) == "spam":
        raise ValueError("账号来源无效、本人循环或广告互动")
    parent_id = str(source_row.get("parent_comment_id") or "")
    parent_row = thread.get("parent") or (
        next((r for r in items_list if str(r.get("comment_id") or "") == parent_id), None) if parent_id else None
    )
    parent_uid = str(
        (parent_row.get("author_uid") or parent_row.get("platform_user_id") or parent_row.get("user_id") or "")
        if isinstance(parent_row, dict)
        else ""
    )
    own_uid = str(row["account_id"] or "")
    mentions_list = list(source_row.get("mentions") or (thread.get("addressing") or {}).get("source_mentions") or [])
    is_direct_to_self = (
        event_row["kind"] == "mention"
        or (event_row["kind"] == "reply" and not parent_uid)
        or (own_uid and parent_uid == own_uid)
        or (own_uid and any(str(m.get("uid") or "") == own_uid for m in mentions_list if isinstance(m, dict)))
    )
    combined_text = source["text"]
    if grouped := source_row.get("grouped_comments"):
        combined_text = "\n".join(filter(None, [combined_text, *(comment_source(g)["text"] for g in grouped)]))
    if skip_reason := low_context_skip_reason(
        combined_text,
        is_direct_to_self=bool(is_direct_to_self),
        mentions=mentions_list,
        attachments=source_row.get("attachments") or (),
        self_uid=own_uid,
    ):
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',source_key=?,error=? WHERE id=?",
            (canonical, skip_reason, event_row["id"]),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return None
    like_enabled = config.get("comment_like_enabled", False)
    like_intent = browsing.publication.comment_like_intent(row, target) if like_enabled else None
    if like_intent:
        if (
            like_intent["auto_authorized"]
            and like_intent["epoch"] == row["epoch"]
            and like_intent["status"] == "pending_review"
            and browsing.publication._action_allowed("comment_like", browsing.effective())
        ):
            browsing.resume_publication(row, like_intent, like_intent["target"])
        browsing.store.execute(
            "UPDATE browsing_events SET status='drafted',source_key=?,error=NULL WHERE id=?",
            (canonical, event_row["id"]),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return like_intent
    is_direct = source_pipeline == "inbox"
    admission = slots.enter_context(
        browsing.comment_admission.slot(row, "reply_public", target.get("bvid"), source["author_uid"], direct=is_direct)
    )
    can_like = like_enabled and not source_row.get("liked_by_self")
    like_admission = None
    # This response can produce one action, so a reply slot also covers the
    # account quota for a possible like.
    if can_like and (admission or not browsing.comment_admission.requires_slot(row, "reply_public")):
        like_admission = slots.enter_context(browsing.comment_admission.slot(row, "comment_like", direct=is_direct))
    reply_limited = bool(
        target.get("bvid")
        and config["reply_enabled"]
        and browsing.comment_admission.review_reply_limit_reached(row, target["bvid"])
    )
    allow_public = config["reply_enabled"] and not admission and not (like_enabled and reply_limited)
    allow_like = can_like and not like_admission
    dynamic = thread.get("dynamic_source")
    if not (
        config.get("dynamic_like_enabled")
        and dynamic
        and not dynamic["liked"]
        and dynamic["author_uid"] != str(row["account_id"])
        and callable(getattr(browsing.adapter, "like_dynamic", None))
        and not browsing.publication.dynamic_like_intent(row, dynamic["dynamic_id"])
    ):
        dynamic = None
    blocked = min(
        (error for error in (admission, like_admission if can_like else None) if error),
        key=lambda error: error.next_at,
        default=None,
    )
    group_ids = event.get("grouped_ids") or []
    if group_ids:
        grouped = [
            item
            for item in items_list
            if str(item["comment_id"]) in group_ids
            and comment_source(item)["author_uid"] == str(source["author_uid"])
            and not operator_reply(items_list, target | {"parent": item["comment_id"]}, row["account_id"])
            and not browsing.publication.source_intent(
                row, canonical_intent("bilibili", resource, item["comment_id"], row["character_id"])
            )
        ]
        if len(grouped) > 1:
            source_row = source_row | {"grouped_comments": grouped}
    author_uid = thread.get("resource_author_uid") or event.get("resource_author_uid")
    confirmed_ids = confirmed_bot_comment_ids(
        browsing.store, row.get("account_id"), row.get("character_id"), resource, target["root"]
    )
    discussion = comment_thread_context(
        source_row,
        items_list,
        target,
        account_id=row.get("account_id"),
        resource_author_uid=author_uid,
        confirmed_ids=confirmed_ids,
    )
    thread.update({key: value for key, value in discussion.items() if key != "source"})
    legacy_source, source = source, discussion["source"]
    if grouped := thread.get("grouped_comments"):
        source = source | {"grouped_comments": grouped}
    if event_row["kind"] == "own_comment":
        thread["resource_author_uid"] = str(row["account_id"])
    reply = browsing.decisions.invited_private_response(row, thread)
    if reply is None and blocked and not (allow_public or allow_like or dynamic):
        raise blocked
    material, keys = None, []
    allowed = {"public:" + str(source["comment_id"])}
    if thread["resource_type"] == "video":
        if source_pipeline == "inbox":
            saved_pair = await reuse_or_prepare_light_evidence(
                browsing, row, target["bvid"], wait_inflight_only=True
            )
            if saved_pair:
                key, material = saved_pair
                keys = [key]
                allowed.update(s["id"] for s in browsing._segments(material))
        else:
            saved_material = browsing.repo.watch_material(row, target["bvid"])
            if saved_material:
                key, material = await browsing.evidence._store(saved_material)
                keys = [key]
                allowed.update(s["id"] for s in browsing._segments(material))
    elif thread["resource_type"] not in {"video", "dynamic"}:
        raise ValueError("未支持通知资源类型")
    browsing.store.execute(
        "UPDATE browsing_events SET status='context_ready',source_key=? WHERE id=?",
        (canonical, event_row["id"]),
    )
    browsing.runtime.relationships.observe(
        row["character_id"],
        "bilibili",
        str(source["author_uid"]),
        event.get("sender_name", ""),
        canonical,
        source["text"],
        account_id=row["account_id"],
    )
    if reply is None:
        previous = browsing.decisions.comment_progress.get(row, resource, source, legacy_source=legacy_source)
        if previous and (
            previous.action == "reply_public"
            and allow_public
            or previous.action == "like_comment"
            and allow_like
            or previous.action == "skip"
            and not (allow_like or dynamic)
        ):
            reply = previous
    can_send = (
        (allow_public and config["reply_auto_publish"])
        or (allow_like and config.get("comment_like_auto_publish"))
        or (dynamic and config.get("dynamic_like_auto_publish"))
    )
    if reply is None and allow_public and not can_send and not browsing.review_capacity("comments", row):
        browsing.store.execute(
            "UPDATE browsing_events SET status='received',error=? WHERE id=?",
            (
                dump({"message": "评论审核积压", "next_at": time.time() + config["notification_interval_seconds"]}),
                event_row["id"],
            ),
        )
        return None
    if reply is None:
        thread["_allow_public"] = allow_public
        thread["_allow_comment_like"] = allow_like
        thread["_dynamic_like_source"] = dynamic
        if allow_public or allow_like or dynamic:
            attachments = await prepare_comment_materials(browsing, target, source_row, row=row)
            if attachments:
                thread["_comment_materials"] = attachments
            if (
                not dynamic
                and not material
                and not source["text"].strip()
                and not any(item["status"] == "read" for item in attachments)
            ):
                reply = ResponseDecision(
                    action="skip",
                    reason="insufficient_evidence",
                    explanation="评论没有可读正文，附件也未能读取",
                )
                reply._comment_materials = attachments
            else:
                reply = await browsing.decisions._response(row, material, thread, allowed, "reply_public", metrics)
        else:
            reply = ResponseDecision(action="skip", reason="no_interest")
        if reply.action != "skip" or allow_public:
            browsing.decisions.comment_progress.save(row, resource, source, reply)
    if reply.action == "reply_public" and not allow_public or reply.action == "like_comment" and not allow_like:
        raise ValueError("回应动作未获当前任务授权")
    if blocked and reply.action in {"skip", "wait"}:
        raise blocked
    if like_enabled and reply_limited and reply.action in {"skip", "wait"}:
        limit = DailyReplyLimitReached(day_window(config.get("business_timezone", "Asia/Taipei")))
        browsing.store.execute(
            "UPDATE browsing_events SET status='reply_deferred',error=? WHERE id=?",
            (dump({"message": str(limit), "next_at": limit.next_at}), event_row["id"]),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return None
    if reply.action in {"skip", "wait"}:
        failed_materials = [item for item in getattr(reply, "_comment_materials", []) if item["status"] != "read"]
        if failed_materials:
            browsing.store.execute(
                "UPDATE browsing_events SET payload=json_set(payload,'$.unavailable_attachments',json(?)) WHERE id=?",
                (dump(failed_materials), event_row["id"]),
            )
        browsing.store.execute(
            "UPDATE browsing_events SET status=?,error=? WHERE id=?",
            (
                "received" if reply.action == "wait" else "skipped",
                dump(
                    {
                        "message": reply.explanation or reply.reason,
                        "next_at": time.time() + config["notification_interval_seconds"],
                    }
                )
                if reply.action == "wait"
                else reply.explanation or reply.reason,
                event_row["id"],
            ),
        )
        if reply.action == "skip":
            settle_aliases(browsing, event_row, row, target, source)
        return None
    target.update(
        recipient=str(source["author_uid"]),
        recipient_name=source.get("author_name") or "",
        source_text=source["text"],
    )
    if event_row["kind"] != "comment_visit":
        target["source_event"] = event
    if reply.action == "handoff_private":
        intent = await handoff_comment(
            browsing,
            row,
            reply,
            target,
            thread,
            material,
            keys,
            event=target.get("source_event"),
            created_at=event_row.get("timestamp", ""),
        )
    else:
        kind = {"like_comment": "comment_like", "like_dynamic": "dynamic_like"}.get(reply.action, "reply_public")
        if kind == "dynamic_like":
            if not dynamic or reply.evidence_ids != ["dynamic:" + dynamic["dynamic_id"]]:
                raise ValueError("原动态来源已变化")
            target = {key: target[key] for key in ("oid", "type", "resource_type", "dynamic_id")} | {
                "source_text": dynamic["text"],
                "recipient": dynamic["author_uid"],
                "root": "0",
                "parent": "0",
            }
        if kind == "reply_public":
            target["source_discussion"] = source_discussion(source_row, discussion, items_list)
            if getattr(reply, "_comment_materials", None):
                target["comment_materials"] = reply._comment_materials
        intent = browsing.publication._intent(
            row, kind, target, reply, keys, reply.evidence_ids, source_pipeline=source_pipeline
        )
        if not intent:
            if target.get("bvid") and browsing.repo.video_reply_limit_reached(row, target["bvid"]):
                raise DailyReplyLimitReached(day_window(config.get("business_timezone", "Asia/Taipei")))
            browsing.store.execute(
                "UPDATE browsing_events SET status='skipped',error='已达到单视频回复上限或同源候选已存在' WHERE id=?",
                (event_row["id"],),
            )
            return None
        if intent["auto_authorized"]:
            browsing.queue_publication(intent["id"], direct=is_direct)
    browsing.store.execute("UPDATE browsing_events SET status='drafted',error=NULL WHERE id=?", (event_row["id"],))
    settle_aliases(browsing, event_row, row, target, source)
    return intent


async def event_unlocked(browsing, event_row, row, metrics, event, thread):
    with ExitStack() as slots:
        claim_handles = []
        if event_row["kind"] != "comment_visit":
            claim_handles = _acquire_inbox_claims(browsing, row, event, thread=thread)
            if claim_handles is False:
                return None
        try:
            return await decide_event(browsing, event_row, row, metrics, event, thread, slots)
        finally:
            _release_inbox_claims(claim_handles)


async def event(browsing, event_row, row, metrics):
    event = json.loads(event_row["payload"])
    event.setdefault("kind", event_row["kind"])
    config = row["config"]
    if event_row["kind"] == "mention" and not config["mention_reply_enabled"] or not (
        config["reply_enabled"] or config.get("comment_like_enabled") or config.get("dynamic_like_enabled")
    ):
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='评论处理权限已关闭' WHERE id=?",
            (event_row["id"],),
        )
        return None
    pre_claims = []
    if event_row["kind"] != "comment_visit" and (event.get("comment_id") or event.get("parent")):
        pre_claims = _acquire_inbox_claims(browsing, row, event)
        if pre_claims is False:
            return None
    token = PIPELINE_READ.set("inbox" if event_row["kind"] != "comment_visit" else "discovery")
    try:
        thread = await read_event_thread(browsing, event)
        target = thread["target"]
        resource = target.get("bvid") or "dynamic:" + str(target["oid"])
        root = str(target.get("root") or thread["source"]["comment_id"])
        if root == "0":
            root = str(thread["source"]["comment_id"])
        scope = (str(row["account_id"]), row["character_id"], resource, root)
        async with browsing.runtime.conversation_turn(scope) as waited:
            if waited:
                browsing._check_round(row)
                thread = await read_event_thread(browsing, event)
            return await browsing._event_unlocked(event_row, row, metrics, event, thread)
    finally:
        _release_inbox_claims(pre_claims)
        PIPELINE_READ.reset(token)


async def attempt_event(browsing, event_row, row, metrics):
    task_id = json.loads(event_row["payload"]).get("visit_task_id")
    intent, generation = None, None
    try:
        intent = await browsing._event(event_row, row, metrics)
    except asyncio.CancelledError:
        browsing.store.execute(
            "UPDATE browsing_events SET status='received' WHERE id=? AND status IN ('context_ready','source_verified')",
            (event_row["id"],),
        )
        raise
    except Exception as error:
        continuation = getattr(error, "continuation", None)
        if continuation:
            generation = continuation
            browsing.runtime.continuations.bind_source_view(
                error.continuation["id"],
                "browsing",
                "event:" + str(event_row["id"]),
                "通知回应生成记录",
            )
            browsing.store.execute(
                "UPDATE browsing_events SET status=?,error=? WHERE id=?",
                (
                    "invalid" if continuation["state"] == "invalid" else "checkpoint",
                    dump(
                        {
                            "message": "输出无效，保留原答供检查"
                            if continuation["state"] == "invalid"
                            else "生成进度已保存，可继续",
                            "generation_continuation_id": error.continuation["id"],
                        }
                    ),
                    event_row["id"],
                ),
            )
            if not (task_id and continuation["state"] == "invalid"):
                metrics["failures"] += 1
        elif isinstance(error, (DailyReplyLimitReached, SendLimitReached)) or getattr(error, "kind", None) in {
            "network",
            "timeout",
            "restricted",
            "rate_limited",
            "anti_bot",
            "thread_pending",
        }:
            previous = event_row.get("error")
            try:
                occurrences = json.loads(previous or "{}").get("occurrences", 0) + 1
            except (ValueError, TypeError, AttributeError):
                occurrences = 1
            browsing.store.execute(
                "UPDATE browsing_events SET status='received',error=? WHERE id=?",
                (
                    dump(
                        {
                            "message": public_error(error),
                            "occurrences": occurrences,
                            "next_at": getattr(
                                error,
                                "next_at",
                                time.time() + row["config"]["notification_interval_seconds"],
                            ),
                        }
                    ),
                    event_row["id"],
                ),
            )
        elif isinstance(error, ValueError):
            browsing.store.execute(
                "UPDATE browsing_events SET status='skipped',error=? WHERE id=?",
                (public_error(error), event_row["id"]),
            )
            metrics["failures"] += 1
        else:
            raise
    if task_id and intent:
        field = "$.like_intent_ids" if intent["kind"] == "comment_like" else "$.reply_intent_ids"
        browsing.store.execute(
            "UPDATE browsing_tasks SET status=CASE WHEN json_extract(payload,'$.task_kind')='full_video' "
            "THEN status ELSE 'selected' END, "
            "payload=json_insert(payload,?,json('[]')),updated_at=? WHERE id=?",
            (field, time.time(), task_id),
        )
        browsing.store.execute(
            "UPDATE browsing_tasks SET payload=json_insert(payload,?,?) WHERE id=?",
            (field + "[#]", intent["id"], task_id),
        )
    elif task_id and generation:
        diagnostic = {
            "comment_id": json.loads(event_row["payload"])["comment_id"],
            "continuation_id": generation["id"],
            "state": generation["state"],
            "feedback": generation["feedback"],
            "original_candidate": generation["original_output"],
            "current_candidate": generation["current_output"],
        }
        browsing.store.execute(
            "UPDATE browsing_tasks SET payload=json_insert(payload,'$.repetition_decisions',json('[]')) WHERE id=?",
            (task_id,),
        )
        browsing.store.execute(
            "UPDATE browsing_tasks SET payload=json_insert(payload,'$.repetition_decisions[#]',json(?)) WHERE id=?",
            (dump(diagnostic), task_id),
        )


async def process_events(browsing, row, metrics, *, own_only=False, batch_only=False, inbox_only=False, revisit_only=False):
    browsing.check_step(row)
    scope = event_scope(row) | {
        "own": own_only,
        "batch_only": batch_only,
        "inbox_only": inbox_only,
        "revisit_only": revisit_only,
        "batch": row["config"].get("batch_id"),
        "now": time.time(),
        "limit": max(2, row["config"]["workers"]),
        "revisits": revisit_only
        or row["preview"]
        or bool(row["config"].get("review_session_id"))
        or (not browsing.comment_window_due() and not browsing._ready_approved_comments()),
    }
    selected = browsing.store.all(
        eligible_event_heads()
        + """
        , eligible AS (
        SELECT id,kind,created_at,
               ROW_NUMBER() OVER (PARTITION BY (kind='comment_visit') ORDER BY created_at,id) AS lane_rank
        FROM heads h
        WHERE position=1 AND COALESCE(CASE WHEN json_valid(error) THEN json_extract(error,'$.next_at') END,0)<=:now
          AND (NOT :own OR kind='own_comment')
          AND (NOT :inbox_only OR kind<>'comment_visit')
          AND (NOT :revisit_only OR kind='comment_visit')
          AND (:revisits OR kind<>'comment_visit')
          AND (NOT :batch_only OR EXISTS(
              SELECT 1 FROM browsing_batch_comment_events item
              JOIN browsing_events event ON event.id=item.event_id
              WHERE item.batch_id=:batch AND (
                item.event_id=h.id OR json_extract(event.payload,'$.resource_type')=h.resource_type
                AND CAST(json_extract(event.payload,'$.oid') AS TEXT)=h.resource
                AND CAST(json_extract(event.payload,'$.root') AS TEXT)=h.root
              )
          ) OR kind<>'comment_visit')
        )
        SELECT id,kind,created_at FROM eligible WHERE lane_rank<=:limit
        ORDER BY (kind='comment_visit'),created_at,id
        """,
        scope,
    )
    claimed = [item for item in selected if item["id"] not in browsing.event_active]
    browsing.event_active.update(item["id"] for item in claimed)

    async def process(item):
        lane = operation_lane.set("background" if item["kind"] == "comment_visit" else "direct")
        ready = operation_ready_at.set(time.monotonic() - max(0, time.time() - item["created_at"]))
        try:
            event_row = browsing.store.one("SELECT * FROM browsing_events WHERE id=?", (item["id"],))
            if event_row["status"] == "reply_deferred":
                browsing.store.execute("UPDATE browsing_events SET status='received' WHERE id=?", (item["id"],))
                event_row["status"] = "received"
            await attempt_event(browsing, event_row, row, metrics)
        finally:
            operation_ready_at.reset(ready)
            operation_lane.reset(lane)
            browsing.event_active.discard(item["id"])
            browsing.event_changed.set()

    async with asyncio.TaskGroup() as group:
        for item in claimed:
            group.create_task(process(item))
    return len(claimed)


__all__ = [
    "attempt_event",
    "collect_own_comments",
    "collect_own_dynamics",
    "decide_event",
    "eligible_event_heads",
    "enqueue_own_comments",
    "event",
    "event_scope",
    "event_unlocked",
    "process_events",
    "read_event_thread",
    "reuse_or_prepare_light_evidence",
    "run_interactions",
    "settle_aliases",
]
