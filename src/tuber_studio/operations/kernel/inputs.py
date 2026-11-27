"""Compact model input and invocation-local, typed evidence bindings."""

import hashlib
import json
import re
import shlex
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from ...core.text import verified_comment_quote
from ...persona.context.subtitles import (
    CitationBlock,
    SourcePart,
    SourceSegment,
    paragraph_sources,
    validate_time,
)
from .caption_corrections import corrected_caption
from .generation import hold_comment_body
from .models import MAX_DECISION_REASON_CHARACTERS, ContentDecision, ResponseDecision

RESPONSE_ACTOR_INSTRUCTION = (
    "self_actor是你的账号。addressed_to列出原话所指的人。接reply_target里的话，楼内对话和视频用作背景。"
    "分清self_actor（你自己）、video_source的UP主、reply_target当前说话人及第三方或二创/官方角色，绝不把别人当成你自己。"
)


def bind_response_actors(data, self_actor):
    """Describe actors without changing original source/citation identities."""
    result = deepcopy(data)
    result["self_actor"] = deepcopy(self_actor)
    own_uid = str(self_actor.get("account_uid") or "")

    def relation(uid):
        uid = str(uid or "")
        return "unknown" if not uid or not own_uid else "self" if uid == own_uid else "other"

    for key in ("video_source", "dynamic_source"):
        if isinstance(result.get(key), dict):
            source = result[key]
            source["uploader_relation_to_self"] = relation(source.get("owner_uid"))

    thread = result.get("thread")
    roles = {
        "self_account": {
            "uid": own_uid or None,
            "name": self_actor.get("account_display_name") or self_actor.get("character_name") or "",
            "role": "你自己（当前账号）",
        }
    }
    if isinstance(result.get("video_source"), dict):
        video = result["video_source"]
        roles["video_author"] = {
            "uid": str(video.get("owner_uid") or "") or None,
            "name": video.get("owner") or "",
            "is_self": video.get("uploader_relation_to_self") == "self",
        }
    if not isinstance(thread, dict):
        result["actor_roles"] = roles
        return result
    source = thread.get("source") or {}
    addressing = thread.setdefault("addressing", {})
    addressing["source_relation_to_self"] = relation(source.get("author_uid"))
    addressed = {}

    def add(uid, name, basis):
        uid = str(uid or "")
        if not uid:
            return
        actor = addressed.setdefault(
            uid, {"uid": uid, "name": name or None, "relation_to_self": relation(uid), "basis": []}
        )
        if not actor["name"] and name:
            actor["name"] = name
        if basis not in actor["basis"]:
            actor["basis"].append(basis)

    mentions = addressing.get("source_mentions") or []
    for mention in mentions:
        if isinstance(mention, dict):
            add(mention.get("uid"), mention.get("name"), "mention")
    parent = thread.get("parent") or {}
    if parent and str(parent.get("comment_id") or "") != str(source.get("comment_id") or ""):
        add(parent.get("author_uid"), parent.get("author_name") or parent.get("display_name"), "reply_parent")
    known_names = {
        str(a.get("name") or "").strip().casefold()
        for a in addressed.values()
        if str(a.get("name") or "").strip()
    }
    self_display = str(self_actor.get("account_display_name") or "").strip().casefold()
    source_text = str(source.get("text") or "")
    for match in re.finditer(r"(?:^|[\s，,。！？!?:：；;(（\[【])@([A-Za-z0-9_\-\u4e00-\u9fff·]{1,40})", source_text):
        handle = match.group(1).strip()
        lowered = handle.casefold()
        if not handle or lowered in known_names or (self_display and lowered == self_display):
            continue
        key = f"plain:{lowered}"
        actor = addressed.setdefault(
            key, {"uid": None, "name": handle, "relation_to_self": "other", "basis": []}
        )
        if "plain_mention" not in actor["basis"]:
            actor["basis"].append("plain_mention")
        known_names.add(lowered)
    addressing["addressed_to"] = list(addressed.values())
    is_explicitly_mentioned = any(
        a.get("relation_to_self") == "self" and "mention" in a.get("basis", [])
        for a in addressed.values()
    )
    is_reply_to_self = any(
        a.get("relation_to_self") == "self" and "reply_parent" in a.get("basis", [])
        for a in addressed.values()
    )
    has_third_party = any(a.get("relation_to_self") == "other" for a in addressed.values())
    mentions_other = any(
        a["relation_to_self"] == "other" and ("mention" in a["basis"] or "plain_mention" in a["basis"])
        for a in addressed.values()
    )
    has_any_mention = bool(mentions) or any("plain_mention" in a.get("basis", []) for a in addressed.values())
    addressing["is_explicitly_mentioned"] = is_explicitly_mentioned
    addressing["is_reply_to_self"] = is_reply_to_self
    addressing["has_third_party_mention"] = has_third_party
    addressing["conversation_nature"] = (
        "direct_interaction_with_self"
        if (is_explicitly_mentioned or (is_reply_to_self and not has_any_mention))
        else "third_party_dialogue"
        if (mentions_other if has_any_mention else has_third_party)
        else "general_public_comment"
    )
    if source:
        result["reply_target"] = deepcopy(source) | {
            "relation_to_self": relation(source.get("author_uid")),
            "addressed_to": deepcopy(addressing["addressed_to"]),
        }
        roles["current_speaker"] = {
            "uid": str(source.get("author_uid") or "") or None,
            "name": source.get("author_name") or source.get("display_name") or "",
            "is_self": relation(source.get("author_uid")) == "self",
        }
    if parent and str(parent.get("comment_id") or "") != str(source.get("comment_id") or ""):
        roles["replying_to"] = {
            "comment_id": str(parent.get("comment_id") or "") or None,
            "uid": str(parent.get("author_uid") or "") or None,
            "name": parent.get("author_name") or parent.get("display_name") or "",
            "is_self": relation(parent.get("author_uid")) == "self",
        }
    result["actor_roles"] = roles
    for key in ("root", "parent"):
        if isinstance(thread.get(key), dict):
            thread[key]["relation_to_self"] = relation(thread[key].get("author_uid"))
    for key in ("timeline", "items"):
        for item in thread.get(key, []):
            if isinstance(item, dict):
                item["relation_to_self"] = relation(
                    item.get("author_uid") or item.get("platform_user_id") or item.get("user_id")
                )
    return result


