"""Public comment evidence shared by the activity feed and dynamic cards."""

import json
import math
import time
from datetime import datetime

from .quotas import publication_time
from .thread_context import COMMENT_ID, VIDEO_ID, comment_source, public_discussion_url


def _id(value):
    value = str(value)
    return value if COMMENT_ID.fullmatch(value) else None


def _json(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _timestamp(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo else None
        except (TypeError, ValueError, OverflowError):
            return None
    return result if math.isfinite(result) and 0 < result < 253402300800 else None


def _table(store, table):
    return store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)) is not None


def resource_key(target):
    if VIDEO_ID.fullmatch(str(target.get("bvid"))):
        return target["bvid"]
    if target.get("resource_type") == "dynamic" or target.get("type") in (11, 17):
        if identifier := _id(target.get("dynamic_id") or target.get("oid")):
            return "dynamic:" + identifier
    return None


def comment_href(resource, comment_id=None):
    if str(resource).startswith("dynamic:"):
        return public_discussion_url(comment_id=comment_id, dynamic_id=resource.removeprefix("dynamic:"))
    return public_discussion_url(resource, comment_id)


def observe_thread(store, row, resource, items, *, via, resource_author_uid=None):
    """Persist only platform-verified public rows, with independent discovery sources."""
    if (row.get("preview") and not row.get("config", {}).get("full_video")) or not (
        resource and items and row.get("account_id") and row.get("character_id")
    ):
        return
    if not _table(store, "browsing_comment_observations"):
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS browsing_comment_timeline("
            "account_id TEXT,character_id TEXT,resource TEXT,root_id TEXT,comment_id TEXT,"
            "parent_comment_id TEXT,author_uid TEXT,author_name TEXT,text TEXT,created_at REAL,"
            "resource_author_uid TEXT,PRIMARY KEY(account_id,character_id,resource,root_id,comment_id))"
        )
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS browsing_comment_observations("
            "account_id TEXT,character_id TEXT,resource TEXT,root_id TEXT,comment_id TEXT,via TEXT,"
            "round_id TEXT,observed_at REAL,"
            "PRIMARY KEY(account_id,character_id,resource,root_id,comment_id,via,round_id))"
        )
        store.db.execute(
            "CREATE INDEX IF NOT EXISTS browsing_comment_observations_recent ON "
            "browsing_comment_observations(account_id,character_id,observed_at)"
        )
        store.db.execute(
            "CREATE INDEX IF NOT EXISTS browsing_comment_observations_round ON "
            "browsing_comment_observations(round_id)"
        )
    entries, observations = [], []
    now = time.time()
    for item in items:
        identifier = _id(item.get("comment_id"))
        root = _id(item.get("thread_id")) or identifier
        if not identifier:
            continue
        source = comment_source(item)
        if not _id(source["author_uid"]):
            continue
        parent = _id(item.get("parent_comment_id")) if item.get("parent_verified") is not False else None
        key = (str(row["account_id"]), row["character_id"], resource, root, identifier)
        entries.append((*key, parent, source["author_uid"], source["author_name"], source["text"],
                        _timestamp(item.get("created_at")), _id(resource_author_uid)))
        observations.append((*key, via, row.get("round_id") or row.get("id"), now))
    if entries:
        store.db.executemany(
            "INSERT INTO browsing_comment_timeline VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,character_id,resource,root_id,comment_id) DO UPDATE SET "
            "parent_comment_id=COALESCE(excluded.parent_comment_id,browsing_comment_timeline.parent_comment_id),"
            "author_uid=excluded.author_uid,author_name=excluded.author_name,text=excluded.text,"
            "created_at=COALESCE(excluded.created_at,browsing_comment_timeline.created_at),"
            "resource_author_uid=COALESCE(excluded.resource_author_uid,browsing_comment_timeline.resource_author_uid)",
            entries,
        )
        store.db.executemany(
            "INSERT INTO browsing_comment_observations VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,character_id,resource,root_id,comment_id,via,round_id) DO UPDATE SET "
            "observed_at=excluded.observed_at",
            observations,
        )


