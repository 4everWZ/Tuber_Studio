"""Canonical Bilibili resource and comment thread URL helpers."""

import re

VIDEO_ID = re.compile(r"BV[0-9A-Za-z]{10}")
COMMENT_ID = re.compile(r"[1-9][0-9]{0,19}")


def public_discussion_url(bvid=None, comment_id=None, dynamic_id=None):
    """Build a public Bilibili video or dynamic URL with optional reply anchor."""
    suffix = f"#reply{comment_id}" if COMMENT_ID.fullmatch(str(comment_id)) else ""
    if VIDEO_ID.fullmatch(str(bvid)):
        return f"https://www.bilibili.com/video/{bvid}{suffix}"
    if COMMENT_ID.fullmatch(str(dynamic_id)):
        return f"https://t.bilibili.com/{dynamic_id}{suffix}"
    return None


def comment_href(resource, comment_id=None):
    """Build a canonical Bilibili link for a BV video or dynamic:id target."""
    if str(resource).startswith("dynamic:"):
        return public_discussion_url(comment_id=comment_id, dynamic_id=resource.removeprefix("dynamic:"))
    return public_discussion_url(resource, comment_id)
