import json
import time

import httpx
import pytest
from test_browsing import BV, finished, model_packet
from test_browsing import browsing as browsing
from test_browsing_batches import authorize

from tuber_studio.api_providers import APIProvider
from tuber_studio.operation_feedback import FeedbackModel
from tuber_studio.operations.browsing.decisions import BrowsingDecisions
from tuber_studio.operations.browsing.models import ResponseDecision
from tuber_studio.publication_errors import SendLimitReached


def test_normal_short_acknowledgements_and_shared_openings_are_not_treated_as_mechanical_repetition():
    check = BrowsingDecisions.repetition_reason
    assert check("鲸鲸记住啦！", [{"text": "鲸鲸记住啦！"}], {}) is None
    opening = "看完这个视频我最想说的是"
    assert (
        check(
            opening + "这段剪辑节奏很舒服，每次切镜都刚刚好。",
            [{"text": opening + "盐水密度的对照很直观，加盐后鸡蛋才浮起来。"}],
            {},
        )
        is None
    )
    repeated = "白饭也看这一版，刚才看到那段画面卡住了。你发我一条我直接补全。"
    assert check(repeated, [{"text": repeated}], {})
    assert check(
        "我猜是DNS，你换个网络试，我帮你查DNS？", [{"text": "我猜是DNS。你换个网络试，我帮你查DNS。"}], {}
    )


async def test_archived_publication_text_stays_in_audit_without_reentering_current_response_context(browsing, monkeypatch):
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    now = time.time()
    old = "以前留下的旧台词不应该再反复塞进当前评论上下文。"
    fresh = "今天这段盐水实验看懂了，慢慢加盐就能看出变化。"
    for identity, text, at in (("archived", old, now - 60), ("fresh", fresh, now)):
        browsing.store.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,status,text,target,created_at,updated_at) "
            "VALUES(?,'100','whale',?,?,'reply_public',?,'published',?,'{}',?,?)",
            (identity, row["character_version"], identity, row["id"], text, at, at),
        )
    browsing.store.set("browsing_history_visible_since", now - 1)

    async def capture(_row, schema, data, instruction, metrics, **kwargs):
        assert "recent_public_responses" not in data
        assert kwargs["diagnose"](ResponseDecision(action="reply_public", text=old, reason="specific_detail", evidence_ids=["public:601"])) is None
        assert kwargs["diagnose"](ResponseDecision(action="reply_public", text=fresh, reason="specific_detail", evidence_ids=["public:601"])) == "正文与近期公开发言／热评重复"
        return ResponseDecision(action="skip", reason="no_interest")

    monkeypatch.setattr(browsing.decisions, "_model", capture)
    await browsing.decisions._response(row, None, {"source": {"comment_id": "601", "text": "这是什么实验？"}}, {"public:601"}, "reply_public", {})
    assert browsing.store.one("SELECT text FROM browsing_intents WHERE id='archived'")["text"] == old


