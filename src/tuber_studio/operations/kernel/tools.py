"""Native OpenAI function tool schemas, boundary executors, and speech post-guard."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...core.text import validate_comment_body, validate_operation_text
from ...infra.web_client.search import search_public_web
from .l2_bridge import (
    MemoryScope,
    execute_saved_memory_tool,
    recall_history,
    recall_history_tool_schema,
    saved_memory_tool_schemas,
    slice_video_segments,
)

ALL_OPERATION_TOOL_NAMES = (
    "search_web",
    "inspect_video_frames",
    "read_video_segment",
    "save_memory",
    "recall_history",
    "like_video",
    "skip_reply",
    "queue_dm_handoff",
)

_INTERNAL_TAG_RE = re.compile(
    r"<(?:think|thought|tool_call|analysis)>.*?</(?:think|thought|tool_call|analysis)>|<\|[^|>]+\|>",
    re.DOTALL,
)
_BRACKET_TAG_RE = re.compile(r"(?:[（(]?\s*指\s*ref\s*:\s*\d+\s*[)）]?|\[(?:ref:\d+|e\d+(?:\|[^\]]*)?)\])", re.IGNORECASE)
_WORD_REF_RE = re.compile(r"(?<![A-Za-z0-9_])ref:\d+(?![A-Za-z0-9_])", re.IGNORECASE)
_FORBIDDEN_WRAPPER_KEYS = frozenset({"action", "evidence_ids", "text_ref", "quote_ref", "public_text_ref", "source_ids"})


def sanitize_spoken_text(text: str | None) -> str:
    """Strip accidental model reasoning or chat control tokens from plain-text speech."""
    cleaned = _INTERNAL_TAG_RE.sub("", str(text or "")).strip()
    return cleaned


def detect_internal_protocol_leak(text: str) -> str | None:
    """Reject internal citation ceremony or JSON-wrapped speech in final plain text."""
    stripped = str(text or "").strip()
    if not stripped:
        return None
    if _BRACKET_TAG_RE.search(stripped) or _WORD_REF_RE.search(stripped):
        return "正文不能包含 [ref:N]、ref:N 或 [eN] 编号"
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and (_FORBIDDEN_WRAPPER_KEYS & parsed.keys() or set(parsed.keys()) <= {"text", "content", "reason"}):
            return "正文不能是 JSON 包裹结构，请直接说出自然话语"
    return None


def validate_spoken_output(text: str, *, kind: str = "reply_public", max_length: int | None = None) -> str:
    """Apply lightweight post-guard and core text safety checks to plain-text speech."""
    cleaned = sanitize_spoken_text(text)
    if leak := detect_internal_protocol_leak(cleaned):
        raise ValueError(leak)
    validated = (
        validate_comment_body(cleaned)
        if kind in {"comments", "comment_reply", "reply_public", "top_level"}
        else validate_operation_text(cleaned)
    )
    if max_length is not None and len(validated) > max_length:
        raise ValueError(f"正文超过长度上限（最多{max_length}字）")
    return validated


def _response_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "name": name, "description": description, "parameters": parameters}


def operation_tool_definitions(
    *,
    format: str = "responses",
    include: tuple[str, ...] | list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build native tool definitions for search, video inspection, memory, and control."""
    allowed = set(include) if include is not None else set(ALL_OPERATION_TOOL_NAMES)
    tools: list[dict[str, Any]] = []

    if "search_web" in allowed:
        tools.append(
            _response_tool(
                "search_web",
                "联网搜索公开网页资料，核实不确定的外部知识、版本号、人物或事件事实。",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "简短精准的公开搜索关键词。"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        )
    if "inspect_video_frames" in allowed:
        tools.append(
            _response_tool(
                "inspect_video_frames",
                "查看视频指定分P和时间区间的真实画面帧，确认字幕没说清的画面细节、数字或操作。",
                {
                    "type": "object",
                    "properties": {
                        "part": {"type": ["integer", "null"], "description": "分P序号（从1开始），单P可传null或1。"},
                        "start_seconds": {"type": "number", "description": "起始秒数。"},
                        "end_seconds": {"type": "number", "description": "结束秒数。"},
                        "reason": {"type": "string", "description": "想看这段画面的具体问题。"},
                    },
                    "required": ["start_seconds", "end_seconds", "reason"],
                    "additionalProperties": False,
                },
            )
        )
    if "read_video_segment" in allowed:
        tools.append(
            _response_tool(
                "read_video_segment",
                "读取视频指定分P或时间范围内的详细字幕／文稿片段，支持长视频分段精读。",
                {
                    "type": "object",
                    "properties": {
                        "part": {"type": ["integer", "null"], "description": "分P序号（从1开始）。"},
                        "start_seconds": {"type": ["number", "null"], "description": "可选起始秒数。"},
                        "end_seconds": {"type": ["number", "null"], "description": "可选结束秒数。"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            )
        )
    if "save_memory" in allowed:
        tools.extend(saved_memory_tool_schemas(format="responses", allow_save=True, allow_update=False, allow_delete=False))
    if "recall_history" in allowed:
        tools.append(recall_history_tool_schema(format="responses"))
    if "like_video" in allowed:
        tools.append(
            _response_tool(
                "like_video",
                "表达对当前视频的真实喜欢／收藏态度（与是否留言评论相互独立）。",
                {
                    "type": "object",
                    "properties": {
                        "like": {"type": "boolean", "description": "是否点赞这个视频。"},
                        "favorite": {"type": "boolean", "description": "是否特别值得收藏（标准更高）。"},
                        "reason": {"type": "string", "description": "简短真实的喜欢或判断理由。"},
                    },
                    "required": ["like", "favorite", "reason"],
                    "additionalProperties": False,
                },
            )
        )
    if "skip_reply" in allowed:
        tools.append(
            _response_tool(
                "skip_reply",
                "明确选择本次不发言、不回复或暂时跳过（结束本次而不输出公开／私信文本）。",
                {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "简洁原因码，例如 no_interest / addressed_other / cooldown / handled / duplicate。",
                        },
                        "explanation": {"type": "string", "description": "简短内部原因说明。"},
                    },
                    "required": ["reason", "explanation"],
                    "additionalProperties": False,
                },
            )
        )
    if "queue_dm_handoff" in allowed:
        tools.append(
            _response_tool(
                "queue_dm_handoff",
                "仅当当前评论存在对方对本账号的明确私聊邀请时，将原话与收件人交由私信模块接手处理。",
                {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "为何转入私信处理。"},
                        "summary": {"type": "string", "description": "私信需要承接的话题摘要。"},
                    },
                    "required": ["reason", "summary"],
                    "additionalProperties": False,
                },
            )
        )
    if format == "chat":
        return [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "parameters": item.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for item in tools
        ]
    return tools


