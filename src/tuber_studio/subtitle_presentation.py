"""Readable subtitle paragraphs with immutable original source bindings."""

import json
import math
import re
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


def clean_stutter(text: str) -> str:
    """Collapse 3+ repetitions of characters or short phrases into 2."""
    if not text:
        return ""
    # Matches any 1-8 char sequence repeated 3 or more times consecutively
    return re.sub(r"(.{1,8}?)\1{2,}", r"\1\1", text)


def deduplicate_subtitles(raw_segments: list[dict]) -> list[dict]:
    """Clean and deduplicate CC subtitle segments from ASR artifacts.

    Handles adjacent exact repeats, progressive prefix typing extensions,
    progressive truncations, suffix-prefix boundary overlaps, and stutter loops.
    """
    if not raw_segments:
        return []

    cleaned = []
    for seg in raw_segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        text = clean_stutter(text)
        if not text:
            continue
        cleaned.append(
            {
                **seg,
                "text": text,
                "start": float(seg["start"]),
                "end": float(seg["end"]),
            }
        )

    if not cleaned:
        return []

    deduped = []
    for curr in cleaned:
        if not deduped:
            deduped.append(curr)
            continue

        prev = deduped[-1]
        p_text = prev["text"].strip()
        c_text = curr["text"].strip()
        time_gap = curr["start"] - prev["end"]

        # 1. Exact duplicate (e.g. "恭喜发财" followed by "恭喜发财" or "Yeah" * 4)
        if p_text == c_text and time_gap <= 2.0:
            prev["end"] = max(prev["end"], curr["end"])
            continue

        # 2. Progressive prefix: curr starts with prev (e.g. "全是赌本" -> "全是赌本兄弟")
        if c_text.startswith(p_text) and len(p_text) >= 2 and time_gap <= 1.5:
            curr["start"] = min(prev["start"], curr["start"])
            deduped[-1] = curr
            continue

        # 3. Progressive truncation: prev starts with curr (e.g. "飞升走了也行" -> "飞升走了")
        if p_text.startswith(c_text) and len(c_text) >= 2 and time_gap <= 1.0:
            prev["end"] = max(prev["end"], curr["end"])
            continue

        # 4. Overlapping boundary (e.g. "在这个世界上有很多" and "有很多有趣的事情")
        max_overlap_len = min(len(p_text), len(c_text), 20)
        found_overlap = False
        if time_gap <= 1.5:
            for length in range(max_overlap_len, 2, -1):
                if p_text.endswith(c_text[:length]):
                    new_c_text = c_text[length:].strip()
                    if new_c_text:
                        curr["text"] = new_c_text
                    else:
                        prev["end"] = max(prev["end"], curr["end"])
                        found_overlap = True
                    break
        if found_overlap:
            continue

        deduped.append(curr)

    return deduped


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


def render_blocks(blocks, *, text_renderer=None):
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
        text = text_renderer(block) if text_renderer is not None else block.text
        lines.append(f"[{block.alias}|{timestamp(block.sources[0].start)}]{text}")
    return "\n".join(lines)
