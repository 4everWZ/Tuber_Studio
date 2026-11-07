import json

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet
from test_browsing_batches import authorize


def candidate(number):
    return {"bvid": f"BV{number:010d}", "title": f"独立视频{number}", "duration": 30}


@pytest.mark.parametrize(("limit", "source_mode"), [(20, "retry"), (3, "retry"), (20, "multiple")])
async def test_overflow_from_retries_or_multiple_sources_is_bounded_and_carried_to_the_next_batch(
    browsing, monkeypatch, limit, source_mode
):
    await browsing.configure(
        {
            "candidate_limit": limit,
            "max_pages": 1,
            "sources": ["recommend", "popular"] if source_mode == "multiple" else ["recommend"],
        }
    )
    batches = []

    async def discover(source, keyword, page, count):
        numbers = (
            range(1, 16)
            if source == "recommend" and source_mode == "multiple"
            else range(7, 22)
            if source_mode == "multiple"
            else range(1, limit + 1)
        )
        return {"items": [candidate(number) for number in numbers], "has_more": False}

    async def skip(messages, schema, *, max_tokens):
        assert schema["title"] == "CandidateBatchPlan"
        packet = model_packet(messages[-1]["content"])
        entries = packet["candidates"]
        assert len(entries) <= limit
        assert schema["properties"]["choices"]["minItems"] == len(entries)
        assert schema["properties"]["choices"]["maxItems"] == len(entries)
        assert schema["properties"]["videos"]["maxItems"] == len(entries)
        assert len({item["bvid"] for item in entries}) == len(entries)
        batches.append([item["bvid"] for item in entries])
        return {
            "videos": [],
            "workers": 1,
            "choices": [
                {"bvid": item["bvid"], "action": "skip", "explanation": item["title"] + "本次先略过"}
                for item in entries
            ],
        }

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", skip)
    original_retries = browsing.repo.retry_candidates
    if source_mode == "retry":
        monkeypatch.setattr(browsing.repo, "retry_candidates", lambda row: [candidate(999)])
    await authorize(browsing)
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed", first
    items = browsing.store.all(
        "SELECT * FROM browsing_batch_items WHERE batch_id=?", (first["config"]["batch_id"],)
    )
    assert len(items) == limit + 1
    deferred = [item for item in items if item["status"] == "later"]
    assert len(deferred) == 1
    carried = json.loads(deferred[0]["payload"])
    assert carried["bvid"] not in batches[0] and "selection" not in carried
    assert "queue_reason" in carried
    for item in items:
        if item["status"] == "skipped":
            assert json.loads(item["payload"])["selection"]["decided_at"] > 0
    if source_mode == "multiple":
        overlap = next(json.loads(item["payload"]) for item in items if item["bvid"] == candidate(7)["bvid"])
        assert {entry["source"] for entry in overlap["sources"]} == {"recommend", "popular"}
    monkeypatch.setattr(browsing.repo, "retry_candidates", original_retries)
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["status"] == "completed"
    assert batches[1] == [carried["bvid"]]
    assert (
        browsing.store.one(
            "SELECT status FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
            (first["config"]["batch_id"], carried["bvid"]),
        )["status"]
        == "completed"
    )


async def test_resuming_a_completed_retrieval_with_21_raw_candidates_uses_a_fresh_bounded_plan(
    browsing, monkeypatch
):
    await authorize(browsing)
    config = browsing.policy() | {
        "autonomous_batch": True,
        "overlay_epoch": browsing.operations.policy()["epoch"],
    }
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character, config, browsing.operations.policy()["epoch"], False
    )
    browsing.repo.batches.ensure(row)
    browsing.repo.batches.merge(row, [candidate(number) for number in range(21)])
    browsing.repo.batches.retrieval(row, {"complete": True})

    async def choose(messages, schema, *, max_tokens):
        entries = model_packet(messages[-1]["content"])["candidates"]
        assert len(entries) == 20
        return {
            "videos": [],
            "workers": 1,
            "choices": [
                {"bvid": item["bvid"], "action": "skip", "explanation": "这一条已经看过标题，先略过"}
                for item in entries
            ],
        }

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", choose)
    assert await browsing._discover(row) == []
    assert (
        browsing.store.one(
            "SELECT COUNT(*) n FROM browsing_batch_items WHERE batch_id=? AND status='later'",
            (row["config"]["batch_id"],),
        )["n"]
        == 1
    )
