import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tuber_studio import dynamics as module
from tuber_studio.bilibili import PublishUncertain
from tuber_studio.dynamics import Dynamics
from tuber_studio.memory import GroundedMemory


class Model:
    def __init__(self):
        self.calls = []
        self.schemas = []
        self.count_calls = []
        self.output = None
        self.summary_output = None
        self.summary_hook = None
        self.hook = None

    async def generate_json(self, messages, schema, *, max_tokens):
        self.calls.append(messages)
        self.schemas.append(schema)
        payload = json.loads(messages[1]["content"])
        if payload.get("task") == "operations_memory_summary":
            if self.summary_hook:
                await self.summary_hook()
            return (
                json.loads(self.summary_output)
                if self.summary_output is not None
                else (
                    {
                        "entries": [
                            {"source_id": source["id"], "quote": source["text"]}
                            for source in payload["public_sources"]
                        ]
                    }
                )
            )
        if self.hook:
            await self.hook()
        return (
            json.loads(self.output)
            if self.output is not None
            else (
                {
                    "action": "post",
                    "reason": "和大家分享公开的虚构故事",
                    "content": "海底小剧场：今天又多盛了一碗饭。",
                    "source_ids": [json.loads(messages[1]["content"])["public_sources"][0]["id"]],
                    "next_evaluation_seconds": 1800,
                }
            )
        )

    async def count_tokens(self, messages):
        self.count_calls.append(messages)
        return len(messages[0]["content"]) // 2 + 1


@pytest.fixture
def service(settings, store, monkeypatch):
    comments = SimpleNamespace(adapter=object(), publish_lock=asyncio.Lock(), configured=True)
    comments.policy = lambda: {"credentials_configured": comments.configured}
    runtime = SimpleNamespace(llm=Model(), generation_lock=asyncio.Lock())
    service = Dynamics(settings, store, runtime, comments)
    service.sent = []

    async def send(adapter, text, *, before_send=None):
        if before_send:
            before_send()
        service.sent.append((adapter, text))
        return {"platform": "bilibili", "code": 0, "dynamic_id": "12345"}

    monkeypatch.setattr(module, "publish_text", send)
    return service


def memory(service, text="在虚构的海底厨房，我又盛了一碗饭。", *, role="assistant", kind=None):
    GroundedMemory(service.store).record(
        "test", service.store.character(), str(uuid4()), role, text, kind=kind
    )
    return service.store.one("SELECT MAX(id) AS id FROM grounded_memory")["id"]


def public_memory(service, **kwargs):
    identifier = memory(service, **kwargs)
    service.mark_memory(identifier, True)
    return identifier


def published_reply(service):
    char = service.store.character()
    cid = service.store.execute(
        "INSERT INTO comments(video_id,comment_id,content) VALUES(?,?,?)",
        ("BV1d8pU6xEZX", "42", "鱼鱼的海底厨房是什么样？"),
    ).lastrowid
    return service.store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,receipt) VALUES(?,?,?,?,?,?)",
        (cid, "想象中的海底厨房有热饭。", "published", char["id"], char["version"], '{"rpid":"77"}'),
    ).lastrowid


def due(service):
    service.store.set(
        "dynamic_schedule", {"next_evaluation_at": 0, "backoff_until": None, "last_error": None}
    )


async def test_disabled_default_and_manual_never_auto_sends(service):
    public_memory(service)
    assert service.policy()["enabled"] is False
    assert service.policy()["auto_publish"] is True
    assert (await service.evaluate())["status"] == "skipped"
    assert not service.runtime.llm.calls
    result = await service.evaluate(manual=True)
    assert result["action"] == "post" and result["status"] == "pending_review"
    assert service.sent == []
    await service.set_policy({"enabled": True})
    with pytest.raises(ValueError, match="未授权"):
        await service.publish(result["post_id"], automatic=True)
    due(service)
    assert (await service.evaluate())["action"] == "wait"
    assert service.sent == []  # No queue sweep of manual drafts.