async def test_repeated_main_reply_stays_reviewable_without_parking_the_batch(browsing, monkeypatch):
    await finished(browsing, preview=True)
    published = browsing.intents()[0]
    browsing.review(published["id"], "approve")
    await browsing.publish(published["id"])
    next_video = "BV1xx411c7mD"
    await browsing.configure({"sources": ["specified"], "watch_videos": [next_video]})

    async def decide(self, row):
        raise AssertionError("A prose diagnostic must not start a model repair decision")

    monkeypatch.setattr(FeedbackModel, "decide", decide)
    await authorize(browsing)
    completed = await finished(browsing, automatic=True, video_only=True)
    assert completed["status"] == "completed", completed
    assert completed["metrics"]["failures"] == 0
    assert len(browsing.adapter.sent) == 1
    intent = next(item for item in browsing.intents() if item["target"].get("bvid") == next_video)
    assert intent["status"] == "pending_review" and not intent["auto_authorized"]
    generation = browsing.runtime.continuations.get(intent["target"]["generation_continuation_id"])
    assert generation["state"] == "awaiting_review" and generation["recoveries"] == 0
    assert generation["feedback"]["kind"] == "style_diagnostic"
    assert not any(step["phase"] == "decision" for step in generation["revisions"])
    saved = browsing.repo.content_progress(completed, next_video)["main_decision"]
    assert saved["action"] == "reply_public" and saved["text"] == published["text"]
    assert saved["video_preference"] and saved["preference_reason"]
    assert browsing.repo.batches.get(completed)["state"] == "completed"

    async def comments(video, max_pages=1):
        return {
            "items": [
                {
                    "comment_id": "601",
                    "thread_id": "601",
                    "platform_user_id": "300",
                    "display_name": "邻居",
                    "content": "鸡蛋浮起来需要多少盐？",
                }
            ]
        }

    async def source(target):
        return {"author_uid": "300", "text": "鸡蛋浮起来需要多少盐？"}

    original = browsing.runtime.llm.generate_json

    async def different_response(messages, schema, *, max_tokens):
        result = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision":
            result["text"] = "盐水的密度够大时鸡蛋就会浮上来，先慢慢加盐试试看。"
        return result

    monkeypatch.setattr(browsing.adapter, "fetch", comments)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", source, raising=False)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", different_response)
    await browsing.configure({"reply_enabled": True})
    revisit = await finished(browsing, automatic=True, video_only=True)
    assert revisit["status"] == "completed"
    assert revisit["id"] != completed["id"]
    reply = await finished(browsing, automatic=True, interaction_only=True)
    assert reply["status"] == "completed"
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["0", "601"]
    main_intents = [item for item in browsing.intents()
                    if item["target"].get("bvid") == next_video and item["kind"] == "top_level"]
    assert len(main_intents) == 1 and main_intents[0]["id"] == intent["id"]
    assert main_intents[0]["status"] == "pending_review"


async def isolated_output_limit_provider(browsing, *, comments=False, network=False, private=None, malformed_body=None):
    """Exercise the real HTTP provider, feedback coordinator and task executor."""
    original = browsing.runtime.llm
    calls = []
    fault = {"pending": True}

    async def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "isolated-browsing"}]})
        body = json.loads(request.content)
        if body["max_tokens"] == 1:
            text, finish = "", "stop"
        else:
            schema = body["response_format"]["schema"]
            if "read" in schema.get("properties", {}):
                calls.append("feedback")
                text = json.dumps({"action": "wait", "reason": "unexpected repair call", "read": ""})
                finish = "stop"
            else:
                # APIProvider keeps the domain schema inside the web-fetch union.
                title = schema.get("title") or schema.get("oneOf", [{}])[0].get("title")
                calls.append(title)
                if title == "CandidateBatchPlan":
                    source = next(m["content"] for m in body["messages"] if m["role"] == "user")
                    candidates = model_packet(source)["candidates"]
                    value = {"workers": 1, "choices": {
                        item["bvid"]: {"action": "comments" if comments else "watch",
                         "explanation": "想接着聊盐水里鸡蛋浮起来的实验", "priority": 0}
                        for item in candidates
                    }}
                    finish = "stop"
                else:
                    assert title == "ResponseDecision"
                    if fault["pending"] and network:
                        raise httpx.ReadTimeout("isolated network interruption", request=request)
                    packet = model_packet(next(m["content"] for m in body["messages"] if m["role"] == "user"))
                    reference = packet["evidence"][0]["id"]
                    value = {
                        "action": "reply_public",
                        "text": "这句还没说完的草稿" if fault["pending"] else "慢慢加盐就能看出鸡蛋浮起来，盐水密度的变化很直观。",
                        "reason": "specific_detail", "explanation": "接着回答鸡蛋浮起来需要多少盐",
                        "evidence_ids": [reference],
                    }
                    if packet.get("video_actions"):
                        value.update(video_preference="neutral", preference_reason="想先看完实验再决定")
                    if private is not None:
                        value.update(action="handoff_private", text="仅供私信查看的实际生成草稿", reason="private_details")
                    finish = "length" if fault["pending"] else "stop"
                    if malformed_body is not None:
                        value["text"], finish = malformed_body, "stop"
                    fault["pending"] = False
                text = json.dumps(value, ensure_ascii=False)
                if title == "ResponseDecision" and private == "unresolved":
                    text = text[:-1]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 1 if body["max_tokens"] == 1 else 20},
        })

    provider = APIProvider(browsing.settings.data["llm"] | {
        "base_url": "https://isolated.invalid/v1", "model": "isolated-browsing",
        "context_tokens": 131072, "native_context_tokens": 131072,
    }, adapter="llama_cpp")
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="https://isolated.invalid/v1", transport=httpx.MockTransport(respond))
    browsing.runtime.llm = provider
    return original, provider, calls


