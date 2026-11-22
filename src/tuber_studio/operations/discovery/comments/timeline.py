"""Shared comment timeline imports scoped for Discovery components."""

from ...channels.comments.timeline import (
    _extract_timestamp,
    comment_source,
    comment_thread_context,
    merge_continuous_comments,
    observe_thread,
    operator_reply,
    read_scoped_thread,
    resource_key,
    source_discussion,
)

__all__ = [
    "_extract_timestamp",
    "comment_source",
    "comment_thread_context",
    "merge_continuous_comments",
    "observe_thread",
    "operator_reply",
    "read_scoped_thread",
    "resource_key",
    "source_discussion",
]