def comment_activity(store, account_id, character_id, *, start, end, round_id=None):
    if not _table(store, "browsing_comment_observations"):
        return []
    return store.all(
        "SELECT resource,root_id,round_id,MAX(observed_at) AS observed_at FROM browsing_comment_observations "
        "WHERE account_id=? AND character_id=? AND observed_at>=? AND observed_at<? "
        "AND (? IS NULL OR round_id=?) GROUP BY resource,root_id,round_id ORDER BY observed_at DESC",
        (str(account_id), character_id, start, end, round_id, round_id),
    )


def comment_thread(store, resource, root_id, *, account_id=None, character_id=None):
    """Merge platform observations, historical imports and confirmed send receipts."""
    root_id = _id(root_id)
    result = {"resource": resource, "root_id": root_id, "resource_author_uid": None,
              "root_author_uid": None, "comments": []}
    if not root_id or not comment_href(resource):
        return result
    comments = {}

    def add(identifier, *, via=None, **details):
        if not (identifier := _id(identifier)):
            return
        item = comments.setdefault(identifier, {
            "comment_id": identifier, "parent_comment_id": None, "author_uid": None, "author_name": "",
            "text": "", "created_at": None, "href": comment_href(resource, identifier),
            "discovered_via": [], "publication": None, "round_id": None,
        })
        item.update({key: value for key, value in details.items() if value is not None and value != ""})
        if via and via not in item["discovered_via"]:
            item["discovered_via"].append(via)

    if _table(store, "comments"):
        for item in store.all(
            "SELECT comment_id,parent_comment_id,platform_user_id,display_name,content,created_at "
            "FROM comments WHERE platform='bilibili' AND video_id=? AND thread_id=?", (resource, root_id),
        ):
            add(item["comment_id"], via="imported", parent_comment_id=_id(item["parent_comment_id"]),
                author_uid=_id(item["platform_user_id"]), author_name=item["display_name"],
                text=item["content"], created_at=_timestamp(item["created_at"]))
    if account_id and character_id and _table(store, "browsing_comment_observations"):
        scope = (str(account_id), character_id, resource, root_id)
        for item in store.all(
            "SELECT * FROM browsing_comment_timeline WHERE account_id=? AND character_id=? AND resource=? "
            "AND root_id=?", scope,
        ):
            add(item["comment_id"], parent_comment_id=item["parent_comment_id"],
                author_uid=item["author_uid"], author_name=item["author_name"], text=item["text"],
                created_at=item["created_at"])
            if item["resource_author_uid"]:
                result["resource_author_uid"] = item["resource_author_uid"]
        for item in store.all(
            "SELECT comment_id,via FROM browsing_comment_observations WHERE account_id=? AND character_id=? "
            "AND resource=? AND root_id=? GROUP BY comment_id,via ORDER BY MIN(observed_at),via", scope,
        ):
            add(item["comment_id"], via=item["via"])
    for item in _receipts(store, resource, root_id, account_id, character_id):
        add(item["comment_id"], **{key: value for key, value in item.items() if key != "comment_id"})
    for item in _event_replies(store, resource, root_id, account_id, character_id):
        current = comments.get(str(item["comment_id"]), {})
        if current.get("publication") != "confirmed":
            add(item["comment_id"], **{key: value for key, value in item.items() if key != "comment_id"})
    if account_id:
        for item in comments.values():
            if item["author_uid"] == str(account_id) and item["parent_comment_id"] and not item["publication"]:
                item["publication"] = "same_account"
    result["root_author_uid"] = comments.get(root_id, {}).get("author_uid")
    result["comments"] = sorted(comments.values(), key=lambda item: (
        item["created_at"] is None, item["created_at"] or 0, int(item["comment_id"]),
    ))
    return result


