"""Public evidence for a weekly operations recap and its triggering run."""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ...infra.platforms.bilibili.urls import COMMENT_ID, VIDEO_ID, comment_href
from ...persona.memory.daily.models import contains_memory_credential
from .quotas import publication_time


def _json_value(raw):
    try:
        return json.loads(raw or "null")
    except (TypeError, ValueError):
        return None


def _object(raw):
    value = raw if isinstance(raw, dict) else _json_value(raw)
    return value if isinstance(value, dict) else {}


def _text(value, limit=160):
    if not isinstance(value, str) or contains_memory_credential(value):
        return ""
    return " ".join(re.sub(r"https?://[^\s]+", "", value).split())[:limit]


def _find_watches(store, scope):
    return store.all(
        "WITH observations AS ("
        " SELECT t.bvid, w.started_at AS seen_at FROM browsing_watch_starts w"
        " JOIN browsing_tasks t ON t.id=w.task_id JOIN browsing_rounds r ON r.id=t.round_id"
        " WHERE w.account_id=:account AND w.character_id=:character AND r.preview=0"
        " AND w.started_at>=:start AND w.started_at<:end AND COALESCE(t.updated_at,w.started_at)<:end"
        " AND t.status IN ('completed','partial','selected','viewed_without_comment','comments_checked')"
        " UNION ALL SELECT t.bvid,t.updated_at FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id"
        " WHERE r.account_id=:account AND r.character_id=:character AND r.preview=0"
        " AND t.updated_at>=:start AND t.updated_at<:end AND t.material_key IS NOT NULL"
        " AND COALESCE(json_extract(t.payload,'$.task_kind'),'') NOT IN ('comment_visit','comment_page','comment_batch')"
        " AND (t.status IN ('selected','viewed_without_comment') OR (t.status='comments_checked'"
        " AND json_type(t.payload,'$.decisions')='array') OR (t.status IN ('completed','partial')"
        " AND json_extract(t.payload,'$.full_coverage.viewing') IN ('watched','subtitles_only','visual_only'))))"
        " SELECT o.bvid, MAX(o.seen_at) AS seen_at, COALESCE(json_extract(p.watch_evidence,'$.material.metadata'),"
        " json_extract(p.material,'$.metadata')) AS metadata"
        " FROM observations o JOIN browsing_video_progress p ON p.account_id=:account"
        " AND p.character_id=:character AND p.bvid=o.bvid AND p.read_state IN ('complete','viewed')"
        " GROUP BY o.bvid ORDER BY seen_at DESC,o.bvid", scope,
    )


def _video_metadata(store, scope, resource):
    if not VIDEO_ID.fullmatch(resource):
        return {}
    row = store.one(
        "SELECT json_extract(material,'$.metadata') AS metadata FROM browsing_video_progress"
        " WHERE account_id=:account AND character_id=:character AND bvid=:resource",
        scope | {"resource": resource},
    )
    return _object(row["metadata"]) if row else {}


def _watch_item(row):
    bvid = row["bvid"]
    if not (url := comment_href(bvid)):
        return None
    metadata = _object(row["metadata"])
    if metadata.get("bvid", bvid) != bvid or not (title := _text(metadata.get("title"), 80)):
        return None
    description = _text(metadata.get("description"))
    return {"source_id": f"watch:{bvid}", "kind": "watched_video", "status": "watched",
            "title": title, "creator": _text(metadata.get("owner"), 48), "url": url, "bvid": bvid,
            "excerpt": description, "excerpt_kind": "video_description" if description else "", "occurred_at": row["seen_at"]}


def _add_watch_caption(store, scope, item):
    bvid = item["bvid"]
    row = store.one(
        "SELECT COALESCE(json_extract(watch_evidence,'$.material.parts'),json_extract(material,'$.parts')) AS parts,"
        " COALESCE(json_extract(watch_evidence,'$.material.metadata.parts'),json_extract(material,'$.metadata.parts')) AS pages"
        " FROM browsing_video_progress WHERE account_id=:account AND character_id=:character AND bvid=:bvid",
        scope | {"bvid": bvid},
    )
    pages = {str(part.get("cid")): (part.get("page") or index)
             for index, part in enumerate(_json_value(row["pages"]) or [], 1)}
    candidates = []
    for part in _json_value(row["parts"]) or []:
        page = pages.get(str(part.get("cid")))
        if part.get("visual") or not isinstance(page, int) or page <= 0:
            continue
        for segment in part.get("segments", []):
            if not isinstance(at := segment.get("start"), (int, float)) or not 0 <= at < 86_400:
                continue
            text = _text(segment.get("text"), 100)
            if len(text) >= 10:
                candidates.append((bool(part.get("is_ai")), at < 8, abs(42 - len(text)), page, at, text))
    if candidates:
        automatic, _, _, page, at, text = min(candidates)
        item.update(excerpt=text, excerpt_kind="automatic_subtitle" if automatic else "subtitle",
                    excerpt_url=f"{item['url']}?p={page}&t={int(at)}")


