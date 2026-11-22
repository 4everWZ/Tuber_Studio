"""Constants, schemas, and credential guards for daily and organized memory."""

import re

ANECDOTE_CATEGORIES = ("comment_anecdote", "live_highlight", "community_meme")
HIGHLIGHT_CATEGORIES = (*ANECDOTE_CATEGORIES, "public_incident")
HIGHLIGHT_STATUSES = ("active", "pending_review", "archived")
MEMORY_CHANNELS = ("live", "comment", "dynamic")
_PUBLIC_PROJECTION_VERSION = 2
_OWNER_PROVENANCE_VERSION = 1
_ORIGINAL_PUBLIC_SOURCE_KINDS = {"comment", "subtitle"}
_PUBLIC_MEMORY_SOURCE = re.compile(
    r"https://(?:www\.bilibili\.com/video/(?P<bvid>BV[0-9A-Za-z]{10})/?"
    r"(?:\?(?:p|t)=[0-9]+(?:&(?:p|t)=[0-9]+)?)?"
    r"|t\.bilibili\.com/(?P<dynamic>[1-9][0-9]{0,19}))"
    r"(?:#reply(?P<comment>[1-9][0-9]{0,19}))?"
)
_MEMORY_CREDENTIAL = re.compile(
    r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"(?:api[ _-]?key|access[ _-]?token|password|secret|密码|密钥|SESSDATA|bili_jct)\s*(?:是|为|[=:：])\s*[^\s，。；]{4,}"
)
HIGHLIGHT_ACCOUNT_FILTER = (
    "COALESCE(CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.operation_account_id') END,?)=?"
)


class MemoryChanged(ValueError):
    """A generated reply refers to memory that changed before delivery."""


def contains_memory_credential(text):
    return bool(_MEMORY_CREDENTIAL.search(text or ""))
