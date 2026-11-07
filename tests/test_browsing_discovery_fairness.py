import json

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet
from test_browsing_batches import authorize
from test_browsing_candidate_windows import candidate


def backlog(browsing, count):
    row, _ = browsing.repo.create_round(
        browsing.account,
        browsing.runtime.character,
        browsing.effective() | {"autonomous_batch": True},
        browsing.operations.policy()["epoch"],
        False,
    )
    items = [candidate(number) for number in range(1, count + 1)]
    browsing.repo.batches.merge(row, items)
    browsing.repo.batches.save_plan(
        row,
        {
            "videos": [],
            "workers": 1,
            "choices": [
                {"bvid": item["bvid"], "action": "later", "explanation": "想稍后再看这条内容"}
                for item in items
            ],
        },
        presented=[item["bvid"] for item in items],
    )
    browsing.repo.finish_round(row["id"], "completed")
    return row


def planning_model(windows, old_ids=()):
    async def generate(messages, schema, *, max_tokens):
        assert schema["title"] == "CandidateBatchPlan"
        entries = model_packet(messages[-1]["content"])["candidates"]
        windows.append(entries)
        return {
            "workers": 1,
            "choices": [
                {
                    "bvid": item["bvid"],
                    "action": "later" if item["bvid"] in old_ids else "skip",
                    "explanation": "本条稍后再看" if item["bvid"] in old_ids else "这个新标题暂不想看",
                }
                for item in entries
            ],
        }

    return generate


async def test_twenty_later_items_leave_a_fresh_slot_without_hiding_the_backlog(browsing, monkeypatch):
    await browsing.configure(
        {"sources": ["search"], "keywords": ["实验"], "candidate_limit": 20, "max_pages": 1}
    )
    await authorize(browsing)
    old = backlog(browsing, 20)
    old_ids = {candidate(number)["bvid"] for number in range(1, 21)}
    pages, windows = [], []

    async def discover(source, keyword, page, limit):
        pages.append(page)
        return {"items": [candidate(100 + page)], "has_more": True}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", planning_model(windows, old_ids))
    for _ in range(2):
        row = await finished(browsing, automatic=True, video_only=True)
        assert row["status"] == "completed"
    assert pages == [1, 2]
    assert all(len(window) == 20 for window in windows)
    assert all(any(item["bvid"] not in old_ids for item in window) for window in windows)
    assert old_ids <= {item["bvid"] for window in windows for item in window}
    assert old_ids <= {item["bvid"] for item in browsing.repo.batches.deferred_candidates(row)}
    assert browsing.repo.batches.get(old)["state"] == "completed"
    assert not browsing.adapter.sent


async def test_backlog_fills_the_window_when_discovery_has_no_new_items(browsing, monkeypatch):
    await browsing.configure({"sources": ["search"], "keywords": ["实验"], "candidate_limit": 20})
    await authorize(browsing)
    backlog(browsing, 20)
    old_ids = {candidate(number)["bvid"] for number in range(1, 21)}
    windows = []

    async def discover(*args):
        return {"items": [], "has_more": False}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", planning_model(windows, old_ids))
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed"
    assert {item["bvid"] for item in windows[0]} == old_ids


@pytest.mark.parametrize("source", ["search", "popular", "recommend"])
async def test_discovery_cursor_advances_across_batches_for_each_source(browsing, monkeypatch, source):
    await browsing.configure(
        {
            "sources": [source],
            "keywords": ["实验"] if source == "search" else [],
            "candidate_limit": 1,
            "max_pages": 1,
        }
    )
    await authorize(browsing)
    pages, windows = [], []

    async def discover(source, keyword, page, limit):
        pages.append(page)
        return {"items": [candidate(page)], "has_more": True}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", planning_model(windows))
    for _ in range(2):
        assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    assert pages == [1, 2]


@pytest.mark.parametrize("source", ["search", "popular", "recommend"])
async def test_page_ten_wraps_next_batch_without_requesting_or_storing_eleven(browsing, monkeypatch, source):
    keyword = "实验" if source == "search" else ""
    await browsing.configure(
        {"sources": [source], "keywords": [keyword] if keyword else [], "candidate_limit": 20, "max_pages": 2}
    )
    await authorize(browsing)
    key = f"browsing_source_page:{browsing.account}:{browsing.runtime.character['id']}:{source}:{keyword}"
    browsing.store.set(key, 10)
    pages, windows = [], []

    async def discover(source, keyword, page, limit):
        assert 1 <= page <= 10
        pages.append(page)
        return {"items": [candidate(page)], "has_more": page == 10}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", planning_model(windows))
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    assert browsing.store.get(key) == 1
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    assert pages == [10, 1]
    assert browsing.store.get(key) == 1


async def test_overlapping_sources_preserve_one_candidate_and_all_source_bindings(browsing, monkeypatch):
    await browsing.configure(
        {"sources": ["recommend", "popular"], "keywords": [], "candidate_limit": 4, "max_pages": 1}
    )
    await authorize(browsing)
    backlog(browsing, 1)
    windows = []

    async def discover(source, keyword, page, limit):
        return {
            "items": [candidate(number) for number in ((1, 2) if source == "recommend" else (2, 3))],
            "has_more": False,
        }

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", planning_model(windows))
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed", row
    assert len(windows[0]) == 3
    assert len({item["bvid"] for item in windows[0]}) == 3
    record = browsing.store.one(
        "SELECT payload FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
        (row["config"]["batch_id"], candidate(2)["bvid"]),
    )
    assert {item["source"] for item in json.loads(record["payload"])["sources"]} == {"recommend", "popular"}


async def test_full_retry_prefix_does_not_skip_fresh_retrieval(browsing, monkeypatch):
    await browsing.configure({"sources": ["search"], "keywords": ["实验"], "inspect_limit": 4})
    await authorize(browsing)
    windows, reads = [], []
    monkeypatch.setattr(
        browsing.repo, "retry_candidates", lambda row: [candidate(number) for number in range(1, 5)]
    )

    async def discover(*args):
        reads.append(args)
        return {"items": [candidate(100)], "has_more": False}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", planning_model(windows))
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    assert len(reads) == 1
    assert candidate(100)["bvid"] in {item["bvid"] for item in windows[0]}