def _receipts(store, resource, root, account, character):
    if not account or not character:
        return
    if _table(store, "browsing_intents"):
        bvid, dynamic = (None, resource.removeprefix("dynamic:")) if resource.startswith("dynamic:") else (resource, None)
        rows = store.all(
            "SELECT id,round_id,text,receipt,target,status,updated_at FROM browsing_intents "
            "WHERE account_id=? AND character_id=? AND kind='reply_public' "
            "AND status IN ('published','operator_replied') "
            "AND CAST(json_extract(target,'$.root') AS TEXT)=? AND "
            "((? IS NOT NULL AND json_extract(target,'$.bvid')=?) OR (? IS NOT NULL AND "
            "COALESCE(CAST(json_extract(target,'$.dynamic_id') AS TEXT),CAST(json_extract(target,'$.oid') AS TEXT))=?))",
            (str(account), character, root, bvid, bvid, dynamic, dynamic),
        )
        for row in rows:
            target = _json(row["target"])
            discussion = target.get("source_discussion") or {}
            for anchor in (discussion.get("parent") or {}, discussion.get("source") or {}):
                if anchor.get("comment_id"):
                    yield {"comment_id": anchor["comment_id"], "parent_comment_id": _id(anchor.get("parent_comment_id")),
                           "author_uid": _id(anchor.get("author_uid")), "author_name": anchor.get("author_name"),
                           "text": anchor.get("text"), "created_at": _timestamp(anchor.get("created_at"))}
            if item := _reply(row, target, account):
                yield item
    if _table(store, "drafts") and _table(store, "comments"):
        columns = {col[1] for col in store.db.execute("PRAGMA table_info(drafts)")}
        if "account_id" not in columns:
            return
        for row in store.all(
            "SELECT d.id,d.text,d.status,d.updated_at,d.receipt,d.answer_check,c.comment_id "
            "FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE d.account_id=? AND d.character_id=? "
            "AND c.platform='bilibili' AND c.video_id=? AND c.thread_id=? "
            "AND d.status IN ('published','operator_replied')",
            (str(account), character, resource, root),
        ):
            check = _json(row["answer_check"])
            if item := _reply(row, check | {"parent": row["comment_id"]}, account):
                yield item


def _event_replies(store, resource, root, account, character):
    if not account or not character or not _table(store, "browsing_events"):
        return
    # Older notification rows only bind a role after the canonical key is saved.
    from ...operations_control import canonical_intent

    for row in store.all(
        "SELECT source_key,payload FROM browsing_events WHERE account_id=? AND status='operator_replied' "
        "AND CAST(json_extract(payload,'$.root') AS TEXT)=?", (str(account), root),
    ):
        payload = _json(row["payload"])
        identifier = _id(payload.get("comment_id") or payload.get("parent"))
        if not identifier or resource_key(payload) != resource:
            continue
        canonical_resource = resource
        if resource.startswith("dynamic:"):
            canonical_resource = "dynamic:" + str(payload.get("oid"))
        if row["source_key"] != canonical_intent("bilibili", canonical_resource, identifier, character):
            continue
        if item := _reply({"status": "operator_replied"}, payload | {"parent": identifier}, account):
            yield item


def _reply(row, target, account):
    receipt = _json(row.get("receipt"))
    identity = target.get("generation_identity") or {}
    observed = target.get("account_reply") or {}
    if row["status"] == "published":
        identifier = _id(receipt.get("rpid") or receipt.get("comment_id"))
        if identifier:
            return {"comment_id": identifier, "parent_comment_id": _id(target.get("parent")),
                    "author_uid": str(account), "author_name": identity.get("account_display_name"),
                    "text": row["text"], "round_id": row.get("round_id"),
                    "created_at": publication_time(receipt, row["updated_at"]), "publication": "confirmed"}
    if row["status"] == "operator_replied" and _id(observed.get("comment_id")) and str(observed.get("author_uid")) == str(account):
        return {"comment_id": observed["comment_id"], "parent_comment_id": _id(target.get("parent")),
                "author_uid": str(account), "author_name": observed.get("author_name"), "text": observed.get("text"),
                "created_at": _timestamp(observed.get("created_at")), "publication": "same_account"}
