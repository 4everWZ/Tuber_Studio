"""Operations prose uses request resources, independently of live speech length."""

import re
from collections import Counter

# A local transport/storage resource bound, not a claimed Bilibili word limit.
MAX_OPERATION_TEXT_BYTES = 256 * 1024
_ADJACENT_PHRASES = re.compile(r"([^\W_]{4,40}?)(?:\1){2,}")
_UNBROKEN_PHRASE = re.compile(r"([^\W_]{5,40}?)\1")
_SOURCE_ALIAS_TOKEN = re.compile(r"(?<![A-Za-z0-9_])r[1-9][0-9]*(?![A-Za-z0-9_])", re.IGNORECASE)


def source_alias_literals(text):
    return {token.casefold() for token in _SOURCE_ALIAS_TOKEN.findall(text)}


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
    # Keep short exclamations and natural laughter out of the loop diagnostic.
    adjacent = any(
        len(set(match[1])) >= 3
        for match in _ADJACENT_PHRASES.finditer(re.sub(r"[，、,。！？!?；; \t]+", "", text))
    ) or any(len(set(match[1])) >= 4 for match in _UNBROKEN_PHRASE.finditer(text))
    paragraphs = Counter(line.strip() for line in text.splitlines() if len(line.strip()) >= 40)
    repeated = sum(len(line) * count for line, count in paragraphs.items() if count >= 8)
    clauses = Counter(
        clause.strip() for clause in re.split(r"[。！？!?；;\n]+", text) if len(clause.strip()) >= 18
    )
    repeated_clauses = sum(len(clause) * count for clause, count in clauses.items() if count >= 6)
    return {
        "visible_characters": len(text),
        "visible_utf8_bytes": len(text.encode("utf-8")),
        "repeated_paragraph_characters": repeated,
        "repeated_clause_characters": repeated_clauses,
        "adjacent_phrase_loop": adjacent,
        "needs_review": (
            adjacent
            or len(text) >= 4096
            and repeated > len(text) * 0.8
            or repeated_clauses >= 480
            and repeated_clauses > len(text) * 0.6
        ),
    }
