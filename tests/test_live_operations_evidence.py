import copy
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from evaluations.live_operations_evidence import collect, has_comment_receipt, tally_reviews
from tuber_studio.operation_continuations import ContinuationCoordinator
from tuber_studio.operations.browsing.repository import BrowsingRepository
from tuber_studio.operations_control import OperationsControl


def test_public_candidates_receipts_and_explicit_reviews_are_distinct(settings, store):
    assert not has_comment_receipt({"code": 0.0, "rpid": "8"})
    BrowsingRepository(store)
    ContinuationCoordinator(store, OperationsControl(store))
    zone = ZoneInfo("Asia/Taipei")
    first = datetime(2026, 11, 11, 23, 59, tzinfo=zone).timestamp()
    next_day = first + 120

    def add_candidate(identity, kind, status, model_body, stored_body, receipt=None):
        generation_id = "generation-" + identity
        if model_body is not None:
            binding = json.dumps({"model": json.dumps(["llama_cpp", "recorded-model", "local", True])})
            store.execute(
                "INSERT INTO operation_continuations (id,channel,entity_id,binding,state,episode,deadline,"
                "feedback,current_output,created_at,updated_at) VALUES (?,'generation.browsing',?,?,'completed',"
                "'episode',?, '{}', ?,?,?)",
                (generation_id, identity, binding, next_day, json.dumps({"text": model_body}), first, first),
            )
        target = json.dumps({"parent": "7", "source_text": "source-text", "generation_continuation_id": generation_id})
        store.execute(
            "INSERT INTO browsing_intents (id,account_id,character_id,source_key,kind,status,text,target,"
            "receipt,created_at,updated_at) VALUES (?,'account-label','character-label',?,?,?,?,?,?,?,?)",
            (identity, identity, kind, status, stored_body, target, json.dumps(receipt), first, next_day),
        )

    add_candidate("edited", "reply_public", "published", "model-body", "stored-body", {"code": 0, "rpid": "8"})
    add_candidate("pending", "reply_public", "pending_review", "reply_target:{", "reply_target:{")
    add_candidate("unknown", "top_level", "published", None, "unconfirmed-body", {"code": 0, "rpid": "0"})
    add_candidate("private", "private_handoff", "pending_review", "private-body", "private-body")
    store.execute(
        "INSERT INTO browsing_reply_quotas VALUES ('account-label','character-label','edited','video',"
        "'2026-11-12','token','published',?,?,?)", (first, next_day, next_day),
    )

    db = settings.runtime / "test.sqlite3"
    start, stop = date(2026, 11, 11), date(2026, 11, 12)
    summary, packet = collect(db, start, stop)
    first_counts, second_counts = summary["days"].values()
    assert first_counts["public_candidates"] == 3
    assert first_counts["recorded_model_bodies"] == 2
    assert first_counts["pending_review_candidates"] == 1
    assert first_counts["stored_candidate_differs_from_model_body"] == 1
    assert first_counts["model_bodies_flagged_by_current_validator"] == 1
    assert first_counts["confirmed_thread_replies"] == 0
    assert second_counts["confirmed_thread_replies"] == 1
    assert second_counts["confirmed_model_body_submissions"] == 0
    assert second_counts["published_without_valid_receipt"] == 1
    assert all(value not in json.dumps(summary) for value in ("model-body", "source-text", "account-label", "private-body"))
    assert tally_reviews(packet) == {"satisfied": 0, "needs_revision": 0, "unreviewed": 2, "no_recorded_model_body": 1}

    reviewed = copy.deepcopy(packet)
    next(case for case in reviewed["cases"] if case["case_id"] == "edited")["judgment"] = "satisfied"
    assert tally_reviews(packet, reviewed)["satisfied"] == 1
    next(case for case in reviewed["cases"] if case["case_id"] == "unknown")["judgment"] = "satisfied"
    with pytest.raises(ValueError, match="requires a recorded model body"):
        tally_reviews(packet, reviewed)
    reviewed["cases"][0]["model_body"] = "swapped"
    with pytest.raises(ValueError, match="changed candidate"):
        tally_reviews(packet, reviewed)

    filtered, _ = collect(db, start, stop, model="recorded-model")
    assert filtered["days"]["2026-11-11"]["public_candidates"] == 2
    assert filtered["days"]["2026-11-12"]["published_without_valid_receipt"] == 0
