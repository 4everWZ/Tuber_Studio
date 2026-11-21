"""Read-only projections used by both the conversation and task tools."""

import datetime as dt
import json
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from ..models import StrictModel
from ..operation_continuations import content_hash
from ..operations.browsing.comment_timeline import comment_href
from ..operations.browsing.quotas import day_window
from ..store import dump
from . import operator_facts


LOOKUP_TOOL = "workshop_lookup"
LOOKUP_DESCRIPTION = (
    "按对象检索工坊记录。video 查已读视频和片段；"
    "comment 查公开评论；dynamic 查动态草稿和平台记录。"
    "day 是业务日，查今天填 today；不指定日期则查跨日材料。"
    "terms 从当前要求或讨论的标题、对象和原话提取，优先用具体名称。"
)
_BV = re.compile(r"BV[0-9A-Za-z]{10}")


class WorkshopLookup(StrictModel):
    collection: Literal["watch_count", "video", "comment", "dynamic"]
    day: Literal["today", "yesterday"] | dt.date | None = None
    terms: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=4,
    )
    # Identity is supplied by a verified conversation reference, or used as
    # an exact scoped filter by a task. It carries no access authority.
    identity: str | None = Field(default=None, max_length=140)
    part: int | None = Field(default=None, ge=1)
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def period(browsing, day=None):
    current = operator_facts.now(browsing)
    if day in (None, "today"):
        day = current.date()
    elif day == "yesterday":
        day = current.date() - dt.timedelta(days=1)
    return {"timezone": current.tzinfo.key, **day_window(
        current.tzinfo.key, dt.datetime.combine(day, dt.time(), tzinfo=current.tzinfo).timestamp(),
    )}


def watch_rows(store, account, character, window):
    return store.all(
        "WITH recorded AS ("
        " SELECT w.bvid,w.started_at,t.round_id,t.status,t.material_key,t.payload"
        " FROM browsing_watch_starts w LEFT JOIN (browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id)"
        " ON t.id=w.task_id AND r.account_id=w.account_id AND r.character_id=w.character_id"
        " WHERE w.account_id=? AND w.character_id=? AND w.started_at>=? AND w.started_at<?"
        " UNION ALL"
        " SELECT t.bvid,t.updated_at,t.round_id,t.status,t.material_key,t.payload"
        " FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id"
        " WHERE r.account_id=? AND r.character_id=? AND t.updated_at>=? AND t.updated_at<?"
        " AND COALESCE(json_extract(t.payload,'$.task_kind'),'')<>'comment_visit'"
        " AND COALESCE(r.preview,0)=0 AND t.status IN ('selected','viewed_without_comment')"
        " AND t.material_key IS NOT NULL AND NOT EXISTS (SELECT 1 FROM browsing_watch_starts"
        " WHERE account_id=r.account_id AND character_id=r.character_id AND task_id=t.id)"
        ") SELECT bvid,started_at,round_id,status,material_key,"
        "json_extract(payload,'$.coverage') AS coverage,"
        "json_extract(payload,'$.full_coverage.viewing') AS priority_viewing,"
        "COALESCE(json_array_length(payload,'$.missing_parts'),0) AS missing_parts"
        " FROM recorded ORDER BY started_at DESC",
        (account, character, window["start_at"], window["next_at"]) * 2,
    )


def _read(row):
    return (row["status"] in {"selected", "viewed_without_comment"} and bool(row["material_key"])) or (
        row["status"] == "completed" and row["priority_viewing"] in {"watched", "subtitles_only", "visual_only"}
    )


def _complete(row):
    return row["priority_viewing"] == "watched" or (
        row["coverage"] == "all_available_cc" and not row["missing_parts"]
    )


def viewing(store, account, character, window, rows=None):
    if not account:
        return window | {"available": False}
    if rows is None:
        rows = watch_rows(store, account, character, window)
    started = {row["bvid"] for row in rows}
    read = {row["bvid"] for row in rows if _read(row)}
    full = {row["bvid"] for row in rows if _read(row) and _complete(row)}
    pending = {row["bvid"] for row in rows if row["status"] in {
        "pending", "queued", "running", "reading", "waiting", "paused", "deferred",
    }} - read
    return window | {
        "available": True, "started": len(started), "completed": len(read),
        "fully_read": len(full), "partial": len(read - full), "in_progress": len(pending),
        "without_result": len(started - pending - read),
        "records": [{key: row[key] for key in ("bvid", "started_at", "round_id", "status", "coverage")}
                    for row in rows[:20]],
    }


def _terms(args):
    return dump(list(dict.fromkeys(term.casefold() for term in args.terms)))


