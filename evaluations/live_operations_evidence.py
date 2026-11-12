"""Summarize real public comment candidates and receipts from a read-only SQLite snapshot."""

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tuber_studio.operation_text import comment_body_issue
from tuber_studio.operations.browsing.quotas import publication_time

ROOT = Path(__file__).resolve().parents[1]
JUDGMENTS = ("satisfied", "needs_revision")

PUBLIC_CANDIDATES = """
    SELECT i.id, i.account_id, i.character_id, i.kind, i.text, i.status, i.receipt,
           i.target, i.evidence, i.material_keys, i.created_at, i.updated_at,
           q.published_at, g.binding, g.current_output
    FROM browsing_intents i
    LEFT JOIN browsing_reply_quotas q ON q.account_id=i.account_id AND q.character_id=i.character_id
         AND q.source_key=i.source_key AND q.status='published'
    LEFT JOIN operation_continuations g ON g.channel='generation.browsing'
         AND g.id=json_extract(CASE WHEN json_valid(i.target) THEN i.target END, '$.generation_continuation_id')
    WHERE i.kind IN ('top_level','reply_public') AND i.created_at < ? AND i.updated_at >= ?
    ORDER BY i.created_at, i.id
"""


def decoded(value, expected):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    return parsed if isinstance(parsed, expected) else expected()


def has_comment_receipt(receipt):
    code = receipt.get("code")
    identifier = str(receipt.get("rpid", ""))
    return code in (0, "0") and type(code) is not bool and identifier.isascii() and (
        identifier.isdecimal() and identifier.lstrip("0") != ""
    )


