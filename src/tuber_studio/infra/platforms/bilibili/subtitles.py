"""ASR subtitle cleaning and deduplication helpers for Bilibili track ingestion."""

import re


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
