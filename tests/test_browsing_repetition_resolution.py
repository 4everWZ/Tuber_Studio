import json
import time

import httpx
import pytest
from test_browsing import BV, finished, model_packet
from test_browsing import browsing as browsing
from test_browsing_batches import authorize

from tuber_studio.api_providers import APIProvider
from tuber_studio.operation_feedback import FeedbackModel
from tuber_studio.operation_generation import GenerationDeferred
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


@pytest.mark.parametrize("action", ["wait", "abandon"])
async def test_repeated_main_reply_can_be_deferred_by_the_model_without_parking_the_batch(
    browsing, monkeypatch, action
):
    await finished(browsing, preview=True)
    published = browsing.intents()[0]
    browsing.review(published["id"], "approve")
    await browsing.publish(published["id"])
    next_video = "BV1xx411c7mD"
    await browsing.configure({"sources": ["specified"], "watch_videos": [next_video]})

    async def decide(self, row):
        assert row["feedback"]["kind"] == "style_diagnostic"
        return {"action": action, "reason": "这句话刚发过，先不重复发，看看别人的新评论"}

    monkeypatch.setattr(FeedbackModel, "decide", decide)
    await authorize(browsing)
    completed = await finished(browsing, automatic=True, video_only=True)
    assert completed["status"] == "completed", completed
    assert completed["metrics"]["failures"] == 0
    assert len(browsing.adapter.sent) == 1
    task = next(
        item
        for item in browsing.tasks(round_id=completed["id"])
        if item["bvid"] == next_video and "generation_resolution" in item.get("payload", {})
    )
    assert task["status"] == "viewed_without_comment" and not task["error"]
    resolution = task["payload"]["generation_resolution"]
    assert resolution["decision"]["action"] == action
    assert resolution["original_candidate"]["text"] == published["text"]
    assert resolution["feedback"]["kind"] == "style_diagnostic"
    assert browsing.repo.content_progress(completed, next_video)["main_decision"]["action"] == (
        "wait" if action == "wait" else "skip"
    )
    saved = browsing.repo.content_progress(completed, next_video)["main_decision"]
    assert saved["video_preference"] == resolution["original_candidate"]["video_preference"]
    assert saved["preference_reason"] == resolution["original_candidate"]["preference_reason"]
    assert saved["text"] == ""
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
    assert (
        len(
            [
                item
                for item in browsing.intents()
                if item["target"].get("bvid") == next_video and item["kind"] == "top_level"
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    ("state", "kind", "message"),
    [
        ("checkpoint", "style_diagnostic", "正文重复"),
        ("waiting", "model_error", "模型网络中断"),
        ("waiting", "timeout", "请求超时"),
        ("waiting", "capacity", "窗口不足"),
        ("waiting", "style_diagnostic", "标点异常"),
    ],
)
def test_unrelated_checkpoints_are_not_finalized_as_repetition(state, kind, message):
    error = GenerationDeferred(
        {
            "id": "saved",
            "state": state,
            "feedback": {"kind": kind, "message": message},
            "revisions": [
                {
                    "phase": "decision",
                    "status": "completed",
                    "action": "wait",
                    "output": {"action": "wait", "reason": "等网络恢复"},
                }
            ],
        }
    )
    assert BrowsingDecisions._generation_resolution(error) is None


@pytest.mark.parametrize(
    ("kind", "same_parent", "reason", "latest_action", "allowed"),
    [
        ("output_limit", False, "下次再接着说", "wait", True),
        ("output_limit", True, "下次再接着说", "wait", False),
        ("timeout", False, "网络中断", "wait", False),
        ("output_limit", False, "  ", "wait", False),
        ("output_limit", False, "继续改写", "revise", False),
    ],
)
async def test_new_episode_rechecks_model_deferral_and_different_parent_atomically(
    browsing, kind, same_parent, reason, latest_action, allowed
):
    first, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    browsing.store.execute("UPDATE browsing_rounds SET status='completed' WHERE id=?", (first["id"],))
    second, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    coordinator = browsing.runtime.continuations
    binding = {"task": "generation", "source": "same-real-comment"}
    saved = coordinator.begin("generation.browsing", "atomic-deferral", binding, {"kind": kind}, parent_round_id=first["id"])
    coordinator._step(saved, "decision", "completed", action=latest_action, output={"action": latest_action, "reason": reason})
    coordinator._state(saved["id"], "waiting")
    parent = first["id"] if same_parent else second["id"]
    if allowed:
        restarted = coordinator.restart_generation(saved["id"], binding=binding, parent_round_id=parent, restart_deferred=True)
        assert restarted["state"] == "pending" and restarted["episode"] != saved["episode"]
        assert restarted["parent_round_id"] == second["id"]
        assert any(step["phase"] == "decision" for step in restarted["revisions"])
    else:
        with pytest.raises(ValueError, match="只有新的生成请求"):
            coordinator.restart_generation(saved["id"], binding=binding, parent_round_id=parent, restart_deferred=True)
        assert coordinator.get(saved["id"])["state"] == "waiting"


async def isolated_output_limit_provider(browsing, action, *, comments=False, network=False, private=None):
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
                text = json.dumps({"action": action, "reason": "现在先停在这里，下一次再接着聊", "read": ""})
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


@pytest.mark.parametrize("action", ["wait", "abandon"])
@pytest.mark.parametrize("comments", [False, True])
async def test_output_limit_explicit_model_choice_finishes_this_batch_and_wait_comments_can_return(
    browsing, monkeypatch, action, comments
):
    from test_browsing_comment_only import attach_comment_source

    await browsing.configure({"sources": ["specified"], "watch_videos": [BV]})
    await authorize(browsing)
    await browsing.configure({"reply_enabled": comments})
    if comments:
        attach_comment_source(browsing, monkeypatch)
    original, provider, calls = await isolated_output_limit_provider(browsing, action, comments=comments)
    try:
        first = await finished(browsing, automatic=True, video_only=True)
        if not comments:
            assert first["status"] == "paused", first
            saved = browsing.repo.content_progress(first, BV)
            assert saved["material"]["metadata"]["bvid"] == BV
            assert saved["main_decision"] is None
            task = browsing.store.one("SELECT * FROM browsing_tasks WHERE status='checkpoint'")
            assert json.loads(task["payload"])["generation_resolution"]["decision"]["action"] == action
            assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1
            assert calls == ["CandidateBatchPlan", "ResponseDecision", "feedback"]
            assert not browsing.intents() and not browsing.adapter.sent
            if action == "wait":
                resumed = await finished(browsing, automatic=True, video_only=True)
                assert resumed["status"] == "completed", resumed
                assert browsing.repo.content_progress(resumed, BV)["main_decision"]["video_preference"] == "neutral"
                assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1
            return
        assert first["status"] == "completed", first
        assert first["metrics"]["failures"] == 0
        assert browsing.repo.batches.get(first)["state"] == "completed"
        assert not browsing.intents() and not browsing.adapter.sent
        assert calls == ["CandidateBatchPlan"]
        first_reply = await finished(browsing, automatic=True, interaction_only=True)
        assert first_reply["status"] == "completed"
        assert calls == ["CandidateBatchPlan", "ResponseDecision", "feedback"]
        assert not browsing.store.all("SELECT * FROM browsing_tasks WHERE status IN ('failed','checkpoint')")
        if comments:
            saved = json.loads(browsing.store.one("SELECT decision FROM browsing_comment_progress")["decision"])
            assert saved["action"] == ("wait" if action == "wait" else "skip")
            assert saved["explanation"] == "现在先停在这里，下一次再接着聊"
            assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
            second = await finished(browsing, automatic=True, video_only=True)
            assert second["status"] == "completed", [
                (item["status"], item["error"]) for item in browsing.tasks(round_id=second["id"])
            ]
            second_reply = await finished(browsing, automatic=True, interaction_only=True)
            assert second_reply["status"] == "completed"
            assert len(browsing.adapter.sent) == int(action == "wait"), {
                "calls": calls, "tasks": browsing.tasks(round_id=second_reply["id"]),
                "intents": browsing.intents(),
            }
            assert calls.count("ResponseDecision") == (2 if action == "wait" else 1)
            if action == "wait":
                assert browsing.adapter.sent[0][1] == "慢慢加盐就能看出鸡蛋浮起来，盐水密度的变化很直观。"
            third = await finished(browsing, automatic=True, video_only=True)
            assert third["status"] == "completed"
            third_reply = await finished(browsing, automatic=True, interaction_only=True)
            assert third_reply["status"] == "completed"
            assert len(browsing.adapter.sent) == int(action == "wait")
    finally:
        await provider.close()
        browsing.runtime.llm = original


async def test_real_http_timeout_keeps_checkpoint_without_inventing_a_model_wait(browsing):
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV]})
    await authorize(browsing)
    await browsing.configure({"reply_enabled": False})
    original, provider, calls = await isolated_output_limit_provider(browsing, "wait", network=True)
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
    original, provider, _ = await isolated_output_limit_provider(browsing, "wait", comments=True, private=private)
    try:
        row = await finished(browsing, automatic=True, video_only=True)
        assert row["status"] == "completed"
        reply = await finished(browsing, automatic=True, interaction_only=True)
        assert reply["status"] == "completed"
        # Query the actual public task DTO, including its duplicate nested payload.
        tasks = browsing.tasks(round_id=reply["id"])
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
    task = browsing.tasks(round_id=row["id"])[0]
    assert task["status"] == "deferred_daily" and task["payload"]["retry_kind"] == "send_limit"
    assert task["payload"]["next_at"] == next_at
    assert not browsing.adapter.sent and not browsing.repo.video_retry(row, BV)
    persisted = browsing.store.one(
        "SELECT payload,status FROM browsing_batch_items WHERE batch_id=?", (row["config"]["batch_id"],)
    )
    assert persisted["status"] == "daily_wait" and json.loads(persisted["payload"])["next_at"] == next_at