def compact_actor_context(bound, current_reference=None, *, current_text_ref=None):
    """Keep actor and thread context readable without synthetic *_ref pointer ceremony."""
    result = deepcopy(bound)
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return result
    thread.pop("public_text", None)
    items = thread.pop("items", None)
    if isinstance(items, list) and items:
        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        for existing_key in ("source", "root", "parent"):
            row = thread.get(existing_key)
            if isinstance(row, dict):
                cid = row.get("comment_id") or row.get("id")
                if cid is not None:
                    seen_ids.add(str(cid))
                txt = row.get("text") if isinstance(row.get("text"), str) else row.get("content")
                if isinstance(txt, str) and txt:
                    seen_texts.add(txt)
        for seq_key in ("timeline", "grouped_comments", "comments"):
            for row in thread.get(seq_key) or ():
                if isinstance(row, dict):
                    cid = row.get("comment_id") or row.get("id")
                    if cid is not None:
                        seen_ids.add(str(cid))
                    txt = row.get("text") if isinstance(row.get("text"), str) else row.get("content")
                    if isinstance(txt, str) and txt:
                        seen_texts.add(txt)
        extras = []
        for row in items:
            if not isinstance(row, dict):
                continue
            cid = row.get("comment_id") or row.get("id")
            txt = row.get("text") if isinstance(row.get("text"), str) else row.get("content")
            if cid is not None and str(cid) in seen_ids:
                continue
            if isinstance(txt, str) and txt in seen_texts:
                continue
            if cid is not None:
                seen_ids.add(str(cid))
            if isinstance(txt, str) and txt:
                seen_texts.add(txt)
            extras.append(row)
        if extras:
            thread["comments"] = list(thread.get("comments") or ()) + extras
    return result


