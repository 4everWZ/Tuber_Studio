import asyncio
import copy
import json
import sqlite3

import httpx
import pytest

from tuber_studio.comments import Comments
from tuber_studio.providers import LlamaCppProvider


class LocalOnly:
    async def close(self):
        pass

    async def fetch(self, *args, **kwargs):
        raise AssertionError("Context rebuild must not fetch a deleted dataset")


def originals(service):
    service.import_items(
        "import-rebuild",
        [
            {
                "comment_id": "old",
                "thread_id": "floor",
                "content": "本楼此前的原始问题",
                "created_at": "1999-01-01",
            },
            {"comment_id": "current", "thread_id": "floor", "content": "现在补充的问题如何解释？"},
            {"comment_id": "foreign", "thread_id": "another", "content": "其他楼的秘密"},
        ],
    )
    return service.store.all("SELECT * FROM comments ORDER BY id")


async def real_protocol(settings, runtime, *, failure=None, first_large=False):
    settings.data["llm"]["kind"] = "llama_cpp"
    provider = LlamaCppProvider(settings.data["llm"])
    await provider.client.aclose()
    requests = []
    entered, closed = asyncio.Event(), asyncio.Event()
    token_calls = 0

    async def respond(request):
        nonlocal token_calls
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "model_alias": settings.data["llm"]["model"],
                    "default_generation_settings": {"n_ctx": 131072},
                },
            )
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered"})
        if request.url.path == "/tokenize":
            token_calls += 1
            return httpx.Response(
                200, json={"tokens": list(range(118000)) if first_large and token_calls == 1 else [1, 2, 3]}
            )
        entered.set()
        if failure == "cancel":
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        if failure == "http":
            return httpx.Response(500, json={"error": "not installed"})
        if callable(failure):
            failure()
        content = (
            "丢弃的prefill输出"
            if body["max_tokens"] == 1
            else (
                "本楼原文已压缩，尚无已发布答复。"
                if "response_format" not in body
                else json.dumps({"spoken_text": "我会根据本楼原文认真回答。"}, ensure_ascii=False)
            )
        )
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 2 if failure == "count" else 3},
                "choices": [
                    {
                        "finish_reason": "length" if body["max_tokens"] == 1 else "stop",
                        "message": {"content": content},
                    }
                ],
            },
        )

    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    await runtime.llm.close()
    runtime.llm = provider
    runtime.context.provider = provider
    return requests, entered, closed


async def test_manual_rebuild_prefills_only_original_floor_data_and_indexes_viewers(settings, store, runtime):
    service = Comments(settings, store, runtime, LocalOnly())
    rows = originals(service)
    service.import_items(
        "import-another", [{"comment_id": "else", "thread_id": "floor", "content": "不同视频的秘密"}]
    )
    store.execute(
        "INSERT INTO comments(platform,video_id,comment_id,thread_id,content) VALUES('mock','import-rebuild','else','floor','不同平台的秘密')"
    )
    character = runtime.character
    scope = service.context_scope("import-rebuild", "floor")
    runtime.context.record(
        scope,
        character | {"version": character["version"] + 1},
        {"event_id": "other-version", "text": "另一角色版本的秘密"},
        "旧版本虚构答复",
    )
    store.execute(
        "INSERT INTO drafts(comment_id,text,status) VALUES(?,?,'pending_review')",
        (rows[0]["id"], "未发旧草稿不能变已说事实"),
    )
    requests, _, _ = await real_protocol(settings, runtime)
    async with runtime.generation_lock:
        result = await service.rebuild_context("import-rebuild", "floor")
    assert result["source_count"] == 2 and result["prefilled"] and result["input_tokens"] == 3
    model_calls = [body for path, body in requests if path == "/v1/chat/completions"]
    assert len(model_calls) == 1 and model_calls[0]["max_tokens"] == 1
    prompt = json.dumps(model_calls[0]["messages"], ensure_ascii=False)
    assert "本楼此前的原始问题" in prompt and "现在补充的问题" in prompt
    assert "其他楼的秘密" not in prompt and "未发旧草稿" not in prompt
    assert (
        "不同视频的秘密" not in prompt
        and "不同平台的秘密" not in prompt
        and "另一角色版本的秘密" not in prompt
    )
    window = runtime.context.existing(result["scope"], runtime.character)
    history = json.loads(window["history"])
    assert all(item["role"] == "user" and item["reconstructed"] for item in history)
    assert all(item["created_at"] >= window["oldest_source_at"] for item in history)
    assert window["oldest_source_at"] > 1000000000  # The 1999 post date is not cache age.
    sources = store.all(
        "SELECT * FROM grounded_memory WHERE scope=? AND character_id=? AND character_version=?",
        (result["scope"], character["id"], character["version"]),
    )
    assert len(sources) == 2 and all(
        row["role"] == "user" and row["kind"] == "viewer_message" and row["facts"] == "[]" for row in sources
    )
    assert store.one("SELECT COUNT(*) AS n FROM drafts")["n"] == 1


async def test_demo_rebuild_is_explicitly_not_a_real_prefill(settings, store, runtime):
    service = Comments(settings, store, runtime, LocalOnly())
    originals(service)
    result = await service.rebuild_context("import-rebuild", "floor")
    assert result["rebuilt"] and result["prefilled"] is False and "演示" in result["message"]


