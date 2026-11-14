import asyncio
import json

import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_batches import authorize


@pytest.mark.parametrize("phase,expected", [
    ("discovered", "selected"), ("claimed", "running"),
    ("preparing", "running"), ("evidence_ready", "running"),
])
def test_intermediate_worker_phases_are_not_reported_as_paused(browsing, phase, expected):
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.policy() | {"autonomous_batch": True}, 0, False
    )
    browsing.repo.batches.merge(row, [{"bvid": BV}])
    identity = browsing.repo.task(row["id"], {"bvid": BV})
    browsing.repo.batches.outcome(identity, phase)
    item = browsing.store.one("SELECT status FROM browsing_batch_items WHERE task_id=?", (identity,))
    assert item["status"] == expected
    assert not browsing.store.all("SELECT * FROM browsing_batch_failures")


async def test_prepared_task_stays_running_until_real_pause_then_resumes_same_batch(browsing, monkeypatch):
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV], "recent_sample_limit": 0})
    await authorize(browsing)
    entered = asyncio.Event()
    original = browsing.runtime.llm.generate_json

    async def paused_model(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            entered.set()
            await asyncio.Future()
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", paused_model)
    start = await browsing.run(automatic=True, video_only=True)
    await asyncio.wait_for(entered.wait(), 3)
    row = browsing.repo.round(start["id"])
    task = browsing.store.one("SELECT * FROM browsing_tasks WHERE round_id=?", (row["id"],))
    item = browsing.store.one("SELECT * FROM browsing_batch_items WHERE task_id=?", (task["id"],))
    assert task["status"] == "evidence_ready" and item["status"] == "running"
    worker = browsing.active[row["id"]]
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert browsing.repo.round(row["id"])["status"] == "paused"
    assert browsing.store.one("SELECT status FROM browsing_batch_items WHERE task_id=?", (task["id"],))["status"] == "paused"
    browsing.repo.recover()
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", original)
    restored = await finished(browsing, automatic=True, video_only=True)
    assert restored["id"] == row["id"] and restored["status"] == "completed"
    assert browsing.store.one(
        "SELECT status FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
        (restored["config"]["batch_id"], BV),
    )["status"] == "completed"
    assert browsing.store.one("SELECT id FROM browsing_tasks WHERE id=?", (task["id"],))
    assert browsing.repo.batches.get(restored)["state"] == "completed"
    assert (await finished(browsing, automatic=True, interaction_only=True))["status"] == "completed"
    assert browsing.store.one(
        "SELECT status FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
        (restored["config"]["batch_id"], BV),
    )["status"] == "completed"
    assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1


async def test_final_metrics_use_this_run_counts_and_duration_but_keep_saved_plan(browsing, monkeypatch):
    await browsing.preflight()
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.policy() | {"video_only": True}, 0, True
    )
    plan = {"workers": 1, "choices": [], "videos": []}

    async def discover(current):
        browsing.store.execute("UPDATE browsing_rounds SET metrics=? WHERE id=?", (
            json.dumps({"prepared": 99, "viewed": 98, "failures": 97, "elapsed_seconds": 9999,
                        "cache_hits": 96, "peak_rss_bytes": 1, "batch_plan": plan,
                        "source_error": "先前检索过程的已保存诊断"}), row["id"],
        ))
        return []

    monkeypatch.setattr(browsing, "_discover", discover)
    await browsing._execute(row, False)
    saved = browsing.repo.round(row["id"])
    metrics = saved["metrics"]
    assert saved["status"] == "completed"
    assert metrics["prepared"] == metrics["viewed"] == metrics["failures"] == metrics["cache_hits"] == 0
    assert 0 <= metrics["elapsed_seconds"] < 5 and metrics["peak_rss_bytes"] > 1
    assert metrics["batch_plan"] == plan
    assert metrics["source_error"] == "先前检索过程的已保存诊断"
