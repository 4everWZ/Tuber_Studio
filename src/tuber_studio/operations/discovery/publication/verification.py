"""Target verification helpers for Discovery publication drafts."""

import json
import time

from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....core.store import dump
from ....core.text import validate_comment_body
from ....infra.platforms.bilibili.client import PlatformReadError
from ...kernel.decision_engine import ResponseEngine
from ...kernel.feedback import publication_binding as make_publication_binding
from ...kernel.generation import generate_with_recovery
from ...kernel.inputs import ResponseDecisionInput
from ...kernel.models import RESPONSE_INSTRUCTION, ResponseDecision

_COMMENT_KINDS = {"top_level", "reply_public"}
_ENGAGEMENT_CHANNELS = {
    "video_like": "video_likes",
    "video_fav": "video_favorites",
    "comment_like": "comment_likes",
    "dynamic_like": "dynamic_likes",
}


async def verify_video_target(browsing, target, *, source_aid=None):
    """Fetch fresh video metadata and validate visibility/AID consistency for a Discovery target."""
    metadata = await browsing._read(browsing.adapter.video_metadata, target["bvid"])
    if source_aid is not None and str(metadata.get("aid", "")) != str(source_aid):
        raise ValueError("视频来源标识已变化，取消发表")
    return metadata


def target_matches_scope(row, account_id, character_id) -> bool:
    """Return True if an intent row belongs to the expected `(account_id, character_id)` scope."""
    if str(row.get("account_id", "")) != str(account_id):
        return False
    return str(row.get("character_id", "")) == str(character_id)


def decode_intent_target(row) -> dict:
    """Decode JSON target dictionary from an intent row safely."""
    raw = row.get("target")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def has_legacy_binding(publisher, intent):
    """Return legacy discovery batch id if `intent` came from a discovery_publish round."""
    parent = publisher.browsing.repo.round(intent["round_id"])
    return parent["config"]["discovery_id"] if parent and parent["config"].get("discovery_publish") else None


def intent_publication_binding(publisher, row, character, metadata):
    """Build normalized FeedbackPublication binding dictionary for a Discovery/Inbox intent."""
    target = row["target"]
    source_text = target.get("source_text")
    source = (
        source_text
        if source_text is not None
        else [{key: item.get(key) for key in ("bvid", "aid")} for item in metadata]
    )
    return make_publication_binding(
        _ENGAGEMENT_CHANNELS.get(row["kind"], "comments"),
        row["account_id"],
        character,
        row["source_key"],
        {
            "platform": "bilibili",
            "resource_type": target.get("resource_type", "video"),
            **({"type": target["type"]} if target.get("type") == 11 else {}),
            "oid": str(target["oid"]),
            "root": str(target.get("root") or "0"),
            "parent": str(target.get("parent") or "0"),
            "recipient_uid": str(target.get("recipient") or ""),
        },
        {
            "kind": row["kind"],
            "local_id": row["id"],
            "platform_id": str(
                target.get("dynamic_id") if row["kind"] == "dynamic_like" else target.get("parent") or target["oid"]
            ),
            "content_sha256": content_hash(source),
            **({"covered_comment_ids": target["covered_comment_ids"]} if target.get("covered_comment_ids") else {}),
            **({"discovery": True} if publisher._has_legacy_binding(row) else {}),
        },
    )


