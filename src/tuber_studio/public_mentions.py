"""Bind an explicit public mention to both a Bilibili account and caption text."""

import json
import re

import httpx


UID = re.compile(r"[1-9][0-9]{0,19}")
MENTION_REQUESTS = {
    "type": "array",
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["label", "account"],
        "properties": {
            "label": {"type": "string", "minLength": 2, "maxLength": 101},
            "account": {"type": "string", "minLength": 1, "maxLength": 100},
        },
    },
}
MENTION_INSTRUCTION = (
    "想真实 @ 某人时，在正文写 @昵称，在 mentions 填原样 label 和对方公开 UID 或准确昵称 account。"
    "只想叫名字时照常写，mentions 留空。"
)


def mention_requests(value):
    if not isinstance(value, list):
        raise ValueError("提及须为列表")
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "account"}:
            raise ValueError("提及须提供正文中的 @昵称和公开账号")
        label, account = item["label"], item["account"]
        if not (isinstance(label, str) and 2 <= len(label) <= 101 and label.startswith("@")
                and isinstance(account, str) and 1 <= len(account) <= 100):
            raise ValueError("提及须提供正文中的 @昵称和公开账号")
    return value


def _ranges(text, requests):
    end = 0
    for item in requests:
        label = item["label"]
        start = text.find(label, end)
        if start < 0:
            raise ValueError(f"正文中找不到 {label}")
        end = start + len(label)
        yield item, start, end


async def bind_mentions(adapter, text, requests, participants=()):
    """Resolve explicit selections only. Keep unresolved targets on the draft."""
    bound, pending, checked = [], [], {}
    mention_requests(requests)
    if not isinstance(text, str):
        raise ValueError("正文须为字符串")
    try:
        placements = list(_ranges(text, requests))
    except ValueError:
        return [], requests
    for item, start, end in placements:
        account = item["account"]
        if account not in checked:
            candidates = {
                str(person["uid"]): person["name"] for person in participants
                if UID.fullmatch(str(person.get("uid"))) and person.get("name")
                and account in (str(person["uid"]), person["name"])
            }
            try:
                if not candidates and UID.fullmatch(account):
                    profile = await adapter.public_profile(account)
                    if str(profile["uid"]) == account and profile.get("name"):
                        candidates[account] = profile["name"]
                elif not candidates:
                    candidates.update(await adapter.mention_accounts(account))
            except (ValueError, httpx.HTTPError, AttributeError):
                pass
            checked[account] = candidates
        candidates = checked[account]
        if len(candidates) != 1:
            pending.append(item | ({"candidates": [{"uid": uid, "name": name} for uid, name in candidates.items()]}
                                   if candidates else {}))
            continue
        uid, name = next(iter(candidates.items()))
        if item["label"] != "@" + name:
            pending.append(item | {"candidates": [{"uid": uid, "name": name}]})
            continue
        bound.append({"start": start, "end": end, "uid": uid, "name": name})
    return bound, pending


def rebase_mentions(text, mentions):
    requests = [{"label": "@" + item["name"], "account": item["uid"]} for item in mentions]
    return [original | {"start": start, "end": end}
            for original, (_, start, end) in zip(mentions, _ranges(text, requests), strict=True)]


def revision_payload(payload, text):
    revised = payload | {"text": text}
    if payload.get("mentions"):
        revised["mentions"] = rebase_mentions(text, payload["mentions"])
    return revised


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
    return {"at_name_to_mid": json.dumps({name: int(uid) for name, uid in names.items()},
                                         ensure_ascii=False, separators=(",", ":"))}


def dynamic_read_mentions(summary):
    mentions, offset = [], 0
    for node in summary.get("rich_text_nodes") or []:
        text = node.get("orig_text") or node.get("text") or ""
        uid = str(node.get("rid", ""))
        if node.get("type") == "RICH_TEXT_NODE_TYPE_AT" and UID.fullmatch(uid):
            mentions.append({"start": offset, "end": offset + len(text), "uid": uid, "name": text.removeprefix("@")})
        offset += len(text)
    return mentions


def draft_mentions(row):
    saved = row.get("target") or row.get("answer_check")
    if isinstance(saved, str):
        saved = json.loads(saved)
    return saved if isinstance(saved, dict) else {}


def mention_participants(thread, video=None):
    people = [thread.get(key) or {} for key in ("source", "root", "parent")]
    people.extend(thread.get("timeline") or [])
    people.extend(thread.get("grouped_comments") or [])
    participants = [{"uid": person.get("author_uid"), "name": person.get("author_name")} for person in people]
    if video:
        participants.append({"uid": video.get("owner_uid"), "name": video.get("owner")})
    return participants
