import asyncio
import copy
import json
import sqlite3

import httpx
import pytest

from tuber_studio.compaction import (
    SUMMARY_PREFIX,
    ContextWindowExceeded,
    IncompleteCompaction,
    approx_token_count,
    build_compacted_history,
    collect_user_messages,
    compact_history,
    insert_initial_context_before_last_real_user_or_summary,
    limits,
    needs_compaction,
    truncate_text,
)
from tuber_studio.context import ContextManager


def user(text, **metadata):
    return {"role": "user", "content": text, "kind": "viewer", **metadata}


def test_codex_20k_budget_keeps_newest_then_restores_order_and_annotations():
    history = [
        user("a" * 40000, source_event_id="old"),
        user("b" * 40000, source_event_id="middle"),
        user("c" * 40000, source_event_id="new"),
    ]
    replacement = build_compacted_history(history, "checkpoint")
    assert [m.get("source_event_id") for m in replacement] == ["middle", "new", None]
    assert replacement[-1]["kind"] == "summary"
    assert replacement[-1]["content"] == "checkpoint"
    assert history[0]["content"] == "a" * 40000


def test_partial_oldest_keeps_head_tail_and_marks_source_incomplete():
    result = build_compacted_history([user("start" + "x" * 100 + "end", source_event_id="x")], "summary", 4)
    assert result[0]["content"] == "startxxx…23 tokens truncated…xxxxxend"
    assert result[0]["source_event_id"] == "x"
    assert result[0]["source_complete"] is False
    assert "summary" == result[-1]["content"]


@pytest.mark.parametrize("text", ["鲸鱼娘🙂" * 7, "abc", "", "🙂" * 9])
@pytest.mark.parametrize("budget", [0, 1, 2, 8, 100])
def test_utf8_truncation_matches_upstream_byte_budget_boundaries(text, budget):
    actual = truncate_text(text, budget)
    if len(text.encode()) <= budget * 4:
        assert actual == text
    else:
        left = budget * 4 // 2
        right = budget * 4 - left
        raw = text.encode()
        expected = raw[:left].decode(errors="ignore")
        expected += f"…{(len(raw) - budget * 4 + 3) // 4} tokens truncated…"
        expected += raw[-right:].decode(errors="ignore") if right else ""
        assert actual == expected
        actual.encode("utf-8")


def test_old_summary_is_not_mistaken_for_a_retained_viewer_message():
    history = [
        user("first"),
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": SUMMARY_PREFIX + "\nold"},
        {"role": "user", "content": "canonical", "kind": "context"},
        user("last"),
    ]
    assert [m["content"] for m in collect_user_messages(history)] == ["first", "last"]


def test_canonical_context_reinjection_precedes_last_real_user_or_summary():
    initial = [{"role": "user", "content": "canonical", "kind": "context"}]
    history = [user("old"), user("new"), {"role": "user", "content": "summary", "kind": "summary"}]
    rebuilt = insert_initial_context_before_last_real_user_or_summary(history, initial)
    assert [m["content"] for m in rebuilt] == ["old", "canonical", "new", "summary"]
    assert insert_initial_context_before_last_real_user_or_summary(history[-1:], initial)[0] == initial[0]
    assert insert_initial_context_before_last_real_user_or_summary([], initial) == initial


def test_128k_trigger_thresholds_and_prefix_scope_keep_full_window_guard():
    config = {"context_tokens": 131072}
    assert limits(config) == (117964, 124518)
    assert not needs_compaction(117963, config)
    assert needs_compaction(117964, config)
    scoped = config | {"auto_compact_token_limit_scope": "body_after_prefix"}
    assert not needs_compaction(120000, scoped, carried_prefix_tokens=20000)
    assert needs_compaction(124518, scoped, carried_prefix_tokens=100000)
    assert limits(config | {"auto_compact_token_limit": 999999})[0] == 117964


class Summarizer:
    server_tokens = 131072
    profile_error = None

    def __init__(self, responses):
        self.responses = list(responses)
        self.inputs = []

    async def summarize(self, messages):
        self.inputs.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def count_tokens(self, messages):
        return sum(approx_token_count(m["content"]) for m in messages)


async def test_overflow_trims_only_work_copy_and_still_retains_original_users():
    provider = Summarizer([ContextWindowExceeded("full"), "summary"])
    original = [user("old"), {"role": "assistant", "content": "response"}, user("new")]
    result = await compact_history(original, [], [], provider)
    assert len(provider.inputs[1]) == len(provider.inputs[0]) - 1
    assert [m["content"] for m in result[:-1]] == ["old", "new"]
    assert len(original) == 3
    assert result[-1]["content"] == SUMMARY_PREFIX + "\nsummary"


async def test_transport_retry_is_bounded_and_partial_attempt_never_installed():
    provider = Summarizer([httpx.ReadTimeout("transient"), "summary"])
    assert (await compact_history([user("x")], [], [], provider, retry_base_seconds=0))[-1][
        "kind"
    ] == "summary"
    failing = Summarizer([httpx.ReadTimeout("again")] * 4)
    with pytest.raises(httpx.ReadTimeout):
        await compact_history([user("x")], [], [], failing, max_retries=3, retry_base_seconds=0)
    assert len(failing.inputs) == 4


@pytest.mark.parametrize("failure", [IncompleteCompaction("partial"), asyncio.CancelledError(), ""])
async def test_incomplete_or_cancelled_summary_preserves_persisted_window(settings, store, failure):
    provider = Summarizer([failure])
    manager = ContextManager(settings, store, provider)
    character = store.character()
    manager.record("comments:video", character, {"event_id": "event", "text": "请记住这个约束"}, "答复")
    before = manager.window("comments:video", character)["history"]
    with pytest.raises((IncompleteCompaction, asyncio.CancelledError)):
        await manager.compact("comments:video", character, "comment_draft")
    after = manager.window("comments:video", character)
    assert after["history"] == before
    assert after["compact_count"] == 0
    assert after["last_error"]


