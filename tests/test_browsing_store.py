"""Cross-channel evidence pins and atomically settled generation budgets."""

import json

import pytest

from tuber_studio.browsing_models import BrowsingPolicy


def test_input_and_output_budget_refunds_use_immediate_transactions(runtime):
    repo = runtime.browsing_repository
    row, _ = repo.create_round("100", runtime.character, BrowsingPolicy().model_dump(), 0, True)
    repo.reserve_budget(row["id"], input_tokens=100, output_tokens=80, model_calls=1)
    statements = []
    runtime.store.db.set_trace_callback(statements.append)
    try:
        repo.settle_input(row["id"], 100, 60)
        repo.settle_output(row["id"], 80, 50)
    finally:
        runtime.store.db.set_trace_callback(None)
    budget = repo.round(row["id"])["budget"]
    assert budget["input_tokens"] == 60 and budget["output_tokens"] == 50 and budget["model_calls"] == 1
    assert statements.count("BEGIN IMMEDIATE") == 2


@pytest.mark.parametrize("reserved,actual", [(10, 11), (10, -1), (True, 0), (10, True)])
def test_invalid_budget_settlement_does_not_refund(runtime, reserved, actual):
    repo = runtime.browsing_repository
    row, _ = repo.create_round("100", runtime.character, BrowsingPolicy().model_dump(), 0, True)
    repo.reserve_budget(row["id"], input_tokens=10)
    with pytest.raises(ValueError):
        repo.settle_input(row["id"], reserved, actual)
    assert repo.round(row["id"])["budget"]["input_tokens"] == 10


def test_unmetered_generation_keeps_full_reservation(runtime):
    repo = runtime.browsing_repository
    row, _ = repo.create_round("100", runtime.character, BrowsingPolicy().model_dump(), 0, True)
    repo.reserve_budget(row["id"], input_tokens=100)
    repo.settle_input(row["id"], 100, None)
    assert repo.round(row["id"])["budget"]["input_tokens"] == 100


def test_dm_video_evidence_remains_pinned_until_draft_no_longer_uses_it(runtime):
    runtime.store.execute("CREATE TABLE dm_drafts(id INTEGER PRIMARY KEY,status TEXT,video_evidence TEXT)")
    for index, status in enumerate(("queued", "generating", "pending_review", "approved", "publishing")):
        runtime.store.execute(
            "INSERT INTO dm_drafts VALUES(?,?,?)",
            (index, status, json.dumps({"material_keys": [f"material-{index}"]})),
        )
    assert runtime.browsing_repository.protected_materials() == {f"material-{i}" for i in range(5)}
    runtime.store.execute("UPDATE dm_drafts SET status='rejected'")
    assert runtime.browsing_repository.protected_materials() == set()