def format_content_input(data):
    """A small JSON context header followed by actual readable text lines."""
    # Segment/part tables are for backend schema and source bindings. Their
    # identifiers and headings already occur once in the readable body.
    header = {key: value for key, value in data.items() if key not in {"content", "segments", "parts"}}
    return json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n\n内容：\n" + data["content"]


CLASSIFICATION_INPUT_VERSION = "content-c6i-v2"
CLASSIFICATION_INSTRUCTION = (
    "explanation结合本条标题或材料写出这次行动的具体理由。"
    "按角色兴趣决定是否准备回应，本阶段不选公私信。interest=like表示想参与，worth_commenting表示有具体想说的话；吐槽、共情、轻互动与分享都算。"
    "reason：specific_detail有回应点；no_interest无兴趣；nothing_to_add读懂无补充；insufficient_evidence想回应的内容缺材料；visual_required想回应的内容需新画面；unsafe_interaction涉及骚扰、伤害他人或公开隐私而略过。"
    "各字段表达同一意图。准备回应：interest=like,worth_commenting=true,reason=specific_detail；想补读：interest=need_more,worth_commenting=false；不发言worth_commenting=false。"
    "source_kinds与has_visual描述已有材料，字幕和画面观察均可作为依据。coverage_required表示还需补充什么：provided当前够用，unread_part需其它分P。"
)


def persona_fingerprint(character):
    value = {key: character[key] for key in ("core", "style", "rules")}
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def classification_registry(root: Path):
    path = root / "config/operations-persona-views.json"
    if not path.is_file():
        return {"format": 1, "enabled": False, "views": []}
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("内容分类角色视图须为受控的小型配置文件")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("format") != 1
        or type(value.get("enabled")) is not bool
        or not isinstance(value.get("views"), list)
    ):
        raise ValueError("内容分类角色视图配置无效")
    return value


def classification_view(root: Path, character):
    registry = classification_registry(root)
    if not registry["enabled"]:
        return None
    fingerprint = persona_fingerprint(character)
    for view in registry["views"]:
        if (
            isinstance(view, dict)
            and view.get("character_id") == character["id"]
            and view.get("persona_sha256") == fingerprint
            and isinstance(view.get("brief"), str)
            and view["brief"].strip()
        ):
            return view
    return None


def join_content_sources(block):
    """Remove added Chinese separators, retaining all source characters."""

    def cjk(char):
        return any(
            lo <= ord(char) <= hi
            for lo, hi in (
                (0x3400, 0x4DBF),
                (0x4E00, 0x9FFF),
                (0xF900, 0xFAFF),
                (0x3000, 0x303F),
                (0xFF00, 0xFFEF),
            )
        )

    pieces = [source.text for source in block.sources if source.text.strip()]
    if not pieces:
        return ""
    result = pieces[0]
    for piece in pieces[1:]:
        separator = (
            "" if result[-1].isspace() or piece[0].isspace() or cjk(result[-1]) and cjk(piece[0]) else " "
        )
        result += separator + piece
    return result


def classification_source_groups(sources):
    """Use wider intermediate citations; final responses retain original rN."""
    groups, current, characters = [], [], 0
    for sources_in_block in paragraph_sources(sources):
        added = sum(len(source.text) for source in sources_in_block)
        if current:
            first, last, following = current[0], current[-1], sources_in_block[0]
            if (
                last.part != following.part
                or last.part.kind == "visual_observation"
                or following.position != last.position + 1
                or following.start < last.end
                or following.start - last.end > 1.2
                or sources_in_block[-1].end - first.start > 90
                or characters + added > 480
            ):
                groups.append(tuple(current))
                current, characters = [], 0
        current.extend(sources_in_block)
        characters += added
    if current:
        groups.append(tuple(current))
    return groups


