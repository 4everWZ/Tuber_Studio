"""Verified discussion participants and reply relationships for model context."""


def comment_source(item):
    # These four fields are the durable CommentProgress source-hash contract.
    return {
        "comment_id": str(item["comment_id"]),
        "author_uid": str(item.get("user_id") or item.get("platform_user_id") or ""),
        "author_name": str(item.get("username") or item.get("display_name") or ""),
        "text": str(item.get("text") or item.get("content") or ""),
    }


def operator_reply(items, target, account):
    """Only an observed same-account reply addressed directly to this source counts."""
    root, parent = str(target["root"]), str(target["parent"])
    return next((item for item in items if (
        str(item.get("thread_id")) == root
        and str(item.get("parent_comment_id")) == parent
        and item.get("parent_verified") is not False
        and comment_source(item)["author_uid"] == str(account)
    )), None)


def comment_thread_context(item, items, target):
    comment_id, root_id = str(item["comment_id"]), str(target["root"])
    by_id = {str(row["comment_id"]): row for row in items if row.get("comment_id")}
    actual = by_id.get(comment_id, item)

    def parent_of(row):
        if row.get("parent_verified") is False:
            return None
        parent = row.get("parent_comment_id")
        return str(parent) if parent not in (None, "", 0, "0") else None

    def present(row, identity):
        if row is None:
            return {"comment_id": identity, "author_uid": None, "author_name": "", "text": "",
                    "root_id": root_id, "parent_comment_id": None, "mentions": [], "available": False}
        return comment_source(row) | {
            "root_id": str(row.get("thread_id") or row["comment_id"]),
            "parent_comment_id": parent_of(row),
            "parent_verified": row.get("parent_verified", "parent_comment_id" in row or str(row["comment_id"]) == root_id),
            "mentions": list(row.get("mentions") or []),
            "available": True,
        }

    parent_id = parent_of(actual)
    root = by_id.get(root_id, item if comment_id == root_id else None)
    parent = by_id.get(parent_id) if parent_id else None
    timeline = [row for row in items if str(row.get("thread_id") or row.get("comment_id")) == root_id]
    return {
        "source": comment_source(item),
        "target": target,
        "root": present(root, root_id),
        "parent": present(parent, parent_id) if parent_id else None,
        "timeline": [present(row, str(row["comment_id"])) | {
            "is_root": str(row["comment_id"]) == root_id,
            "is_target": str(row["comment_id"]) == comment_id,
        } for row in timeline[:8]],
        "items": timeline,
        **({"grouped_comments": [present(row, str(row["comment_id"])) for row in item["grouped_comments"]]}
           if item.get("grouped_comments") else {}),
        "addressing": {
            "source_mentions": list(actual.get("mentions") or []),
            "source_parent_comment_id": parent_id,
            "source_parent_verified": actual.get("parent_verified", "parent_comment_id" in actual or comment_id == root_id),
            "source_root_comment_id": root_id,
        },
    }
