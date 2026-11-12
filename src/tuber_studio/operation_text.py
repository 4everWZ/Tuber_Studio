"""Operations prose uses request resources, independently of live speech length."""

import re
from collections import Counter

from .llama_sampling import live_thinking_budget

# A local transport/storage resource bound, not a claimed Bilibili word limit.
MAX_OPERATION_TEXT_BYTES = 256 * 1024
_ADJACENT_PHRASES = re.compile(r"([^\W_]{4,40}?)(?:\1){2,}")
_UNBROKEN_PHRASE = re.compile(r"([^\W_]{5,40}?)\1")
_CODE_LITERAL = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_QUOTED_LITERAL = r'“[^“”]*”|‘[^‘’]*’|"(?:[^"\\]|\\.)*"'
_FIELD_ASSIGNMENT = re.compile(
    r'(?:^|[{},，、])\s*("[a-z_][a-z_0-9]*"|“[a-z_][a-z_0-9]*”|[a-z_][a-z_0-9]*)\s*[:：=]|'
    + _QUOTED_LITERAL,
    re.MULTILINE,
)
_COMMENT_PROTOCOL_FIELDS = {
    "reply_target", "evidence_ids", "covered_comments", "video_preference",
    "preference_reason", "contact_purpose", "contact_trigger", "reply_to", "animation_id",
}


def reply_output_budget(config, mode):
    if mode in {"comment_draft", "direct_message"}:
        return config.get("operations_max_tokens", 8192)
    return config.get("max_tokens", 160) + (live_thinking_budget(config) if mode == "live" else 0)


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


def comment_body_issue(text, *, recipient_name=""):
    """Detect response framing in prose, while retaining quoted/code literals."""
    visible = _CODE_LITERAL.sub(" ", text)
    depth = 0
    for token in re.finditer(r"[{}]", re.sub(_QUOTED_LITERAL, " ", visible)):
        depth += 1 if token[0] == "{" else -1
        if depth < 0:
            break
    if depth:
        return "评论正文含未配对的结构括号，请修改后发布"

    fields = {match[1].strip('"“”') for match in _FIELD_ASSIGNMENT.finditer(visible) if match[1]}
    if fields & _COMMENT_PROTOCOL_FIELDS or (
        fields & {"text", "spoken_text"} and fields & {"action", "explanation", "emotion", "reason"}
    ):
        return "评论正文含内部结构字段，请修改后发布"

    if recipient_name:
        prefix = "@" + recipient_name
        opening = text.lstrip()
        suffix = opening.removeprefix(prefix)
        if suffix != opening and suffix and not suffix[0].isspace():
            return "开头的 @ 回复对象后需要空格，请确认用户名和正文的分隔"
    return None


def validate_comment_body(text, *, recipient_name=""):
    text = validate_operation_text(text, label="评论正文")
    if issue := comment_body_issue(text, recipient_name=recipient_name):
        raise ValueError(issue)
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