def encode_repeated_command_prefix(description):
    """Factor a repeated shell-word prefix; return a literal reversible map."""
    alias = "§"
    if not isinstance(description, str) or alias in description:
        return description, None
    lines = description.splitlines(keepends=True)
    groups = {}
    for index, line in enumerate(lines):
        command = line.lstrip(" \t").rstrip("\r\n")
        try:
            lexer = shlex.shlex(command, posix=False)
            lexer.whitespace_split, lexer.commenters = True, ""
            words = list(lexer)
        except ValueError:
            continue
        if len(words) < 3 or not re.fullmatch(r"[A-Za-z0-9_./\\-]+", words[0]):
            continue
        if not any(re.match(r"--?[A-Za-z]", word) for word in words[1:]):
            continue
        groups.setdefault(tuple(words[:2]), []).append((index, command, words))
    ranked = []
    for group in groups.values():
        if len(group) < 3:
            continue
        shared_words = []
        for aligned in zip(*(words for _, _, words in group), strict=False):
            if len(set(aligned)) != 1:
                break
            shared_words.append(aligned[0])
        if len(shared_words) < 2:
            continue
        first, end = group[0][1], 0
        for word in shared_words:
            end = first.index(word, end) + len(word)
        prefix = first[:end]
        if not all(command.startswith(prefix) for _, command, _ in group):
            continue
        indices = {index for index, _, _ in group}
        ranked.append(((len(prefix) - len(alias)) * len(indices) - len(prefix) - 3, prefix, indices))
    if not ranked:
        return description, None
    savings, prefix, indices = max(ranked, key=lambda candidate: (candidate[0], len(candidate[1])))
    if savings <= 0:
        return description, None
    encoded = list(lines)
    for index in indices:
        line = lines[index]
        start = len(line) - len(line.lstrip(" \t"))
        encoded[index] = line[:start] + alias + line[start + len(prefix) :]
    return "".join(encoded), (alias, prefix)


