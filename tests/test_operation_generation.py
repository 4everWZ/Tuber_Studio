"""Single generation attempts retain invalid output and validated review candidates."""

import asyncio
import json

import httpx
import pytest

from tuber_studio import web_fetch
from tuber_studio.api_providers import APIProvider
from tuber_studio.generation_meter import GenerationMeter
from tuber_studio.models import OperationReply
from tuber_studio.operation_generation import GenerationInvalid, generate_with_recovery
from tuber_studio.operation_text import operation_output_diagnostics
from tuber_studio.operations.browsing.models import BrowsingPolicy, ResponseDecision


async def model_for(
    runtime,
    settings,
    *,
    first=None,
    corrected=None,
    first_finish="stop",
    corrected_finish="stop",
    decision="revise",
    failure=None,
):
    requests, generated = [], []
    if corrected is None:
        corrected = {"spoken_text": "那本鱼先把软垫给你挪一点，你慢慢说。", "reply_to": ["source-event"]}

    async def respond(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        probe, finish = body["max_tokens"] == 1, "stop"
        if probe:
            text = ""
        elif "read" in body["response_format"].get("schema", {}).get("properties", {}):
            text = json.dumps(
                {"action": decision, "reason": "继续当前这次交流", "read": ""}, ensure_ascii=False
            )
        else:
            if failure:
                raise failure
            value = (first if first is not None else corrected) if not generated else corrected
            finish = first_finish if not generated else corrected_finish
            generated.append(value)
            text = json.dumps(value, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 1 if probe else 20},
            },
        )

    config = settings.data["llm"] | {
        "base_url": "https://isolated.invalid/v1",
        "model": "test-model",
        "context_tokens": 131072,
        "native_context_tokens": 131072,
        "enable_thinking": True,
    }
    provider = APIProvider(config, adapter="llama_cpp")
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://isolated.invalid/v1", transport=httpx.MockTransport(respond)
    )
    await runtime.llm.close()
    runtime.llm = provider
    return requests, generated, corrected


@pytest.mark.parametrize("mode", ["comment_draft", "direct_message", "live"])
@pytest.mark.parametrize("provided_url", [False, True])
async def test_runtime_operation_web_fetch_is_model_selected(
    runtime, settings, monkeypatch, mode, provided_url
):
    url = "https://example.org/penguin"
    first = {"web_fetch": url} if mode != "live" else {"spoken_text": "本鲸在呢。"}
    requests, generated, _ = await model_for(runtime, settings, first=first)
    fetched = []

    async def fetch(target):
        fetched.append(target)
        return {"url": target, "title": "企鹅资料", "excerpt": "企鹅生活在南半球。", "truncated": False}

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    event = {
        "event_id": "source-event",
        "text": f"我看到 {url}，你怎么看？" if provided_url else "企鹅生活在哪里？",
    }
    reply, _ = await runtime.generate_reply(mode, runtime.character, event, mode=mode)
    if mode == "live":
        assert reply.spoken_text == "本鲸在呢。"
        assert not fetched and len(generated) == 1
        assert not any("web_fetch" in json.dumps(item, ensure_ascii=False) for item in requests)
        return
    assert reply.reply_to == ["source-event"]
    assert fetched == [url] and len(generated) == 2
    responses = [body for body in requests if body["max_tokens"] != 1]
    assert responses[0]["response_format"]["schema"]["oneOf"][-1]["required"] == ["web_fetch"]
    assert "oneOf" not in responses[1]["response_format"]["schema"]
    assert any("企鹅生活在南半球。" in message["content"] for message in responses[1]["messages"])
    episode = runtime.store.one(
        "SELECT id FROM operation_continuations WHERE channel=?", (f"generation.{mode}",)
    )
    step = next(
        step for step in runtime.continuations.get(episode["id"])["revisions"] if step["phase"] == "web_fetch"
    )
    assert step["context"]["url"] == url and "excerpt" not in step["context"]


async def generate(runtime, *, diagnose=None, parent=None):
    schema = OperationReply.model_json_schema()
    schema["properties"]["reply_to"]["items"] = {"type": "string", "enum": ["source-event"]}

    def validate(value):
        value = OperationReply.model_validate(value)
        if value.reply_to != ["source-event"]:
            raise ValueError("reply_to需要当前source-event；原目标由后端绑定")
        return value

    # Match Runtime/Browsing callers which already own this lock. Recovery must
    # reuse that scope instead of trying to acquire it again.
    async with runtime.offline_generation():
        return await asyncio.wait_for(
            generate_with_recovery(
                runtime,
                channel="test-generation",
                identity="one-source",
                binding={
                    "task": "generation",
                    "account_id": "100",
                    "character": {"id": runtime.character["id"], "version": runtime.character["version"]},
                    "source": {"event_id": "source-event"},
                },
                character=runtime.character,
                messages=[{"role": "user", "content": "今天有点累，想找个软垫歇歇。"}],
                generate=lambda messages: runtime.llm.generate_json(messages, schema, max_tokens=8192),
                validate=validate,
                max_tokens=8192,
                guard=lambda: True,
                diagnose=diagnose,
                parent_round_id=parent,
            ),
            timeout=2,
        )


