"""Incremental decoding of the assistant's visible JSON string."""

import json
import re

_ANSWER = re.compile(r'^\s*\{\s*"action"\s*:\s*"chat"\s*,\s*"reply_markdown"\s*:\s*"')
_STRING_PREFIX = re.compile(r'(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*')
_ANALYSIS_MARKER = re.compile(r"<\s*/?\s*(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$|<\s*/?\s*think\b", re.I)


class AssistantTextStream:
    def __init__(self, deliver, *, limit=48_000):
        self.deliver = deliver
        self.limit = limit
        self.buffer = ""
        self.emitted = ""

    def feed(self, content):
        self.buffer += content
        if len(self.buffer) > self.limit:
            raise ValueError("回复内容超出资源限制")
        start = _ANSWER.match(self.buffer)
        if not start:
            return
        encoded = _STRING_PREFIX.match(self.buffer, start.end()).group()
        decoded = json.loads('"' + encoded + '"')
        # A UTF-16 pair can straddle chunks. Keep the high surrogate pending.
        if decoded and 0xD800 <= ord(decoded[-1]) <= 0xDBFF:
            decoded = decoded[:-1]
        # Internal analysis markers and protocol fields cannot become partial UI.
        marker = _ANALYSIS_MARKER.search(decoded)
        if marker:
            decoded = decoded[:marker.start()]
        if decoded.startswith(self.emitted) and len(decoded) > len(self.emitted):
            self.deliver(decoded[len(self.emitted):])
            self.emitted = decoded