def _native_actions(store, scope):
    return store.all(
        "SELECT i.id,i.kind,i.status,i.target,i.receipt,i.visibility,"
        " CASE WHEN i.status='published' AND i.visibility='public_visible' THEN i.text END AS text,"
        " CASE WHEN i.status='pending_review' THEN i.created_at ELSE COALESCE(w.started_at,i.updated_at) END AS at,"
        " i.updated_at FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id"
        " LEFT JOIN account_writes w ON (w.account_id,w.character_id,w.intent)=(i.account_id,i.character_id,i.source_key)"
        " WHERE i.account_id=:account AND i.character_id=:character AND r.preview=0 AND"
        " ((i.kind IN ('top_level','reply_public') AND i.status='pending_review' AND i.created_at>=:start AND i.created_at<:end)"
        " OR (i.kind IN ('top_level','reply_public','video_like','comment_like','dynamic_like') AND i.status='published'"
        " AND i.receipt IS NOT NULL AND i.updated_at>=:start)) ORDER BY at DESC,i.id", scope,
    )


def _manual_comments(store, scope):
    for row in store.all(
        "SELECT 'draft:'||d.id AS id,d.status,d.receipt,d.visibility,c.video_id,c.comment_id,c.thread_id,"
        " c.platform_user_id,c.display_name,c.content,"
        " CASE WHEN d.status='published' AND d.visibility='public_visible' THEN d.text END AS text,"
        " CASE WHEN d.status='pending_review' THEN d.created_at ELSE d.updated_at END AS at,d.updated_at"
        " FROM drafts d LEFT JOIN comments c ON c.id=d.comment_id AND c.platform='bilibili'"
        " WHERE d.account_id=:account AND d.character_id=:character AND"
        " ((d.status='published' AND d.receipt IS NOT NULL AND d.updated_at>=:start)"
        " OR (d.status='pending_review' AND d.created_at>=:start AND d.created_at<:end AND c.video_id IS NOT NULL))"
        " ORDER BY at DESC,d.id", scope,
    ):
        source = {"comment_id": row["comment_id"], "author_uid": row["platform_user_id"],
                  "author_name": row["display_name"], "text": row["content"]}
        target = {"bvid": row["video_id"], "parent": row["comment_id"], "root": row["thread_id"],
                  "recipient": row["platform_user_id"], "source_discussion": {"source": source}}
        yield row | {"kind": "reply_public", "target": target}


def _target_comment(store, scope, resource, comment_id, target):
    if not COMMENT_ID.fullmatch(comment_id):
        return "", ""
    observed = store.one(
        "SELECT author_uid,author_name,text FROM browsing_comment_timeline"
        " WHERE account_id=:account AND character_id=:character AND resource=:resource AND comment_id=:comment"
        " ORDER BY created_at DESC LIMIT 1", scope | {"resource": resource, "comment": comment_id},
    )
    discussion = target.get("source_discussion") or {}
    source = discussion.get("source") or {}
    author = str(target.get("recipient") or "")
    if not observed and str(source.get("comment_id")) == comment_id:
        observed = source
    if not observed or not observed.get("author_uid") or (author and str(observed["author_uid"]) != author):
        return "", ""
    return _text(observed.get("text")), _text(observed.get("author_name"), 48)