async def test_successful_window_is_persisted_and_modes_and_ocs_do_not_mix(settings, store):
    provider = Summarizer(["已完成评论分类；仍需人工审核回复。"])
    manager = ContextManager(settings, store, provider)
    character = store.character()
    event = {"event_id": "comment:1", "text": "鱼鱼好"}
    manager.record("comments:BV1d8pU6xEZX", character, event, "你好呀", "comment_draft")
    manager.record("live:session", character, {"event_id": "live:1", "text": "开播啦"}, "欢迎")
    result = await manager.compact("comments:BV1d8pU6xEZX", character, "comment_draft")
    assert result["compact_count"] == 1
    restored = ContextManager(settings, store, provider)
    row = restored.window("comments:BV1d8pU6xEZX", character)
    assert json.loads(row["history"])[-1]["kind"] == "summary"
    assert "开播啦" not in row["history"]
    assert restored.window("live:session", character)["compact_count"] == 0
    assert restored.window("comments:BV1d8pU6xEZX", character | {"id": "another_oc"})["history"] == "[]"


async def test_install_commit_failure_rolls_back_before_recording_compaction_error(settings, store):
    provider = Summarizer(["已完成草稿，等待审核。"])
    manager = ContextManager(settings, store, provider)
    character = store.character()
    manager.record("comments:video", character, {"event_id": "x", "text": "你好鱼鱼"}, "你好呀")
    original = manager.window("comments:video", character)["history"]
    store.db.executescript("""
        CREATE TABLE guard_parent(id INTEGER PRIMARY KEY);
        CREATE TABLE guard_child(parent_id INTEGER REFERENCES guard_parent(id) DEFERRABLE INITIALLY DEFERRED);
        CREATE TRIGGER fail_context_commit AFTER UPDATE OF compact_count ON context_windows
        WHEN NEW.compact_count > OLD.compact_count BEGIN INSERT INTO guard_child VALUES(999); END;
    """)
    with pytest.raises(sqlite3.IntegrityError):
        await manager.compact("comments:video", character)
    row = manager.window("comments:video", character)
    assert row["history"] == original
    assert row["compact_count"] == 0
    assert row["last_error"]
    assert store.all("SELECT * FROM guard_child") == []


class TokenCountSummarizer(Summarizer):
    def __init__(self, responses, counts):
        super().__init__(responses)
        self.counts = iter(counts)
        self.counted_frames = []

    async def count_tokens(self, messages):
        self.counted_frames.append(copy.deepcopy(messages))
        return next(self.counts)


@pytest.mark.parametrize("counts,rounds", [([52428], 1), ([52429, 45000], 2)])
async def test_40_percent_threshold_runs_second_full_core_round_only_when_reached(
    settings, store, counts, rounds
):
    provider = TokenCountSummarizer(["first", "second"], counts)
    manager = ContextManager(settings, store, provider)
    character = store.character()
    manager.record("comments:video", character, {"event_id": "c1", "text": "鱼鱼好"}, "欢迎")
    result = await manager.compact("comments:video", character)
    assert len(provider.inputs) == rounds
    assert result["compaction_passes_last"] == rounds
    assert result["compact_count"] == rounds
    assert result["last_compaction_warning"] is None
    if rounds == 2:
        assert any(m["content"] == SUMMARY_PREFIX + "\nfirst" for m in provider.inputs[1])


async def test_second_round_still_above_40_percent_is_visible_and_bounded(settings, store):
    provider = TokenCountSummarizer(["first", "second"], [60000, 55000])
    manager = ContextManager(settings, store, provider)
    character = store.character()
    manager.record("comments:video", character, {"event_id": "c1", "text": "鱼鱼好"}, "欢迎")
    result = await manager.compact("comments:video", character)
    assert len(provider.inputs) == 2
    assert result["occupancy_percent"] >= 40
    assert result["last_compaction_warning"]
    assert result["last_compaction_error"] is None


async def test_second_round_failure_preserves_entire_pre_operation_window(settings, store):
    provider = TokenCountSummarizer(["first", IncompleteCompaction("partial second pass")], [60000])
    manager = ContextManager(settings, store, provider)
    character = store.character()
    manager.record("comments:video", character, {"event_id": "c1", "text": "鱼鱼好"}, "欢迎")
    before = manager.window("comments:video", character)["history"]
    with pytest.raises(IncompleteCompaction):
        await manager.compact("comments:video", character)
    row = manager.window("comments:video", character)
    assert row["history"] == before
    assert row["compact_count"] == 0
    assert row["last_passes"] == 2
    assert row["last_error"]


async def test_second_round_ratio_counts_pending_input_but_does_not_persist_it(settings, store):
    provider = TokenCountSummarizer(["first", "second"], [60000, 45000, 44000])
    manager = ContextManager(settings, store, provider)
    character = store.character()
    manager.record("comments:video", character, {"event_id": "old", "text": "旧问题"}, "旧回复")
    row = manager.window("comments:video", character)
    prefix, initial, pending, suffix = manager.parts(
        character, {"event_id": "pending", "text": "新问题"}, {}, "live"
    )
    await manager._compact(row, prefix, initial, pending, suffix)
    row = manager.window("comments:video", character)
    assert row["input_tokens"] == 45000
    assert row["carried_prefix_tokens"] == 44000
    assert "pending" not in row["history"]
    assert all(any("新问题" in m["content"] for m in frame) for frame in provider.counted_frames[:2])
    assert not any("新问题" in m["content"] for m in provider.counted_frames[-1])