def collect(database, first_day, last_day, *, timezone="Asia/Taipei", model=None):
    if last_day < first_day:
        raise ValueError("the last business day precedes the first")
    zone = ZoneInfo(timezone)
    days = [first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)]
    start = datetime.combine(first_day, time.min, zone).timestamp()
    stop = datetime.combine(last_day + timedelta(days=1), time.min, zone).timestamp()
    day_counts = {day.isoformat(): {
        "public_candidates": 0, "pending_review_candidates": 0, "recorded_model_bodies": 0,
        "stored_candidate_differs_from_model_body": 0, "model_bodies_flagged_by_current_validator": 0,
        "confirmed_model_body_submissions": 0, "confirmed_top_level_comments": 0,
        "confirmed_thread_replies": 0, "published_without_valid_receipt": 0,
    } for day in days}
    cases, profiles, accounts, characters = [], Counter(), set(), set()
    with closing(sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        # The production receipt clock accepts a platform timestamp up to five minutes ahead.
        for row in db.execute(PUBLIC_CANDIDATES, (stop, start - 300)):
            signature = decoded(decoded(row["binding"], dict).get("model"), list)
            model_label = signature[1] if len(signature) > 1 else None
            if model is not None and model_label != model:
                continue

            created_day = datetime.fromtimestamp(row["created_at"], zone).date().isoformat()
            target = decoded(row["target"], dict)
            recorded = decoded(row["current_output"], dict).get("text")
            model_body = recorded if isinstance(recorded, str) and recorded.strip() else None
            same_body = model_body is not None and model_body.strip() == row["text"]
            receipt = decoded(row["receipt"], dict)
            confirmed = row["status"] == "published" and has_comment_receipt(receipt)
            receipt_day = None
            if row["status"] == "published":
                published = row["published_at"] or publication_time(receipt, row["updated_at"])
                receipt_day = datetime.fromtimestamp(published, zone).date().isoformat()
                if receipt_day in day_counts:
                    counts = day_counts[receipt_day]
                    if confirmed:
                        field = "confirmed_top_level_comments" if row["kind"] == "top_level" else "confirmed_thread_replies"
                        counts[field] += 1
                        counts["confirmed_model_body_submissions"] += same_body
                    else:
                        counts["published_without_valid_receipt"] += 1
            if created_day in day_counts or receipt_day in day_counts:
                accounts.add(row["account_id"])
                characters.add(row["character_id"])
            if created_day not in day_counts:
                continue

            body_issue = comment_body_issue(model_body, recipient_name=target.get("recipient_name") or "") if model_body else None
            counts = day_counts[created_day]
            counts["public_candidates"] += 1
            counts["pending_review_candidates"] += row["status"] == "pending_review"
            counts["recorded_model_bodies"] += model_body is not None
            counts["stored_candidate_differs_from_model_body"] += model_body is not None and not same_body
            counts["model_bodies_flagged_by_current_validator"] += body_issue is not None
            thinking = signature[3] if len(signature) > 3 else None
            profiles["thinking_on" if thinking is True else "thinking_off" if thinking is False else "unknown"] += 1
            cases.append({
                "case_id": row["id"],
                "observed_day": created_day,
                "kind": row["kind"],
                "source": {key: target[key] for key in ("bvid", "oid", "root", "parent", "source_text") if key in target},
                "evidence_ids": decoded(row["evidence"], list),
                "material_keys": decoded(row["material_keys"], list),
                "model_body": model_body,
                "model_body_issue": body_issue,
                "stored_body": row["text"],
                "stored_status": row["status"],
                "confirmed_receipt": confirmed,
                "judgment": None,
                "note": "",
            })

    cohort = {"first_day": first_day.isoformat(), "last_day": last_day.isoformat(), "timezone": timezone}
    summary = {
        "cohort": cohort,
        "filtered_by_recorded_model": model is not None,
        "account_count": len(accounts),
        "character_count": len(characters),
        "candidate_thinking_settings": dict(sorted(profiles.items())),
        "days": day_counts,
    }
    return summary, {"cohort": cohort, "model_filter": model, "cases": cases}


def tally_reviews(current, reviewed=None):
    cases = current["cases"]
    judgeable = sum(case["model_body"] is not None for case in cases)
    totals = dict.fromkeys(JUDGMENTS, 0)
    if reviewed is not None:
        if any(reviewed[key] != current[key] for key in ("cohort", "model_filter")):
            raise ValueError("review packet belongs to a different cohort")
        by_id = {case["case_id"]: case for case in cases}
        seen = set()
        for saved in reviewed["cases"]:
            case_id = saved["case_id"]
            case = by_id.get(case_id)
            if case is None or case_id in seen or any(
                saved[key] != case[key] for key in ("kind", "source", "model_body", "evidence_ids", "material_keys")
            ):
                raise ValueError("review packet has an unknown, duplicate, or changed candidate")
            seen.add(case_id)
            judgment = saved["judgment"]
            if judgment is None:
                continue
            if judgment not in JUDGMENTS or case["model_body"] is None:
                raise ValueError("a judgment requires a recorded model body and an explicit supported value")
            totals[judgment] += 1
    return totals | {"unreviewed": judgeable - sum(totals.values()), "no_recorded_model_body": len(cases) - judgeable}


def local_review_path(value):
    path = Path(value).resolve()
    if not any(path.is_relative_to(ROOT / folder) and path != ROOT / folder for folder in ("runtime", "tmp")):
        raise argparse.ArgumentTypeError("review packets must stay under the repository's ignored runtime/ or tmp/")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--from-day", type=date.fromisoformat, required=True)
    parser.add_argument("--through-day", type=date.fromisoformat, required=True)
    parser.add_argument("--timezone", default="Asia/Taipei")
    parser.add_argument("--model", help="exact recorded model label, optional")
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--write-review-packet", type=local_review_path)
    review.add_argument("--read-review-packet", type=local_review_path)
    args = parser.parse_args()
    try:
        summary, packet = collect(args.database, args.from_day, args.through_day, timezone=args.timezone, model=args.model)
        reviewed = None
        if args.read_review_packet:
            reviewed = json.loads(args.read_review_packet.read_text(encoding="utf-8"))
        summary["explicit_model_body_reviews"] = tally_reviews(packet, reviewed)
        if args.write_review_packet:
            with args.write_review_packet.open("x", encoding="utf-8") as output:
                json.dump(packet, output, ensure_ascii=False, indent=2)
                output.write("\n")
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        # Do not emit paths, packet values or SQLite details to shared output.
        parser.exit(1, f"Unable to read the operations snapshot or review packet ({type(error).__name__}).\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
