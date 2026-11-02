"""Compact model input and invocation-local, typed evidence bindings."""

import json
from copy import deepcopy
from dataclasses import dataclass

from .browsing_models import ContentDecision, ResponseDecision
from .subtitle_presentation import (
    CitationBlock,
    SourcePart,
    SourceSegment,
    paragraph_sources,
    render_blocks,
    validate_time,
)


def format_content_input(data):
    """A small JSON context header followed by actual readable text lines."""
    # Segment/part tables are for backend schema and source bindings. Their
    # identifiers and headings already occur once in the readable body.
    header = {key: value for key, value in data.items() if key not in {"content", "segments", "parts"}}
    return json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n\n内容：\n" + data["content"]


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
            lines.append(f"[{ref.alias}{time_label}]" + segment["text"])
            extra = {
                key: value
                for key, value in segment.items()
                if key not in {"id", "cid", "text", "start", "end"}
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
    def __init__(self, material):
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
            CitationBlock(f"e{index}", group) for index, group in enumerate(paragraph_sources(selected), 1)
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
            "content": render_blocks(blocks),
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