async def test_enabled_auto_publishes_only_new_background_decision(service):
    public_memory(service)
    await service.set_policy({"enabled": True})
    result = await service.evaluate()
    assert result["status"] == "published"
    assert len(service.sent) == 1
    post = service.list_posts()[0]
    assert post["receipt"]["dynamic_id"] == "12345" and post["published_at"]
    with pytest.raises(ValueError):
        await service.publish(post["id"])


async def test_review_mode_and_current_client(service):
    public_memory(service)
    await service.set_policy({"enabled": True, "auto_publish": False})
    result = await service.evaluate()
    with pytest.raises(ValueError, match="审核"):
        await service.publish(result["post_id"])
    service.review(result["post_id"], "approve", "虚构海底剧场：小厨房开饭啦！")
    replacement = object()
    service.comments.adapter = replacement
    await service.publish(result["post_id"])
    assert service.sent == [(replacement, "虚构海底剧场：小厨房开饭啦！")]


async def test_only_explicit_public_memory_enters_prompt(service):
    private_id = memory(service, "私聊里尚未公开的秘密ABC")
    memory(service, "用户的私人数据XYZ", role="user")
    memory(service, "未执行的角色计划", kind="role_plan")
    assert (await service.evaluate(manual=True))["action"] == "wait"
    assert not service.runtime.llm.calls
    assert [m["id"] for m in service.list_memories()] == [private_id]
    public_memory(service, text="明确允许公开的虚构角色故事")
    await service.evaluate(manual=True)
    prompt = json.dumps(service.runtime.llm.calls, ensure_ascii=False)
    assert "明确允许公开" in prompt and "fictional" in prompt
    assert "秘密ABC" not in prompt and "私人数据XYZ" not in prompt and "未执行的角色计划" not in prompt


async def test_published_reply_includes_original_comment_and_receipt(service):
    rid = published_reply(service)
    decision = await service.evaluate(manual=True)
    assert decision["source_ids"] == [f"reply:{rid}"]
    assert decision["sources"][0]["original_comment"]["text"] == "鱼鱼的海底厨房是什么样？"
    assert decision["sources"][0]["receipt_id"] == "77"
    assert decision["sources"][0]["fictional"] is True


@pytest.mark.parametrize("change", ["delete", "unmark", "rewrite", "version", "character"])
async def test_changed_source_or_character_invalidates_before_write(service, change):
    mid = public_memory(service)
    result = await service.evaluate(manual=True)
    service.review(result["post_id"], "approve")
    if change == "delete":
        service.store.execute("DELETE FROM grounded_memory WHERE id=?", (mid,))
    elif change == "unmark":
        service.mark_memory(mid, False)
    elif change == "rewrite":
        service.store.execute("UPDATE grounded_memory SET text='changed' WHERE id=?", (mid,))
    elif change == "version":
        service.store.save_character(service.store.character())
    else:
        service.store.set("active_character", "whale_calm")
    with pytest.raises(ValueError, match="来源已变化"):
        await service.publish(result["post_id"])
    assert service.list_posts()[0]["status"] == "invalidated" and not service.sent