def compact_content_input(data):
    """Keep source facts, using one reversible spelling for repeated commands."""
    metadata = deepcopy(data["metadata"])
    metadata.pop("fetched_at", None)
    if metadata.get("tags") == []:
        metadata.pop("tags")
    lines = []
    labels = {
        "title": "标题",
        "owner": "作者",
        "duration": "时长秒",
        "category_id": "分区",
        "description_complete": "简介完整",
        "published_at": "发布Unix",
        "tags": "标签",
        "tags_missing": "标签缺失",
    }
    for key, label in labels.items():
        if key in metadata:
            value = metadata.pop(key)
            text = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            )
            lines.append(label + ":" + text)
    if "description" in metadata:
        description = metadata.pop("description")
        description, substitution = encode_repeated_command_prefix(description)
        if substitution:
            alias, prefix = substitution
            lines.append(alias + "=" + prefix)
        if not isinstance(description, str):
            description = json.dumps(description, ensure_ascii=False, separators=(",", ":"))
        lines.append("简介:\n" + description)
    if metadata:
        lines.append("其它元数据:" + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
    other = {
        key: value for key, value in data.items() if key not in {"metadata", "content", "parts", "segments"}
    }
    lines.append(json.dumps(other, ensure_ascii=False, separators=(",", ":")))
    content = data["content"]
    title = data["metadata"].get("title")
    prefix = "视频第1部分（P1） " + title if isinstance(title, str) and title else None
    if prefix and content.startswith(prefix):
        content = "P1" + content[len(prefix) :]
    return "\n".join(lines) + "\n内容:\n" + content


@dataclass(frozen=True, slots=True)
class ResponseReference:
    alias: str
    canonical_id: str
    source_json: str | None

    def source(self):
        return json.loads(self.source_json) if self.source_json is not None else None


class ResponseDecisionInput:
    """Structure readable context and resolve Python-side provenance cleanly without ref aliases."""

    FORMAT_VERSION = "response-v3"
    COMMENT_FORMAT_VERSION = "comment-v4"

    def __init__(self, data):
        records = deepcopy(data.get("evidence", []))
        canonical = {}
        for segment in records:
            identity = segment.get("id")
            if not isinstance(identity, str) or not identity or identity in canonical:
                raise ValueError("回应材料引用无效或重复")
            if not isinstance(segment.get("text"), str):
                raise ValueError("回应材料正文无效")
            for key in ("start", "end"):
                if segment.get(key) is not None:
                    validate_time(segment[key])
            if (
                segment.get("start") is not None
                and segment.get("end") is not None
                and segment["start"] > segment["end"]
            ):
                raise ValueError("回应材料起止时码无效")
            canonical[identity] = segment
        for identity in sorted(data.get("allowed_evidence_ids", [])):
            if not isinstance(identity, str) or not identity:
                raise ValueError("回应来源引用无效")
            canonical.setdefault(identity, None)

        refs = [
            ResponseReference(
                identity,
                identity,
                json.dumps(segment, ensure_ascii=False) if segment is not None else None,
            )
            for identity, segment in canonical.items()
        ]
        self.references = tuple(refs)
        self.allowed_aliases = frozenset(canonical)
        self._by_alias = {ref.alias: ref for ref in refs}
        self._canonical_segments = canonical
        thread = data.get("thread") or {}
        source = thread.get("source") or {}
        self.comment_reply = data.get("kind") == "reply_public"
        self.comment_video_available = data.get("comment_video_available", False)
        self.source_text = source.get("text") or ""
        self.recipient_name = source.get("author_name") or ""
        self.allow_public = data.get("allow_public", True)
        self.allow_comment_like = data.get("allow_comment_like", False)
        self.dynamic_ref = data.get("dynamic_evidence") if data.get("dynamic_evidence") in canonical else None
        group = {"public:" + str(item["comment_id"]) for item in thread.get("grouped_comments", [])}
        self.grouped_comment_refs = frozenset(cid for cid in canonical if cid in group)
        source_cid = "public:" + str(source.get("comment_id")) if source.get("comment_id") else None
        self.anchor_alias = source_cid if source_cid and source_cid in canonical else None

        header = deepcopy(
            {key: value for key, value in data.items() if key not in {"evidence", "allowed_evidence_ids"}}
        )
        header = compact_actor_context(header)
        if "self_actor" in header:
            header = {
                **{key: header[key] for key in ("self_actor", "reply_target") if key in header},
                **header,
            }
        lines, source_numbers = [], {}
        previous_source = object()
        for ref in refs:
            segment = ref.source()
            if segment is None:
                continue
            cid = segment.get("cid")
            if cid is not None and cid != previous_source:
                source_numbers.setdefault(str(cid), len(source_numbers) + 1)
                lines.append(f"材料{source_numbers[str(cid)]}")
                previous_source = cid
            times = [str(segment[k]) if segment[k] is not None else "?" for k in ("start", "end") if k in segment]
            time_label = "[" + "–".join(times) + "s] " if times else ""
            source_label = segment.get("source_label")
            lines.append(time_label + (f"{source_label} " if source_label else "") + segment["text"])
            extra = {
                key: value
                for key, value in segment.items()
                if key not in {"id", "cid", "text", "start", "end", "source_label"}
            }
            if extra:
                lines.append("补充：" + json.dumps(extra, ensure_ascii=False, separators=(",", ":")))
        if self.comment_reply and self.source_text:
            lines.append(f"当前评论原文：\n{self.source_text}")
        body_block = ("\n\n" + "\n".join(lines)) if lines else ""
        self._presentation = json.dumps(header, ensure_ascii=False, separators=(",", ":")) + body_block

    def format(self):
        return self._presentation

    def default_provenance(self, action: str) -> list[str]:
        """Compute canonical Python-side provenance without requiring model citation IDs."""
        if action in {"skip", "wait", "finish", "read_comments"}:
            return []
        if action == "like_dynamic":
            return [self.dynamic_ref] if self.dynamic_ref else []
        if action in {"like_comment", "handoff_private"}:
            return [self.anchor_alias] if self.anchor_alias else []
        ordered = []
        if self.anchor_alias:
            ordered.append(self.anchor_alias)
        for ref in self.references:
            if ref.canonical_id not in ordered and (ref.source_json is not None or ref.canonical_id == self.anchor_alias):
                ordered.append(ref.canonical_id)
        if not ordered:
            ordered = [ref.canonical_id for ref in self.references]
        return ordered

    def validate_wire(self, value):
        from .tools import detect_internal_protocol_leak

        raw = dict(value.model_dump() if hasattr(value, "model_dump") else value)
        raw.setdefault("evidence_ids", [])
        value = ResponseDecision.model_validate(raw)
        for field in ("explanation", "preference_reason", "contact_purpose"):
            if len(getattr(value, field)) > MAX_DECISION_REASON_CHARACTERS:
                raise ValueError(f"{field}须概括具体原因，单次最多{MAX_DECISION_REASON_CHARACTERS}字符")
        if leak := detect_internal_protocol_leak(value.text):
            raise ValueError(leak)
        if self.comment_reply and value.action == "read_more":
            quote = verified_comment_quote(self.source_text, value.source_quote)
            if not self.comment_video_available or not quote:
                raise ValueError("补查视频须引用当前评论里需要核查的原话")
            value.source_quote = quote
        elif value.source_quote:
            raise ValueError("仅评论补查接受原话定位")
        if value.action == "reply_public" and not self.allow_public:
            raise ValueError("当前任务未提供公开回复")
        if value.action == "like_comment" and (not self.allow_comment_like or not self.anchor_alias or value.text):
            raise ValueError("点赞只接受本次已核验的目标评论")
        if value.action == "like_dynamic" and (not self.dynamic_ref or value.text):
            raise ValueError("动态点赞须引用本次已读的原动态")
        if value.covered_comments and not (self.action_allows_covered(value.action) and self.grouped_comment_refs):
            raise ValueError("当前动作不接受连续来评覆盖")
        return value

    @staticmethod
    def action_allows_covered(action):
        return action == "reply_public"

    def diagnose(self, value):
        """Keep a defective body editable without admitting it to automatic send."""
        from .tools import detect_internal_protocol_leak

        if value.action == "reply_public":
            hold_comment_body(value, value.text, recipient_name=self.recipient_name)
        if leak := detect_internal_protocol_leak(value.text):
            return leak
        return None

    def resolve(self, value, *, provenance: list[str] | None = None):
        wire = self.validate_wire(value)
        resolved_ids = list(provenance) if provenance is not None else [
            self._by_alias[cid].canonical_id if cid in self._by_alias else cid
            for cid in (wire.evidence_ids or self.default_provenance(wire.action))
        ]
        resolved_targets = [
            target.model_dump()
            | {"evidence_id": self._by_alias[target.evidence_id].canonical_id if target.evidence_id in self._by_alias else target.evidence_id}
            for target in wire.visual_targets
        ]
        covered = [
            cid.split(":", 1)[1] if ":" in cid else cid
            for cid in wire.covered_comments
        ]
        if not covered and wire.action == "reply_public" and self.grouped_comment_refs:
            covered = [cid.split(":", 1)[1] for cid in sorted(self.grouped_comment_refs)]
        return ResponseDecision.model_validate(
            wire.model_dump()
            | {
                "evidence_ids": resolved_ids,
                "visual_targets": resolved_targets,
                "covered_comments": list(dict.fromkeys(covered)),
            }
        )


@dataclass(frozen=True, slots=True)
class ResolvedContentDecision:
    """Internal result: eight model blocks may resolve to many original sources.

    This is a separate type, not a model-output ContentDecision with its
    max-eight validator bypassed. Durable references remain original IDs.
    """

    wire_decision: ContentDecision
    selected_blocks: tuple[CitationBlock, ...]
    sources: tuple[SourceSegment, ...]

    @property
    def interest(self):
        return self.wire_decision.interest

    @property
    def worth_commenting(self):
        return self.wire_decision.worth_commenting

    @property
    def reason(self):
        return self.wire_decision.reason

    @property
    def explanation(self):
        return self.wire_decision.explanation

    @property
    def coverage_required(self):
        return self.wire_decision.coverage_required

    @property
    def evidence_ids(self):
        return [source.id for source in self.sources]

    @property
    def visual_targets(self):
        return self.wire_decision.visual_targets

    def model_dump(self):
        return self.wire_decision.model_dump() | {"evidence_ids": self.evidence_ids}


class ContentDecisionInput:
    def __init__(self, material, *, text_renderer=None, source_grouper=None):
        self.text_renderer = text_renderer
        self.source_grouper = source_grouper or paragraph_sources
        self.metadata = deepcopy(material["metadata"])
        self.coverage = deepcopy(material["coverage"])
        self.missing_parts = deepcopy(material["missing_parts"])
        self.metadata_parts = {
            str(part["cid"]): (page, part) for page, part in enumerate(self.metadata.get("parts", []), 1)
        }
        self.parts, self.sources = {}, {}
        self.caption_text = {}
        identities = set()
        for index, part in enumerate(material["parts"], 1):
            cid = str(part["cid"])
            page, metadata = self.metadata_parts.get(cid, (index, {}))
            page = metadata.get("page", page)
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError("内容材料分P序号无效")
            source_part = SourcePart(
                index=index,
                cid=cid,
                page=page,
                title=metadata.get("title") or "",
                kind=("visual_observation" if part.get("visual") else
                      "audio_transcript" if part.get("asr") else "subtitle"),
                language=part.get("language"),
                is_ai=part.get("is_ai"),
                metadata_json=json.dumps(
                    {key: value for key, value in part.items() if key != "segments"},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            self.parts[index] = source_part
            for position, segment in enumerate(part["segments"]):
                identity = segment["id"]
                if not isinstance(identity, str) or not identity or identity in identities:
                    raise ValueError("内容片段身份重复或无效")
                if not isinstance(segment["text"], str):
                    raise ValueError("内容片段正文类型无效")
                start, end = validate_time(segment["start"]), validate_time(segment["end"])
                if end < start:
                    raise ValueError("内容片段时码顺序无效")
                identities.add(identity)
                self.sources[cid, identity] = SourceSegment(
                    source_part, position, identity, segment["text"], start, end
                )
                if source_part.kind == "subtitle":
                    corrected = corrected_caption(self.metadata.get("bvid"), cid, segment)
                    if corrected is not segment:
                        self.caption_text[cid, identity] = corrected["text"]

    def render_source(self, block):
        sources = tuple(
            replace(source, text=self.caption_text[source.part.cid, source.id])
            if (source.part.cid, source.id) in self.caption_text else source
            for source in block.sources
        )
        display = replace(block, sources=sources)
        return self.text_renderer(display) if self.text_renderer else display.text

    def present(self, chunk):
        """Return readable source text and bindings without [eN|...] citation ceremony."""
        from ...persona.context.subtitles import timestamp as _ts

        selected, seen, previous = [], set(), None
        for segment in chunk:
            key = str(segment["cid"]), segment["id"]
            source = self.sources.get(key)
            if source is None or key in seen:
                raise ValueError("内容片段不属于当前材料或重复")
            validate_time(segment["start"])
            validate_time(segment["end"])
            if any(segment[key] != getattr(source, key) for key in ("text", "start", "end")):
                raise ValueError("内容片段原文或时码已变化")
            order = source.part.index, source.position
            if previous is not None and order <= previous:
                raise ValueError("内容片段顺序已变化")
            previous = order
            seen.add(key)
            selected.append(source)
        blocks = tuple(
            CitationBlock(f"b{index}", group) for index, group in enumerate(self.source_grouper(selected), 1)
        )
        used_parts = sorted({block.part.index for block in blocks})
        kinds = sorted({self.parts[index].kind for index in used_parts})
        lines, last_part = [], None
        for block in blocks:
            part = block.part
            if part != last_part:
                header = f"视频第{part.page}部分（P{part.page}）"
                if part.title:
                    header += f" {part.title}"
                if part.kind == "visual_observation":
                    header += " [画面观察]"
                elif part.kind == "audio_transcript":
                    header += " [音频转写]"
                lines.append(header)
                last_part = part
            lines.append(f"[{_ts(block.sources[0].start)}] {self.render_source(block)}")
        metadata = {
            key: deepcopy(value)
            for key, value in self.metadata.items()
            if key
            not in {"aid", "bvid", "cid", "id", "guid", "owner_uid", "parts", "track_id", "content_hash"}
        }
        missing = []
        for part in self.missing_parts:
            page, detail = self.metadata_parts.get(str(part.get("cid")), (None, {}))
            missing.append(
                {
                    "page": detail.get("page", page),
                    "title": detail.get("title", ""),
                    **{key: deepcopy(value) for key, value in part.items() if key != "cid"},
                }
            )
        return {
            "metadata": metadata,
            "content": "\n".join(lines),
            "segments": [{"id": block.alias, "part": block.part.index} for block in blocks],
            "parts": [self.parts[index].wire() for index in used_parts],
            "missing_parts": missing,
            "coverage": deepcopy(self.coverage),
            "source_kinds": kinds,
            "has_visual": "visual_observation" in kinds,
        }, {block.alias: block for block in blocks}


def bind_content_decision(decision, aliases, *, provenance=None):
    """Resolve presented segment provenance deterministically inside Python boundaries."""
    wire = ContentDecision.model_validate(decision.model_dump())
    canonical_targets = []
    for target in wire.visual_targets:
        block = aliases.get(target.evidence_id)
        if block is None and target.evidence_id:
            for candidate in aliases.values():
                if any(source.id == target.evidence_id for source in candidate.sources):
                    block = candidate
                    break
        sources = block.sources if block and block.part.kind in {"subtitle", "audio_transcript"} else ()
        if not sources:
            raise ValueError("画面时点需绑定到本次字幕或语音转写")
        source = sources[-1] if target.position == "after" else sources[0] if target.position == "before" else sources[len(sources) // 2]
        canonical_targets.append(target.model_copy(update={"evidence_id": source.id}))
    wire = wire.model_copy(update={"visual_targets": canonical_targets})
    selected, sources, seen = [], [], set()
    chosen_blocks = []
    wanted = set(provenance) if provenance is not None else None
    if wanted:
        chosen_blocks = [b for b in aliases.values() if any(s.id in wanted for s in b.sources)]
    elif wire.evidence_ids:
        for ref in dict.fromkeys(wire.evidence_ids):
            if ref in aliases:
                chosen_blocks.append(aliases[ref])
            else:
                for block in aliases.values():
                    if any(s.id == ref for s in block.sources):
                        chosen_blocks.append(block)
                        break
    elif wire.worth_commenting:
        chosen_blocks = list(aliases.values())
    for block in chosen_blocks:
        if block not in selected:
            selected.append(block)
        for source in block.sources:
            if wanted is not None and source.id not in wanted:
                continue
            if source.id not in seen:
                seen.add(source.id)
                sources.append(source)
    return ResolvedContentDecision(wire, tuple(selected), tuple(sources))


def restore_stored_content_decisions(material, values):
    """Restore canonical source bindings for stored content decisions.

    Stored records may hold canonical segment IDs from earlier turns or empty
    structural metadata when no citation selection was needed.
    """
    sources = {source.id: source for source in ContentDecisionInput(material).sources.values()}
    restored = []
    for value in values:
        references = value.get("evidence_ids") or []
        if not isinstance(references, list) or any(
            not isinstance(ref, str) or ref not in sources for ref in references
        ):
            raise ValueError("已存内容判断包含无效的原始材料引用")
        fields = ContentDecision.model_validate(value | {"evidence_ids": []})
        if any(target.evidence_id not in sources for target in fields.visual_targets):
            raise ValueError("已存画面时点缺少有效的原始材料引用")
        restored.append(ResolvedContentDecision(fields, (), tuple(dict.fromkeys(sources[ref] for ref in references))))
    return restored