def _action_item(store, scope, row, target, receipt, kind, state, occurred_at):
    dynamic = target.get("dynamic_id") or (target.get("oid") if target.get("type") == 17 else None)
    resource = str(target.get("bvid") or receipt.get("video_id") or (f"dynamic:{dynamic}" if dynamic else ""))
    parent = str(target.get("parent") or "")
    target_url = comment_href(resource, parent)
    if not target_url:
        return None
    public_rpid = str(receipt.get("rpid") or "") if state == "public_visible" else ""
    url = comment_href(resource, public_rpid or parent)
    metadata = _video_metadata(store, scope, resource)
    prefix = {"pending_review": "待审评论", "public_visible": "公开评论", "confirmed": "点赞"}[state]
    title = _text(metadata.get("title") or target.get("title"), 80) or ("动态" if dynamic else "视频") + prefix
    own_excerpt = _text(row.get("text")) if state == "public_visible" else ""
    target_excerpt, target_author = _target_comment(store, scope, resource, parent, target)
    return {"source_id": f"{kind}:{resource}:{public_rpid or parent or 'main'}", "kind": kind, "status": state,
            "title": title, "url": url, "bvid": resource if VIDEO_ID.fullmatch(resource) else None,
            "creator": _text(metadata.get("owner"), 48), "excerpt": own_excerpt,
            "target_excerpt": target_excerpt, "target_author": target_author,
            "target_url": target_url, "occurred_at": occurred_at}


def _dynamic_item(store, scope, row, receipt):
    published = row["status"] == "published"
    sources = _json_value(row["sources"])
    sources = sources if isinstance(sources, list) else []
    if any(item.get("type") == "operations_closeout" for item in sources if isinstance(item, dict)):
        return None
    if published:
        result = {"source_id": f"dynamic:{receipt['dynamic_id']}", "kind": "public_dynamic", "status": "confirmed",
                  "title": "有发布回执的动态", "url": comment_href(f"dynamic:{receipt['dynamic_id']}"), "occurred_at": row["at"]}
    else:
        result = {"kind": "pending_dynamic", "status": "pending_review", "occurred_at": row["at"]}
    # Only public origins explicitly bound to the post can describe its topic.
    for item in sources:
        if not isinstance(item, dict) or item.get("fictional"):
            continue
        if item.get("type") == "watched_video":
            video = item.get("video") or {}
            resource = str(video.get("bvid") or "")
            if (url := comment_href(resource)) and VIDEO_ID.fullmatch(resource) and (title := _text(video.get("title"), 80)):
                evidence = {"title": title, "bvid": resource, "creator": _text(video.get("owner"), 48), "target_url": url}
                return {"source_id": f"pending_dynamic:{resource}", "url": url} | result | evidence
        if item.get("type") == "public_comment":
            reference = item.get("reference") or {}
            resource, comment = str(reference.get("resource") or ""), str(reference.get("comment_id") or "")
            if not COMMENT_ID.fullmatch(comment) or not (url := comment_href(resource, comment)):
                continue
            excerpt, author = _target_comment(store, scope, resource, comment, {"recipient": reference.get("author_uid")})
            if excerpt:
                metadata = _video_metadata(store, scope, resource)
                evidence = {"title": _text(metadata.get("title"), 80) or "动态引用的公开评论",
                            "bvid": resource if VIDEO_ID.fullmatch(resource) else None,
                            "target_url": url, "target_excerpt": excerpt, "target_author": author}
                return {"source_id": f"pending_dynamic:{resource}:{comment}", "url": url} | result | evidence
    return result if published else None


def _select(items, run_start, day_start):
    groups = defaultdict(list)
    for item in items:
        item["in_current_run"] = item["occurred_at"] >= run_start
        item["earlier_in_week"] = item["occurred_at"] < day_start
        groups[item["kind"]].append(item)
    selected, urls, identities = [], set(), set()
    for group in groups.values():
        group.sort(key=lambda item: (-item["occurred_at"], item["source_id"]))
        item = next((item for item in group if item["url"] not in urls), group[0])
        selected.append(item)
        urls.add(item["url"])
        identities.add(item["source_id"])
    if not any(item["earlier_in_week"] for item in selected):
        older = [item for item in items if item["earlier_in_week"] and item["url"] not in urls
                 and item["source_id"] not in identities]
        if older:
            item = max(older, key=lambda record: (record["kind"] == "watched_video", record["occurred_at"]))
            selected.append(item)
            urls.add(item["url"])
            identities.add(item["source_id"])
    for group in groups.values():
        for item in group:
            if len(selected) >= 8:
                return selected
            if item["url"] not in urls and item["source_id"] not in identities:
                selected.append(item)
                urls.add(item["url"])
                identities.add(item["source_id"])
                break
    return selected