async def test_deleted_public_comment_invalidates(service):
    published_reply(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    service.store.execute("DELETE FROM comments")
    with pytest.raises(ValueError):
        await service.publish(decision["post_id"])
    assert not service.sent


@pytest.mark.parametrize(
    "output",
    [
        "```json\n{}\n```",
        "{} trailing",
        "{}",
        '{"action":"post","reason":"r","content":"c","source_ids":["memory:999"],"next_evaluation_seconds":1800}',
        '{"action":"post","reason":"r","content":"c","source_ids":[],"next_evaluation_seconds":1800}',
        '{"action":"wait","reason":"r","content":"c","source_ids":[],"next_evaluation_seconds":0}',
    ],
)
async def test_invalid_or_ungrounded_model_output_never_posts(service, output):
    public_memory(service)
    service.runtime.llm.output = output
    await service.set_policy({"enabled": True})
    result = await service.evaluate()
    assert result["action"] == "error" and service.sent == [] and service.list_posts() == []
    assert service.policy()["backoff_until"] > time.time()


async def test_generation_holds_unique_runtime_lock_and_character_rechecked(service):
    public_memory(service)

    async def change():
        assert service.runtime.generation_lock.locked()
        service.store.save_character(service.store.character())

    service.runtime.llm.hook = change
    result = await service.evaluate(manual=True)
    assert result["action"] == "error" and not service.list_posts()


async def test_disable_while_model_running_prevents_send(service):
    public_memory(service)
    await service.set_policy({"enabled": True})

    async def disable():
        await service.set_policy({"enabled": False})

    service.runtime.llm.hook = disable
    result = await service.evaluate()
    assert result["status"] == "pending_review" and not service.sent


@pytest.mark.parametrize(
    "failure,status",
    [
        (PublishUncertain("SESSDATA secret"), "publish_uncertain"),
        (RuntimeError("SESSDATA secret"), "publish_uncertain"),
        (ValueError("SESSDATA secret"), "publish_failed"),
    ],
)
async def test_failed_attempts_are_durable_safe_and_not_retried(service, monkeypatch, failure, status):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    calls = []

    async def fail(*args, before_send=None):
        if before_send:
            before_send()
        calls.append(args)
        raise failure

    monkeypatch.setattr(module, "publish_text", fail)
    await service.set_policy({"enabled": True})
    with pytest.raises(ValueError):
        await service.publish(decision["post_id"])
    post = service.list_posts()[0]
    assert post["status"] == status and post["attempted_at"]
    assert "secret" not in json.dumps(service.policy()) + json.dumps(post)
    if status == "publish_uncertain":
        assert service.policy()["enabled"] is False
    again = Dynamics(service.settings, service.store, service.runtime, service.comments)
    with pytest.raises(ValueError):
        await again.publish(post["id"])
    assert len(calls) == 1
    with pytest.raises(ValueError, match="间隔"):
        again._quota()


async def test_inflight_cancel_pauses_and_restart_never_retries(service, monkeypatch):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    started = asyncio.Event()

    async def hang(*args, before_send=None):
        if before_send:
            before_send()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "publish_text", hang)
    task = asyncio.create_task(service.publish(decision["post_id"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.list_posts()[0]["status"] == "publish_uncertain"
    assert service.policy()["enabled"] is False
    service.store.execute("UPDATE dynamic_posts SET status='publishing'")
    again = Dynamics(service.settings, service.store, service.runtime, service.comments)
    assert again.list_posts()[0]["status"] == "publish_uncertain"


async def test_six_hour_and_two_per_rolling_day_hard_limits(service):
    for index in range(3):
        public_memory(service, text=f"公开虚构故事{index}")
        decision = await service.evaluate(manual=True)
        service.review(decision["post_id"], "approve", f"海底虚构小剧场第{index}回。")
        if index == 1:
            with pytest.raises(ValueError, match="间隔"):
                await service.publish(decision["post_id"])
            service.store.execute(
                "UPDATE dynamic_posts SET attempted_at=? WHERE attempted_at IS NOT NULL",
                (time.time() - 21601,),
            )
        if index == 2:
            service.store.execute(
                "UPDATE dynamic_posts SET attempted_at=? WHERE attempted_at IS NOT NULL",
                (time.time() - 21601,),
            )
            with pytest.raises(ValueError, match="上限"):
                await service.publish(decision["post_id"])
        else:
            await service.publish(decision["post_id"])
    assert len(service.sent) == 2


async def test_normalized_duplicate_and_used_sources_block_repetition(service):
    public_memory(service)
    first = await service.evaluate(manual=True)
    service.review(first["post_id"], "approve", "海底 小剧场！")
    await service.publish(first["post_id"])
    calls = len(service.runtime.llm.calls)
    assert (await service.evaluate(manual=True))["action"] == "wait"
    assert len(service.runtime.llm.calls) == calls
    public_memory(service, text="另一条公开的虚构故事")
    second = await service.evaluate(manual=True)
    service.review(second["post_id"], "approve", "海底小剧场。")
    service.store.execute(
        "UPDATE dynamic_posts SET attempted_at=? WHERE attempted_at IS NOT NULL", (time.time() - 90000,)
    )
    with pytest.raises(ValueError, match="相同正文"):
        await service.publish(second["post_id"])


async def test_publish_shares_comment_lock_and_rechecks_source_after_wait(service):
    mid = public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    async with service.comments.publish_lock:
        task = asyncio.create_task(service.publish(decision["post_id"]))
        await asyncio.sleep(0)
        assert not service.sent
        service.mark_memory(mid, False)
    with pytest.raises(ValueError):
        await task
    assert not service.sent


@pytest.mark.parametrize(
    "patch",
    [
        {"evaluate_interval_seconds": 299},
        {"evaluate_interval_seconds": True},
        {"min_post_interval_seconds": 21599},
        {"max_posts_per_day": 3},
        {"enabled": 1},
        {"auto_publish": "true"},
        {"csrf": "secret"},
    ],
)
async def test_policy_bounds(service, patch):
    with pytest.raises(ValueError):
        await service.set_policy(patch)


async def test_enable_requires_credentials_and_worker_disabled_by_default(service):
    service.comments.configured = False
    with pytest.raises(ValueError, match="凭证"):
        await service.set_policy({"enabled": True})
    await service.open()
    await asyncio.sleep(0)
    await service.close()
    assert service.runtime.llm.calls == [] and service.sent == [] and service.worker is None


@pytest.mark.parametrize("change", ["stop", "review_mode", "unmark", "character", "account"])
async def test_final_guard_after_signing_await_cancels_without_write(service, monkeypatch, change):
    mid = public_memory(service)
    await service.set_policy({"enabled": True})
    writes = []

    async def preparing(adapter, text, *, before_send):
        # Represents the awaited read-only WBI preparation before the write.
        await asyncio.sleep(0)
        if change == "stop":
            await service.set_policy({"enabled": False})
        elif change == "review_mode":
            await service.set_policy({"auto_publish": False})
        elif change == "unmark":
            service.mark_memory(mid, False)
        elif change == "character":
            service.store.save_character(service.store.character())
        else:
            service.comments.adapter = object()
        before_send()
        writes.append(text)
        return {"dynamic_id": "123"}

    monkeypatch.setattr(module, "publish_text", preparing)
    decision = await service.evaluate()
    post = service.list_posts()[0]
    assert decision["status"] in {"pending_review", "invalidated"}
    assert post["attempted_at"] is None and post["status"] != "publish_uncertain"
    assert not writes


@pytest.mark.parametrize("after_lock", [False, True])
async def test_live_priority_checked_before_and_inside_generation_lock(service, after_lock):
    public_memory(service)
    await service.set_policy({"enabled": True})
    service.runtime.state = "live"
    service.runtime.queue = []
    service.runtime.current_id = None
    if after_lock:
        async with service.runtime.generation_lock:
            task = asyncio.create_task(service.evaluate())
            await asyncio.sleep(0)
            service.runtime.current_id = "speech-job"
        result = await task
    else:
        service.runtime.queue = ["speech-job"]
        result = await service.evaluate()
    assert result["action"] == "wait" and "直播" in result["reason"]
    assert not service.runtime.llm.calls and not service.sent
    assert result["next_evaluation_at"] >= result["created_at"] + 300


async def test_public_revoke_while_waiting_for_model_lock_never_enters_prompt(service):
    mid = public_memory(service)
    async with service.runtime.generation_lock:
        task = asyncio.create_task(service.evaluate(manual=True))
        await asyncio.sleep(0)
        service.mark_memory(mid, False)
    result = await task
    assert result["action"] == "wait" and not service.runtime.llm.calls


async def test_old_draft_cannot_reuse_already_attempted_source(service):
    mid = public_memory(service)
    first = await service.evaluate(manual=True)
    # An old independently created draft of the same source must not evade
    # evaluation-time filtering. Simulate a legacy invalidation and reactivation,
    # independent from the current rule that an operator's rejection stays final.
    service.store.execute("UPDATE dynamic_posts SET status='invalidated' WHERE id=?", (first["post_id"],))
    second = await service.evaluate(manual=True)
    service.store.execute("UPDATE dynamic_posts SET status='approved' WHERE id=?", (first["post_id"],))
    service.review(second["post_id"], "approve", "另一段虚构海底故事")
    await service.publish(first["post_id"])
    service.store.execute(
        "UPDATE dynamic_posts SET attempted_at=? WHERE id=?", (time.time() - 90000, first["post_id"])
    )
    with pytest.raises(ValueError, match="来源已尝试"):
        await service.publish(second["post_id"])
    assert service.list_memories()[0]["id"] == mid and len(service.sent) == 1


def test_memory_id_reuse_new_created_at_needs_new_public_mark(service):
    mid = public_memory(service)
    service.store.execute("UPDATE grounded_memory SET created_at=created_at+1 WHERE id=?", (mid,))
    assert service.list_memories()[0]["public"] is False


async def test_operational_prompt_contains_timezone_and_only_published_history(service):
    public_memory(service)
    first = await service.evaluate(manual=True)
    service.review(first["post_id"], "approve", "已经发布的虚构故事")
    await service.publish(first["post_id"])
    public_memory(service, text="新公开的虚构记忆")
    await service.evaluate(manual=True)
    payload = json.loads(service.runtime.llm.calls[-1][1]["content"])
    assert payload["current_time"][-6] in {"+", "-"}
    assert payload["recent_public_posts"] == [
        {
            "content": "已经发布的虚构故事",
            "published_at": service.list_posts("published")[0]["published_at"],
        }
    ]


async def test_receipt_persistence_failure_is_uncertain_even_for_value_error(service, monkeypatch):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    original = service._status

    def fail_success(post_id, status, error=None, receipt=None):
        if status == "published":
            raise ValueError("local storage validation failed")
        return original(post_id, status, error, receipt)

    monkeypatch.setattr(service, "_status", fail_success)
    with pytest.raises(ValueError, match="回执失败"):
        await service.publish(decision["post_id"])
    assert service.list_posts()[0]["status"] == "publish_uncertain"
    assert not service.policy()["enabled"] and len(service.sent) == 1


async def test_operations_summary_exact_quote_only_prompt_full_original_saved(service):
    mid = public_memory(service, text="在虚构的海底厨房，我又盛了一碗饭。另一个无关细节不应进入决定。")
    service.runtime.llm.summary_output = json.dumps(
        {
            "entries": [
                {"source_id": f"memory:{mid}", "quote": "在虚构的海底厨房，我又盛了一碗饭。"},
            ]
        },
        ensure_ascii=False,
    )
    decision = await service.evaluate(manual=True)
    assert decision["status"] == "pending_review"
    decision_prompt = service.runtime.llm.calls[-1]
    public_sources = json.loads(decision_prompt[1]["content"])["public_sources"]
    assert public_sources == [
        {
            "id": f"memory:{mid}",
            "text": "在虚构的海底厨房，我又盛了一碗饭。",
            "type": "public_memory",
            "fictional": True,
        }
    ]
    assert "另一个无关细节" not in json.dumps(decision_prompt, ensure_ascii=False)
    assert "另一个无关细节" in decision["sources"][0]["text"]
    status = service.summary_status()
    assert status["valid"] and status["max_tokens"] == 2048 and status["entries"]
    assert status["token_count"] <= status["max_tokens"]


async def test_summary_reused_without_append_new_source_updates_and_budget_invalidates(service):
    public_memory(service)
    service.runtime.llm.output = json.dumps(
        {
            "action": "wait",
            "reason": "暂不发布",
            "content": "",
            "source_ids": [],
            "next_evaluation_seconds": 1800,
        }
    )
    await service.evaluate(manual=True)
    initial = service.summary_status()
    await service.evaluate(manual=True)

    def summary_counts():
        return [
            messages
            for messages in service.runtime.llm.count_calls
            if len(messages) == 1 and "entries" in json.loads(messages[0]["content"])
        ]

    assert len(summary_counts()) == 1
    assert service.summary_status()["updated_at"] == initial["updated_at"]
    public_memory(service, text="新的公开虚构故事")
    assert service.summary_status()["valid"] is False and service.summary_status()["entries"] == []
    await service.evaluate(manual=True)
    assert len(summary_counts()) == 2
    assert service.store.one("SELECT COUNT(*) AS n FROM dynamic_memory_summaries")["n"] == 1
    await service.set_policy({"memory_summary_max_tokens": 512})
    assert service.summary_status()["max_tokens"] == 512 and not service.summary_status()["valid"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "openai_compatible"),
        ("base_url", "https://api.example.test/v1"),
        ("model", "different-model"),
        ("tokenizer_path", "different-tokenizer"),
    ],
)
async def test_provider_or_tokenizer_configuration_invalidates_token_measurement(service, field, value):
    public_memory(service)
    await service.evaluate(manual=True)
    assert service.summary_status()["valid"]
    service.settings.data["llm"][field] = value
    status = service.summary_status()
    assert status["valid"] is False and status["entries"] == [] and status["token_count"] is None


async def test_public_revoke_invalid_preview_and_retention_delete_rebuild_safe(service):
    mid = public_memory(service)
    await service.evaluate(manual=True)
    service.mark_memory(mid, False)
    assert service.summary_status()["entries"] == [] and not service.summary_status()["valid"]
    service.store.execute("DELETE FROM dynamic_memory_summaries")
    assert service.summary_status()["updated_at"] is None


@pytest.mark.parametrize("failure", ["rewrite", "overbudget", "incomplete", "unquoted_id"])
async def test_summary_failure_never_falls_back_to_original_source_decision(service, monkeypatch, failure):
    mid = public_memory(service)
    await service.set_policy({"enabled": True})
    if failure == "rewrite":
        service.runtime.llm.summary_output = json.dumps(
            {
                "entries": [
                    {"source_id": f"memory:{mid}", "quote": "改写而非原句"},
                ]
            },
            ensure_ascii=False,
        )
    elif failure == "incomplete":
        service.runtime.llm.summary_output = '{"entries":['
    elif failure == "overbudget":

        async def count(messages):
            assert service.runtime.generation_lock.locked()
            return 2049

        monkeypatch.setattr(service.runtime.llm, "count_tokens", count)
    else:
        second = public_memory(service, text="第二条未选入摘要的公开来源")
        service.runtime.llm.summary_output = json.dumps(
            {
                "entries": [
                    {"source_id": f"memory:{mid}", "quote": "我又盛了一碗饭。"},
                ]
            },
            ensure_ascii=False,
        )
        service.runtime.llm.output = json.dumps(
            {
                "action": "post",
                "reason": "wrong source",
                "content": "虚构故事",
                "source_ids": [f"memory:{second}"],
                "next_evaluation_seconds": 1800,
            }
        )
    result = await service.evaluate()
    assert result["action"] == "error" and not service.list_posts() and not service.sent
    if failure != "unquoted_id":
        assert len(service.runtime.llm.calls) == 1
        assert not service.summary_status()["valid"]


async def test_summary_and_its_token_measurement_hold_same_gpu_generation_lock(service, monkeypatch):
    public_memory(service)
    counted = service.runtime.llm.count_tokens

    async def assert_count(messages):
        assert service.runtime.generation_lock.locked()
        return await counted(messages)

    async def assert_summary():
        assert service.runtime.generation_lock.locked()

    service.runtime.llm.summary_hook = assert_summary
    service.runtime.llm.hook = assert_summary
    monkeypatch.setattr(service.runtime.llm, "count_tokens", assert_count)
    await service.evaluate(manual=True)
    assert len(service.runtime.llm.calls) == 2 and len(service.runtime.llm.count_calls) == 2


async def test_revoke_during_summary_generation_prevents_commit_and_decision(service):
    mid = public_memory(service)

    async def revoke():
        service.mark_memory(mid, False)

    service.runtime.llm.summary_hook = revoke
    result = await service.evaluate(manual=True)
    assert result["action"] == "error" and len(service.runtime.llm.calls) == 1
    assert not service.summary_status()["valid"] and not service.list_posts()


@pytest.mark.parametrize("value", [255, 4097, True, "2048"])
async def test_summary_budget_policy_bounds(service, value):
    with pytest.raises(ValueError):
        await service.set_policy({"memory_summary_max_tokens": value})


async def test_posting_does_not_clear_summary_preview_but_does_not_reuse_source(service):
    public_memory(service)
    await service.set_policy({"enabled": True})
    assert (await service.evaluate())["status"] == "published"
    assert service.summary_status()["valid"] and service.summary_status()["entries"]
    due(service)
    assert (await service.evaluate())["action"] == "wait"
    assert len(service.sent) == 1


async def test_decision_grammar_references_only_actual_summary_sources(service):
    chosen = public_memory(service)
    omitted = public_memory(service, text="不进入摘要的另一条公开故事")
    service.runtime.llm.summary_output = json.dumps(
        {
            "entries": [
                {"source_id": f"memory:{chosen}", "quote": "我又盛了一碗饭。"},
            ]
        },
        ensure_ascii=False,
    )
    result = await service.evaluate(manual=True)
    assert result["action"] == "post"
    schema = service.runtime.llm.schemas[-1]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) == {"action", "reason", "content", "source_ids", "next_evaluation_seconds"}
    props = schema["properties"]
    assert props["action"]["enum"] == ["post", "wait"]
    assert props["source_ids"]["items"]["enum"] == [f"memory:{chosen}"]
    assert f"memory:{omitted}" not in props["source_ids"]["items"]["enum"]
    assert props["next_evaluation_seconds"] == {"type": "integer", "minimum": 300, "maximum": 86400}