async def refresh_intent_identity(publisher, identity, automatic):
    """Regenerate pending public comment intents when the account persona identity changes."""
    if not automatic:
        return
    browsing = publisher.browsing
    refresh = getattr(browsing.runtime, "refresh_identity", None)
    if refresh is None or not hasattr(browsing.adapter, "account_profile"):
        return
    row = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
    if not row or row["kind"] not in _COMMENT_KINDS or row["status"] != "pending_review":
        return
    validate_comment_body(row["text"], recipient_name=row["target"].get("recipient_name") or "")
    character = browsing.store.character(row["character_id"], row["character_version"])
    account = await browsing._current_account(row["account_id"])
    current = await refresh(character, account)
    previous = row["target"].get("generation_identity")
    if previous and previous.get("identity_version") == current["identity_version"]:
        return
    comment_thread_context = publisher._channel("comment_thread_context")
    model_comment_materials = publisher._channel("model_comment_materials")
    rebase_mentions = publisher._channel("rebase_mentions")
    materials = publisher._intent_materials(row)
    target = row["target"]
    model_target = {
        key: value
        for key, value in target.items()
        if key not in {"generation_identity", "source_event", "source_discussion", "comment_materials"}
    }
    if row["kind"] == "reply_public":
        cited = set(row["evidence"])
        segments = [
            segment
            for material in materials
            for segment in ResponseEngine._selected_video_segments(material, cited)
        ]
        if target.get("source_event"):
            thread = await browsing._read(browsing.adapter.read_public_thread, target["source_event"])
        elif hasattr(browsing.adapter, "read_video_thread"):
            thread = await browsing._read(browsing.adapter.read_video_thread, target)
        else:
            thread = {"source": await browsing._read(browsing.adapter.read_video_reply, target)}
        source = thread["source"]
        if source["text"] != target["source_text"] or str(source["author_uid"]) != str(target["recipient"]):
            raise ValueError("原始互动已删除或变化，保留草稿")
        recipient_name = source.get("author_name") or target.get("recipient_name", "")
        target = target | {"recipient_name": recipient_name}
        model_target["recipient_name"] = recipient_name
        items = thread.get("items", [])
        source_row = next((item for item in items if str(item["comment_id"]) == str(target["parent"])), None)
        if source_row:
            thread |= comment_thread_context(source_row, items, model_target)
        else:
            thread = thread | {"target": model_target}
    else:
        segments = [segment for material in materials for segment in browsing._segments(material)]
        thread = {"target": model_target, "video": [material["metadata"] for material in materials]}
    allowed = [segment["id"] for segment in segments]
    if row["kind"] == "reply_public":
        allowed.append("public:" + str(thread["source"]["comment_id"]))
    request = row | {"config": browsing.policy() | {"comment_handoff_enabled": False}}
    comment_materials = model_comment_materials(target.get("comment_materials", []))
    engine = getattr(browsing, "response_engine", None) or browsing.decisions
    data = engine._response_actor_data(
        request,
        {
            "kind": row["kind"],
            "thread": thread,
            "evidence": segments,
            "coverage": "public_text" if not materials else materials[0]["coverage"],
            "allowed_evidence_ids": allowed,
            **({"comment_materials": comment_materials} if comment_materials else {}),
        },
    )
    response_input = ResponseDecisionInput(data)
    _, messages, schema = engine._model_input(
        request, ResponseDecision, data, RESPONSE_INSTRUCTION, response_input=response_input
    )

    def guard():
        fresh = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        if not fresh or any(fresh[key] != row[key] for key in ("text", "status", "target")):
            raise ValueError("身份更新期间候选已变化，未覆盖")
        if runtime_identity(browsing.runtime, character, account)["identity_version"] != current["identity_version"]:
            raise ValueError("生成期间账号身份再次变化")
        browsing.operations.assert_allowed(
            automatic=automatic,
            channel="comments",
            account_id=account,
            character=character,
            epoch=row["epoch"] if automatic else None,
        )

    async def invoke(packet):
        return await browsing.runtime.operation_llm.generate_json(
            packet, schema, max_tokens=request["config"]["decision_max_tokens"]
        )

    async with browsing.runtime.offline_generation("background"):
        wire, audit = await generate_with_recovery(
            browsing.runtime,
            channel="generation.browsing_identity",
            identity=identity + ":" + current["identity_version"],
            binding={
                "task": "generation",
                "account_id": account,
                "character": {key: character[key] for key in ("id", "version")},
                "source_sha256": content_hash([thread, comment_materials])
                if comment_materials
                else content_hash(thread),
                "self_identity": current,
            },
            character=character,
            messages=messages,
            generate=invoke,
            validate=response_input.validate_wire,
            max_tokens=request["config"]["decision_max_tokens"],
            guard=guard,
            diagnose=response_input.diagnose,
        )
        response = response_input.resolve(wire)
        guard()
        if response.action != "reply_public":
            raise ValueError("身份更新后选择暂不发送：" + (response.explanation or response.reason))
        target = target | {"generation_identity": current}
        if target.get("mentions"):
            target["mentions"] = rebase_mentions(response.text, target["mentions"])
        if audit:
            target["generation_continuation_id"] = audit["id"]
        browsing.store.execute(
            "UPDATE browsing_intents SET text=?,target=?,evidence=?,reason=?,error=NULL,updated_at=? WHERE id=?",
            (
                response.text,
                dump(target),
                dump(response.evidence_ids),
                response.explanation or response.reason,
                time.time(),
                identity,
            ),
        )
        if audit and audit["state"] == "awaiting_review":
            browsing.store.execute(
                "UPDATE browsing_intents SET status='pending_review',auto_authorized=0 WHERE id=?", (identity,)
            )


