"""Incremental decoding of the assistant's visible JSON string."""

import json
import re

_ANSWER = re.compile(r'^\s*\{\s*"action"\s*:\s*"chat"\s*,\s*"reply_markdown"\s*:\s*"')
_REPLY_ONLY_ANSWER = re.compile(r'^\s*\{\s*(?:"action"\s*:\s*"chat"\s*,\s*)?"reply_markdown"\s*:\s*"')
_STRING_PREFIX = re.compile(r'(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*')
_ANALYSIS_MARKER = re.compile(r"<\s*/?\s*(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$|<\s*/?\s*think\b", re.I)
_THINK_BLOCK_INLINE = re.compile(r"<\s*think\b[^>]*>[\s\S]*?<\s*/\s*think\s*>\s*", re.I)
_WRAPPER_SUFFIX = re.compile(r'[\s"”}]+$')
_MARKDOWN_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_WORKBENCH_FIELD = re.compile(r"\{\{(local_date|local_time|local_weekday)\}\}")
_EXACT_CLOCK_READOUT = {
    "local_date": re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    "local_time": re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)"),
    "local_weekday": re.compile(r"星期[一二三四五六日天]"),
}
_CALENDAR_FIELDS = {
    "local_date": re.compile(r"(?:年月日|日期|几月几[日号]|几[日号])"),
    "local_time": re.compile(r"(?:钟点|时刻|时间|几点|几分)"),
    "local_weekday": re.compile(r"(?:星期[几哪]|周几|礼拜几)"),
}
_CLOCK_QUESTION = re.compile(
    r"(?:(?:现在|今天|今日|当前|此刻|本地|工坊|日历|的|上|是|具体|完整|什么|查一下|告诉我|说一下|请问|说起来|那|我说的是))*"
    r"(?:年月日|日期|几月几[日号]|几[日号]|钟点|时刻|时间|几点(?:几分)?|几分|星期[几哪]|周几|礼拜几)"
    r"(?:是(?:多少|什么)|是什么|是多少|了|来着|呀|啊|呢|哦|吧|嘛|吗)*"
)


def workbench_fields(text, clock, *, partial=False):
    if partial:
        unfinished = re.search(r"\{(?:\{[a-z_]*\}?)?$", text)
        if unfinished:
            text = text[:unfinished.start()]
    values = {"local_date": f"{clock.year}年{clock.month}月{clock.day}日",
              "local_time": clock.strftime("%H:%M"), "local_weekday": "星期" + "一二三四五六日"[clock.weekday()]}
    # Quoted examples and Markdown code can still show the literal field names.
    parts = re.split("(" + _MARKDOWN_CODE.pattern + ")", text)
    return "".join(part if index % 2 else _WORKBENCH_FIELD.sub(lambda match: values[match[1]], part)
                   for index, part in enumerate(parts))


def clock_readout(reply, fields, clock):
    values = {field: workbench_fields("{{" + field + "}}", clock) for field in fields}
    compact = re.sub(r"\s+", "", reply)
    if "{{" in compact or any(
        match != values.get(field) for field, pattern in _EXACT_CLOCK_READOUT.items() for match in pattern.findall(compact)
    ):
        reply = compact = ""
    missing = [value for value in values.values() if value not in compact]
    if missing:
        exact = "现在是" + "、".join(missing) + "。"
        return reply.rstrip() + "\n\n" + exact if reply else exact
    return reply


def calendar_fields(message, selected):
    clauses = [clause for clause in re.split(r"[\s，,。！？?!、]+", message) if clause]
    named = tuple(field for field, label in _CALENDAR_FIELDS.items()
                  if any(label.search(clause) and _CLOCK_QUESTION.fullmatch(clause) for clause in clauses))
    if named and all(_CLOCK_QUESTION.fullmatch(clause) for clause in clauses):
        return named
    return tuple(field for field in selected if _CALENDAR_FIELDS[field].search(message))


def assistant_markdown(text):
    """Drop unmatched response framing at the end of the decoded prose."""
    suffix = _WRAPPER_SUFFIX.search(text)
    if not suffix or "}" not in suffix[0] or not text[:suffix.start()].strip():
        return text
    prose = _MARKDOWN_CODE.sub("", text)
    if prose.count("}") <= prose.count("{") or prose.count("```") % 2:
        return text
    return text[:suffix.start()].rstrip()


class AssistantTextStream:
    def __init__(self, deliver, *, limit=48_000, plain=False, allow_reply_only=False):
        self.deliver = deliver
        self.limit = limit
        self.plain = plain
        self.allow_reply_only = allow_reply_only
        self.buffer = ""
        self.emitted = ""

    def feed(self, content):
        self.buffer += content
        if len(self.buffer) > self.limit:
            raise ValueError("回复内容超出资源限制")
        if self.plain:
            decoded = _THINK_BLOCK_INLINE.sub("", self.buffer.lstrip())
            # Let the final decoder handle an unsolicited JSON wrapper.
            if decoded.startswith("{"):
                return
            closed = False
        else:
            pattern = _REPLY_ONLY_ANSWER if self.allow_reply_only else _ANSWER
            start = pattern.match(self.buffer)
            if not start:
                return
            encoded = _STRING_PREFIX.match(self.buffer, start.end())
            decoded = json.loads('"' + encoded.group() + '"')
            closed = self.buffer[encoded.end():].startswith('"')
            # A UTF-16 pair can straddle chunks. Keep the high surrogate pending.
            if decoded and 0xD800 <= ord(decoded[-1]) <= 0xDBFF:
                decoded = decoded[:-1]
        # Internal analysis markers and protocol fields cannot become partial UI.
        marker = _ANALYSIS_MARKER.search(decoded)
        if marker:
            decoded = decoded[:marker.start()]
        decoded = assistant_markdown(decoded)
        # A quote can be the first character of a leftover JSON wrapper.
        # Wait for following prose or for the verified final answer.
        if not closed and (suffix := _WRAPPER_SUFFIX.search(decoded)) and suffix[0].strip():
            decoded = decoded[:suffix.start()]
        if decoded.startswith(self.emitted) and len(decoded) > len(self.emitted):
            self.deliver(decoded[len(self.emitted):])
            self.emitted = decoded


def plain_assistant_markdown(text):
    if _ANSWER.match(text):
        data = json.loads(text)
        return data["reply_markdown"]
    return text