@pytest.mark.parametrize(
    "invalid",
    [
        {"public_sources": []},
        {"action": "wait", "public_sources": []},
        {
            "action": "wait",
            "reason": "r",
            "content": "",
            "source_ids": [],
            "next_evaluation_seconds": 1800,
            "extra": "model metadata",
        },
        "```json\n{}\n```",
        [],
    ],
)
async def test_decision_input_echo_extra_fields_or_non_object_never_publishes(service, monkeypatch, invalid):
    public_memory(service)
    await service.set_policy({"enabled": True})
    original = service.runtime.llm.generate_json

    async def incorrect(messages, schema, *, max_tokens):
        if json.loads(messages[1]["content"]).get("task") == "operations_memory_summary":
            return await original(messages, schema, max_tokens=max_tokens)
        return invalid

    monkeypatch.setattr(service.runtime.llm, "generate_json", incorrect)
    result = await service.evaluate()
    assert result["action"] == "error" and service.list_posts() == [] and service.sent == []


async def test_rejected_source_is_not_regenerated_by_autonomous_publisher(service):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "reject")
    count = len(service.runtime.llm.calls)
    await service.set_policy({"enabled": True})
    due(service)
    assert (await service.evaluate())["action"] == "wait"
    assert len(service.runtime.llm.calls) == count and service.sent == []


async def test_decision_with_internal_source_id_in_body_is_not_a_publishable_draft(service):
    identifier = public_memory(service)
    service.runtime.llm.output = json.dumps(
        {
            "action": "post",
            "reason": "想分享故事",
            "content": f"海底小剧场(memory:{identifier})",
            "source_ids": [f"memory:{identifier}"],
            "next_evaluation_seconds": 1800,
        }
    )
    decision = await service.evaluate(manual=True)
    assert decision["action"] == "error" and service.list_posts() == [] and service.sent == []