def public_period_brief(store, run):
    """Select observed topics from the preceding seven local days through the closeout."""
    zone = ZoneInfo(run["period_timezone"])
    run_start, end = (datetime.fromtimestamp(run[f"period_{key}_at"], zone) for key in ("start", "end"))
    start = end - timedelta(days=7)
    scope = {"account": run["account_id"], "character": run["character_id"],
             "start": start.timestamp(), "end": end.timestamp()}
    counters = {key: set() for key in ("watched_videos", "accepted_comments", "public_comments", "public_dynamics",
                                       "review_comments", "confirmed_likes", "review_dynamics")}
    current = {key: set() for key in counters}
    items = []

    def count(key, identity, at):
        counters[key].add(identity)
        if at >= run_start.timestamp():
            current[key].add(identity)

    for row in _find_watches(store, scope):
        count("watched_videos", row["bvid"], row["seen_at"])
        if item := _watch_item(row):
            items.append(item)
    for row in [*_native_actions(store, scope), *_manual_comments(store, scope)]:
        native_kind, target, receipt = row["kind"], _object(row["target"]), _object(row["receipt"])
        occurred_at = publication_time(receipt, row["at"]) if row["status"] == "published" else row["at"]
        if not scope["start"] <= occurred_at < scope["end"]:
            continue
        if row["status"] == "pending_review":
            identity = (target.get("bvid") or target.get("oid"), target.get("parent"), native_kind)
            count("review_comments", identity if identity[0] else row["id"], occurred_at)
            kind, state = "pending_comment", "pending_review"
        elif native_kind.endswith("_like"):
            if receipt.get("code") != 0 or receipt.get("already_liked"):
                continue
            identity = (native_kind, receipt.get("rpid") or receipt.get("video_id") or receipt.get("dynamic_id")
                        or target.get("parent") or target.get("oid") or row["id"])
            count("confirmed_likes", identity, occurred_at)
            kind, state = "confirmed_like", "confirmed"
        else:
            rpid = str(receipt.get("rpid") or "")
            if not COMMENT_ID.fullmatch(rpid) or receipt.get("code", 0) != 0:
                continue
            count("accepted_comments", rpid, occurred_at)
            if row["visibility"] != "public_visible":
                continue
            count("public_comments", rpid, occurred_at)
            kind = "public_reply" if native_kind == "reply_public" else "public_comment"
            state = "public_visible"
        if item := _action_item(store, scope, row, target, receipt, kind, state, occurred_at):
            items.append(item)
    for row in store.all(
        "SELECT id,status,receipt,sources,"
        " CASE WHEN status='published' THEN published_at ELSE created_at END AS at"
        " FROM dynamic_posts WHERE account_id=:account AND character_id=:character AND"
        " ((status='published' AND published_at>=:start AND published_at<:end)"
        " OR (status='pending_review' AND created_at>=:start AND created_at<:end)) ORDER BY at DESC,id DESC", scope,
    ):
        receipt = _object(row["receipt"])
        if row["status"] == "published":
            if receipt.get("code", 0) != 0 or not COMMENT_ID.fullmatch(str(receipt.get("dynamic_id"))):
                continue
            count("public_dynamics", str(receipt["dynamic_id"]), row["at"])
        else:
            count("review_dynamics", row["id"], row["at"])
        if item := _dynamic_item(store, scope, row, receipt):
            items.append(item)

    day_start = run_start.replace(hour=0, minute=0, second=0, microsecond=0)
    selected = _select(items, run_start.timestamp(), day_start.timestamp())
    sources, highlights = [], []
    for item in selected:
        is_video = item["kind"] == "watched_video"
        if is_video and not item["excerpt"]:
            _add_watch_caption(store, scope, item)
        sources.append({"source_id": item["source_id"], "kind": "watched_video" if is_video else
                        "public_comment" if item.get("target_excerpt") or item["status"] == "public_visible"
                        else "public_incident", "title": item["title"], "url": item["url"], "bvid": item.get("bvid"),
                        "excerpt": item.get("excerpt") or item.get("target_excerpt") or ""})
        if is_video:
            credit = f"UP 主 {item['creator']}" if item["creator"] else "B 站投稿"
            highlights.append({"title": item["title"], "text": f"看过《{item['title']}》。",
                               "source_id": item["source_id"], "credit": credit})
    name = _text(store.character(run["character_id"], run["character_version"]).get("name"), 32)
    label = f"{start:%Y-%m-%d %H:%M} 至 {end:%m-%d %H:%M}（{zone.key}）"
    run_label = f"{run_start:%Y-%m-%d %H:%M} 至 {end:%m-%d %H:%M}（{zone.key}）"
    return {"title": f"{name}的运营周报", "period_label": label, "run_period_label": run_label,
            **{key: len(values) for key, values in counters.items()},
            "current_run": {key: len(values) for key, values in current.items()},
            "sources": sources, "highlights": highlights, "editorial_items": selected}
