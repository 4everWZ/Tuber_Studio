"""Native ContextTree + multi-turn Responses tool runner for video browsing & comment floors."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from ...core.identity import captured_identity
from ...core.meter import GenerationMeter
from ...core.models import content_hash
from ...core.text import operation_output_diagnostics
from .generation import generate_with_recovery, hold_comment_body
from .l2_bridge import (
    ContextNode,
    append_branch_turn,
    build_base_node,
    create_context_tree,
    fork_comment_floor_branch,
    fork_top_level_comment_branch,
    render_openai_messages,
)
from .models import ResponseDecision
from .session import OperationTurnResult, run_tool_session
from .tools import ALL_OPERATION_TOOL_NAMES, OperationToolSession


def _confirmed_bot_ids(store: Any, account_id: str, character_id: str, resource: str, root_id: str) -> set[str]:
    """Return confirmed published bot comment IDs in this floor so manual owner replies stay separated."""
    if not store or not account_id or not character_id or not resource or not root_id:
        return set()
    confirmed: set[str] = set()
    tables = {r["name"] for r in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
    if "browsing_intents" in tables:
        bvid, dynamic = (None, resource.removeprefix("dynamic:")) if resource.startswith("dynamic:") else (resource, None)
        rows = store.all(
            "SELECT receipt FROM browsing_intents WHERE account_id=? AND character_id=? "
            "AND kind='reply_public' AND status='published' "
            "AND CAST(json_extract(target,'$.root') AS TEXT)=? AND "
            "((? IS NOT NULL AND json_extract(target,'$.bvid')=?) OR "
            "(? IS NOT NULL AND COALESCE(CAST(json_extract(target,'$.dynamic_id') AS TEXT),"
            "CAST(json_extract(target,'$.oid') AS TEXT))=?))",
            (str(account_id), str(character_id), str(root_id), bvid, bvid, dynamic, dynamic),
        )
        for row in rows:
            receipt = json.loads(row.get("receipt") or "{}")
            rpid = str(receipt.get("rpid") or receipt.get("comment_id") or "")
            if rpid:
                confirmed.add(rpid)
    if "drafts" in tables and "comments" in tables:
        rows = store.all(
            "SELECT d.receipt FROM drafts d JOIN comments c ON c.id=d.comment_id "
            "WHERE d.account_id=? AND d.character_id=? AND c.platform='bilibili' "
            "AND c.video_id=? AND c.thread_id=? AND d.status='published'",
            (str(account_id), str(character_id), resource, str(root_id)),
        )
        for row in rows:
            receipt = json.loads(row.get("receipt") or "{}")
            rpid = str(receipt.get("rpid") or receipt.get("comment_id") or "")
            if rpid:
                confirmed.add(rpid)
    return confirmed


def _handoff_eligibility(engine: Any, row: dict[str, Any], thread: dict[str, Any], comment_reply: bool) -> dict[str, Any]:
    if not comment_reply:
        return {"eligible": False, "blocked_reason": "not_comment_reply", "message": "非评论回复场景，不可转入私信。"}
    if not (row.get("config") or {}).get("comment_handoff_enabled"):
        return {"eligible": False, "blocked_reason": "handoff_disabled", "message": "当前运营策略未启用评论转私信。"}
    policy = engine.browsing.messages.policy()
    if not (policy.get("enabled") and policy.get("allow_comment_handoff")):
        return {"eligible": False, "blocked_reason": "dm_handoff_disabled", "message": "私信模块未启用评论转入。"}
    source = thread.get("source") or {}
    recipient = (thread.get("target") or {}).get("recipient") or source.get("author_uid")
    confirmed_invitation = engine._channel("confirmed_invitation")
    invitation = confirmed_invitation(thread, row.get("account_id"), recipient)
    if invitation is None:
        return {
            "eligible": False,
            "blocked_reason": "no_confirmed_invitation",
            "message": "原评论没有面向本账号的明确私信邀请，无法转入私信。",
        }
    return {
        "eligible": True,
        "recipient_uid": str(recipient or ""),
        "source_comment_id": str(source.get("comment_id") or ""),
        "source_quote": str(invitation.get("quote") or ""),
        "contact_trigger": str(invitation.get("kind") or "explicit_request"),
    }


def _build_browsing_tree(
    engine: Any,
    row: dict[str, Any],
    *,
    character: dict[str, Any],
    bound: dict[str, Any],
    material: dict[str, Any] | None,
    context: dict[str, Any],
    kind: str,
    recent: list[dict[str, Any]],
    comment_materials: list[dict[str, Any]],
    allow_public: bool,
    allow_comment_like: bool,
    dynamic_source: dict[str, Any] | None,
    video_actions: bool,
) -> tuple[Any, str]:
    self_actor = dict(bound.get("self_actor") or {})
    own_uid = str(self_actor.get("account_uid") or "")
    target = context.get("target") or {}
    comment_reply = kind == "reply_public" and not dynamic_source
    base_video = (
        material
        if material and (not comment_reply or target.get("bvid") == (material.get("metadata") or {}).get("bvid"))
        else None
    )
    extra_base_blocks: list[str] = []
    if bound.get("own_public_experiences"):
        exp_lines = [
            f"- {item.get('text') or ''}"
            for item in bound["own_public_experiences"]
            if str(item.get("text") or "").strip()
        ]
        if exp_lines:
            extra_base_blocks.append("过往公开互动经历：\n" + "\n".join(exp_lines))

    base = build_base_node(
        character,
        channel="video_browsing",
        self_actor=self_actor,
        operating_account=self_actor,
        memories=bound.get("unified_public_memories") or (),
        memory_scope="public",
        video_material=base_video,
        extra_base_blocks=tuple(extra_base_blocks),
    )
    tree = create_context_tree(base)

    if dynamic_source:
        lines = [
            f"当前动态（dynamic_id {dynamic_source.get('dynamic_id')}）：{dynamic_source.get('text') or ''}",
            "请判断这条动态是否值得互动。你可以调用 search_web / recall_history / save_memory，"
            "如果想回复，请直接自然写出公开回复；如果觉得不合适发言，请调用 skip_reply。",
        ]
        branch = tree.add_node(
            ContextNode(
                node_id=f"branch:dynamic_reply:{uuid4().hex[:12]}",
                kind="branch",
                parent_id=base.node_id,
                scope=f"dynamic:{dynamic_source.get('dynamic_id') or ''}",
                channel="dynamic_reply",
                title="动态互动分支",
            )
        )
        branch = append_branch_turn(tree, branch.node_id, {"role": "user", "content": "\n\n".join(lines)})
        return tree, branch.node_id

    if comment_reply:
        source = context.get("source") or {}
        root_id = str(target.get("root") or source.get("thread_id") or source.get("comment_id") or "")
        resource = str(target.get("bvid") or ("dynamic:" + str(target.get("oid"))) if target.get("oid") else "")
        confirmed_ids = _confirmed_bot_ids(
            engine.browsing.store, str(row.get("account_id") or ""), str(row["character_id"]), resource, root_id
        )
        uploader_uid = context.get("resource_author_uid") or (bound.get("video_source") or {}).get("owner_uid")
        owner_uid = str((character.get("bilibili_owner") or {}).get("uid") or "") or None
        extra_ctx: dict[str, Any] = {}
        if context.get("confirmed_relationship"):
            rel = context["confirmed_relationship"]
            extra_ctx["relationship"] = {k: rel[k] for k in ("affinity", "tone_hint", "interaction_count") if k in rel}
        if comment_materials:
            extra_ctx["comment_attachments"] = engine._channel("model_comment_materials")(comment_materials)
        if context.get("grouped_comments"):
            extra_ctx["grouped_comments"] = [
                {"comment_id": g.get("comment_id"), "text": g.get("text") or g.get("content") or ""}
                for g in context["grouped_comments"]
            ]
        if bound.get("actor_roles"):
            extra_ctx["actor_roles"] = bound["actor_roles"]
        objective = (
            "请像 B 站楼里顺手回一句那样，用 1-2 句简短口语（通常 10-50 字）直接接住当前评论的话头，别写长段分析或套模板；"
            "若不想回、对方没跟本账号说话、或不需要插话，请调用 skip_reply。"
            if allow_public
            else "当前只允许观察或跳过（不可公开发言），决定不回复请调用 skip_reply。"
        )
        if base_video:
            objective += " 如评论涉及视频具体细节但当前上下文没写清，可调用 read_video_segment 或 inspect_video_frames 核查。"
        if allow_comment_like:
            objective += "（当前评论也可由程序按互动策略决定点赞，如不发言请调用 skip_reply。）"
        timeline = list(context.get("timeline") or context.get("items") or ())
        addressing = (bound.get("thread") or {}).get("addressing") or None
        video_author = (bound.get("actor_roles") or {}).get("video_author") or None
        branch = fork_comment_floor_branch(
            tree,
            root_id=root_id,
            timeline=timeline,
            reply_target=source,
            self_uid=own_uid or None,
            confirmed_bot_comment_ids=confirmed_ids,
            uploader_uid=str(uploader_uid or "") or None,
            owner_uid=owner_uid,
            objective=objective,
            extra_context=extra_ctx or None,
            addressing=addressing,
            video_author=video_author,
        )
        return tree, branch.node_id

    # Video top_level (or video_actions) branch
    objectives = []
    if allow_public:
        objectives.append(
            "如果你想发表一条视频主评，请像真实 B 站观众一样直接写 1-2 句自然短评（通常 15-55 字），"
            "挑一个最戳人的具体画面、细节或槽点开聊，不要写复述全片或分点总结的长评。"
        )
    else:
        objectives.append("本次仅观看与评估，不可发表公开留言；处理完互动偏好后请调用 skip_reply。")
    if video_actions:
        objectives.append("如果想表达喜欢或不喜欢这个视频（点赞／收藏态度），可调用 like_video。")
        objectives.append("如果只想先看评论区再聊，可调用 skip_reply(reason='read_comments', ...)。")
        objectives.append("如果看完但不打算留言，可调用 skip_reply(reason='finish', ...)。")
    elif allow_public:
        objectives.append("如果不需要发主评，请调用 skip_reply。")
    if base.degraded:
        objectives.append("注意：完整字幕较长，上方仅显示分P目录；需要细看某一段请调用 read_video_segment。")
    branch = fork_top_level_comment_branch(
        tree,
        existing_hot_comments=recent,
        objective="\n".join(objectives),
    )
    return tree, branch.node_id


async def run_native_browsing_response(
    engine: Any,
    row: dict[str, Any],
    material: dict[str, Any] | None,
    context: dict[str, Any],
    allowed: set[str] | frozenset[str],
    kind: str,
    metrics: dict[str, Any] | None,
    *,
    data: dict[str, Any],
    bound: dict[str, Any],
    recent: list[dict[str, Any]],
    comment_materials: list[dict[str, Any]],
    allow_public: bool,
    allow_comment_like: bool,
    dynamic_source: dict[str, Any] | None,
    video_actions: bool,
    source_links: tuple[str, ...],
) -> ResponseDecision:
    """Execute native ContextTree + Responses tool loop and bind deterministic provenance."""
    character = engine.browsing.store.character(row["character_id"], row["character_version"])
    memory_audit = bound.pop("_memory_audit", None)
    comment_reply = kind == "reply_public" and not dynamic_source
    target = context.get("target") or {}
    source = context.get("source") or {}
    root_id = str(target.get("root") or source.get("comment_id") or "")
    resource = str(target.get("bvid") or ("dynamic:" + str(target.get("oid"))) if target.get("oid") else (material or {}).get("metadata", {}).get("bvid") or "")

    tree, branch_id = _build_browsing_tree(
        engine,
        row,
        character=character,
        bound=bound,
        material=material,
        context=context,
        kind=kind,
        recent=recent,
        comment_materials=comment_materials,
        allow_public=allow_public,
        allow_comment_like=allow_comment_like,
        dynamic_source=dynamic_source,
        video_actions=video_actions,
    )

    def allow_web_url_str(raw_url: str, tgt: dict[str, Any]) -> bool:
        bvid = str(tgt.get("bvid") or "").casefold()
        if not bvid or "bilibili.com" not in raw_url.casefold():
            return True
        return bvid not in raw_url.casefold()

    async def web_search_cb(query: str):
        from ...infra.web_client.search import search_public_web

        res = await search_public_web(engine.browsing.settings, query)
        if isinstance(res, dict) and "results" in res:
            res["results"] = [
                item for item in res["results"]
                if allow_web_url_str(str(item.get("url") or ""), target)
            ]
        return res

    async def inspect_frames_cb(request: dict[str, Any]) -> dict[str, Any]:
        if not material or not engine.browsing.evidence.has_visual_provider():
            return {"ok": False, "reason": "visual_unavailable", "message": "视觉服务当前不可用。"}
        # Look up matching segments around requested time window so existing evidence collector can sample real frames.
        s0 = float(request.get("start_seconds") or 0.0)
        s1 = float(request.get("end_seconds") or s0)
        req_part = request.get("part")
        pages = {str(p.get("cid")): idx for idx, p in enumerate((material.get("metadata") or {}).get("parts") or (), 1)}
        matched_ids: list[str] = []
        for part in material.get("parts") or ():
            if part.get("visual"):
                continue
            cid = str(part.get("cid") or "")
            if req_part is not None and int(pages.get(cid, 1)) != int(req_part):
                continue
            for seg in part.get("segments") or ():
                if float(seg.get("end") or 0.0) >= s0 and float(seg.get("start") or 0.0) <= s1:
                    matched_ids.append(str(seg["id"]))
        if not matched_ids:
            all_segs = engine.browsing._segments(material)
            if all_segs:
                matched_ids = [all_segs[0]["id"]]
        if hasattr(engine.browsing.evidence, "sample_frames"):
            sampled = await engine.browsing.evidence.sample_frames(
                material, matched_ids, row=row, question=request.get("reason") or ""
            )
            return {"ok": True, "segments": sampled, "observations": "\n".join(s.get("text", "") for s in sampled)}
        return {"ok": False, "reason": "visual_deferred", "message": "本次上下文仅提供现有字幕，可直接结合字幕回应或跳过。"}

    handoff_elig = _handoff_eligibility(engine, row, context, comment_reply)
    tool_session = OperationToolSession(
        channel="video_browsing",
        character=character,
        account_id=str(row.get("account_id") or ""),
        store=engine.browsing.store,
        unified_memory=getattr(engine.browsing.runtime, "unified_memory", None),
        settings=engine.browsing.settings,
        material=material,
        self_uid=str((bound.get("self_actor") or {}).get("account_uid") or "") or None,
        resource=resource or None,
        root_id=root_id or None,
        video_engagement_enabled=bool(video_actions),
        visual_allowed=bool(material and engine.browsing.evidence.has_visual_provider() and not (material or {}).get("visual_source_ids")),
        web_search_allowed=True,
        handoff_eligibility=handoff_elig,
        search_fn=web_search_cb,
        inspect_frames_fn=inspect_frames_cb,
    )

    profile_uid = source.get("author_uid")
    relations = engine.browsing.runtime.relationships
    profile_revision = relations.profile_revision(row["account_id"], row["character_id"], "bilibili", profile_uid)

    def guard():
        engine.browsing._check_round(row)
        engine.browsing.runtime.validate_memory_delivery(memory_audit)
        if profile_revision != relations.profile_revision(row["account_id"], row["character_id"], "bilibili", profile_uid):
            raise ValueError("等待模型期间用户档案已更新")
        src_id = data.get("source_identity")
        if src_id and engine.browsing.publication.has_manual_draft(row, src_id["resource"], src_id["comment_id"]):
            raise ValueError("等待模型期间同源评论已有候选，合并处理")

    output_tokens = int((row.get("config") or {}).get("decision_max_tokens") or 1024)

    async def invoke_native(packet_or_unused, *, web_fetch=None):
        wait = time.monotonic()
        async with engine.browsing.runtime.offline_generation():
            if metrics is not None:
                metrics["lock_wait_seconds"] = metrics.get("lock_wait_seconds", 0.0) + (time.monotonic() - wait)
            engine.browsing.check_step(row)
            guard()
            with GenerationMeter(engine.browsing.repo, row["id"]):
                res = await run_tool_session(
                    engine.browsing.runtime,
                    tree,
                    branch_id,
                    tool_session,
                    tool_names=ALL_OPERATION_TOOL_NAMES,
                    spoken_kind="top_level" if kind == "top_level" else "reply_public",
                    max_output_tokens=output_tokens,
                )
                return {
                    "action": res.action,
                    "text": res.text,
                    "skip_reason": res.skip_reason,
                    "skip_explanation": res.skip_explanation,
                    "handoff": res.handoff,
                    "video_engagement": res.video_engagement,
                    "provenance": res.provenance,
                    "tool_calls": res.tool_calls,
                    "usage": res.usage,
                    "elapsed_ms": res.elapsed_ms,
                    "transport": res.transport,
                }

    rendered = render_openai_messages(tree, branch_id)
    binding = {
        "task": "generation",
        "account_id": row["account_id"],
        "character": {"id": row["character_id"], "version": row["character_version"]},
        "schema": "ResponseDecision",
        "protocol": "native-multiturn-tools-v1",
        "self_identity": captured_identity(rendered),
        "source_sha256": content_hash({"kind": kind, "resource": resource, "root_id": root_id, "source": source}),
        "model": engine.browsing.model_signature(),
    }
    if profile_revision is not None:
        binding["profile_revision"] = profile_revision
    if memory_audit:
        binding["memory_audit"] = memory_audit
    identity = content_hash(binding)

    def validate_result(payload: dict[str, Any]) -> dict[str, Any]:
        turn = OperationTurnResult(
            action=str(payload.get("action") or "speak"),
            text=str(payload.get("text") or ""),
            skip_reason=str(payload.get("skip_reason") or ""),
            skip_explanation=str(payload.get("skip_explanation") or ""),
            handoff=payload.get("handoff"),
            video_engagement=payload.get("video_engagement"),
            provenance=list(payload.get("provenance") or ()),
            tool_calls=list(payload.get("tool_calls") or ()),
            usage=dict(payload.get("usage") or {}),
            elapsed_ms=float(payload.get("elapsed_ms") or 0.0),
            transport=str(payload.get("transport") or "responses"),
        )
        if turn.action == "speak" and not allow_public:
            raise ValueError("本次未开放公开留言，可调用 skip_reply")
        return turn.model_dump()

    def diagnose_result(payload: dict[str, Any]) -> str | None:
        if str(payload.get("action") or "speak") != "speak":
            return None
        text = str(payload.get("text") or "")
        recipient_name = source.get("author_name") or ""
        hold_comment_body(payload, text, recipient_name=recipient_name)
        if rep := engine.repetition_reason(text, recent, context):
            return rep
        if operation_output_diagnostics(text)["needs_review"]:
            return "正文包含大量重复内容；结合表达意图选择保留、修订或暂缓"
        return None

    turn_payload, _ = await generate_with_recovery(
        engine.browsing.runtime,
        channel="generation.browsing",
        identity=identity,
        binding=binding,
        character=character,
        messages=rendered,
        generate=invoke_native,
        validate=validate_result,
        diagnose=diagnose_result,
        max_tokens=output_tokens,
        guard=guard,
        parent_round_id=row["id"],
        deadline=row.get("deadline"),
        context_data={"instruction": rendered[0]["content"], "source": rendered[-1]["content"]},
        reuse_completed=False,
        allow_web_fetch=False,
    )
    turn_result = (
        turn_payload
        if isinstance(turn_payload, OperationTurnResult)
        else OperationTurnResult(
            action=str(turn_payload.get("action") or "speak"),
            text=str(turn_payload.get("text") or ""),
            skip_reason=str(turn_payload.get("skip_reason") or ""),
            skip_explanation=str(turn_payload.get("skip_explanation") or ""),
            handoff=turn_payload.get("handoff"),
            video_engagement=turn_payload.get("video_engagement"),
            provenance=list(turn_payload.get("provenance") or ()),
            tool_calls=list(turn_payload.get("tool_calls") or ()),
            usage=dict(turn_payload.get("usage") or {}),
            elapsed_ms=float(turn_payload.get("elapsed_ms") or 0.0),
            transport=str(turn_payload.get("transport") or "responses"),
        )
    )

    engine.browsing.runtime.validate_memory_delivery(memory_audit)
    engagement = turn_result.video_engagement or {}
    if video_actions:
        if engagement:
            vpref = "like" if engagement.get("should_like") else "not_interested"
            vreason = str(engagement.get("reason") or "根据视频内容判断")
        elif turn_result.action == "speak":
            vpref = "like"
            vreason = "对视频内容产生共鸣并留评"
        else:
            vpref = "neutral"
            vreason = str(turn_result.skip_explanation or "看完视频后的自然判断")
    else:
        vpref = None
        vreason = ""

    # Populate canonical provenance strictly in Python
    prov: list[str] = []
    if comment_reply and source.get("comment_id"):
        prov.append("public:" + str(source["comment_id"]))
    for item in turn_result.provenance:
        if item not in prov:
            prov.append(item)
    if dynamic_source and dynamic_source.get("dynamic_id"):
        dyn_id = "dynamic:" + str(dynamic_source["dynamic_id"])
        if dyn_id not in prov:
            prov.append(dyn_id)
    if not prov and turn_result.action == "speak" and material:
        prov = [s["id"] for s in engine.browsing._segments(material) if s["id"] in allowed]

    if turn_result.action == "handoff_private" and turn_result.handoff:
        decision = ResponseDecision(
            action="handoff_private",
            reason="user_requested",
            explanation="原评论请求私聊，" + turn_result.handoff["source_quote"],
            contact_purpose=turn_result.handoff["source_quote"],
            contact_trigger=handoff_elig.get("contact_trigger", "explicit_request"),
            evidence_ids=["public:" + str(source["comment_id"])] if source.get("comment_id") else [],
            video_preference=vpref,
            preference_reason=vreason,
        )
    elif turn_result.action == "skip":
        s_reason = turn_result.skip_reason.lower()
        mapped_action = "read_comments" if "read_comments" in s_reason else "finish" if "finish" in s_reason else "skip"
        reason_code = (
            "light_interaction"
            if mapped_action == "read_comments"
            else "no_interest"
            if s_reason in {"no_interest", "skip", "finish"}
            else "cooldown"
            if "cooldown" in s_reason or "wait" in s_reason
            else "insufficient_evidence"
            if "insufficient" in s_reason or "wait" in s_reason
            else "no_interest"
        )
        decision = ResponseDecision(
            action=mapped_action,
            reason=reason_code,
            explanation=turn_result.skip_explanation or turn_result.skip_reason or "跳过本次回复",
            text="",
            evidence_ids=[],
            video_preference=vpref,
            preference_reason=vreason,
        )
    else:
        covered = (
            [str(item["comment_id"]) for item in context.get("grouped_comments", [])]
            if context.get("grouped_comments")
            else []
        )
        decision = ResponseDecision(
            action="reply_public",
            reason="specific_detail",
            explanation="基于当前上下文自然表达",
            text=turn_result.text,
            evidence_ids=prov,
            covered_comments=covered,
            video_preference=vpref,
            preference_reason=vreason,
        )
        # Resolve explicit @mentions mentioned in plain text against real floor participants
        mention_participants = engine._channel("mention_participants")
        bind_mentions = engine._channel("bind_mentions")
        participants = mention_participants(context, bound.get("video_source"))
        matches = []
        seen_names: set[str] = set()
        for p in participants:
            name = str(p.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            pos = decision.text.find(f"@{name}")
            if pos >= 0:
                seen_names.add(name)
                uid = str(p.get("uid") or "").strip()
                matches.append((pos, {"label": f"@{name}", "account": uid or name}))
        matches.sort(key=lambda entry: entry[0])
        detected_mentions = [item for _, item in matches]
        if detected_mentions:
            mentions, pending = await bind_mentions(
                engine.browsing.adapter,
                decision.text.strip(),
                detected_mentions,
                participants,
            )
            decision._mention_selection = {"mentions": mentions, "pending_mentions": pending}

    decision._memory_audit = memory_audit
    decision._generation_identity = binding["self_identity"]
    saved_generation = engine.browsing.runtime.continuations.find("generation.browsing", identity)
    decision._generation_id = saved_generation["id"] if saved_generation else None
    decision._comment_materials = comment_materials
    decision._turn_result = turn_result
    return decision
