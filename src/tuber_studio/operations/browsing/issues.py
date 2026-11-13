"""Acknowledgement of displayed errors, independent of task execution."""

import json
import time


def _key(browsing):
    return "browsing_issues_ack:" + json.dumps(browsing.discovery.history_scope()[:2], ensure_ascii=False)


def _identity(issue):
    return json.dumps([issue.get("bvid"), issue.get("error")], ensure_ascii=False)


def issue_history(browsing, issues):
    ack = browsing.store.get(_key(browsing), {})
    version = browsing.discovery.history_scope()[2]
    result = []
    for issue in issues:
        identity = _identity(issue)
        seen = ack.get("counts", {}).get(identity, 0)
        total = issue.get("occurrences", 1)
        newer = issue.get("updated_at", 0) > ack.get("at", 0)
        selected = ack.get("selected", {}).get(identity)
        if selected:
            if selected.get("version", version) != version or total < selected["count"] or (
                total == selected["count"] and issue.get("updated_at", 0) > selected.get("updated_at", float("inf"))
            ):
                count = total
            else:
                count = total - selected["count"]
        elif identity in ack.get("counts", {}) and total > seen:
            # Several failures can share a clock tick. The counter is stronger
            # evidence of a new occurrence than their wall-clock timestamp.
            count = total - seen
        elif newer:
            # A legacy rolling window or a newly visible version may reset the
            # aggregate. Do not carry its previous counter into this window.
            count = total if total < seen else max(1, total - seen)
        else:
            count = 0
        result.append(issue | {
            "unread_occurrences": count,
            "severity": "warning" if issue.get("status") in {"retry_wait", "recovered"} else "error",
        })
    return result


def clear_issues(browsing):
    records = browsing.discovery.records()
    old = browsing.store.get(_key(browsing), {})
    counts = dict(old.get("counts", {}))
    for issue in records["issues"]:
        key = _identity(issue)
        counts[key] = issue.get("total_occurrences", counts.get(key, 0) + issue.get("occurrences", 1))
    browsing.store.set(_key(browsing), {"at": time.time(), "counts": counts})
    return browsing.discovery.records()


def acknowledge_issues(browsing, observations):
    """Acknowledge only the occurrences present in the operator's verified snapshot."""
    key = _key(browsing)
    history = browsing.discovery.records()["issue_history"]
    current = {issue["id"]: issue for issue in history}
    ack = browsing.store.get(key, {})
    selected = dict(ack.get("selected", {}))
    version = browsing.discovery.history_scope()[2]
    for observation in observations:
        issue = current.get(observation["id"])
        if not issue or observation["updated_at"] != issue.get("updated_at"):
            raise ValueError("告警记录已变化，请按新记录核对")
        count = observation["occurrences"]
        if count != issue.get("occurrences", 1):
            raise ValueError("告警发生次数已变化，请按新记录核对")
        selected[_identity(issue)] = {"count": count, "version": version, "updated_at": issue["updated_at"]}
    browsing.store.set(key, ack | {"selected": selected})
    return browsing.discovery.records()
