"""Readable subtitle paragraphs with immutable original source bindings."""

import json
import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SourcePart:
    index: int
    cid: str
    page: int
    title: str
    kind: Literal["subtitle", "visual_observation"]
    language: str | None
    is_ai: bool | None
    metadata_json: str

    def metadata(self):
        """All original part metadata, including track/version, as a fresh copy."""
        return json.loads(self.metadata_json)

    def wire(self):
        result = {"index": self.index, "page": self.page, "kind": self.kind}
        if self.language is not None:
            result["language"] = self.language
        if self.is_ai is not None:
            result["is_ai"] = self.is_ai
        return result


@dataclass(frozen=True, slots=True)
class SourceSegment:
    part: SourcePart
    position: int
    id: str
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class CitationBlock:
    alias: str
    sources: tuple[SourceSegment, ...]

    @property
    def part(self):
        return self.sources[0].part

    @property
    def text(self):
        # A separator avoids joining English words across subtitle boundaries.
        # Blank lines cost no model tokens; their original sources remain bound.
        return " ".join(source.text for source in self.sources if source.text.strip())


def validate_time(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("内容片段时码无效")
    return value


def timestamp(seconds):
    # Display precision only: bindings retain the exact original numeric times.
    millis = round(seconds * 1000)
    minutes, millis = divmod(millis, 60000)
    second, fraction = divmod(millis, 1000)
    tail = f"{second:02d}" + (f".{fraction:03d}".rstrip("0") if fraction else "")
    if minutes >= 60:
        hours, minute = divmod(minutes, 60)
        return f"{hours:02d}:{minute:02d}:{tail}"
    return f"{minutes:02d}:{tail}"


def paragraph_sources(sources):
    """Join adjacent subtitles, preserving track, source gaps and visual frames.

    Character/duration thresholds only end a paragraph; a long original segment
    remains whole and is still subject to the caller's real-token chunk budget.
    """
    groups, current, characters = [], [], 0

    def flush():
        nonlocal current, characters
        if current:
            groups.append(tuple(current))
            current, characters = [], 0

    for source in sources:
        previous = current[-1] if current else None
        same_track = previous is not None and source.part == previous.part
        adjacent = same_track and source.position == previous.position + 1
        if current and (
            not adjacent
            or source.part.kind == "visual_observation"
            or source.start < previous.end
            or source.start - previous.end > 1.2
            or source.end - current[0].start > 30
            or characters + len(source.text) > 160
        ):
            flush()
        current.append(source)
        characters += len(source.text)
        if source.part.kind == "visual_observation" or (
            characters >= 64 and source.text.rstrip().endswith(("。", "！", "？", ".", "!", "?", "；", ";"))
        ):
            flush()
    flush()
    return [group for group in groups if any(source.text.strip() for source in group)]


def render_blocks(blocks):
    lines, previous_page, previous_track = [], None, None
    for block in blocks:
        part = block.part
        if part.page != previous_page:
            lines.append(f"视频第{part.page}部分（P{part.page}）" + (f" {part.title}" if part.title else ""))
            previous_page, previous_track = part.page, None
        if part.index != previous_track:
            label = "画面观察" if part.kind == "visual_observation" else "字幕"
            hints = [part.language] if part.language else []
            if part.is_ai is True:
                hints.append("自动")
            lines.append(label + ("（" + "/".join(hints) + "）" if hints else ""))
            previous_track = part.index
        lines.append(f"[{block.alias}|{timestamp(block.sources[0].start)}]{block.text}")
    return "\n".join(lines)