@dataclass
class OperationToolSession:
    """Boundary-enforced executor and provenance recorder for one operations turn loop."""

    channel: str
    character: dict[str, Any]
    account_id: str
    store: Any = None
    unified_memory: Any = None
    settings: Any = None
    material: dict[str, Any] | None = None
    self_uid: str | None = None
    resource: str | None = None
    root_id: str | None = None
    video_engagement_enabled: bool = False
    visual_allowed: bool = False
    web_search_allowed: bool = True
    handoff_eligibility: dict[str, Any] = field(default_factory=dict)
    search_fn: Callable[[str], Awaitable[Any]] | None = None
    inspect_frames_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    provenance: list[str] = field(default_factory=list)
    read_segments: list[dict[str, Any]] = field(default_factory=list)
    inspected_frames: list[dict[str, Any]] = field(default_factory=list)
    web_results: list[dict[str, Any]] = field(default_factory=list)
    video_engagement: dict[str, Any] | None = None
    skipped: dict[str, str] | None = None
    handoff_queued: dict[str, Any] | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)

    def add_provenance(self, identifier: str) -> None:
        item = str(identifier or "").strip()
        if item and item not in self.provenance:
            self.provenance.append(item)

    def _character_id(self) -> str:
        return str((self.character or {}).get("id") or "")

    async def execute(self, name: str, raw_arguments: dict[str, Any] | str) -> dict[str, Any]:
        args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments or {})
        result = await self._dispatch(name, args)
        self.tool_events.append({"name": name, "arguments": args, "ok": bool(result.get("ok", True))})
        return result

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_web":
            return await self._search_web(args)
        if name == "inspect_video_frames":
            return await self._inspect_video_frames(args)
        if name == "read_video_segment":
            return self._read_video_segment(args)
        if name == "save_memory":
            return self._save_memory(args)
        if name == "recall_history":
            return self._recall_history(args)
        if name == "like_video":
            return self._like_video(args)
        if name == "skip_reply":
            return self._skip_reply(args)
        if name == "queue_dm_handoff":
            return self._queue_dm_handoff(args)
        return {"ok": False, "reason": "unknown_tool", "message": f"未知工具：{name}"}

    async def _search_web(self, args: dict[str, Any]) -> dict[str, Any]:
        query = " ".join(str(args.get("query") or "").split())
        if not query:
            return {"ok": False, "reason": "empty_query", "results": []}
        if not self.web_search_allowed:
            return {"ok": False, "reason": "web_search_disabled", "message": "当前设置未启用联网搜索。"}
        if self.search_fn is not None:
            raw = await self.search_fn(query)
        else:
            if self.settings is None:
                return {"ok": False, "reason": "web_search_unavailable", "message": "联网搜索配置不可用。"}
            raw = await search_public_web(self.settings, query)
        items = list(raw.get("results") if isinstance(raw, dict) else (raw or ()))
        normalized = []
        for entry in items[:8]:
            if isinstance(entry, dict):
                url = str(entry.get("url") or entry.get("link") or "")
                title = str(entry.get("title") or "")
                snippet = str(entry.get("snippet") or entry.get("summary") or entry.get("content") or "")
                if url:
                    self.add_provenance("web:" + url)
                normalized.append({"title": title, "url": url, "snippet": snippet[:400]})
        self.web_results.extend(normalized)
        return {"ok": True, "query": query, "results": normalized, "count": len(normalized)}

    async def _inspect_video_frames(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.material:
            return {"ok": False, "reason": "no_video_material", "message": "当前场景没有可查看的视频材料。"}
        if not self.visual_allowed or self.inspect_frames_fn is None:
            return {"ok": False, "reason": "visual_unavailable", "message": "当前不可取帧，请结合已有字幕或评论处理。"}
        request = {
            "part": args.get("part"),
            "start_seconds": float(args.get("start_seconds") or 0.0),
            "end_seconds": float(args.get("end_seconds") or 0.0),
            "reason": str(args.get("reason") or "").strip(),
        }
        res = await self.inspect_frames_fn(request)
        if not isinstance(res, dict):
            return {"ok": False, "reason": "visual_failed", "message": "取帧返回格式无效。"}
        for seg in res.get("segments") or ():
            if isinstance(seg, dict) and seg.get("id"):
                self.add_provenance(str(seg["id"]))
                self.inspected_frames.append(seg)
        return res

    def _read_video_segment(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.material:
            return {"ok": False, "reason": "no_video_material", "message": "当前场景没有可读取的视频字幕。"}
        res = slice_video_segments(
            self.material,
            part=args.get("part"),
            start_seconds=args.get("start_seconds"),
            end_seconds=args.get("end_seconds"),
        )
        raw_segments = res.get("segments") or []
        for seg in raw_segments:
            if isinstance(seg, dict) and seg.get("id"):
                self.add_provenance(str(seg["id"]))
                self.read_segments.append(seg)
        return {
            "ok": bool(res.get("ok", True)),
            "bvid": res.get("bvid", ""),
            "part": res.get("part"),
            "start_seconds": res.get("start_seconds"),
            "end_seconds": res.get("end_seconds"),
            "content": res.get("content", ""),
            "segment_count": int(res.get("segment_count") or len(raw_segments)),
            "has_more": bool(res.get("has_more")),
            "next_start_seconds": res.get("next_start_seconds"),
        }

    def _save_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.unified_memory is None:
            return {"ok": False, "action": "rejected", "reason": "memory_store_unavailable"}
        # Hard Python isolation: operations tools may ONLY ever write PUBLIC scope.
        return execute_saved_memory_tool(
            self.unified_memory,
            "save_memory",
            {"content": str(args.get("content") or ""), "tags": list(args.get("tags") or ())},
            scope=MemoryScope.PUBLIC,
            character_id=self._character_id(),
            source=self.channel,
            allowed_scopes=(MemoryScope.PUBLIC,),
        )

    def _recall_history(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            return {"ok": True, "scope": str(MemoryScope.PUBLIC), "items": [], "count": 0}
        # Hard Python isolation: never read OWNER scope or operator chat turns from operations tools.
        return recall_history(
            self.store,
            query=str(args.get("query") or ""),
            scope=MemoryScope.PUBLIC,
            character_id=self._character_id(),
            account_id=str(self.account_id or ""),
            days=int(args.get("days") or 14),
            limit=int(args.get("limit") or 8),
            private_owner=False,
            resource=args.get("resource") or self.resource,
            root_id=args.get("root_id") or self.root_id,
        )

    def _like_video(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.video_engagement_enabled:
            return {"ok": False, "reason": "not_video_task", "message": "当前非视频浏览场景，不能对视频执行赞藏。"}
        like = bool(args.get("like"))
        favorite = bool(args.get("favorite"))
        reason = str(args.get("reason") or "").strip()[:1024]
        self.video_engagement = {"should_like": like, "should_favorite": favorite, "reason": reason}
        return {"ok": True, "should_like": like, "should_favorite": favorite, "reason": reason}

    def _skip_reply(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = str(args.get("reason") or "skip").strip() or "skip"
        explanation = str(args.get("explanation") or "").strip()[:1024]
        self.skipped = {"reason": reason, "explanation": explanation}
        return {"ok": True, "action": "skip", "reason": reason}

    def _queue_dm_handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        eligibility = dict(self.handoff_eligibility or {})
        if not eligibility.get("eligible"):
            return {
                "ok": False,
                "reason": eligibility.get("blocked_reason") or "handoff_ineligible",
                "message": eligibility.get("message") or "当前评论没有经过核实的对本账号明确私信邀请，或暂不可转入私信；请直接在公开区回应或跳过。",
            }
        # Always bind original recipient UID and verbatim invitation quote inside Python boundaries.
        self.handoff_queued = {
            "reason": str(args.get("reason") or "confirmed_invitation").strip()[:240],
            "summary": str(args.get("summary") or "").strip()[:500],
            "recipient_uid": str(eligibility.get("recipient_uid") or ""),
            "source_comment_id": str(eligibility.get("source_comment_id") or ""),
            "source_quote": str(eligibility.get("source_quote") or ""),
        }
        return {
            "ok": True,
            "action": "queue_dm_handoff",
            "recipient_uid": self.handoff_queued["recipient_uid"],
            "source_comment_id": self.handoff_queued["source_comment_id"],
        }
