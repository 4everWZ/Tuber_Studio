"""Bilibili platform wire helpers for @mention encoding and decoding."""

import json
import re

UID = re.compile(r"[1-9][0-9]{0,19}")


def dynamic_contents(text, mentions):
    contents, end = [], 0
    for item in mentions:
        start, stop = item["start"], item["end"]
        if start < end or text[start:stop] != "@" + item["name"] or not UID.fullmatch(item["uid"]):
            raise ValueError("正文中的提及已变化")
        if start > end:
            contents.append({"raw_text": text[end:start], "type": 1, "biz_id": ""})
        contents.append({"raw_text": text[start:stop], "type": 2, "biz_id": item["uid"]})
        end = stop
    if end < len(text):
        contents.append({"raw_text": text[end:], "type": 1, "biz_id": ""})
    return contents


def comment_mention_form(text, mentions):
    if not mentions:
        return {}
    dynamic_contents(text, mentions)  # Same text and range validation.
    names = {item["name"]: item["uid"] for item in mentions}
    if any(names[item["name"]] != item["uid"] for item in mentions):
        raise ValueError("同一条评论中的同名提及无法区分")
    return {
        "at_name_to_mid": json.dumps(
            {name: int(uid) for name, uid in names.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }


def dynamic_read_mentions(summary):
    mentions, offset = [], 0
    for node in summary.get("rich_text_nodes") or []:
        text = node.get("orig_text") or node.get("text") or ""
        uid = str(node.get("rid", ""))
        if node.get("type") == "RICH_TEXT_NODE_TYPE_AT" and UID.fullmatch(uid):
            mentions.append(
                {"start": offset, "end": offset + len(text), "uid": uid, "name": text.removeprefix("@")}
            )
        offset += len(text)
    return mentions
