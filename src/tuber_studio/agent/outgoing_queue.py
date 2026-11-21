"""Scoped view of existing publications and their current review state."""

from ..store import dump

CHANNELS = {
    "dynamics": ("动态", "dynamic_posts", "content"),
    "comments": ("评论回复", "drafts", "text"),
    "messages": ("私信", "dm_drafts", "text"),
}
STATES = ("pending_review", "approved", "copied", "publishing", "publish_uncertain")


def pending_dynamic_ids(store, scope):
    return [row["id"] for row in store.all(
        "SELECT id FROM dynamic_posts WHERE account_id=? AND character_id=? AND character_version=? "
        "AND status IN ('approved','pending_review') AND attempted_at IS NULL ORDER BY id", tuple(scope[:3]),
    )]


def named_channel(source):
    names = {"dynamics": ("动态", "帖子"), "comments": ("评论", "留言"), "messages": ("私信", "站内信")}
    matches = [key for key, terms in names.items() if any(term in source for term in terms)]
    return matches[0] if len(matches) == 1 else None


def queue_snapshot(store, scope, channel, *, prepare):
    account, role, version, *_ = scope
    groups = []
    for key in (channel,) if channel else CHANNELS:
        label, table, _ = CHANNELS[key]
        where = (
            "account_id=? AND character_id=? AND character_version=? "
            "AND status IN (SELECT value FROM json_each(?))"
        )
        args = (account, role, version, dump(STATES))
        counts = {
            row["status"]: row["count"] for row in store.all(
                f"SELECT status, COUNT(*) AS count FROM {table} WHERE {where} GROUP BY status", args,
            )
        }
        rows = store.all(f"SELECT id FROM {table} WHERE {where} ORDER BY id LIMIT 10", args) if counts else []
        groups.append({"channel": key, "label": label, "counts": counts, "ids": [row["id"] for row in rows]})
    return {"groups": groups, "prepare": prepare}


def queue_view(store, scope, snapshot, dynamics, draft_view):
    account, role, version, *_ = scope
    groups = []
    for group in snapshot["groups"]:
        key = group["channel"]
        _, table, field = CHANNELS[key]
        items = []
        for identity in group["ids"]:
            row = store.one(f"SELECT id, status, substr({field}, 1, 240) AS preview FROM {table} "
                            "WHERE id=? AND account_id=? AND character_id=? AND character_version=?",
                            (identity, account, role, version))
            if row:
                if key == "dynamics":
                    row["draft"] = draft_view(dynamics._post(identity))
                items.append(row)
        groups.append({k: v for k, v in group.items() if k != "ids"} | {"items": items})
    return {"groups": groups, "prepare": snapshot["prepare"]}


def queue_answer(snapshot):
    lines = []
    for group in snapshot["groups"]:
        counts = group["counts"]
        label = group["label"]
        states = (
            (counts.get("approved", 0) + counts.get("copied", 0), "已审核"),
            (counts.get("pending_review", 0), "待审核"),
            (counts.get("publishing", 0), "正在提交"),
            (counts.get("publish_uncertain", 0), "待核对回执"),
        )
        if details := "，".join(f"{status} {count} 条" for count, status in states if count):
            lines.append(f"{label}有{details}。")
        elif len(snapshot["groups"]) == 1:
            lines.append(f"{label}队列里没有待处理的草稿。")
    if snapshot["prepare"] and any(group["ids"] for group in snapshot["groups"]):
        lines.append("可以打开对应工作台核对稿件。")
    return "\n".join(lines) or "当前没有待处理的稿件。"