async def verify_reply_source(publisher, target, account):
    """Fetch fresh platform reply/thread source and validate text, author, type, and media."""
    read_scoped_thread = publisher._channel("read_scoped_thread")
    validate_comment_material_sources = publisher._channel("validate_comment_material_sources")

    if target.get("source_event"):
        verified = await read_scoped_thread(
            publisher.browsing.store,
            publisher.browsing.adapter.read_public_thread,
            target["source_event"],
            target["source_event"],
            invoke=publisher.browsing._read,
        )
    elif hasattr(publisher.browsing.adapter, "read_video_thread"):
        verified = await read_scoped_thread(
            publisher.browsing.store,
            publisher.browsing.adapter.read_video_thread,
            target,
            target | {"account_id": account, "resource_type": "video"},
            invoke=publisher.browsing._read,
        )
    else:
        verified = {"source": await publisher.browsing._read(publisher.browsing.adapter.read_video_reply, target)}
    if (
        verified["source"]["text"] != target["source_text"]
        or str(verified["source"]["author_uid"]) != str(target["recipient"])
    ):
        raise PlatformReadError("invalid", "原始互动已删除或变化")
    observed_type = (verified.get("target") or {}).get("type")
    if (
        target.get("resource_type") == "dynamic"
        and observed_type is not None
        and observed_type != target.get("type", 17)
    ):
        raise ValueError("原始动态评论类型已变化")
    validate_comment_material_sources(target.get("comment_materials", []), verified.get("items", []), target["parent"])
    return verified


async def verify_dynamic_like(publisher, target):
    """Verify dynamic existence/author/text and return observation dict when already liked."""
    current = await publisher.browsing._read(publisher.browsing.adapter.read_dynamic_like, target["dynamic_id"])
    if (
        not current
        or current["dynamic_id"] != target["dynamic_id"]
        or current["author_uid"] != target["recipient"]
        or current["text"] != target["source_text"]
    ):
        raise ValueError("原始动态或作者已变化")
    if current["liked"]:
        return {
            "platform": "bilibili",
            "action": "dynamic_like",
            "dynamic_id": current["dynamic_id"],
            "observed": True,
        }


def liked_target(thread, target) -> bool:
    """Return True if the verified comment thread shows target comment liked by current account."""
    return any(
        str(item["comment_id"]) == str(target["parent"]) and item.get("liked_by_self") is True
        for item in thread.get("items", [])
    )


def like_observation(target) -> dict:
    """Build synthetic receipt payload for an observed already-liked comment."""
    return {
        "platform": "bilibili",
        "action": "comment_like",
        "oid": str(target["oid"]),
        "resource_type": target.get("resource_type", "video"),
        "rpid": str(target["parent"]),
        "observed": True,
    }


def record_account_reply(publisher, row, handled):
    """Mark an intent as `operator_replied` when the account has already replied externally."""
    comment_source = publisher._channel("comment_source")
    target = row["target"] | {"account_reply": comment_source(handled) | {"created_at": handled.get("created_at")}}
    publisher.browsing.store.execute(
        "UPDATE browsing_intents SET status='operator_replied',target=?,error=NULL,auto_authorized=0,updated_at=? "
        "WHERE id=? AND status=? AND text=?",
        (dump(target), time.time(), row["id"], row["status"], row["text"]),
    )
    return publisher.browsing.repo.decode(
        publisher.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (row["id"],))
    )
