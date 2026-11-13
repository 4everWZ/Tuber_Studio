"""Source-backed facts for the operator's conversation."""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..operations.browsing.activity_history import activity
from ..operations.browsing.comment_timeline import comment_href, comment_thread


def now(browsing):
    return datetime.now(ZoneInfo(browsing.policy()["business_timezone"]))


def _excerpt(browsing, account, character, bvid):
    scope = {"account_id": account, "character_id": character}
    progress = browsing.repo.content_progress(scope, bvid)
    priority = browsing.repo.watch_evidence(scope, bvid)
    material = (priority or {}).get("material") or (progress or {}).get("material") or {}
    pages = {str(part["cid"]): part.get("page", index)
             for index, part in enumerate(material.get("metadata", {}).get("parts", []), 1)}
    for part in material.get("parts", []):
        if part.get("asr"):
            continue
        for segment in part.get("segments", []):
            if text := segment.get("text"):
                number = pages.get(str(part.get("cid")))
                if not number:
                    continue
                start = segment.get("start") or 0
                return {"text": text, "kind": "visual" if part.get("visual") else "subtitle", "part": number,
                        "start": start, "href": f"https://www.bilibili.com/video/{bvid}?p={number}&t={int(start)}"}
    return None


def refresh_reply(browsing, event, timeline=None):
    anchor = event.get("thread")
    if not anchor:
        return event
    account, character, _ = browsing.discovery.history_scope()
    thread = timeline if timeline is not None else comment_thread(
        browsing.store, anchor["resource"], anchor["root_id"], account_id=account, character_id=character,
    )
    replies = [item for item in thread["comments"] if item["parent_comment_id"] == anchor["comment_id"]
               and item["publication"] in {"confirmed", "same_account", "owner_replied"}]
    if replies:
        item = max(replies, key=lambda reply: (reply["created_at"] or 0, int(reply["comment_id"])))
        state = {"confirmed": "confirmed", "same_account": "same_account_replied",
                 "owner_replied": "owner_replied"}[item["publication"]]
        return event | {"reply": {"state": state, "text": item["text"], "href": item["href"]}}
    return event


def daily_events(browsing, day):
    account, character, _ = browsing.discovery.history_scope()
    if not account:
        return []
    # Public browsing/comment sources only. Exclude drafts and outgoing/private material.
    videos = activity(browsing, day=day, kind="videos", page_size=100)["items"]
    comments = activity(browsing, day=day, kind="comments", page_size=100)["items"]
    by_thread = defaultdict(list)
    for comment in comments:
        context = comment.get("thread") or {}
        resource = context.get("resource") or comment.get("bvid")
        if resource and comment.get("source_text") and comment.get("source_href"):
            by_thread[resource, context.get("root_id") or comment["source_href"]].append(comment)
    metadata = {video["bvid"]: video for video in videos}
    linked_resources = {resource for resource, _ in by_thread}
    candidates = list(by_thread) + [(resource, None) for resource in metadata.keys() - linked_resources]
    excerpts = {}
    events = []
    for resource, root in candidates:
        bvid = None if resource.startswith("dynamic:") else resource
        video = metadata.get(resource, {})
        related = by_thread.get((resource, root), [])
        comment = min(related, key=lambda item: (
            item.get("comment_outcome") not in {"new_reply", "previous_reply", "same_account", "owner_replied"},
            item["status"] != "publish_uncertain", item["updated_at"], item["id"],
        ), default=None)
        if not comment and not video.get("watched"):
            continue
        source = None
        reply = None
        anchor = None
        timeline = None
        if comment:
            source = {"kind": "comment", "text": comment["source_text"], "author": comment.get("recipient_name"),
                      "uid": comment.get("recipient_uid"), "href": comment.get("source_href")}
            if context := comment.get("thread"):
                timeline = comment_thread(browsing.store, context["resource"], context["root_id"],
                                          account_id=account, character_id=character)
                matched = next((item for item in timeline["comments"] if item["href"] == source["href"]), None)
                if matched:
                    source.update(author=matched["author_name"] or source["author"], uid=matched["author_uid"] or source["uid"])
                comment_id = (matched or {}).get("comment_id") or comment.get("source_comment_id")
                if comment_id:
                    anchor = {"resource": context["resource"], "root_id": context["root_id"], "comment_id": comment_id}
            if comment.get("comment_outcome") == "new_reply" and comment.get("receipt"):
                reply = {"state": "confirmed", "text": comment["text"], "href": comment.get("sent_href")}
            elif comment["status"] == "publish_uncertain":
                reply = {"state": "uncertain"}
            elif state := {"previous_reply": "previously_replied", "owner_replied": "owner_replied",
                           "same_account": "same_account_replied"}.get(comment.get("comment_outcome")):
                reply = {"state": state, "text": comment.get("confirmed_reply_text") or comment.get("account_reply_text"),
                         "href": comment.get("sent_href")}
        if bvid and bvid not in excerpts:
            excerpts[bvid] = _excerpt(browsing, account, character, bvid)
        material = excerpts.get(bvid)
        if not source and not material:
            continue
        href = comment_href(resource)
        event = {"bvid": bvid, "resource": resource, "root_id": root, "day": day,
                       "title": video.get("title") or bvid or "动态评论", "href": href, "source": source or material,
                       "material": material if source else None, "reply": reply, "thread": anchor,
                       "at": min(item["updated_at"] for item in related) if related else video["updated_at"]}
        events.append(refresh_reply(browsing, event, timeline))
    return sorted(events, key=lambda item: (
        -bool(item["reply"]), -bool(item["source"].get("href")), -bool(item["material"]),
        -item["at"], item["resource"], item["root_id"] or "",
    ))


def recent_events(browsing, *, limit=6):
    current = now(browsing)
    items = {}
    if browsing.discovery.history_scope()[0]:
        for offset in range(3):
            day = (current.date() - timedelta(days=offset)).isoformat()
            for event in daily_events(browsing, day):
                items.setdefault((event["resource"], event["root_id"]), event)
                if len(items) == limit:
                    break
            if len(items) == limit:
                break
    return {"day": current.date().isoformat(), "timezone": str(current.tzinfo), "items": list(items.values())}