async def test_rebuild_refuses_nonempty_or_absent_originals_without_overwrite(settings, store, runtime):
    service = Comments(settings, store, runtime, LocalOnly())
    scope = service.context_scope("import-rebuild", "floor")
    runtime.context.record(scope, runtime.character, {"event_id": "kept", "text": "已有问题"}, "已有答复")
    original = copy.deepcopy(runtime.context.existing(scope, runtime.character))
    with pytest.raises(ValueError, match="非空"):
        await service.rebuild_context("import-rebuild", "floor")
    assert runtime.context.existing(scope, runtime.character) == original
    with pytest.raises(ValueError, match="没有保留"):
        await service.rebuild_context("import-rebuild", "absent")
    assert (
        runtime.context.existing(service.context_scope("import-rebuild", "absent"), runtime.character) is None
    )


@pytest.mark.parametrize("failure", ["http", "count", "cancel"])
async def test_failed_or_cancelled_prefill_never_installs_history_or_source_index(
    settings, store, runtime, failure
):
    service = Comments(settings, store, runtime, LocalOnly())
    originals(service)
    scope = service.context_scope("import-rebuild", "floor")
    previous = copy.deepcopy(runtime.context.window(scope, runtime.character))
    _, entered, closed = await real_protocol(settings, runtime, failure=failure)
    work = asyncio.create_task(service.rebuild_context("import-rebuild", "floor"))
    await entered.wait()
    if failure == "cancel":
        work.cancel()
    with pytest.raises((ValueError, httpx.HTTPError, asyncio.CancelledError)):
        await work
    assert runtime.context.existing(scope, runtime.character) == previous
    assert not store.all("SELECT * FROM grounded_memory WHERE scope=?", (scope,))
    if failure == "cancel":
        assert closed.is_set()


async def test_dataset_removed_during_prefill_is_not_reanimated(settings, store, runtime):
    service = Comments(settings, store, runtime, LocalOnly())
    originals(service)
    await real_protocol(
        settings, runtime, failure=lambda: store.execute("DELETE FROM comments WHERE thread_id='floor'")
    )
    with pytest.raises(ValueError, match="清理"):
        await service.rebuild_context("import-rebuild", "floor")
    assert (
        runtime.context.existing(service.context_scope("import-rebuild", "floor"), runtime.character) is None
    )


async def test_auto_rebuild_excludes_current_comment_and_uses_generation_prefill_once(
    settings, store, runtime
):
    service = Comments(settings, store, runtime, LocalOnly())
    rows = originals(service)
    store.execute(
        "INSERT INTO drafts(comment_id,text,status) VALUES(?,?,'pending_review')",
        (rows[0]["id"], "旧草稿的虚构经历"),
    )
    requests, _, _ = await real_protocol(settings, runtime)
    await service._process_comment(rows[1], runtime.character, False)
    calls = [body for path, body in requests if path == "/v1/chat/completions"]
    assert len(calls) == 1 and calls[0]["max_tokens"] != 1
    content = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert content.count("现在补充的问题如何解释？") == 1
    assert (
        "本楼此前的原始问题" in content
        and "旧草稿的虚构经历" not in content
        and "其他楼的秘密" not in content
    )
    history = json.loads(
        runtime.context.existing(service.context_scope("import-rebuild", "floor"), runtime.character)[
            "history"
        ]
    )
    assert len([item for item in history if item["role"] == "assistant"]) == 1
    assert store.one("SELECT * FROM drafts WHERE comment_id=?", (rows[1]["id"],))


async def test_auto_install_commit_failure_rolls_back_draft_and_window_together(settings, store, runtime):
    service = Comments(settings, store, runtime, LocalOnly())
    rows = originals(service)
    await real_protocol(settings, runtime)
    store.db.executescript(
        "CREATE TRIGGER reject_rebuild BEFORE INSERT ON context_windows BEGIN SELECT RAISE(ABORT,'reject install'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await service._process_comment(rows[1], runtime.character, False)
    assert store.one("SELECT * FROM drafts WHERE comment_id=?", (rows[1]["id"],)) is None
    assert (
        store.all("SELECT * FROM context_windows") == [] and store.all("SELECT * FROM grounded_memory") == []
    )


async def test_rebuild_reuses_existing_core_compaction_and_preserves_ingestion_age(
    settings, store, runtime, monkeypatch
):
    service = Comments(settings, store, runtime, LocalOnly())
    originals(service)
    requests, _, _ = await real_protocol(settings, runtime, first_large=True)
    clock = [1800000000.0]
    monkeypatch.setattr("tuber_studio.context.time.time", lambda: clock[0])
    result = await service.rebuild_context("import-rebuild", "floor")
    assert result["compacted"] and result["source_count"] == 2
    calls = [body for path, body in requests if path == "/v1/chat/completions"]
    assert len(calls) == 2 and calls[-1]["max_tokens"] == 1
    scope = result["scope"]
    row = runtime.context.existing(scope, runtime.character)
    assert row["oldest_source_at"] == clock[0]
    assert all(
        item["created_at"] == clock[0] for item in json.loads(row["history"]) if item["kind"] == "viewer"
    )
    clock[0] += 100
    runtime.context.record(scope, runtime.character, {"event_id": "new", "text": "新问题"}, "新答复")
    await runtime.context.compact(scope, runtime.character, "comment_draft")
    row = runtime.context.existing(scope, runtime.character)
    assert row["oldest_source_at"] == 1800000000.0 and row["updated_at"] == clock[0]