def _video_rows(store, account, character, args, day_ids):
    # Rank and limit after scope and time selection. Source material stays in
    # the browsing repository; a lookup never starts or counts another watch.
    return store.all(
        "WITH scoped AS MATERIALIZED (SELECT bvid,read_state,updated_at,"
        "COALESCE(json_extract(watch_evidence,'$.material'),material) AS body "
        "FROM browsing_video_progress WHERE account_id=? AND character_id=? "
        "AND read_state IN ('complete','viewed') AND (? IS NULL OR bvid=?) "
        "AND (? IS NULL OR bvid IN (SELECT value FROM json_each(?)))) "
        "SELECT *, (SELECT COUNT(*) FROM json_each(?) WHERE instr(lower(bvid||' '||body), value)>0) AS hits "
        "FROM scoped WHERE json_array_length(?)=0 OR hits>0 ORDER BY hits DESC,updated_at DESC,bvid LIMIT 6",
        (account, character, args.identity, args.identity, day_ids, day_ids, _terms(args), _terms(args)),
    )


def _segments(body, args, bvid):
    pages = {str(part["cid"]): index for index, part in enumerate(body["metadata"].get("parts", []), 1)}
    segments = []
    for part in body.get("parts", []):
        number = pages.get(str(part.get("cid")))
        if not number or args.part not in (None, number):
            continue
        kind = "visual" if part.get("visual") else "transcript" if part.get("asr") else "subtitle"
        for segment in part.get("segments", []):
            if not isinstance(segment.get("text"), str) or not segment["text"].strip():
                continue
            start = segment.get("start") or 0
            if not isinstance(start, (int, float)) or start < 0:
                continue
            end = segment.get("end")
            if not isinstance(end, (int, float)):
                end = start
            if args.seconds is not None and not start <= args.seconds <= end:
                continue
            score = sum(term.casefold() in segment["text"].casefold() for term in args.terms)
            entry = {"kind": kind, "text": segment["text"][:320], "part": number, "start": start, "end": end,
                     "href": f"https://www.bilibili.com/video/{bvid}?p={number}&t={int(start)}"}
            segments.append((score, entry))
    segments.sort(key=lambda item: (-item[0], item[1]["part"], item[1]["start"]))
    return [entry for _, entry in segments[:3]]


def _videos(store, account, character, args, day_ids):
    items = []
    for row in _video_rows(store, account, character, args, day_ids):
        body = json.loads(row["body"] or "{}")
        bvid = row["bvid"]
        if not _BV.fullmatch(bvid) or body.get("metadata", {}).get("bvid") != bvid:
            continue
        entries = _segments(body, args, bvid)
        title = str(body["metadata"].get("title") or bvid)
        items.append({"kind": "watched_video", "identity": bvid, "title": title,
                      "revision": row["updated_at"], "state": row["read_state"], "entries": entries,
                      "author": body["metadata"].get("owner"),
                      "href": f"https://www.bilibili.com/video/{bvid}"})
    return items


def _comments(store, account, character, args, window):
    rows = store.all(
        "WITH scoped AS MATERIALIZED (SELECT resource,root_id,comment_id,text,author_name,author_uid,"
        "COALESCE((SELECT MAX(observed_at) FROM browsing_comment_observations o "
        "WHERE o.account_id=browsing_comment_timeline.account_id AND o.character_id=browsing_comment_timeline.character_id "
        "AND o.resource=browsing_comment_timeline.resource AND o.root_id=browsing_comment_timeline.root_id "
        "AND o.comment_id=browsing_comment_timeline.comment_id),created_at) AS updated_at,"
        "'comment:'||resource||':'||root_id||':'||comment_id AS identity FROM browsing_comment_timeline "
        "WHERE account_id=? AND character_id=? AND text<>'' AND (? IS NULL OR EXISTS ("
        " SELECT 1 FROM browsing_comment_observations o WHERE o.account_id=browsing_comment_timeline.account_id "
        " AND o.character_id=browsing_comment_timeline.character_id AND o.resource=browsing_comment_timeline.resource "
        " AND o.root_id=browsing_comment_timeline.root_id AND o.comment_id=browsing_comment_timeline.comment_id "
        " AND o.observed_at>=? AND o.observed_at<?))) "
        "SELECT *, (SELECT COUNT(*) FROM json_each(?) WHERE instr(lower(text||' '||COALESCE(author_name,'')),value)>0) hits "
        "FROM scoped WHERE (? IS NULL OR identity=?) AND (json_array_length(?)=0 OR hits>0) "
        "ORDER BY hits DESC,updated_at DESC,identity LIMIT 6",
        (account, character, window.get("day"), window.get("start_at"), window.get("next_at"),
         _terms(args), args.identity, args.identity, _terms(args)),
    )
    return [{"kind": "public_comment", "identity": row["identity"], "title": row["author_name"] or "评论",
             "revision": row["updated_at"], "state": "read", "href": href,
             "entries": [{"kind": "comment", "href": href, "text": row["text"][:420], "author": row["author_name"],
                          "uid": row["author_uid"]}]} for row in rows
            if (href := comment_href(row["resource"], row["comment_id"]))]


