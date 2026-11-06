"""Compact model input and invocation-local, typed evidence bindings."""

import hashlib
import json
import re
import shlex
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from ...subtitle_presentation import (
    CitationBlock,
    SourcePart,
    SourceSegment,
    paragraph_sources,
    render_blocks,
    validate_time,
)
from .models import ContentDecision, ResponseDecision


def format_content_input(data):
    """A small JSON context header followed by actual readable text lines."""
    # Segment/part tables are for backend schema and source bindings. Their
    # identifiers and headings already occur once in the readable body.
    header = {key: value for key, value in data.items() if key not in {"content", "segments", "parts"}}
    return json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n\n内容：\n" + data["content"]


CLASSIFICATION_INPUT_VERSION = "content-c6i-v1"
CLASSIFICATION_INSTRUCTION = (
    "按角色兴趣决定是否准备回应，本阶段不选公私信。interest=like表示想参与，worth_commenting表示有具体想说的话；吐槽、共情、轻互动与分享都算。"
    "reason：specific_detail有回应点；no_interest无兴趣；nothing_to_add读懂无补充；insufficient_evidence想回应的内容缺材料；visual_required想回应的内容需新画面；unsafe_interaction涉及骚扰、伤害他人或公开隐私而略过。"
    "各字段表达同一意图。准备回应：interest=like,worth_commenting=true,reason=specific_detail；想补读：interest=need_more,worth_commenting=false；不发言worth_commenting=false。"
    "source_kinds与has_visual描述已有材料，字幕和画面观察均可作为依据。coverage_required表示还需补充什么：provided当前够用，unread_part需其它分P。"
    "evidence_ids选相关[eN|起始时码]块，准备回应选1–8项；字幕/画面按分P与类型区分。"
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
    """One invocation-local alias per original reference; never merge citations."""

    FORMAT_VERSION = "response-reference-lines-v1"

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

        refs, number = [], 1
        for identity, segment in canonical.items():
            while f"r{number}" in canonical:
                number += 1
            refs.append(
                ResponseReference(
                    f"r{number}",
                    identity,
                    json.dumps(segment, ensure_ascii=False) if segment is not None else None,
                )
            )
            number += 1
        self.references = tuple(refs)
        self.allowed_aliases = frozenset(ref.alias for ref in refs)
        self._by_alias = {ref.alias: ref for ref in refs}
        header = deepcopy(
            {key: value for key, value in data.items() if key not in {"evidence", "allowed_evidence_ids"}}
        )
        lines, source_numbers = [], {}
        previous_source = object()
        for ref in refs:
            segment = ref.source()
            if segment is None:
                lines.append(f"[{ref.alias}] 当前公开来源（原文见thread）")
                continue
            cid = segment.get("cid")
            if cid is not None and cid != previous_source:
                source_numbers.setdefault(str(cid), len(source_numbers) + 1)
                lines.append(f"材料{source_numbers[str(cid)]}")
                previous_source = cid
            times = []
            for key in ("start", "end"):
                if key in segment:
                    times.append(str(segment[key]) if segment[key] is not None else "?")
            time_label = "|" + "–".join(times) + "s" if times else ""
            source_label = segment.get("source_label")
            lines.append(
                f"[{ref.alias}{time_label}]" + (f"{source_label} " if source_label else "") + segment["text"]
            )
            extra = {
                key: value
                for key, value in segment.items()
                if key not in {"id", "cid", "text", "start", "end", "source_label"}
            }
            if extra:
                lines.append("补充：" + json.dumps(extra, ensure_ascii=False, separators=(",", ":")))
        self._presentation = (
            json.dumps(header, ensure_ascii=False, separators=(",", ":"))
            + "\n\n所选材料：\n"
            + "\n".join(lines)
        )

    def format(self):
        return self._presentation

    def validate_wire(self, value):
        value = ResponseDecision.model_validate(value.model_dump() if hasattr(value, "model_dump") else value)
        if not set(value.evidence_ids) <= self.allowed_aliases:
            raise ValueError("回应引用不属于本次短编号")
        if value.action in {"reply_public", "handoff_private"} and not value.evidence_ids:
            raise ValueError("回应引用为空")
        return value

    def resolve(self, value):
        wire = self.validate_wire(value)
        return ResponseDecision.model_validate(
            wire.model_dump()
            | {"evidence_ids": [self._by_alias[alias].canonical_id for alias in wire.evidence_ids]}
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
    def coverage_required(self):
        return self.wire_decision.coverage_required

    @property
    def evidence_ids(self):
        return [source.id for source in self.sources]

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
                kind="visual_observation" if part.get("visual") else "subtitle",
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

    def present(self, chunk):
        """Return short text and bindings; structured source IDs stay private."""
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
            CitationBlock(f"e{index}", group) for index, group in enumerate(self.source_grouper(selected), 1)
        )
        used_parts = sorted({block.part.index for block in blocks})
        kinds = sorted({self.parts[index].kind for index in used_parts})
        # Do not redact numeric facts or IDs occurring naturally in source text.
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
            "content": render_blocks(blocks, text_renderer=self.text_renderer),
            "segments": [{"id": block.alias, "part": block.part.index} for block in blocks],
            "parts": [self.parts[index].wire() for index in used_parts],
            "missing_parts": missing,
            "coverage": deepcopy(self.coverage),
            "source_kinds": kinds,
            "has_visual": "visual_observation" in kinds,
        }, {block.alias: block for block in blocks}


def bind_content_decision(decision, aliases):
    """Validate the wire limit, then resolve this exact invocation's blocks."""
    wire = ContentDecision.model_validate(decision.model_dump())
    if any(reference not in aliases for reference in wire.evidence_ids) or (
        wire.worth_commenting and not wire.evidence_ids
    ):
        raise ValueError("内容判断引用不存在或为空")
    selected, sources, seen = [], [], set()
    for reference in dict.fromkeys(wire.evidence_ids):
        block = aliases[reference]
        if not isinstance(block, CitationBlock) or block.alias != reference:
            raise ValueError("内容判断引用绑定无效")
        selected.append(block)
        for source in block.sources:
            if source.id not in seen:
                seen.add(source.id)
                sources.append(source)
    return ResolvedContentDecision(wire, tuple(selected), tuple(sources))
