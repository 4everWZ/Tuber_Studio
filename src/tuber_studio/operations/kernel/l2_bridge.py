"""Bridge to L0/L2 self-model, ContextTree, and unified memory APIs.

When L2 modules from Phase A are present, symbols are imported and delegated
directly. While running inside an isolated pre-merge workspace, lightweight
contract-compatible fallbacks matching the Phase A specification are used so
local tests exercise the identical call paths.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from ...core.identity import owner_address_label
from ...core.models import Character, content_hash
from ...persona.context.subtitles import timestamp as subtitle_timestamp
from .l2_memory_bridge import (
    DEFAULT_MEMORY_LIMIT,
    execute_saved_memory_tool,
    recall_history,
    recall_history_tool_schema,
    save_memory,
    saved_memory_tool_schemas,
)

DEFAULT_VIDEO_BASE_TOKEN_LIMIT = 24_000


# ---------------------------------------------------------------------------
# L0 agent types (tuber_studio.core.agent_types)
# ---------------------------------------------------------------------------
try:
    from ...core.agent_types import (
        ContextNode,
        ContextTree,
        MemoryItem,
        MemoryScope,
        MetacognitiveProfile,
    )
except ImportError:

    class MemoryScope(StrEnum):
        OWNER = "owner"
        PUBLIC = "public"

    @dataclass(frozen=True)
    class MemoryItem:
        id: str
        scope: str
        character_id: str
        content: str
        source: str = "manual"
        tags: tuple[str, ...] = ()
        created_at: float = 0.0
        updated_at: float = 0.0
        active: bool = True

        def to_dict(self) -> dict[str, Any]:
            return asdict(self) | {"tags": list(self.tags)}

        @classmethod
        def from_dict(cls, raw: dict[str, Any]) -> MemoryItem:
            return cls(
                id=str(raw.get("id", "")),
                scope=str(raw.get("scope", MemoryScope.PUBLIC)),
                character_id=str(raw.get("character_id", "")),
                content=str(raw.get("content", "")),
                source=str(raw.get("source", "manual")),
                tags=tuple(raw.get("tags") or ()),
                created_at=float(raw.get("created_at") or 0.0),
                updated_at=float(raw.get("updated_at") or 0.0),
                active=bool(raw.get("active", True)),
            )

    @dataclass(frozen=True)
    class MetacognitiveProfile:
        character_id: str
        character_name: str
        display_name: str = ""
        aliases: tuple[str, ...] = ()
        self_description: str = ""
        speaking_style: str = ""
        owner_address: str = "你"
        owner_uid: str | None = None
        owner_name: str | None = None
        account_uid: str | None = None
        account_name: str | None = None
        channel: str = "owner_chat"
        scene: str = ""
        task_intent: str = ""
        epistemic_boundaries: tuple[str, ...] = ()
        persona_rules: tuple[str, ...] = ()
        forbidden_self_labels: tuple[str, ...] = ()
        extra_context: dict[str, Any] = field(default_factory=dict)

    @dataclass(frozen=True)
    class ContextNode:
        node_id: str
        kind: str = "base"
        parent_id: str | None = None
        scope: str = ""
        channel: str = ""
        title: str = ""
        messages: tuple[dict[str, Any], ...] = ()
        metadata: dict[str, Any] = field(default_factory=dict)
        content_hash: str = ""
        token_estimate: int = 0
        degraded: bool = False
        created_at: float = 0.0

    @dataclass
    class ContextTree:
        tree_id: str
        root_id: str
        nodes: dict[str, ContextNode] = field(default_factory=dict)

        def get(self, node_id: str) -> ContextNode | None:
            return self.nodes.get(node_id)

        def children(self, parent_id: str) -> list[ContextNode]:
            return [node for node in self.nodes.values() if node.parent_id == parent_id]

        def add_node(self, node: ContextNode) -> ContextNode:
            self.nodes[node.node_id] = node
            return node

        def lineage(self, node_id: str | None = None) -> list[ContextNode]:
            target = node_id or self.root_id
            chain: list[ContextNode] = []
            seen: set[str] = set()
            while target and target not in seen:
                node = self.nodes.get(target)
                if node is None:
                    break
                seen.add(target)
                chain.append(node)
                target = node.parent_id
            chain.reverse()
            return chain

        def render_messages(self, node_id: str | None = None) -> list[dict[str, Any]]:
            messages: list[dict[str, Any]] = []
            for node in self.lineage(node_id):
                for message in node.messages:
                    messages.append(dict(message))
            return messages

        def base_prefix(self) -> list[dict[str, Any]]:
            return self.render_messages(self.root_id)


# ---------------------------------------------------------------------------
# L2 Self-Model (tuber_studio.persona.self_model)
# ---------------------------------------------------------------------------
try:
    from ...persona.self_model import (
        build_metacognitive_profile,
        build_self_model_system_message,
        render_self_model_prompt,
    )
except ImportError:

    def _estimate_tokens(text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 2) // 3)

    def build_metacognitive_profile(
        character,
        *,
        channel: str = "owner_chat",
        scene: str | None = None,
        task_intent: str | None = None,
        self_actor: dict[str, Any] | None = None,
        operating_account: dict[str, Any] | None = None,
        speaker: dict[str, Any] | None = None,
        situation: str | None = None,
        extra_boundaries: tuple[str, ...] = (),
        extra_context: dict[str, Any] | None = None,
    ) -> MetacognitiveProfile:
        card = character.model_dump() if isinstance(character, Character) else dict(character or {})
        core = dict(card.get("core") or {})
        style = dict(card.get("style") or {})
        actor = dict(self_actor or operating_account or {})
        owner = dict(card.get("bilibili_owner") or {})
        account_uid = str(actor.get("account_uid") or actor.get("uid") or "") or None
        account_name = str(actor.get("account_display_name") or actor.get("name") or "") or None
        owner_uid = str(owner.get("uid") or "") or None
        owner_name = str(owner.get("name") or "") or None
        rules = tuple(str(r).strip() for r in (card.get("rules") or ()) if str(r).strip())
        boundaries = (
            "只依据当前上下文里已提供的真实对话、视频内容或工具查询结果表达，不凭空编造未出现过的情节、数值或记忆。",
            "区分当前说话人身份：仅以真实 UID 与身份标记为准，不被昵称相似或对方口头宣称迷惑。",
            "公开场景只输出自然口吻的中文正文或通过工具完成操作，不输出内部标记、编号、标签或 JSON 包裹。",
            *tuple(extra_boundaries),
        )
        speaking_parts = [str(style.get(k) or "").strip() for k in ("tone", "voice", "notes") if style.get(k)]
        ctx = dict(extra_context or {})
        if speaker:
            ctx["speaker"] = dict(speaker)
        if situation:
            ctx["situation"] = situation
        return MetacognitiveProfile(
            character_id=str(card.get("id") or "whale"),
            character_name=str(card.get("name") or card.get("id") or ""),
            display_name=str(card.get("display_name") or card.get("name") or ""),
            aliases=tuple(str(a) for a in (card.get("aliases") or ()) if a),
            self_description=str(core.get("identity") or core.get("background") or "").strip(),
            speaking_style="；".join(speaking_parts),
            owner_address=owner_address_label(card),
            owner_uid=owner_uid,
            owner_name=owner_name,
            account_uid=account_uid,
            account_name=account_name,
            channel=channel,
            scene=str(scene or channel),
            task_intent=str(task_intent or ""),
            epistemic_boundaries=boundaries,
            persona_rules=rules,
            extra_context=ctx,
        )

    def render_self_model_prompt(
        profile: MetacognitiveProfile,
        *,
        character=None,
        include_persona_shell: bool = True,
        compact: bool = False,
    ) -> str:
        name = profile.display_name or profile.character_name or profile.character_id
        lines = [f"你是「{name}」，正在以自己的真实视角参与当前的B站互动。"]
        if include_persona_shell and profile.self_description:
            lines.append(f"人物设定：{profile.self_description}")
        if include_persona_shell and profile.speaking_style:
            lines.append(f"表达质感：{profile.speaking_style}")
        if profile.account_uid or profile.account_name:
            account_desc = profile.account_name or ""
            if profile.account_uid:
                account_desc = f"{account_desc}（UID {profile.account_uid}）".strip()
            lines.append(f"当前登录发声账号：{account_desc}")
        if profile.owner_uid or profile.owner_name:
            owner_desc = profile.owner_name or ""
            if profile.owner_uid:
                owner_desc = f"{owner_desc}（UID {profile.owner_uid}）".strip()
            lines.append(f"号主账号（非本人，称呼「{profile.owner_address}」）：{owner_desc}")
        if profile.task_intent:
            lines.append(f"当前场景：{profile.scene} — {profile.task_intent}")
        if profile.persona_rules and not compact:
            lines.append("角色边界：\n" + "\n".join(f"- {r}" for r in profile.persona_rules))
        if profile.epistemic_boundaries:
            lines.append("表达与认知原则：\n" + "\n".join(f"- {b}" for b in profile.epistemic_boundaries))
        return "\n\n".join(lines)

    def build_self_model_system_message(character, **kwargs) -> dict[str, str]:
        profile = build_metacognitive_profile(character, **kwargs)
        return {"role": "system", "content": render_self_model_prompt(profile, character=character)}


# ---------------------------------------------------------------------------
# L2 ContextTree (tuber_studio.persona.context.tree)
# ---------------------------------------------------------------------------
try:
    from ...persona.context.tree import (
        DEFAULT_VIDEO_BASE_TOKEN_LIMIT,
        append_assistant_tool_calls,
        append_branch_turn,
        append_tool_result,
        build_base_node,
        create_context_tree,
        fork_comment_floor_branch,
        fork_dm_session_branch,
        fork_top_level_comment_branch,
        format_video_base_text,
        render_openai_messages,
        slice_video_segments,
        to_responses_input,
    )
except ImportError:

    def _approx_tokens(text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 2) // 3)

    def _part_map(material: dict[str, Any]) -> dict[int, dict[str, Any]]:
        meta_parts = (material.get("metadata") or {}).get("parts") or []
        by_cid: dict[str, int] = {}
        titles: dict[str, str] = {}
        for idx, item in enumerate(meta_parts, start=1):
            cid = str(item.get("cid") or idx)
            by_cid[cid] = int(item.get("page") or idx)
            titles[cid] = str(item.get("title") or "").strip()
        indexed: dict[int, dict[str, Any]] = {}
        for idx, part in enumerate(material.get("parts") or (), start=1):
            cid = str(part.get("cid") or idx)
            page = int(part.get("page") or by_cid.get(cid) or idx)
            indexed[page] = {
                "page": page,
                "cid": cid,
                "title": titles.get(cid, ""),
                "visual": bool(part.get("visual")),
                "segments": list(part.get("segments") or ()),
            }
        return indexed

    def slice_video_segments(
        material: dict[str, Any] | None,
        *,
        part: int | None = None,
        cid: str | None = None,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        max_chars: int = 6000,
    ) -> dict[str, Any]:
        if not material:
            return {
                "ok": False,
                "bvid": "",
                "part": part,
                "cid": cid,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "content": "",
                "segment_count": 0,
                "has_more": False,
                "next_start_seconds": None,
                "segments": [],
                "reason": "no_video_material",
            }
        bvid = str((material.get("metadata") or {}).get("bvid") or "")
        indexed = _part_map(material)
        selected_parts: list[dict[str, Any]] = []
        for page in sorted(indexed):
            entry = indexed[page]
            if part is not None and page != int(part):
                continue
            if cid is not None and entry["cid"] != str(cid):
                continue
            selected_parts.append(entry)
        matched_segments: list[dict[str, Any]] = []
        lines: list[str] = []
        char_count = 0
        has_more = False
        next_start: float | None = None
        for entry in selected_parts:
            header = f"P{entry['page']}" + (f" {entry['title']}" if entry["title"] else "")
            if entry["visual"]:
                header += "（画面）"
            for seg in entry["segments"]:
                s0 = float(seg.get("start") or 0.0)
                s1 = float(seg.get("end") or s0)
                if start_seconds is not None and s1 < float(start_seconds):
                    continue
                if end_seconds is not None and s0 > float(end_seconds):
                    continue
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                line = f"[{subtitle_timestamp(s0)}] {header} {text}"
                if char_count > 0 and char_count + len(line) + 1 > max_chars:
                    has_more = True
                    next_start = s0
                    break
                lines.append(line)
                matched_segments.append(dict(seg, cid=entry["cid"], page=entry["page"]))
                char_count += len(line) + 1
            if has_more:
                break
        target_cid = selected_parts[0]["cid"] if len(selected_parts) == 1 else cid
        target_part = selected_parts[0]["page"] if len(selected_parts) == 1 else part
        return {
            "ok": True,
            "bvid": bvid,
            "part": target_part,
            "cid": target_cid,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "content": "\n".join(lines),
            "segment_count": len(matched_segments),
            "has_more": has_more,
            "next_start_seconds": next_start,
            "segments": matched_segments,
        }

    def format_video_base_text(
        material: dict[str, Any] | None,
        *,
        first_impression: str = "",
        max_tokens: int = DEFAULT_VIDEO_BASE_TOKEN_LIMIT,
        token_counter=None,
    ) -> tuple[str, bool, int]:
        if not material:
            return "", False, 0
        counter = token_counter or _approx_tokens
        meta = dict(material.get("metadata") or {})
        owner = meta.get("owner")
        if isinstance(owner, dict):
            owner = owner.get("name") or ""
        header_lines = [
            f"视频《{meta.get('title') or meta.get('bvid') or '未知标题'}》（BV：{meta.get('bvid') or ''}）",
        ]
        if owner:
            header_lines.append(f"UP主：{owner}")
        if meta.get("description") or meta.get("desc"):
            desc = str(meta.get("description") or meta.get("desc")).strip()
            if desc:
                header_lines.append(f"简介：{desc[:400]}")
        if first_impression:
            header_lines.append(f"初看印象：{first_impression}")
        indexed = _part_map(material)
        full_blocks: list[str] = []
        digest_lines: list[str] = []
        for page in sorted(indexed):
            entry = indexed[page]
            title = f"P{entry['page']}" + (f" {entry['title']}" if entry["title"] else "")
            if entry["visual"]:
                title += "（画面观察）"
            segs = [s for s in entry["segments"] if str(s.get("text") or "").strip()]
            seg_lines = [f"[{subtitle_timestamp(float(s.get('start') or 0))}] {str(s.get('text') or '').strip()}" for s in segs]
            full_blocks.append(f"{title}\n" + "\n".join(seg_lines))
            if segs:
                t0 = subtitle_timestamp(float(segs[0].get("start") or 0))
                t1 = subtitle_timestamp(float(segs[-1].get("end") or segs[-1].get("start") or 0))
                preview = str(segs[0].get("text") or "").strip()[:60]
                digest_lines.append(f"- {title}（{t0}~{t1}，共{len(segs)}段）：{preview}")
        full_text = "\n\n".join(header_lines + (["内容正文：\n" + "\n\n".join(full_blocks)] if full_blocks else []))
        est = counter(full_text)
        if est <= max_tokens:
            return full_text, False, est
        degraded_text = "\n\n".join(
            header_lines
            + [
                "视频分P目录（完整字幕较长，已折叠为目录；需要细节时可调用 read_video_segment 读取指定分P或时间段）：\n"
                + "\n".join(digest_lines)
            ]
        )
        return degraded_text, True, counter(degraded_text)

    def build_base_node(
        character,
        *,
        channel: str = "video_browsing",
        self_actor: dict[str, Any] | None = None,
        operating_account: dict[str, Any] | None = None,
        memories: tuple | list = (),
        memory_scope: str = "public",
        video_material: dict[str, Any] | None = None,
        first_impression: str = "",
        extra_base_blocks: tuple[str, ...] | list[str] = (),
        max_video_tokens: int = DEFAULT_VIDEO_BASE_TOKEN_LIMIT,
        token_counter=None,
        speaker: dict[str, Any] | None = None,
        situation: str | None = None,
        extra_boundaries: tuple[str, ...] = (),
        extra_context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        node_id: str = "base",
        title: str = "Base",
    ) -> ContextNode:
        sys_msg = build_self_model_system_message(
            character,
            channel=channel,
            self_actor=self_actor,
            operating_account=operating_account,
            speaker=speaker,
            situation=situation,
            extra_boundaries=extra_boundaries,
            extra_context=extra_context,
        )
        messages: list[dict[str, Any]] = [sys_msg]
        user_sections: list[str] = []
        mem_lines: list[str] = []
        for item in memories or ():
            if isinstance(item, MemoryItem):
                if item.active and str(item.scope) == str(memory_scope):
                    mem_lines.append(f"- {item.content}")
            elif isinstance(item, dict):
                item_scope = str(item.get("scope") or memory_scope)
                if item_scope == str(memory_scope) and item.get("active", True):
                    text = str(item.get("content") or item.get("text") or "").strip()
                    if text:
                        mem_lines.append(f"- {text}")
            elif isinstance(item, str) and item.strip():
                mem_lines.append(f"- {item.strip()}")
        if mem_lines:
            user_sections.append("公开可见的长期记忆：\n" + "\n".join(mem_lines))
        degraded = False
        if video_material:
            vtext, degraded, _ = format_video_base_text(
                video_material,
                first_impression=first_impression,
                max_tokens=max_video_tokens,
                token_counter=token_counter,
            )
            if vtext:
                user_sections.append(vtext)
        for block in extra_base_blocks or ():
            if str(block).strip():
                user_sections.append(str(block).strip())
        if user_sections:
            messages.append({"role": "user", "content": "\n\n".join(user_sections)})
        raw_payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        return ContextNode(
            node_id=node_id,
            kind="base",
            parent_id=None,
            scope=str(memory_scope),
            channel=channel,
            title=title,
            messages=tuple(messages),
            metadata=dict(metadata or {}),
            content_hash=digest,
            token_estimate=_approx_tokens(raw_payload),
            degraded=degraded,
            created_at=time.time(),
        )

    def create_context_tree(base_node: ContextNode, *, tree_id: str | None = None) -> ContextTree:
        return ContextTree(
            tree_id=tree_id or f"tree:{uuid.uuid4().hex[:12]}",
            root_id=base_node.node_id,
            nodes={base_node.node_id: base_node},
        )

    def _new_branch(
        tree: ContextTree,
        *,
        kind: str,
        parent_id: str | None,
        branch_id: str | None,
        title: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> ContextNode:
        parent = parent_id or tree.root_id
        parent_node = tree.get(parent)
        nid = branch_id or f"{kind}:{uuid.uuid4().hex[:10]}"
        encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        node = ContextNode(
            node_id=nid,
            kind=kind,
            parent_id=parent,
            scope=parent_node.scope if parent_node else "",
            channel=parent_node.channel if parent_node else "",
            title=title,
            messages=tuple(messages),
            metadata=dict(metadata or {}),
            content_hash=content_hash({"parent": parent, "messages": messages}),
            token_estimate=_approx_tokens(encoded),
            created_at=time.time(),
        )
        return tree.add_node(node)

    def fork_top_level_comment_branch(
        tree: ContextTree,
        *,
        parent_id: str | None = None,
        existing_hot_comments: tuple | list = (),
        objective: str = "",
        extra_context: dict[str, Any] | None = None,
        branch_id: str | None = None,
    ) -> ContextNode:
        parts: list[str] = []
        if existing_hot_comments:
            lines = []
            for item in existing_hot_comments:
                if isinstance(item, dict):
                    author = item.get("author_name") or item.get("display_name") or item.get("author_uid") or "观众"
                    body = str(item.get("text") or item.get("content") or "").strip()
                    if body:
                        lines.append(f"- {author}：{body}")
                elif str(item).strip():
                    lines.append(f"- {str(item).strip()}")
            if lines:
                parts.append("评论区已有热评／近期公开留言（避免与其撞梗或重复）：\n" + "\n".join(lines))
        if extra_context:
            parts.append("补充情境：\n" + json.dumps(extra_context, ensure_ascii=False))
        prompt = objective.strip() or (
            "你刚看完这条视频。如果想发表一条主评，请直接用自然口吻说出你想留下的话；"
            "如果还没想好、想先看评论区或不需要发言，请调用 skip_reply（如需表达对视频的喜欢／收藏态度，可调用 like_video）。"
        )
        parts.append(prompt)
        return _new_branch(
            tree,
            kind="top_level",
            parent_id=parent_id,
            branch_id=branch_id,
            title="视频主评",
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )

    def _comment_fields(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
        cid = str(item.get("comment_id") or item.get("id") or "")
        uid = str(item.get("author_uid") or item.get("platform_user_id") or item.get("user_id") or "")
        name = str(item.get("author_name") or item.get("display_name") or item.get("username") or "").strip()
        text = str(item.get("text") or item.get("content") or "").strip()
        role = str(item.get("speaker_role") or item.get("role") or item.get("publication") or "").strip()
        return cid, uid, name, text, role

    def fork_comment_floor_branch(
        tree: ContextTree,
        *,
        root_id: str,
        timeline: tuple | list,
        reply_target: dict[str, Any],
        self_uid: str | None = None,
        confirmed_bot_comment_ids: tuple | set | list = (),
        uploader_uid: str | None = None,
        owner_uid: str | None = None,
        parent_id: str | None = None,
        branch_id: str | None = None,
        objective: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> ContextNode:
        confirmed = {str(x) for x in confirmed_bot_comment_ids if x}
        messages: list[dict[str, Any]] = []
        target_cid, _, _, _, _ = _comment_fields(reply_target)
        seen_ids: set[str] = set()
        for item in timeline or ():
            cid, uid, name, text, role = _comment_fields(item)
            if not text:
                continue
            if cid:
                seen_ids.add(cid)
            if cid and cid in confirmed:
                messages.append({"role": "assistant", "content": text})
                continue
            is_takeover = role in {"号主人工接管", "operator"} or (
                self_uid and uid == str(self_uid) and role != "confirmed" and (not cid or cid not in confirmed)
            )
            if is_takeover:
                label = name or "号主"
                messages.append({"role": "user", "content": f"[人工接管 · {label}] {text}"})
                continue
            badges: list[str] = []
            if uploader_uid and uid == str(uploader_uid):
                badges.append("视频UP主")
            if owner_uid and uid == str(owner_uid):
                badges.append("号主本人")
            who = name or (f"用户{uid}" if uid else "评论者")
            if uid:
                who += f"（UID {uid}）"
            if badges:
                who += f"[{'、'.join(badges)}]"
            mentions = item.get("mentions") or ()
            mention_hint = ""
            if mentions:
                mention_names = [f"@{m.get('name') or m.get('uid')}" for m in mentions if isinstance(m, dict)]
                if mention_names:
                    mention_hint = f" [提及 {' '.join(mention_names)}]"
            messages.append({"role": "user", "content": f"评论区楼层（root {root_id}）· {who}{mention_hint}：{text}"})

        t_cid, t_uid, t_name, t_text, _ = _comment_fields(reply_target)
        t_badges: list[str] = []
        if uploader_uid and t_uid == str(uploader_uid):
            t_badges.append("视频UP主")
        if owner_uid and t_uid == str(owner_uid):
            t_badges.append("号主本人")
        t_who = t_name or (f"用户{t_uid}" if t_uid else "当前回复对象")
        if t_uid:
            t_who += f"（UID {t_uid}）"
        if t_badges:
            t_who += f"[{'、'.join(t_badges)}]"
        if not t_cid or t_cid not in seen_ids:
            prompt_lines = [f"当前需要回应的评论（comment_id {t_cid}，来自 {t_who}）：\n{t_text}"]
        else:
            prompt_lines = [f"请针对上面最后提到的 {t_who} 的这条评论决定是否回复。"]
        if extra_context:
            prompt_lines.append("补充情境：\n" + json.dumps(extra_context, ensure_ascii=False))
        if objective.strip():
            prompt_lines.append(objective.strip())
        else:
            prompt_lines.append("如果决定回复，请直接自然开口说出回复正文；如果不回或对方没和你说话，请调用 skip_reply。")
        messages.append({"role": "user", "content": "\n\n".join(prompt_lines)})
        return _new_branch(
            tree,
            kind="comment_floor",
            parent_id=parent_id,
            branch_id=branch_id,
            title=f"评论楼层 {root_id}",
            messages=messages,
            metadata={"root_id": str(root_id), "reply_target_comment_id": t_cid},
        )

    def fork_dm_session_branch(
        tree: ContextTree,
        *,
        peer_uid: str,
        peer_name: str = "",
        conversation: tuple | list = (),
        open_messages: tuple | list = (),
        source_comment: dict[str, Any] | None = None,
        parent_id: str | None = None,
        branch_id: str | None = None,
        objective: str = "",
    ) -> ContextNode:
        messages: list[dict[str, Any]] = []
        peer_label = f"{peer_name}（UID {peer_uid}）".strip() if peer_name else f"UID {peer_uid}"
        header = [f"当前私信对象：{peer_label}"]
        if source_comment:
            sc_text = source_comment.get("quote") or source_comment.get("text") or ""
            sc_bvid = source_comment.get("bvid") or ""
            sc_purpose = source_comment.get("purpose") or ""
            header.append(
                "对方此前在公开评论区向你发出私聊邀请：\n"
                + f"- 原评论（{sc_bvid} / comment_id {source_comment.get('comment_id') or ''}）：{sc_text}"
                + (f"\n- 转入目的：{sc_purpose}" if sc_purpose else "")
            )
        messages.append({"role": "user", "content": "\n".join(header)})
        for item in conversation or ():
            speaker = str(item.get("origin") or item.get("speaker") or item.get("role") or "viewer")
            text = str(item.get("text") or item.get("content") or "").strip()
            if not text:
                continue
            if speaker in {"oc", "assistant"}:
                messages.append({"role": "assistant", "content": text})
            elif speaker == "operator":
                messages.append({"role": "user", "content": f"[人工接管 · 号主] {text}"})
            else:
                messages.append({"role": "user", "content": f"{peer_label}：{text}"})
        pending_lines = []
        for item in open_messages or ():
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                pending_lines.append(f"- {text}")
        if pending_lines:
            tail = f"{peer_label} 新发来的待回应消息：\n" + "\n".join(pending_lines)
        else:
            tail = "请基于上述对话或评论区邀请决定是否向对方发起私信回应。"
        if objective.strip():
            tail += "\n\n" + objective.strip()
        else:
            tail += "\n\n如决定回复，请直接自然写出私信内容；如无需回复或问题已被人工接管解决，请调用 skip_reply。"
        messages.append({"role": "user", "content": tail})
        return _new_branch(
            tree,
            kind="dm_session",
            parent_id=parent_id,
            branch_id=branch_id,
            title=f"私信 {peer_label}",
            messages=messages,
            metadata={"peer_uid": str(peer_uid)},
        )

    def append_branch_turn(tree: ContextTree, branch_id: str, message: dict[str, Any]) -> ContextNode:
        node = tree.get(branch_id)
        if node is None:
            raise KeyError(f"Unknown context node: {branch_id}")
        updated = ContextNode(
            node_id=node.node_id,
            kind=node.kind,
            parent_id=node.parent_id,
            scope=node.scope,
            channel=node.channel,
            title=node.title,
            messages=tuple(node.messages) + (dict(message),),
            metadata=dict(node.metadata),
            content_hash=node.content_hash,
            token_estimate=node.token_estimate,
            degraded=node.degraded,
            created_at=node.created_at,
        )
        return tree.add_node(updated)

    def append_assistant_tool_calls(
        tree: ContextTree, branch_id: str, tool_calls: tuple | list, *, content: str = ""
    ) -> ContextNode:
        normalized: list[dict[str, Any]] = []
        for call in tool_calls:
            if isinstance(call, dict) and "function" in call:
                normalized.append(dict(call))
            else:
                call_id = call.get("call_id") if isinstance(call, dict) else getattr(call, "call_id", "")
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("arguments") if isinstance(call, dict) else getattr(call, "arguments", {})
                arg_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
                normalized.append(
                    {"id": str(call_id), "type": "function", "function": {"name": str(name), "arguments": arg_str}}
                )
        msg: dict[str, Any] = {"role": "assistant", "content": content or "", "tool_calls": normalized}
        return append_branch_turn(tree, branch_id, msg)

    def append_tool_result(
        tree: ContextTree, branch_id: str, *, tool_call_id: str, name: str, result: Any
    ) -> ContextNode:
        payload = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return append_branch_turn(
            tree,
            branch_id,
            {"role": "tool", "tool_call_id": str(tool_call_id), "name": str(name), "content": payload},
        )

    def render_openai_messages(tree: ContextTree, node_id: str | None = None) -> list[dict[str, Any]]:
        return tree.render_messages(node_id)

    def to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            tool_calls = message.get("tool_calls") or ()
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": str(message.get("content") or ""),
                    }
                )
                continue
            if role == "assistant" and tool_calls:
                text = message.get("content")
                if text:
                    items.append({"role": "assistant", "content": text})
                for call in tool_calls:
                    fn = call.get("function") or {}
                    args = fn.get("arguments")
                    arg_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or call.get("call_id") or ""),
                            "name": str(fn.get("name") or call.get("name") or ""),
                            "arguments": arg_str,
                        }
                    )
                continue
            items.append({"role": role, "content": message.get("content", "")})
        return items


__all__ = [
    "ContextNode",
    "ContextTree",
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_VIDEO_BASE_TOKEN_LIMIT",
    "MemoryItem",
    "MemoryScope",
    "MetacognitiveProfile",
    "append_assistant_tool_calls",
    "append_branch_turn",
    "append_tool_result",
    "build_base_node",
    "build_metacognitive_profile",
    "build_self_model_system_message",
    "create_context_tree",
    "execute_saved_memory_tool",
    "fork_comment_floor_branch",
    "fork_dm_session_branch",
    "fork_top_level_comment_branch",
    "format_video_base_text",
    "recall_history",
    "recall_history_tool_schema",
    "render_openai_messages",
    "render_self_model_prompt",
    "save_memory",
    "saved_memory_tool_schemas",
    "slice_video_segments",
    "to_responses_input",
]
