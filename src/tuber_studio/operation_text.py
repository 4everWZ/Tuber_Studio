"""Operations prose uses request resources, independently of live speech length."""

import re
from collections import Counter

# A local transport/storage resource bound, not a claimed Bilibili word limit.
MAX_OPERATION_TEXT_BYTES = 256 * 1024


def reply_output_budget(config, mode):
    return (
        config.get("operations_max_tokens", 8192)
        if mode in {"comment_draft", "direct_message"}
        else config.get("max_tokens", 160)
    )


def operation_character(character):
    return character | {
        "style": {
            key: value for key, value in character["style"].items() if key not in {"min_chars", "max_chars"}
        }
    }


def validate_operation_text(text, *, label="正文"):
    if not isinstance(text, str) or not text.strip():
        raise ValueError(label + "为空")
    if len(text.encode("utf-8")) > MAX_OPERATION_TEXT_BYTES:
        raise ValueError(label + "超过本地256KiB请求资源边界")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError(label + "含无效控制字符")
    return text


def operation_output_diagnostics(text):
    # Report substantial repeated paragraphs for review; short laughter and
    # catchphrases do not become failed replies or trigger automatic rewrites.
    paragraphs = Counter(line.strip() for line in text.splitlines() if len(line.strip()) >= 40)
    repeated = sum(len(line) * count for line, count in paragraphs.items() if count >= 8)
    return {
        "visible_characters": len(text),
        "visible_utf8_bytes": len(text.encode("utf-8")),
        "repeated_paragraph_characters": repeated,
        "needs_review": len(text) >= 4096 and repeated > len(text) * 0.8,
    }
