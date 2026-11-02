"""Actual adapter HTTP responses drive one bound publication/revision chain."""

import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from tuber_studio.api_providers import APIProvider
from tuber_studio.bilibili_browsing import BrowsingAdapter
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.comments import Comments
from tuber_studio.generation_meter import GenerationMeter

BV = "BV1d8pU6xEZX"


async def prepared_service(
    settings,
    store,
    runtime,
    *,
    action="revise",
    always_reject=False,
    max_tokens=8192,
    unknown=False,
    delivered=False,
):
    credentials = {"sessdata": "isolated-session", "csrf": "isolated-csrf"}
    posted = []
    model_requests = []

    def platform(request):
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 100}})
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, json={"code": 0, "data": {"bvid": BV, "aid": 12, "title": "真实隔离视频"}}
            )
        if request.url.path == "/x/v2/reply/reply":
            service.reconciliation_requests.append(request)
            rows = (
                [
                    {
                        "rpid": 321,
                        "root": 120,
                        "parent": 123,
                        "member": {"mid": 100},
                        "content": {"message": posted[0]["message"][0]},
                        "ctime": int(time.time()),
                    }
                ]
                if delivered
                else []
            )
            return httpx.Response(200, json={"code": 0, "data": {"replies": rows}})
        assert request.url.path == "/x/v2/reply/add" and request.method == "POST"
        posted.append(parse_qs(request.content.decode()))
        if unknown:
            raise httpx.ReadTimeout("after final POST", request=request)
        if len(posted) == 1 or always_reject:
            return httpx.Response(200, json={"code": 12025, "message": "message too long"})
        return httpx.Response(200, json={"code": 0, "data": {"rpid": 321}})

    adapter = BrowsingAdapter(credentials)
    original = adapter.client
    adapter.client = httpx.AsyncClient(
        base_url=original.base_url,
        headers=original.headers,
        cookies=original.cookies,
        transport=httpx.MockTransport(platform),
    )
    await original.aclose()
    service = Comments(settings, store, runtime, adapter)
    service.reconciliation_requests = []
    service.credentials = credentials
    service.import_items(
        BV,
        [{"comment_id": "123", "thread_id": "120", "platform_user_id": "200", "content": "今天煮饭怎么做？"}],
    )
    source = store.one("SELECT id FROM comments")
    character = runtime.character
    identity = store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,input_summary,created_at,updated_at) VALUES(?,?,'approved',?,?,?, ?,?)",
        (
            source["id"],
            "原来这段米饭故事这么长。" * 100,
            character["id"],
            character["version"],
            json.dumps({"event": {"text": "今天煮饭怎么做？"}}, ensure_ascii=False),
            time.time(),
            time.time(),
        ),
    ).lastrowid
    settings.data["llm"]["operations_max_tokens"] = max_tokens
    config = settings.data["llm"] | {
        "base_url": "https://isolated.invalid/v1",
        "model": "test-model",
        "context_tokens": 131072,
        "native_context_tokens": 131072,
        "enable_thinking": True,
    }
    provider = APIProvider(config, adapter="llama_cpp")
    previous = provider.client

    def model(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        model_requests.append(payload)
        if payload["max_tokens"] == 1:
            text = ""
        else:
            schema = payload["response_format"]["schema"]
            text = json.dumps(
                {"action": action, "reason": "结合平台反馈继续这次交流", "read": ""}
                if "action" in schema["properties"]
                else {"text": "这锅饭我先替你闻香，火候放慢一点。"},
                ensure_ascii=False,
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 1 if payload["max_tokens"] == 1 else 20},
            },
        )

    provider.client = httpx.AsyncClient(
        base_url="https://isolated.invalid/v1", transport=httpx.MockTransport(model)
    )
    await previous.aclose()
    await runtime.llm.close()
    runtime.llm = provider
    return service, identity, posted, model_requests


