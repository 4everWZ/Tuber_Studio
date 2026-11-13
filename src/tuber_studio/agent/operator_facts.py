"""Source-backed facts for the operator's conversation."""

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from ..operations.browsing.activity_history import activity
from ..operations.browsing.comment_timeline import comment_thread


def now(browsing):
    return datetime.now(ZoneInfo(browsing.policy()["business_timezone"]))


def _excerpt(browsing, account, character, bvid):
    progress = browsing.repo.content_progress({"account_id": account, "character_id": character}, bvid)
    material = (progress or {}).get("material") or {}
    for part in material.get("parts", []):
        for segment in part.get("segments", []):
            if text := segment.get("text"):
                number = part.get("part") or 1
                start = segment.get("start") or 0
                return {"text": text, "kind": "visual" if part.get("visual") else "subtitle", "part": number,
                        "start": start, "href": f"https://www.bilibili.com/video/{bvid}?p={number}&t={int(start)}"}
    return None


def refresh_reply(browsing, event):
    anchor = event.get("thread")
    if not anchor:
        return event
    account, character, _ = browsing.discovery.history_scope()
    thread = comment_thread(browsing.store, anchor["resource"], anchor["root_id"],
                            account_id=account, character_id=character)
    for item in reversed(thread["comments"]):
        if item["parent_comment_id"] != anchor["comment_id"]:
            continue
        state = {"confirmed": "confirmed", "same_account": "same_account_replied",
                 "owner_replied": "owner_replied"}.get(item["publication"])
        if state:
            return event | {"reply": {"state": state, "text": item["text"], "href": item["href"]}}
    return event


def daily_events(browsing, day):
    account, character, _ = browsing.discovery.history_scope()
    if not account:
        return []
    # Public browsing/comment sources only. Exclude drafts and outgoing/private material.
    videos = activity(browsing, day=day, kind="videos", page_size=100)["items"]
    comments = activity(browsing, day=day, kind="comments", page_size=100)["items"]
    by_video = defaultdict(list)
    for comment in comments:
        if comment.get("bvid") and comment.get("source_text"):
            by_video[comment["bvid"]].append(comment)
    metadata = {video["bvid"]: video for video in videos}
    candidates = set(metadata) | set(by_video)
    events = []
    for bvid in candidates:
        video = metadata.get(bvid, {})
        thread = sorted(by_video.get(bvid, []), key=lambda item: (
            item["status"] != "published", not item.get("source_href"), -item["updated_at"], item["id"],
        ))
        comment = thread[0] if thread else None
        if not comment and not video.get("watched"):
            continue
        source = None
        reply = None
        anchor = None
        if comment:
            source = {"kind": "comment", "text": comment["source_text"], "author": comment.get("recipient_name"),
                      "uid": comment.get("recipient_uid"), "href": comment.get("source_href")}
            if context := comment.get("thread"):
                timeline = comment_thread(browsing.store, context["resource"], context["root_id"],
                                          account_id=account, character_id=character)
                matched = next((item for item in timeline["comments"] if item["href"] == source["href"]), None)
                if matched:
                    source.update(author=matched["author_name"] or source["author"], uid=matched["author_uid"] or source["uid"])
                    anchor = {"resource": context["resource"], "root_id": context["root_id"], "comment_id": matched["comment_id"]}
            if comment.get("comment_outcome") == "new_reply" and comment.get("receipt"):
                reply = {"state": "confirmed", "text": comment["text"], "href": comment.get("sent_href")}
            elif comment["status"] == "publish_uncertain":
                reply = {"state": "uncertain"}
            elif state := {"previous_reply": "previously_replied", "owner_replied": "owner_replied",
                           "same_account": "same_account_replied"}.get(comment.get("comment_outcome")):
                reply = {"state": state, "text": comment.get("confirmed_reply_text") or comment.get("account_reply_text"),
                         "href": comment.get("sent_href")}
        material = _excerpt(browsing, account, character, bvid)
        if not source and not material:
            continue
        events.append({"bvid": bvid, "day": day, "title": video.get("title") or bvid,
                       "href": f"https://www.bilibili.com/video/{bvid}", "source": source or material,
                       "material": material if source else None, "reply": reply, "thread": anchor,
                       "at": max(video.get("updated_at", 0), (comment or {}).get("updated_at", 0))})
    return sorted(events, key=lambda item: (-bool(item["reply"]), -bool(item["source"].get("href")),
                                            -bool(item["material"]), -item["at"], item["bvid"]))
