"""Real HTTP-provider feedback repairs machine fields, preserving free OC prose."""

import asyncio
import json
from contextlib import nullcontext

import httpx
import pytest

from tuber_studio.api_providers import APIProvider
from tuber_studio.browsing_models import BrowsingPolicy, ResponseDecision
from tuber_studio.generation_meter import GenerationMeter
from tuber_studio.models import OperationReply
from tuber_studio.operation_generation import generate_with_recovery
from tuber_studio.operation_text import operation_output_diagnostics


async def model_for(
    runtime, settings, *, first=None, corrected=None, first_finish="stop", decision="revise", failure=None
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
        elif "read" in body["response_format"]["schema"]["properties"]:
            text = json.dumps(
                {"action": decision, "reason": "继续当前这次交流", "read": ""}, ensure_ascii=False
            )
        else:
            if failure:
                raise failure
            value = (first if first is not None else corrected) if not generated else corrected
            finish = first_finish if not generated else "stop"
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
                generation_context=nullcontext,
                parent_round_id=parent,
            ),
            timeout=2,
        )


async def test_invalid_reply_target_is_returned_to_agent_then_repaired(runtime, settings):
    first = {"spoken_text": "本鱼听到了。", "reply_to": ["outside-event"]}
    requests, generated, corrected = await model_for(runtime, settings, first=first)
    reply, continuation = await generate(runtime)
    assert reply.spoken_text == corrected["spoken_text"] and reply.reply_to == ["source-event"]
    row = runtime.continuations.find("test-generation", "one-source")
    original = (
        json.loads(row["original_output"])
        if isinstance(row["original_output"], str)
        else row["original_output"]
    )
    assert original == first and row["binding"]["source"]["event_id"] == "source-event"
    assert row["state"] == "completed" and row["recoveries"] == 1 and continuation
    assert len(generated) == 2 and len(requests) == 6
    retry_messages = [message["content"] for body in requests[2:] for message in body["messages"]]
    assert any("outside-event" in text and "reply_to" in text for text in retry_messages)
    assert row["budget"] == {"model_calls": 6, "input_tokens": 1200, "output_tokens": 63}
    assert all(body["chat_template_kwargs"]["enable_thinking"] is True for body in requests)


async def test_parseable_length_result_is_not_adopted_as_a_finished_reply(runtime, settings):
    first = {"spoken_text": "本鱼想把这锅白饭的故事慢慢讲给你听。", "reply_to": ["source-event"]}
    requests, generated, corrected = await model_for(runtime, settings, first=first, first_finish="length")
    reply, _ = await generate(runtime)
    row = runtime.continuations.find("test-generation", "one-source")
    assert reply.spoken_text == corrected["spoken_text"] and len(generated) == 2
    assert json.loads(row["original_output"])["spoken_text"] == first["spoken_text"]
    assert any(
        "finish_reason=length" in message["content"] for body in requests[2:] for message in body["messages"]
    )


async def test_agent_may_keep_valid_intentional_style_without_regenerating(runtime, settings):
    assert operation_output_diagnostics("鱼鱼、本鱼、鲸鱼——都听到啦。")["needs_review"] is False
    chorus = "海面把月光轻轻摇进碗里，鱼鱼守着暖呼呼的小屋，等晚归的朋友一起把今天的故事讲完。" * 3
    first = {"spoken_text": "\n".join([chorus] * 40), "reply_to": ["source-event"]}
    requests, generated, _ = await model_for(runtime, settings, first=first, decision="keep")

    def diagnose(value):
        return (
            "长段重复，确认是否为有意的叙事反复"
            if operation_output_diagnostics(value.spoken_text)["needs_review"]
            else None
        )

    reply, _ = await generate(runtime, diagnose=diagnose)
    assert reply.spoken_text == first["spoken_text"] and len(generated) == 1 and len(requests) == 4
    row = runtime.continuations.find("test-generation", "one-source")
    assert row["state"] == "completed" and row["trigger_feedback"]["kind"] == "style_diagnostic"
    assert any(step["action"] == "keep" for step in row["revisions"])


async def test_parent_meter_counts_actual_probe_and_generation_once(runtime, settings):
    requests, _, _ = await model_for(
        runtime, settings, first={"spoken_text": "本鱼接住。", "reply_to": ["wrong"]}
    )
    config = BrowsingPolicy(max_model_calls=6, max_input_tokens=1048576, max_output_tokens=65536).model_dump()
    parent, _ = runtime.browsing_repository.create_round("100", runtime.character, config, 0, True)
    with GenerationMeter(runtime.browsing_repository, parent["id"]):
        reply, _ = await generate(runtime, parent=parent["id"])
    assert reply.reply_to == ["source-event"]
    saved = runtime.browsing_repository.round(parent["id"])
    assert saved["budget"]["model_calls"] == len(requests) == 6
    assert saved["budget"]["input_tokens"] == 1200 and saved["budget"]["output_tokens"] == 63
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


async def test_invalid_evidence_is_repaired_then_bound_to_original_source(runtime, settings):
    corrected = {
        "action": "reply_public",
        "text": "本鱼也想挪个软垫过来，慢慢听你说。",
        "reason": "light_interaction",
        "evidence_ids": ["e1"],
    }
    first = corrected | {"evidence_ids": ["e99"]}
    requests, generated, _ = await model_for(runtime, settings, first=first, corrected=corrected)
    schema = ResponseDecision.model_json_schema()
    schema["properties"]["evidence_ids"]["items"] = {"type": "string", "enum": ["e1"]}
    original = "cid:123:part:0:segment:1"
    binding = {"account_id": "100", "source": {"e1": original}, "target": {"parent": "200"}}

    def validate(value):
        decision = ResponseDecision.model_validate(value)
        if decision.evidence_ids != ["e1"]:
            raise ValueError("引用不在当前已读资料中；本包提供e1")
        return decision

    async with runtime.offline_generation():
        result, _ = await asyncio.wait_for(
            generate_with_recovery(
                runtime,
                channel="test-source-decision",
                identity="source-1",
                binding=binding,
                character=runtime.character,
                messages=[{"role": "user", "content": "[e1|00:15] 今天有点累，想找个软垫歇歇。"}],
                generate=lambda messages: runtime.llm.generate_json(messages, schema, max_tokens=8192),
                validate=validate,
                max_tokens=8192,
                guard=lambda: True,
                generation_context=nullcontext,
            ),
            timeout=2,
        )
    bound = ResponseDecision.model_validate(
        result.model_dump() | {"evidence_ids": [binding["source"][ref] for ref in result.evidence_ids]}
    )
    assert bound.evidence_ids == [original] and bound.text == corrected["text"]
    row = runtime.continuations.find("test-source-decision", "source-1")
    assert row["binding"] == binding and row["state"] == "completed"
    assert len(generated) == 2 and len(requests) == 6
    assert any(
        "e99" in message["content"] and "e1" in message["content"]
        for body in requests[2:]
        for message in body["messages"]
    )