async def test_definite_length_rejection_revises_then_manual_approval_reuses_same_intent(
    settings, store, runtime
):
    service, identity, posted, requests = await prepared_service(settings, store, runtime)
    try:
        result = await service.publish(identity)
        assert result["status"] == "pending_review" and len(posted) == 1
        saved = store.one("SELECT * FROM drafts WHERE id=?", (identity,))
        assert saved["status"] == "pending_review" and saved["text"] == result["text"]
        continuation = runtime.continuations.find("comments", identity)
        assert continuation["trigger_feedback"]["code"] == 12025
        assert continuation["trigger_feedback"]["message"] == "message too long"
        assert continuation["original_output"]["text"] == posted[0]["message"][0]
        for payload in requests:
            assert payload["chat_template_kwargs"]["enable_thinking"] is True
            assert "12025" in json.dumps(payload["messages"], ensure_ascii=False)
        service.review(identity, "approve")
        assert (await service.publish(identity))["status"] == "published"
        assert len(posted) == 2 and posted[1]["message"] == [saved["text"]]
        assert all(item["parent"] == ["123"] and item["root"] == ["120"] for item in posted)
        root = store.one("SELECT * FROM account_writes")
        child = store.one("SELECT * FROM operation_write_retries")
        assert root["status"] == "rejected" and child["status"] == "published"
        assert root["intent"] == child["intent"] and root["token"] == child["previous_token"]
        assert runtime.continuations.find("comments", identity)["state"] == "completed"
    finally:
        await service.close()