async def test_valid_outer_json_with_protocol_in_comment_body_requires_an_edit(browsing):
    text = '看完啦？”}、explanation:“接着聊一下”，reply_target:{'
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV]})
    await authorize(browsing)
    original, provider, calls = await isolated_output_limit_provider(browsing, malformed_body=text)
    try:
        row = await finished(browsing, automatic=True, video_only=True)
        assert row["status"] == "completed"
        intent = browsing.intents()[0]
        assert intent["status"] == "pending_review" and not intent["auto_authorized"]
        assert intent["text"] == text and "结构" in intent["error"]
        audit = browsing.runtime.continuations.get(intent["target"]["generation_continuation_id"])
        model = next(step for step in audit["revisions"] if step["phase"] == "generation_model")
        assert model["context"]["finish_reason"] == "stop" and json.loads(model["output"])["text"] == text
        assert audit["feedback"]["kind"] == "body_integrity" and audit["recoveries"] == 0
        assert calls == ["CandidateBatchPlan", "ResponseDecision"] and not browsing.adapter.sent
        with pytest.raises(ValueError, match="结构"):
            browsing.review(intent["id"], "approve")

        # Older pending candidates could already have automatic authorization.
        browsing.store.execute("UPDATE browsing_intents SET auto_authorized=1 WHERE id=?", (intent["id"],))
        with pytest.raises(ValueError, match="结构"):
            await browsing.publish(intent["id"], automatic=True)
        assert not browsing.store.all("SELECT * FROM operation_write_attempts")
        corrected = "盐水里的鸡蛋浮起来啦！（本鱼看懂了）"
        browsing.review(intent["id"], "approve", corrected)
        sent = await browsing.publish(intent["id"])
        assert sent["status"] == "published" and sent["receipt"]["rpid"] == "900"
        assert [body for _, body in browsing.adapter.sent] == [corrected]
    finally:
        await provider.close()
        browsing.runtime.llm = original


@pytest.mark.parametrize("comments", [False, True])
async def test_output_limit_keeps_diagnostic_without_feedback_or_cross_round_model_retry(
    browsing, monkeypatch, comments
):
    from test_browsing_comment_only import attach_comment_source

    await browsing.configure({"sources": ["specified"], "watch_videos": [BV]})
    await authorize(browsing)
    await browsing.configure({"reply_enabled": comments})
    if comments:
        attach_comment_source(browsing, monkeypatch)
    original, provider, calls = await isolated_output_limit_provider(browsing, comments=comments)
    try:
        main = await finished(browsing, automatic=True, video_only=True)
        assert main["status"] == ("completed" if comments else "partial")
        assert browsing.repo.batches.get(main)["state"] == "completed"
        if comments:
            diagnostic = browsing.tasks(round_id=main["id"])[0]["repetition_decisions"][0]
            assert diagnostic["state"] == "invalid" and diagnostic["comment_id"] == "601"
            assert not browsing.store.all("SELECT * FROM browsing_comment_progress")
            assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
        else:
            saved = browsing.repo.content_progress(main, BV)
            assert saved["material"]["metadata"]["bvid"] == BV and saved["main_decision"] is None
            task = browsing.tasks(round_id=main["id"])[0]
            assert task["status"] == "invalid" and task["error"]
            assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1
        assert calls == ["CandidateBatchPlan", "ResponseDecision"]
        assert not browsing.intents() and not browsing.adapter.sent
        generation = browsing.store.one("SELECT id FROM operation_continuations "
            "WHERE json_extract(binding,'$.schema')='ResponseDecision'")
        audit = browsing.runtime.continuations.get(generation["id"])
        assert audit["state"] == "invalid" and audit["feedback"]["kind"] == "output_limit"
        assert audit["current_output"] is None and audit["original_output"]
        await finished(browsing, automatic=True, video_only=True)
        if comments:
            await finished(browsing, automatic=True, interaction_only=True)
        assert calls.count("ResponseDecision") == 1 and "feedback" not in calls
        assert not browsing.adapter.sent
    finally:
        await provider.close()
        browsing.runtime.llm = original