async def test_invalid_reply_target_terminates_once_and_keeps_bound_original(runtime, settings):
    first = {"spoken_text": "本鱼听到了。", "reply_to": ["outside-event"]}
    requests, generated, _ = await model_for(runtime, settings, first=first)
    with pytest.raises(GenerationInvalid):
        await generate(runtime)
    row = runtime.continuations.find("test-generation", "one-source")
    original = json.loads(row["original_output"]) if isinstance(row["original_output"], str) else row["original_output"]
    assert original == first and row["binding"]["source"]["event_id"] == "source-event"
    assert row["state"] == "invalid" and row["recoveries"] == 0 and row["can_resume"]
    assert len(generated) == 1 and len(requests) == 2
    with pytest.raises(GenerationInvalid):
        await generate(runtime)
    assert len(requests) == 2
    assert row["budget"] == {"model_calls": 2, "input_tokens": 400, "output_tokens": 21}
    assert all(body["chat_template_kwargs"]["enable_thinking"] is True for body in requests)


async def test_parseable_length_result_is_invalid_without_a_repair_call(runtime, settings):
    first = {"spoken_text": "本鱼想把这锅白饭的故事慢慢讲给你听。", "reply_to": ["source-event"]}
    requests, generated, _ = await model_for(runtime, settings, first=first, first_finish="length")
    with pytest.raises(GenerationInvalid) as failure:
        await generate(runtime)
    row = runtime.continuations.find("test-generation", "one-source")
    assert len(generated) == 1 and len(requests) == 2
    assert json.loads(row["original_output"])["spoken_text"] == first["spoken_text"]
    assert row["current_output"] is None and row["state"] == "invalid"
    assert failure.value.continuation["feedback"]["kind"] == "output_limit"


async def test_only_explicit_resume_starts_one_new_episode_after_invalid(runtime, settings):
    first = {"spoken_text": "刚蒸好的那锅饭……", "reply_to": ["source-event"]}
    requests, generated, corrected = await model_for(runtime, settings, first=first, first_finish="length")
    with pytest.raises(GenerationInvalid):
        await generate(runtime)
    original = runtime.continuations.find("test-generation", "one-source")
    with pytest.raises(GenerationInvalid):
        await generate(runtime)
    assert len(generated) == 1
    runtime.continuations.resume(original["id"], binding=original["binding"])
    reply, _ = await generate(runtime)
    row = runtime.continuations.find("test-generation", "one-source")
    assert reply.spoken_text == corrected["spoken_text"] and generated == [first, corrected]
    assert row["state"] == "completed" and row["episode"] != original["episode"]
    assert len(row["revisions"]) > len(original["revisions"]) and len(requests) == 3
    assert not any(step["phase"] == "decision" for step in row["revisions"])


async def test_repetitive_valid_reply_is_retained_for_review_without_model_revision(runtime, settings):
    text = "选默认配置，选默认配置，选默认配置。"
    repeated = {"spoken_text": text, "reply_to": ["source-event"]}
    requests, generated, _ = await model_for(runtime, settings, first=repeated, corrected=repeated)

    def diagnose(value):
        return "正文出现连续短语循环" if operation_output_diagnostics(value.spoken_text)["needs_review"] else None

    reply, continuation = await generate(runtime, diagnose=diagnose)
    row = runtime.continuations.find("test-generation", "one-source")
    assert reply.spoken_text == text and generated == [repeated] and len(requests) == 2
    assert row["state"] == continuation["state"] == "awaiting_review" and not row["can_resume"]
    assert row["current_output"]["spoken_text"] == text and row["recoveries"] == 0
    await generate(runtime, diagnose=diagnose)
    assert len(requests) == 2


async def test_complete_intentional_style_stays_reviewable_without_an_agent_keep_call(runtime, settings):
    assert operation_output_diagnostics("鱼鱼、本鱼、鲸鱼——都听到啦。")["needs_review"] is False
    chorus = "海面把月光轻轻摇进碗里，鱼鱼守着暖呼呼的小屋，等晚归的朋友一起把今天的故事讲完。" * 3
    first = {"spoken_text": "\n".join([chorus] * 40), "reply_to": ["source-event"]}
    requests, generated, _ = await model_for(runtime, settings, first=first, decision="keep")
    reply, _ = await generate(runtime, diagnose=lambda value: "长段重复，保留人工审核")
    assert reply.spoken_text == first["spoken_text"] and len(generated) == 1 and len(requests) == 2
    row = runtime.continuations.find("test-generation", "one-source")
    assert row["state"] == "awaiting_review" and row["trigger_feedback"]["kind"] == "style_diagnostic"
    assert not any(step["phase"] == "decision" for step in row["revisions"])