def _dynamics(store, scope, args, window):
    rows = store.all(
        "SELECT id,content,status,receipt,updated_at,(SELECT COUNT(*) FROM json_each(?) "
        "WHERE instr(lower(content), value)>0) AS hits FROM dynamic_posts "
        "WHERE account_id=? AND character_id=? AND character_version=? AND (? IS NULL OR id=?) "
        "AND (? IS NULL OR CASE WHEN status='published' THEN published_at ELSE created_at END>=? "
        "AND CASE WHEN status='published' THEN published_at ELSE created_at END<?) "
        "AND (json_array_length(?)=0 OR hits>0) ORDER BY hits DESC,updated_at DESC,id DESC LIMIT 6",
        (_terms(args), *scope[:3], args.identity, args.identity, window.get("day"),
         window.get("start_at"), window.get("next_at"), _terms(args)),
    )
    items = []
    for row in rows:
        receipt = json.loads(row["receipt"] or "{}") or {}
        remote = str(receipt.get("dynamic_id") or receipt.get("dynamic_id_str") or receipt.get("id_str") or "")
        confirmed = row["status"] == "published" and re.fullmatch(r"[1-9][0-9]{0,19}", remote)
        href = f"https://www.bilibili.com/opus/{remote}" if confirmed else None
        if href is None and row["status"] == "published":
            state = "publish_uncertain"
        else:
            state = row["status"]
        items.append({"kind": "dynamic_post", "identity": str(row["id"]), "title": f"动态 #{row['id']}",
                      "revision": row["updated_at"], "state": state, "href": href,
                      "platform_id": remote if confirmed else None,
                      "entries": [{"kind": "dynamic", "text": (row["content"] or "")[:420], "href": href}]})
    return items


def lookup(browsing, scope, args):
    account, character = scope[:2]
    if not account:
        return {"state": "unavailable", "items": [], "reason": "观看与运营记录需要已核验的运营账号。"}
    store = browsing.store
    window = period(browsing, args.day) if args.day or args.collection == "watch_count" else {}
    watches = watch_rows(store, account, character, window) if window and args.collection in {"watch_count", "video"} else None
    day_ids = dump(sorted({row["bvid"] for row in watches if _read(row)})) if watches is not None else None
    if args.collection == "watch_count":
        values = viewing(store, account, character, window, watches)
        return {"state": "ok", "metrics": values, "items": []}
    elif args.collection == "video":
        items = _videos(store, account, character, args, day_ids)
    elif args.collection == "comment":
        items = _comments(store, account, character, args, window)
    else:
        items = _dynamics(store, scope, args, window)
    for item in items:
        item["source_version"] = content_hash(item)
    result = {"state": "ok" if items else "empty", "items": items,
              "day": window.get("day"), "timezone": window.get("timezone")}
    return result


def watch_answer(stats):
    if not stats["available"]:
        return "当前读不到运营账号的观看记录。"
    text = f"{stats['day']} 有 {stats['completed']} 条视频读到了内容"
    if stats["completed"]:
        text += f"，其中 {stats['fully_read']} 条完整读完"
    if stats["in_progress"]:
        text += f"。还有 {stats['in_progress']} 条正在读取或等待继续"
    if stats["without_result"]:
        text += f"。另外 {stats['without_result']} 条已开始，但没有读到内容"
    return text + "。"


def present(items, reply):
    sections, sources = [], []
    states = {"published": "已发布", "pending_review": "待审核", "approved": "已审核",
              "publishing": "正在提交", "publish_uncertain": "待核对回执", "rejected": "已退回",
              "draft": "草稿", "failed": "发布失败"}
    for index, item in enumerate(items):
        entries = item["entries"]
        if item["kind"] == "dynamic_post":
            section = item["title"] + " · " + states.get(item["state"], "已保存")
            if item["platform_id"]:
                section += " · 平台编号 " + item["platform_id"]
            if entries and entries[0]["text"]:
                section += "\n" + entries[0]["text"]
        else:
            section = label(item)
            if index == 0 and reply:
                section = reply + "\n" + section
            if entries and entries[0].get("text"):
                section += "\n「" + entries[0]["text"] + "」"
            if not entries and item["kind"] == "watched_video":
                section += " · 记录里暂时没有对应的片段"
        sections.append(section)
        sources.append({"title": item["title"], "entries": entries or (
            [{"kind": "video", "href": item["href"], "text": item["title"]}] if item["href"] else []
        )})
    return sections, sources


def label(item):
    if item["kind"] == "watched_video":
        return " · ".join(filter(None, (item["title"], item.get("author"), item["identity"])))
    return item["title"]