async def test_real_http_timeout_keeps_checkpoint_without_inventing_a_model_wait(browsing):
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV]})
    await authorize(browsing)
    await browsing.configure({"reply_enabled": False})
    original, provider, calls = await isolated_output_limit_provider(browsing, network=True)
    try:
        row = await finished(browsing, automatic=True, video_only=True)
        assert row["status"] == "paused"
        task = browsing.tasks(round_id=row["id"])[0]
        assert task["status"] == "checkpoint" and task["error"]
        assert "feedback" not in calls
        assert not browsing.intents() and not browsing.adapter.sent
        assert not browsing.repo.content_progress(row, BV)["main_decision"]
    finally:
        await provider.close()
        browsing.runtime.llm = original


@pytest.mark.parametrize("private", ["parsed", "unresolved"])
async def test_actual_deferred_private_candidate_is_only_visible_in_private_continuation(browsing, monkeypatch, private):
    from test_browsing_comment_only import attach_comment_source

    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV]})
    await authorize(browsing)
    attach_comment_source(browsing, monkeypatch)
    original, provider, _ = await isolated_output_limit_provider(browsing, comments=True, private=private)
    try:
        row = await finished(browsing, automatic=True, video_only=True)
        assert row["status"] == "completed"
        # Query the actual public task DTO, including its duplicate nested payload.
        tasks = browsing.tasks(round_id=row["id"])
        public = json.dumps(tasks, ensure_ascii=False)
        assert "仅供私信查看的实际生成草稿" not in public
        resolution = tasks[0]["repetition_decisions"][0]
        assert resolution["continuation"]["output_visibility"] == ("private" if private == "parsed" else "unresolved")
        assert resolution["continuation"]["private_href"] == "#messages"
        audit = browsing.runtime.continuations.get(resolution["continuation_id"])
        assert "仅供私信查看的实际生成草稿" in json.dumps(audit, ensure_ascii=False)
        assert not browsing.store.all("SELECT * FROM dm_drafts")
        assert not browsing.adapter.sent
    finally:
        await provider.close()
        browsing.runtime.llm = original


@pytest.mark.parametrize("on_resume", [False, True])
async def test_real_send_limit_is_persisted_as_scheduling_instead_of_a_failed_retry(
    browsing, monkeypatch, on_resume
):
    next_at = time.time() + 3600

    def full(*args, **kwargs):
        raise SendLimitReached("达到渠道发送上限", next_at)

    if on_resume:

        async def resume(*args, **kwargs):
            full()

        monkeypatch.setattr(browsing, "_resume_batch_publications", resume)
    else:
        monkeypatch.setattr(browsing.operations, "reserve", full)
    await authorize(browsing)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed" and row["metrics"]["failures"] == 0
    if not on_resume:
        from test_browsing import wait_for_sends

        await wait_for_sends(browsing)
        queued = browsing.store.one("SELECT wake_at FROM operation_send_queue")
        assert queued["wake_at"] == next_at
        assert not browsing.adapter.sent and not browsing.repo.video_retry(row, BV)
        return
    task = browsing.tasks(round_id=row["id"])[0]
    assert task["status"] == "deferred_daily" and task["payload"]["retry_kind"] == "send_limit"
    assert task["payload"]["next_at"] == next_at
    assert not browsing.adapter.sent and not browsing.repo.video_retry(row, BV)
    persisted = browsing.store.one(
        "SELECT payload,status FROM browsing_batch_items WHERE batch_id=?", (row["config"]["batch_id"],)
    )
    assert persisted["status"] == "daily_wait" and json.loads(persisted["payload"])["next_at"] == next_at