async def test_parent_meter_counts_one_probe_and_one_invalid_generation(runtime, settings):
    requests, _, _ = await model_for(runtime, settings, first={"spoken_text": "本鱼接住。", "reply_to": ["wrong"]})
    config = BrowsingPolicy(max_model_calls=6, max_input_tokens=1048576, max_output_tokens=65536).model_dump()
    parent, _ = runtime.browsing_repository.create_round("100", runtime.character, config, 0, True)
    with GenerationMeter(runtime.browsing_repository, parent["id"]):
        with pytest.raises(GenerationInvalid):
            await generate(runtime, parent=parent["id"])
    saved = runtime.browsing_repository.round(parent["id"])
    assert {key: saved["budget"][key] for key in ("model_calls", "input_tokens", "output_tokens")} == {"model_calls": 2, "input_tokens": 400, "output_tokens": 21}
    assert len(requests) == 2
    row = runtime.continuations.find("test-generation", "one-source")
    own = runtime.store.one("SELECT budget FROM operation_continuation_budgets WHERE id=?", (row["id"],))
    assert json.loads(own["budget"])["model_calls"] == 0


async def test_transport_failure_preserves_checkpoint_without_rewrite_attempt(runtime, settings):
    requests, generated, _ = await model_for(
        runtime, settings, failure=httpx.ReadTimeout("model read timed out")
    )
    with pytest.raises(httpx.ReadTimeout):
        await generate(runtime)
    row = runtime.continuations.find("test-generation", "one-source")
    assert row["state"] == "checkpoint" and row["current_output"] is None
    assert not generated and len(requests) == 2
    _, regenerated, _ = await model_for(runtime, settings)
    reply, _ = await generate(runtime)
    resumed = runtime.continuations.find("test-generation", "one-source")
    assert reply.reply_to == ["source-event"] and len(regenerated) == 1
    assert resumed["episode"] != row["episode"] and resumed["state"] == "completed"
    assert len(resumed["revisions"]) > len(row["revisions"])


async def test_valid_model_wait_is_deferred_without_cross_parent_automatic_reconsideration(runtime, settings):
    waiting = {"action": "wait", "reason": "cooldown", "explanation": "这条讨论先留待对方补充", "text": "", "evidence_ids": []}
    requests, generated, _ = await model_for(runtime, settings, first=waiting, corrected=waiting)
    binding = {"task": "generation", "account_id": "100", "source": {"comment_id": "601"}}

    async def request(parent=None):
        return await generate_with_recovery(runtime, channel="test-wait", identity="same-source", binding=binding,
            character=runtime.character, messages=[{"role": "user", "content": "当前原讨论"}],
            generate=lambda packet: runtime.llm.generate_json(packet, ResponseDecision.model_json_schema(), max_tokens=8192),
            validate=ResponseDecision.model_validate, max_tokens=8192, guard=lambda: True, parent_round_id=parent)

    value, record = await request()
    assert value.action == "wait" and record["state"] == "deferred" and len(generated) == 1
    parent, _ = runtime.browsing_repository.create_round("100", runtime.character, BrowsingPolicy().model_dump(), 0, True)
    await request(parent["id"])
    assert len(requests) == 2 and len(generated) == 1
    saved = runtime.continuations.find("test-wait", "same-source")
    runtime.continuations.resume(saved["id"], binding=saved["binding"], parent_round_id=parent["id"])
    await request(parent["id"])
    assert len(generated) == 2 and len(requests) == 3


async def test_invalid_evidence_is_not_repaired_and_keeps_original_source_binding(runtime, settings):
    first = {"action": "reply_public", "text": "本鱼也想挪个软垫过来，慢慢听你说。",
             "reason": "light_interaction", "evidence_ids": ["e99"]}
    requests, generated, _ = await model_for(runtime, settings, first=first)
    schema = ResponseDecision.model_json_schema()
    binding = {"task": "generation", "account_id": "100", "source": {"e1": "cid:123:part:0:segment:1"}, "target": {"parent": "200"}}

    def validate(value):
        decision = ResponseDecision.model_validate(value)
        if decision.evidence_ids != ["e1"]:
            raise ValueError("引用不在当前已读资料中；本包提供e1")
        return decision

    with pytest.raises(GenerationInvalid):
        await generate_with_recovery(runtime, channel="test-source-decision", identity="source-1", binding=binding,
            character=runtime.character, messages=[{"role": "user", "content": "当前原资料"}],
            generate=lambda packet: runtime.llm.generate_json(packet, schema, max_tokens=8192),
            validate=validate, max_tokens=8192, guard=lambda: True)
    row = runtime.continuations.find("test-source-decision", "source-1")
    assert row["state"] == "invalid" and {k: v for k, v in row["binding"].items() if k != "self_identity"} == binding
    assert len(generated) == 1 and len(requests) == 2
    assert row["current_output"] is None