async def test_high_explicit_budget_does_not_strand_body_after_decision_refund(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(settings, store, runtime, max_tokens=65536)
    try:
        assert (await service.publish(identity))["status"] == "pending_review"
        assert [item["max_tokens"] for item in requests] == [1, 65536, 1, 65536]
        row = runtime.continuations.find("comments", identity)
        assert row["budget"] == {"model_calls": 4, "input_tokens": 800, "output_tokens": 42}
        assert row["limits"]["max_output_tokens"] == 2097152 and len(posted) == 1
    finally:
        await service.close()


@pytest.mark.parametrize("action,state", [("wait", "waiting"), ("abandon", "abandoned")])
async def test_platform_feedback_can_defer_without_another_write(settings, store, runtime, action, state):
    service, identity, posted, requests = await prepared_service(settings, store, runtime, action=action)
    try:
        result = await service.publish(identity)
        assert result["continuation"]["state"] == state
        assert len(posted) == 1 and len(requests) == 2
        assert not store.one("SELECT * FROM operation_write_retries")
    finally:
        await service.close()


@pytest.mark.parametrize("mode", ["disabled", "wait"])
async def test_human_can_edit_approve_and_submit_after_definite_rejection_without_more_model_calls(
    settings, store, runtime, mode
):
    service, identity, posted, requests = await prepared_service(settings, store, runtime, action="wait")
    if mode == "disabled":
        service.operations.configure({"max_recoveries": 0})
    try:
        result = await service.publish(identity)
        assert result["continuation"]["state"] == ("checkpoint" if mode == "disabled" else "waiting")
        calls = len(requests)
        corrected = "我自己改好的饭锅回复，火候慢一点。"
        service.review(identity, "approve", text=corrected)
        assert (await service.publish(identity))["status"] == "published"
        assert len(posted) == 2 and posted[1]["message"] == [corrected] and len(requests) == calls
        root = store.one("SELECT * FROM account_writes")
        child = store.one("SELECT * FROM operation_write_retries")
        assert root["status"] == "rejected" and child["status"] == "published"
        assert (
            root["intent"] == child["intent"]
            and root["token"] == child["previous_token"]
            and child["automatic"] == 0
        )
    finally:
        await service.close()


@pytest.mark.parametrize(
    "failure", ["unchanged", "unapproved", "active_lease", "source_changed", "automatic"]
)
async def test_human_revision_still_requires_changed_approved_bound_idle_manual_submission(
    settings, store, runtime, failure
):
    service, identity, posted, requests = await prepared_service(settings, store, runtime)
    service.operations.configure({"max_recoveries": 0})
    try:
        result = await service.publish(identity)
        continuation_id = result["continuation"]["id"]
        original = store.one("SELECT text FROM drafts WHERE id=?", (identity,))["text"]
        text = original if failure == "unchanged" else "人工重新写好的新正文。"
        service.review(identity, "reject" if failure == "unapproved" else "approve", text=text)
        if failure == "active_lease":
            store.execute(
                "UPDATE operation_continuations SET owner='active-agent',lease_until=? WHERE id=?",
                (time.time() + 60, continuation_id),
            )
        if failure == "source_changed":
            store.execute("UPDATE comments SET content='原帖子已变更'")
        if failure in {"unapproved", "automatic"}:
            assert (await service.publish(identity, automatic=failure == "automatic"))[
                "status"
            ] == "pending_review"
        else:
            with pytest.raises(ValueError):
                await service.publish(identity)
        row = runtime.continuations.get(continuation_id)
        assert row["state"] == "checkpoint" and not row["manual_revision"]
        assert row["owner"] == ("active-agent" if failure == "active_lease" else None)
        assert len(posted) == 1 and not requests
        assert not store.all("SELECT * FROM operation_revision_tickets") and not store.all(
            "SELECT * FROM operation_write_retries"
        )
    finally:
        await service.close()


async def test_expired_recovery_owner_is_fenced_by_manual_revision(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(settings, store, runtime)
    service.operations.configure({"max_recoveries": 0})
    try:
        result = await service.publish(identity)
        continuation_id = result["continuation"]["id"]
        store.execute(
            "UPDATE operation_continuations SET owner='expired-agent',lease_until=? WHERE id=?",
            (time.time() - 1, continuation_id),
        )
        service.review(identity, "approve", text="用户重新审核过的新正文。")
        assert (await service.publish(identity))["status"] == "published"
        runtime.continuations._state(continuation_id, "checkpoint", owner="expired-agent")
        row = runtime.continuations.get(continuation_id)
        assert row["state"] == "completed" and row["owner"] is None and row["manual_revision"]
        assert len(posted) == 2 and not requests
    finally:
        await service.close()


async def test_unknown_cannot_take_manual_revision_path_even_if_draft_is_changed(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(settings, store, runtime, unknown=True)
    try:
        with pytest.raises(ValueError):
            await service.publish(identity)
        # The ordinary review API rejects this edit. Simulate stale state to
        # verify the common facade still checks the original unknown attempt.
        store.execute("UPDATE drafts SET text='新的候选',status='approved' WHERE id=?", (identity,))
        with pytest.raises(ValueError):
            await service.publish(identity)
        row = runtime.continuations.find("comments", identity)
        assert row["state"] == "needs_reconciliation" and not row["manual_revision"]
        assert len(posted) == 1 and not requests and len(service.reconciliation_requests) == 2
        assert not store.all("SELECT * FROM operation_revision_tickets")
    finally:
        await service.close()


async def test_unknown_is_never_revision_or_second_post(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(settings, store, runtime, unknown=True)
    try:
        for _ in range(2):
            with pytest.raises(ValueError):
                await service.publish(identity)
        assert len(posted) == 1 and not requests
        assert len(service.reconciliation_requests) == 2
        assert store.one("SELECT status FROM account_writes")["status"] == "uncertain"
        assert runtime.continuations.find("comments", identity)["state"] == "needs_reconciliation"
    finally:
        await service.close()


async def test_unknown_read_back_positive_receipt_resolves_without_resend(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(
        settings, store, runtime, unknown=True, delivered=True
    )
    try:
        result = await service.publish(identity)
        assert result["status"] == "published" and len(posted) == 1 and not requests
        root = store.one("SELECT token,status FROM account_writes")
        resolved = runtime.operations.get_attempt(root["token"])
        assert root["status"] == "uncertain" and resolved["resolved_status"] == "published"
        assert resolved["resolution_receipt"]["rpid"] == "321"
        assert store.one("SELECT status FROM drafts")["status"] == "published"
        assert not store.one("SELECT * FROM operation_channel_blocks")
    finally:
        await service.close()


async def test_existing_parent_meter_is_reused_without_double_reservation(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(settings, store, runtime)
    config = BrowsingPolicy(max_model_calls=8, max_input_tokens=1048576, max_output_tokens=65536).model_dump()
    parent, _ = runtime.browsing_repository.create_round("100", runtime.character, config, 0, True)
    try:
        with GenerationMeter(runtime.browsing_repository, parent["id"]):
            assert (await service.publish(identity))["status"] == "pending_review"
        row = runtime.continuations.find("comments", identity)
        assert row["parent_round_id"] == parent["id"] and row["deadline"] == parent["deadline"]
        assert row["budget"]["model_calls"] == 4 and row["budget"]["output_tokens"] == 42
        own = store.one("SELECT budget FROM operation_continuation_budgets WHERE id=?", (row["id"],))
        assert json.loads(own["budget"])["model_calls"] == 0
        assert len(requests) == 4 and len(posted) == 1
    finally:
        await service.close()


async def test_tight_parent_rejects_generation_before_an_extra_request(settings, store, runtime):
    service, identity, posted, requests = await prepared_service(settings, store, runtime)
    parent, _ = runtime.browsing_repository.create_round(
        "100", runtime.character, BrowsingPolicy(max_model_calls=1).model_dump(), 0, True
    )
    try:
        with GenerationMeter(runtime.browsing_repository, parent["id"]):
            result = await service.publish(identity)
        assert result["continuation"]["state"] == "checkpoint"
        assert result["continuation"]["budget"]["model_calls"] == 1
        assert len(requests) == 1 and requests[0]["max_tokens"] == 1 and len(posted) == 1
    finally:
        await service.close()


async def test_positive_resolution_survives_local_bookkeeping_failure_without_resend(
    settings, store, runtime, monkeypatch
):
    service, identity, posted, requests = await prepared_service(
        settings, store, runtime, unknown=True, delivered=True
    )
    execute = store.execute
    failed = False

    def fail_once(sql, args=()):
        nonlocal failed
        if "UPDATE drafts SET status='published'" in sql and not failed:
            failed = True
            raise OSError("isolated bookkeeping failure")
        return execute(sql, args)

    monkeypatch.setattr(store, "execute", fail_once)
    try:
        with pytest.raises(ValueError, match="本地记录待同步"):
            await service.publish(identity)
        token = store.one("SELECT token FROM account_writes")["token"]
        assert service.operations.get_attempt(token)["resolved_status"] == "published"
        assert runtime.continuations.find("comments", identity)["state"] == "needs_reconciliation"
        assert (await service.publish(identity))["status"] == "published"
        assert len(posted) == 1 and len(service.reconciliation_requests) == 2 and not requests
        assert not store.all("SELECT * FROM operation_write_retries")
    finally:
        await service.close()


async def test_source_changed_during_rejected_post_keeps_original_feedback_without_model_call(
    settings, store, runtime, monkeypatch
):
    service, identity, posted, requests = await prepared_service(settings, store, runtime)
    prepare = service._publication_context

    async def changed(*args, **kwargs):
        store.execute("UPDATE comments SET content='原帖在请求期间已变化'")
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(service, "_publication_context", changed)
    try:
        with pytest.raises(ValueError, match="不一致"):
            await service.publish(identity)
        row = runtime.continuations.find("comments", identity)
        root = store.one("SELECT token FROM account_writes")
        assert (
            row["state"] == "checkpoint"
            and row["binding"] == service.operations.get_attempt(root["token"])["binding"]
        )
        assert row["feedback"]["code"] == 12025 and row["original_output"]["text"] == posted[0]["message"][0]
        assert not requests and not store.all("SELECT * FROM operation_write_retries")
    finally:
        await service.close()


@pytest.mark.parametrize(
    "output,visibility",
    [
        ({"action": "handoff_private", "text": "只给这位鱼宝看的正文"}, "private"),
        ("不完整的回应结构和正文", "unresolved"),
    ],
)
def test_uncertain_response_raw_is_private_scoped_and_never_creates_recipient(
    runtime, store, output, visibility
):
    from tuber_studio.operation_feedback import generation_view, private_generation_views

    char = runtime.character
    row = runtime.continuations.begin(
        "generation.browsing",
        "isolated-task",
        {
            "task": "generation",
            "schema": "ResponseDecision",
            "account_id": "100",
            "character": {"id": char["id"], "version": char["version"]},
        },
        {"kind": "validation_error", "message": "正文出现在验证错误中"},
        original_output=output,
    )
    runtime.continuations.bind_source_view(row["id"], "browsing", "task-1", "浏览回应生成记录")
    public = generation_view(runtime, row["id"], "browsing", "task-1")
    assert public["output_visibility"] == visibility
    assert public["original_output"] is None and public["revisions"] == []
    assert "正文出现在验证错误中" not in json.dumps(public, ensure_ascii=False)
    private = private_generation_views(runtime, "100", char)
    assert private[0]["original_output"] == output
    assert private[0]["resume_target"] == {"channel": "browsing", "local_id": "generation:task-1"}
    assert private_generation_views(runtime, "200", char) == []
    for table in ("dm_drafts", "dm_inbox"):
        if store.one("SELECT name FROM sqlite_master WHERE name=?", (table,)):
            assert not store.all("SELECT * FROM " + table)


@pytest.mark.parametrize("channel", ["messages", "dynamics"])
async def test_other_channels_revise_real_business_rejection_under_same_intent(
    settings, store, runtime, channel
):
    from tuber_studio.bilibili_messages import BilibiliMessagesAdapter
    from tuber_studio.direct_messages import DirectMessages
    from tuber_studio.dynamics import Dynamics

    comments, comment_draft, _, requests = await prepared_service(settings, store, runtime)
    writes = []

    def platform(request):
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "isLogin": True,
                        "mid": 100,
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                        },
                    },
                },
            )
        expected = "/web_im/v1/web_im/send_msg" if channel == "messages" else "/x/dynamic/feed/create/dyn"
        assert request.url.path == expected and request.method == "POST"
        writes.append(
            parse_qs(request.content.decode()) if channel == "messages" else json.loads(request.content)
        )
        return httpx.Response(
            200,
            json={"code": 12025, "message": "message too long"}
            if len(writes) == 1
            else {"code": 0, "data": {"msg_key": 9001, "dyn_id_str": "9001"}},
        )

    def transport(adapter):
        previous = adapter.client
        adapter.client = httpx.AsyncClient(
            base_url=previous.base_url,
            headers=previous.headers,
            cookies=previous.cookies,
            transport=httpx.MockTransport(platform),
        )
        return previous

    if channel == "messages":
        adapter = BilibiliMessagesAdapter(comments.credentials)
        await transport(adapter).aclose()
        service = DirectMessages(settings, store, runtime, comments, adapter)
        await service.configure({"enabled": True})
        source, _ = service.ingest(
            {
                "message_id": "isolated-incoming",
                "platform_user_id": "200",
                "display_name": "鱼宝",
                "text": "想接着聊这锅饭。",
                "timestamp": time.time(),
                "account_id": "100",
            }
        )
        identity = service.enqueue("message", source, schedule=False)
        store.execute("UPDATE dm_drafts SET text='原来的正文',status='approved' WHERE id=?", (identity,))
        send = service.send
    else:
        await transport(comments.adapter).aclose()
        store.execute(
            "UPDATE drafts SET status='published',receipt=? WHERE id=?",
            (json.dumps({"rpid": "77"}), comment_draft),
        )
        service = Dynamics(settings, store, runtime, comments)
        sources = service._sources()
        decision = service._save_decision(
            runtime.character,
            True,
            {
                "action": "post",
                "reason": "接着聊公开故事",
                "content": "原来的动态正文",
                "source_ids": [sources[0]["id"]],
                "next_evaluation_seconds": 1800,
            },
            sources,
        )
        identity = decision["post_id"]
        service.review(identity, "approve")
        send = service.publish
    try:
        result = await send(identity)
        assert result["status"] == "pending_review" and len(writes) == 1 and len(requests) == 4
        service.review(identity, "approve")
        assert (await send(identity))["status"] == "published" and len(writes) == 2
        root = store.one("SELECT * FROM account_writes")
        child = store.one("SELECT * FROM operation_write_retries")
        assert root["status"] == "rejected" and child["status"] == "published"
        assert root["intent"] == child["intent"] and root["token"] == child["previous_token"]
        if channel == "messages":
            assert all(write["msg[receiver_id]"] == ["200"] for write in writes)
            assert len(store.all("SELECT * FROM dm_attempts")) == 2
    finally:
        await service.close()
        await comments.close()
