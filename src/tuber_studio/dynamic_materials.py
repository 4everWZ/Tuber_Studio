"""Dynamic material projections over durable, scoped operations records."""

import json
import re

from .operations.browsing.comment_timeline import comment_href


def public_reads(store, account, character, bound_ids):
    videos = json.dumps([value.removeprefix("video:") for value in bound_ids if value.startswith("video:")])
    for row in store.all(
        "SELECT bvid,material,watch_evidence,updated_at FROM browsing_video_progress "
        "WHERE account_id=? AND character_id=? AND read_state IN ('complete','viewed') "
        "AND bvid NOT IN (SELECT value FROM json_each(?)) ORDER BY updated_at DESC LIMIT 8",
        (account, character, videos),
    ) + store.all(
        "SELECT bvid,material,watch_evidence,updated_at FROM browsing_video_progress "
        "WHERE account_id=? AND character_id=? AND read_state IN ('complete','viewed') "
        "AND bvid IN (SELECT value FROM json_each(?))",
        (account, character, videos),
    ):
        evidence = json.loads(row["watch_evidence"] or "{}")
        material = evidence.get("material") or json.loads(row["material"])
        metadata, bvid = material.get("metadata", {}), row["bvid"]
        if metadata.get("bvid") != bvid or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            continue
        excerpts = []
        for part in material.get("parts", []):
            for segment in part.get("segments", []):
                if segment.get("text") and len(excerpts) < 4:
                    visual = "画面 · " if part.get("visual") else ""
                    excerpts.append(f"CID {part['cid']} · {segment['start']}s · {visual}{segment['text'][:240]}")
            if len(excerpts) == 4:
                break
        if not excerpts:
            continue
        yield row["updated_at"], {
            "id": f"video:{bvid}", "type": "watched_video", "fictional": False,
            "text": metadata.get("title", "") + " · 已读片段\n" + "\n".join(excerpts),
            "source_url": f"https://www.bilibili.com/video/{bvid}",
            "participants": [{"uid": str(metadata["owner_uid"]), "name": metadata.get("owner", "")}]
            if metadata.get("owner_uid") else [],
        }
    if not store.one("SELECT 1 FROM sqlite_master WHERE name='browsing_comment_observations'"):
        return
    for row in store.all(
        "WITH recent AS (SELECT resource,root_id,comment_id,MAX(observed_at) AS observed_at "
        "FROM browsing_comment_observations WHERE account_id=? AND character_id=? "
        "GROUP BY resource,root_id,comment_id ORDER BY observed_at DESC LIMIT 16) "
        "SELECT t.*, COALESCE(recent.observed_at,0) AS observed_at FROM browsing_comment_timeline t "
        "LEFT JOIN recent USING(resource,root_id,comment_id) WHERE t.account_id=? AND t.character_id=? "
        "AND (recent.observed_at IS NOT NULL OR 'comment:'||t.resource||':'||t.root_id||':'||t.comment_id "
        "IN (SELECT value FROM json_each(?)))",
        (account, character, account, character, json.dumps(bound_ids)),
    ):
        if not row["text"] or not (href := comment_href(row["resource"], row["comment_id"])):
            continue
        yield row["observed_at"], {
            "id": f"comment:{row['resource']}:{row['root_id']}:{row['comment_id']}", "type": "public_comment",
            "fictional": False, "text": row["text"], "source_url": href,
            "participants": [{"uid": row["author_uid"], "name": row["author_name"]}],
            "reference": {"resource": row["resource"], "thread_id": row["root_id"], "comment_id": row["comment_id"],
                          "author_name": row["author_name"], "author_uid": row["author_uid"], "created_at": row["created_at"]},
        }


def source_links(source):
    if source.get("type") == "public_incident":
        links = [comment_href(ref.get("bvid"), ref.get("comment_id")) for ref in source.get("public_sources", [])]
        if any(links):
            return [link for link in dict.fromkeys(links) if link]
    if source.get("type") == "published_reply" and (video := re.search(r"/video/(BV[0-9A-Za-z]{10})",
                                                                   source.get("source_url", ""))):
        return [comment_href(video[1], (source.get("original_comment") or {}).get("id"))]
    return [source["source_url"]] if source.get("source_url") else []


# The private model returns only role-side emotional or creative direction.
# No arbitrary text, sender or incoming content crosses this interface.
REFLECTIONS = {
    "gratitude": "想记录今天心里暖暖的感觉",
    "touched": "今天有一点感动，想给大家留一句温柔的话",
    "amused": "心情很好，想跟大家分享一点快乐",
    "curious": "产生了新的好奇心，想聊聊自己愿意探索的事",
    "inspired": "想记下一点自己的创作灵感",
    "reflective": "想聊聊自己的一点心情或感触",
}

REFLECTION_INSTRUCTION = (
    "这次来信如果让你想在动态聊聊自己的感触，public_reflection 可选 gratitude、touched、amused、"
    "curious、inspired 或 reflective；没有想分享的就留空。这里记录的是自己的心情或创作方向。"
)


def ensure_reflections(store):
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS dynamic_reflections(account_id TEXT NOT NULL,character_id TEXT NOT NULL,"
        "character_version INTEGER NOT NULL,impression_key TEXT NOT NULL,impression TEXT NOT NULL,created_at REAL NOT NULL,"
        "PRIMARY KEY(account_id,character_id,character_version,impression_key))"
    )


def reflections(store, account, character, version, bound_ids):
    rows = store.all(
        "SELECT impression_key,impression,created_at FROM dynamic_reflections "
        "WHERE account_id=? AND character_id=? AND character_version=? "
        "AND (impression_key IN (SELECT value FROM json_each(?)) OR impression_key IN "
        "(SELECT impression_key FROM dynamic_reflections WHERE account_id=? AND character_id=? "
        "AND character_version=? ORDER BY created_at DESC LIMIT 8))",
        (account, character, version, json.dumps(bound_ids), account, character, version),
    )
    for row in rows:
        if row["impression"] in REFLECTIONS:
            yield row["created_at"], {"id": row["impression_key"], "type": "own_reflection", "fictional": False,
                                      "text": REFLECTIONS[row["impression"]]}
