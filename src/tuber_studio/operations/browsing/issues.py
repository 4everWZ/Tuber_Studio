"""Acknowledgement of displayed errors, independent of task execution."""

import json
import time


def _key(browsing):
    return "browsing_issues_ack:" + json.dumps(browsing.discovery.history_scope()[:2], ensure_ascii=False)


def _identity(issue):
    return json.dumps([issue.get("bvid"), issue.get("error")], ensure_ascii=False)


def unread_issues(browsing, issues):
    ack = browsing.store.get(_key(browsing), {})
    result = []
    for issue in issues:
        identity = _identity(issue)
        seen = ack.get("counts", {}).get(identity, 0)
        total = issue.get("occurrences", 1)
        newer = issue.get("updated_at", 0) > ack.get("at", 0)
        if identity in ack.get("counts", {}) and total > seen:
            # Several failures can share a clock tick. The counter is stronger
            # evidence of a new occurrence than their wall-clock timestamp.
            count = total - seen
        elif newer:
            # A legacy rolling window or a newly visible version may reset the
            # aggregate. Do not carry its previous counter into this window.
            count = total if total < seen else max(1, total - seen)
        else:
            continue
        result.append(issue | {"occurrences": count, "total_occurrences": total})
    return result


def clear_issues(browsing):
    records = browsing.discovery.records()
    old = browsing.store.get(_key(browsing), {})
    counts = dict(old.get("counts", {}))
    for issue in records["issues"]:
        key = _identity(issue)
        counts[key] = issue.get("total_occurrences", counts.get(key, 0) + issue.get("occurrences", 1))
    browsing.store.set(_key(browsing), {"at": time.time(), "counts": counts})
    browsing.last_error = None
    browsing.discovery.error = None
    return browsing.discovery.records()
