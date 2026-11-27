"""Operations prose uses request resources, independently of live speech length."""

import re
from collections import Counter

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
    "source_quote", "web_search", "web_fetch",
}

_STRONG_REASONING_MARKERS = (
    re.compile(r"\bcheck\s+for\s+banned\s+phrases\b", re.IGNORECASE),
    re.compile(r"^\s*Final\s+text\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Self-reference\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bNo\s+need\s+to\s+skip\b", re.IGNORECASE),
    re.compile(r"</(?:think|thought|analysis)>", re.IGNORECASE),
)

_META_DRAFTING_CUES = (
    re.compile(r'^\s*Wait,\s+["\'“]', re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bActually,?\s+let\s+me\b", re.IGNORECASE),
    re.compile(r"\bLet\s+me\s+(?:check|refine|double-check|rethink|rewrite|adjust|verify)\b", re.IGNORECASE),
    re.compile(r"^\s*Another\s+consideration\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bShould\s+I\s+(?:call|use|mention|skip|keep|add|include)\b", re.IGNORECASE),
    re.compile(r"\bFine\s+as\s+is\b", re.IGNORECASE),
    re.compile(r"^\s*Hmm,\s+good\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bThis\s+is\s+good\s*[—–-]", re.IGNORECASE),
)


def detect_reasoning_leak(text: str) -> str | None:
    """Detect English chain-of-thought or meta-drafting leakage in visible output."""
    if not isinstance(text, str) or not text.strip():
        return None
    if any(pattern.search(text) for pattern in _STRONG_REASONING_MARKERS):
        return "检测到模型思考过程或英文草稿分析泄露进正文，已拦截保留原稿"
    hits = sum(1 for pattern in _META_DRAFTING_CUES if pattern.search(text))
    if hits >= 2:
        return "检测到模型思考过程或英文草稿分析泄露进正文，已拦截保留原稿"
    return None


def reply_output_budget(config, mode):
    if mode in {"comment_draft", "direct_message"}:
        return config.get("operations_max_tokens", 8192)
    return config.get("max_tokens", 160)


def operation_character(character):
    return character | {
        "style": {
            key: value for key, value in (character.get("style") or {}).items() if key not in {"min_chars", "max_chars"}
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


def verified_comment_quote(source, quote):
    quote = quote.strip()
    if len(quote) >= 2 and quote in source:
        return quote
    # Models can copy the short presentation reference along with the quote.
    text = re.sub(r"^\[ref:[1-9][0-9]*\]\s*", "", quote)
    return text if len(text) >= 2 and text in source else None


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
    reasoning_leak = bool(detect_reasoning_leak(text))
    return {
        "visible_characters": len(text),
        "visible_utf8_bytes": len(text.encode("utf-8")),
        "repeated_paragraph_characters": repeated,
        "repeated_clause_characters": repeated_clauses,
        "adjacent_phrase_loop": adjacent,
        "reasoning_leak": reasoning_leak,
        "needs_review": (
            reasoning_leak
            or adjacent
            or len(text) >= 4096
            and repeated > len(text) * 0.8
            or repeated_clauses >= 480
            and repeated_clauses > len(text) * 0.6
        ),
    }
