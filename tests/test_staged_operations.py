import asyncio
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.browsing import BrowsingService
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.providers import LlamaCppProvider

SPEC = importlib.util.spec_from_file_location(
    "staged_operations", Path(__file__).resolve().parents[1] / "evaluations/staged_operations.py"
)
staged = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staged)

DECISION = {"action": "reply_public", "reason": "specific_detail", "evidence_ids": ["11:0"]}
SKIP = {"action": "skip", "reason": "insufficient_evidence", "evidence_ids": []}
CASE = {
    "id": "opaque-id-not-in-prompt",
    "data": {
        "kind": "reply_public",
        "max_chars": 60,
        "evidence": [{"id": "11:0", "text": "加盐以后，鸡蛋浮起来了。", "cid": "11", "start": 0, "end": 8}],
        "metadata": {"owner_uid": "200", "owner": "小明", "title": "原始标题不能变成事实"},
        "thread": {"source": {"author_uid": "300", "display_name": "小明", "text": "这个实验挺有趣"}},
    },
    "allowed_evidence_ids": ["11:0"],
    "expected_actions": ["reply_public"],
    "criterion": "gold-not-in-prompt",
}


@pytest.fixture
async def experiment(runtime):
    await runtime.llm.close()
    provider = LlamaCppProvider(
        {
            "model": "candidate",
            "base_url": "http://127.0.0.1:18090",
            "context_tokens": 131072,
            "native_context_tokens": 262144,
            "enable_thinking": True,
            "temperature": 0.2,
            "seed": 42,
        }
    )
    await provider.client.aclose()
    state = {
        "queue": [],
        "requests": [],
        "generations": [],
        "tokens": 120,
        "block": False,
        "started": asyncio.Event(),
        "cancelled": asyncio.Event(),
    }

    async def handle(request):
        payload = json.loads(request.content) if request.content else None
        state["requests"].append((request.url.path, payload))
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"model_alias": "candidate", "default_generation_settings": {"n_ctx": 131072}}
            )
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "whole-source-template"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(state["tokens"]))})
        assert request.url.path == "/v1/chat/completions"
        state["generations"].append(payload)
        if state["block"]:
            state["started"].set()
            try:
                await asyncio.Event().wait()
            finally:
                state["cancelled"].set()
        item = state["queue"].pop(0)
        content = item.get("content", json.dumps(item.get("decision", DECISION), ensure_ascii=False))
        message = {
            "content": content,
            "reasoning_content": "private-chain-of-thought-never-store",
        } | item.get("message", {})
        usage = {"prompt_tokens": state["tokens"], "completion_tokens": item.get("completion", 100)}
        usage.update(item.get("usage", {}))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": item.get("finish", "stop"), "message": message}],
                "usage": usage,
            },
        )

    provider.client = httpx.AsyncClient(
        base_url="http://127.0.0.1:18090", transport=httpx.MockTransport(handle)
    )
    runtime.llm = provider
    service = SimpleNamespace(
        runtime=runtime,
        store=runtime.store,
        repo=runtime.browsing_repository,
        _check_round=lambda row: None,
        repetition_reason=BrowsingService.repetition_reason,
    )
    row, _ = service.repo.create_round(
        "evaluation-only", runtime.character, BrowsingPolicy().model_dump(), 0, True
    )
    return service, row, state


async def run(experiment, queue, case=CASE, **kwargs):
    service, row, state = experiment
    state["queue"] = deepcopy(queue)
    return await staged.evaluate_staged_case(service, row, deepcopy(case), **kwargs)


async def test_small_decision_plain_body_and_backend_binding_share_native_parameters(experiment):
    result = await run(experiment, [{}, {"content": "盐水里的鸡蛋浮起来啦。"}])
    service, _, state = experiment
    assert result["actual"] == DECISION | {"text": "盐水里的鸡蛋浮起来啦。"}
    assert result["structural_complete"] and result["pipeline_acceptance"]
    assert result["model_calls"] == 2 and result["budget_delta"]["model_calls"] == 2
    assert result["output_tokens_charged"] == result["budget_delta"]["output_tokens"] == 200
    first, body = state["generations"]
    branches = first["response_format"]["schema"]["oneOf"]
    assert all(set(branch["properties"]) == {"action", "reason", "evidence_ids"} for branch in branches)
    assert "response_format" not in body and all("tools" not in payload for payload in (first, body))
    assert [first["max_tokens"], body["max_tokens"]] == [1024, 3072]
    assert all(payload["chat_template_kwargs"] == {"enable_thinking": True} for payload in (first, body))
    assert all(payload["seed"] == 42 and payload["temperature"] == 0.2 for payload in (first, body))
    assert result["backend_binding"]["source_author_uid"] == "300"
    assert result["backend_binding"]["account_id"] == "evaluation-only"
    assert all(stage["stop"] and stage["usage_verified"] for stage in result["stages"])
    assert "private-chain-of-thought-never-store" not in json.dumps(result)
    assert all(stage["reasoning_characters_observed_not_retained"] > 0 for stage in result["stages"])
    prompt = json.dumps(state["generations"], ensure_ascii=False)
    assert CASE["id"] not in prompt and CASE["criterion"] not in prompt and "expected_actions" not in prompt
    assert service.runtime.settings.data["llm"]["kind"] == "demo"


async def test_skip_needs_no_body_generation(experiment):
    result = await run(experiment, [{"decision": SKIP}])
    assert result["actual"] == SKIP | {"text": ""} and result["model_calls"] == 1


async def test_decision_json_repair_retains_original_and_is_a_separate_request(experiment):
    result = await run(experiment, [{"content": "not json"}, {"decision": SKIP}])
    assert result["pipeline_acceptance"] and result["retries_used"] == 1
    assert result["stages"][0]["error"]["kind"] == "invalid_decision_json"
    assert result["stages"][0]["raw_output"] == "not json"
    assert result["stages"][1]["retry"]["kind"] == "decision_format_repair"
    assert result["output_tokens_charged"] == 200
    assert result["first_model_decision"] is None and result["raw_model_action_matches"] is None
    assert result["stages"][0]["json_parseable"] is False


async def test_body_wrapper_supplies_speech_while_backend_keeps_the_decision(experiment):
    result = await run(
        experiment,
        [{}, {"content": '{"action":"handoff_private","target_uid":"999","text":"给你留了热饭。"}'}],
    )
    assert result["model_calls"] == 2 and result["retries_used"] == 0
    assert result["actual"]["action"] == "reply_public"
    assert result["actual"]["text"] == "给你留了热饭。"
    assert result["backend_binding"]["source_author_uid"] == "300"
    payload = experiment[2]["generations"][-1]
    assert "response_format" not in payload and '"fixed_decision"' in payload["messages"][1]["content"]


async def test_at_most_one_retry_across_both_stages(experiment):
    result = await run(experiment, [{"content": "bad"}, {}, {"content": ""}])
    assert not result["pipeline_acceptance"] and result["model_calls"] == 3
    assert result["retries_used"] == 1 and len([s for s in result["stages"] if "error" in s]) == 2


async def test_shared_output_allowance_accounts_length_and_limits_retry(experiment):
    result = await run(
        experiment,
        [
            {"completion": 900},
            {"content": "bad", "finish": "length", "completion": 3000},
            {"content": "好呀", "completion": 20},
        ],
    )
    assert [s["max_tokens"] for s in result["stages"]] == [1024, 3072, 196]
    assert result["output_tokens_charged"] == 3920 <= 4096
    assert result["stages"][1]["finish_reason"] == "length" and not result["stages"][1]["stop"]
    assert result["stages"][1]["error"]["kind"] == "not_stop"
    assert result["stages"][2]["retry"]["kind"] == "new_body_attempt"


@pytest.mark.parametrize(
    "patch,kind",
    [
        ({"evidence_ids": ["invented"]}, "invalid_references"),
        ({"action": "handoff_private", "reason": "specific_detail"}, "channel_reason_mismatch"),
        ({"uid": "666"}, "invalid_decision_fields"),
    ],
)
async def test_decision_contract_rejects_ids_channel_and_extra_identity_fields(experiment, patch, kind):
    result = await run(experiment, [{"decision": DECISION | patch}], max_retries=0)
    assert not result["structural_complete"] and result["error"]["kind"] == kind
    assert result["model_calls"] == 1 and result["stages"][0]["stop"]


async def test_private_recipient_is_bound_only_to_current_verified_source(experiment):
    private = {"action": "handoff_private", "reason": "user_requested", "evidence_ids": ["11:0"]}
    result = await run(experiment, [{"decision": private}, {"content": "我们私下继续聊吧。"}])
    assert (
        result["backend_binding"]["source_author_uid"] == "300"
        and result["actual"]["action"] == "handoff_private"
    )


async def test_model_cannot_fill_a_missing_recipient(experiment):
    case = deepcopy(CASE)
    case["data"]["thread"]["source"].pop("author_uid")
    private = {"action": "handoff_private", "reason": "user_requested", "evidence_ids": ["11:0"]}
    result = await run(experiment, [{"decision": private}], case, max_retries=0)
    assert result["error"]["kind"] == "unverified_recipient" and result["model_calls"] == 1


async def test_pure_staged_does_not_turn_coarse_flags_into_semantic_success(experiment):
    case = deepcopy(CASE)
    case["data"].update(coverage="partial", missing_parts=["22"], visual_available=False)
    case["expected_actions"] = ["skip"]
    result = await run(experiment, [{}, {"content": "盐水里的鸡蛋浮起来啦。"}], case)
    assert result["pipeline_acceptance"] and result["raw_model_action_matches"] is False
    assert result["source_gate_policy"] == "necessary" and result["action_matches"] is False


async def test_conservative_gate_is_explicit_and_not_model_improvement(experiment):
    case = deepcopy(CASE)
    case["data"].update(coverage="partial", missing_parts=["22"])
    case["expected_actions"] = ["skip"]
    result = await run(experiment, [{}, {"decision": SKIP}], case, source_guard_mode="conservative")
    assert result["raw_model_action_matches"] is False and result["final_model_action_matches"] is True
    assert result["source_gate_policy"] == "conservative"
    assert result["stages"][0]["error"]["kind"] == "source_rejected"
    assert result["stages"][1]["retry"]["kind"] == "decision_reselection_after_source_rejection"
    assert all(stage["stage"] == "decision" for stage in result["stages"])


@pytest.mark.parametrize("mode", ["tools", "bad_usage", "embedded_think"])
async def test_native_failures_never_become_body_or_hide_reasoning(experiment, mode):
    queue = {
        "tools": {"message": {"tool_calls": [{"function": {"name": "send_message"}}]}},
        "bad_usage": {"usage": {"prompt_tokens": 121}},
        "embedded_think": {"content": '<think>private chain hidden</think>{"action":"skip"}'},
    }
    result = await run(experiment, [queue[mode]], max_retries=0)
    assert not result["pipeline_acceptance"] and result["model_calls"] == 1
    assert "private chain hidden" not in json.dumps(result)
    assert result["stages"][0]["stop"]
    assert result["output_tokens_charged"] == (1024 if mode == "bad_usage" else 100)


async def test_parent_budget_blocks_second_generation(experiment):
    service, row, _ = experiment
    config = row["config"] | {"max_model_calls": 1}
    service.store.execute("UPDATE browsing_rounds SET config=? WHERE id=?", (json.dumps(config), row["id"]))
    result = await run(experiment, [{}, {"content": "不会运行"}])
    assert not result["pipeline_acceptance"] and len(experiment[2]["generations"]) == 1
    assert result["budget_delta"]["model_calls"] == 1 and result["stages"][1]["model_calls"] == 0


async def test_native_input_overflow_never_truncates_or_generates(experiment):
    experiment[2]["tokens"] = 124000
    result = await run(experiment, [{}])
    assert not result["pipeline_acceptance"] and not experiment[2]["generations"]
    assert result["error"]["kind"] == "context_window"


async def test_cancelled_generation_releases_shared_slot(experiment):
    service, row, state = experiment
    state["block"] = True
    task = asyncio.create_task(staged.evaluate_staged_case(service, row, CASE))
    await state["started"].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["cancelled"].is_set() and not service.runtime.generation_lock.locked()


def packet(quote="鸡蛋浮起来了。", *, answerability="answerable", evidence_id="11:0"):
    return {
        "answerability": answerability,
        "missing": [],
        "quotes": [{"evidence_id": evidence_id, "quote": quote}],
    }


async def test_evidence_first_only_forwards_verified_substrings_and_preserves_source_identity(experiment):
    result = await run(
        experiment, [{"decision": packet()}, {}, {"content": "鸡蛋浮起来了呀。"}], strategy="evidence_first"
    )
    state = experiment[2]
    assert result["pipeline_acceptance"] and [s["stage"] for s in result["stages"]] == [
        "evidence",
        "decision",
        "body",
    ]
    assert [s["max_tokens"] for s in result["stages"]] == [1024, 1024, 2048]
    assert result["budget_delta"]["model_calls"] == 3 and result["output_tokens_charged"] == 300
    trace = result["evidence_packet"]
    source = CASE["data"]["evidence"][0]["text"]
    mapped = trace["quote_mapping"][0]
    assert source[mapped["source_char_start"] : mapped["source_char_end"]] == "鸡蛋浮起来了。"
    assert mapped["source_text_sha256"] != mapped["quote_sha256"]
    assert not trace["evidence_rewritten"] and trace["substring_validation_only"]
    assert trace["evidence_text_preserved"]
    assert result["packet_policy"] == trace["packet_policy"] == "selected_full_segments_with_verified_quotes"
    assert trace["packet"]["evidence"][0]["text"] == source
    assert trace["packet"]["evidence"][0]["verified_quotes"][0] == mapped
    assert trace["dropped_metadata_fields"] == ["title"]
    assert trace["packet"]["metadata"] == {"owner": "小明", "owner_uid": "200"}
    assert trace["packet"]["thread"]["source"] == CASE["data"]["thread"]["source"]
    assert result["original_materials"] == CASE["data"]
    downstream = json.dumps(state["generations"][1:], ensure_ascii=False)
    assert "原始标题不能变成事实" not in downstream and "加盐以后" in downstream
    assert "unverified_model_assessment_not_a_fact_verdict" in downstream
    assert all(s["packet_sha256"] == trace["packet_sha256"] for s in result["stages"][1:])
    assert "style" in state["generations"][1]["messages"][0]["content"]


@pytest.mark.parametrize(
    "value",
    [
        packet("鸡蛋沉下去了。"),
        packet(evidence_id="not-present"),
        {"answerability": "answerable", "missing": [], "quotes": []},
    ],
)
async def test_bad_extracted_quotes_never_reach_routing_or_body(experiment, value):
    result = await run(experiment, [{"decision": value}], strategy="evidence_first", max_retries=0)
    assert not result["pipeline_acceptance"] and result["model_calls"] == 1
    assert result["stages"][0]["stage"] == "evidence" and "evidence_packet" not in result
    assert result["error"]["kind"] in {"unverified_quote", "missing_quote"}


async def test_evidence_retry_is_explicit_and_uses_the_same_shared_allowance(experiment):
    result = await run(
        experiment,
        [
            {"decision": packet("鸡蛋沉下去了。"), "completion": 900},
            {"decision": packet(), "completion": 900},
            {},
            {"content": "鸡蛋浮起来了呀。"},
        ],
        strategy="evidence_first",
    )
    assert result["pipeline_acceptance"] and result["model_calls"] == 4 and result["retries_used"] == 1
    assert [stage["max_tokens"] for stage in result["stages"]] == [1024, 1024, 1024, 2048]
    assert result["output_tokens_charged"] == 2000 <= 4096
    assert result["stages"][1]["retry"]["kind"] == "evidence_reselection"
    assert result["stages"][0]["error"]["kind"] == "unverified_quote"


async def test_valid_quote_does_not_turn_answerability_or_a_bad_decision_into_truth(experiment):
    case = deepcopy(CASE)
    case["data"]["evidence"][0]["text"] = "接下来看我这个手势。"
    case["data"]["visual_available"] = False
    case["expected_actions"] = ["skip", "wait"]
    result = await run(
        experiment,
        [{"decision": packet("接下来看我这个手势。")}, {}, {"content": "手势对应11。"}],
        case,
        strategy="evidence_first",
    )
    assert result["pipeline_acceptance"] and result["raw_model_action_matches"] is False
    assert result["semantic_quality_accepted"] is None and result["action_matches"] is False
    assert result["evidence_packet"]["model_extraction"]["answerability"] == "answerable"
    assert result["evidence_packet"]["substring_validation_only"]


async def test_evidence_packet_can_be_empty_for_no_evidence_but_never_invents_reference(experiment):
    case = deepcopy(CASE)
    case["data"]["evidence"] = []
    case["expected_actions"] = ["skip"]
    value = {"answerability": "insufficient", "missing": ["没有内容依据"], "quotes": []}
    result = await run(experiment, [{"decision": value}, {"decision": SKIP}], case, strategy="evidence_first")
    assert result["actual"] == SKIP | {"text": ""} and result["model_calls"] == 2
    assert not result["evidence_packet"]["quote_mapping"]


async def test_core_rules_only_is_an_explicit_decision_condition_not_a_silent_style_change(experiment):
    result = await run(experiment, [{}, {"content": "鸡蛋浮起来了呀。"}], decision_character="core_rules")
    first, body = experiment[2]["generations"]
    assert '"style"' not in first["messages"][0]["content"]
    assert '"style"' in body["messages"][0]["content"]
    assert result["decision_character"] == "core_rules"
    assert [stage["character_fields"] for stage in result["stages"]] == [
        ["core", "rules"],
        ["core", "style", "rules"],
    ]


async def test_exhausted_allowance_never_starts_or_counts_a_retry(experiment):
    result = await run(
        experiment, [{"completion": 1024}, {"content": "partial", "finish": "length", "completion": 3072}]
    )
    assert not result["pipeline_acceptance"] and result["output_tokens_charged"] == 4096
    assert result["retries_used"] == result["retry_directives_issued"] == 0
    assert result["model_calls"] == 2 and result["error"]["kind"] == "output_allowance_exhausted"
    assert result["stages"][1]["error"]["kind"] == "not_stop"


async def test_stopped_parent_round_never_sends_generation(experiment):
    service, row, state = experiment
    service.repo.finish_round(row["id"], "cancelled")
    result = await run(experiment, [{}])
    assert not result["pipeline_acceptance"] and result["model_calls"] == 0 and not state["generations"]
    assert result["budget_delta"]["model_calls"] == 0


async def test_familiar_oc_wording_is_not_rejected_by_the_evaluator(experiment):
    case = deepcopy(CASE)
    body = "盐水里的鸡蛋终于慢慢浮起来啦。"
    case["data"]["recent_public_responses"] = [{"text": body}]
    result = await run(experiment, [{}, {"content": body}], case, max_retries=0)
    assert result["pipeline_acceptance"] and result["actual"]["text"] == body
    assert result["semantic_quality_accepted"] is None


def test_selected_full_segment_preserves_negation_but_does_not_certify_model_answerability():
    data = deepcopy(CASE["data"])
    data["evidence"][0]["text"] = "没有做糖水实验。"
    routed, audit = staged._packet(packet("糖水实验"), data)
    assert routed["evidence"][0]["text"] == "没有做糖水实验。"
    assert routed["evidence"][0]["verified_quotes"][0]["quote"] == "糖水实验"
    assert not audit["evidence_rewritten"] and audit["substring_validation_only"]
    assert audit["semantic_quality_accepted"] is None
    assert audit["quote_mapping"][0]["source_char_start"] == 3


def test_full_segments_keep_original_order_and_group_multiple_quotes_without_duplicate_rows():
    data = deepcopy(CASE["data"])
    data["evidence"] = [
        {"id": "first", "text": "没有做糖水实验；做了盐水实验。", "cid": "11"},
        {"id": "second", "text": "其他独立事实。", "cid": "22"},
        {"id": "unselected", "text": "没有选中的整条来源。"},
    ]
    value = {
        "answerability": "answerable",
        "missing": [],
        "quotes": [
            {"evidence_id": "second", "quote": "其他"},
            {"evidence_id": "first", "quote": "糖水实验"},
            {"evidence_id": "first", "quote": "盐水实验"},
        ],
    }
    routed, audit = staged._packet(value, data)
    assert [row["id"] for row in routed["evidence"]] == ["first", "second"]
    assert routed["evidence"][0]["text"] == data["evidence"][0]["text"]
    assert len(routed["evidence"][0]["verified_quotes"]) == 2
    assert audit["unselected_evidence_ids"] == ["unselected"]
    assert data["evidence"][0].get("verified_quotes") is None


@pytest.mark.parametrize("control", ["\x00", "\x08", "\x1f", "\x7f"])
async def test_native_body_control_characters_are_rejected_without_cleaning(experiment, control):
    raw = "好呀" + control
    result = await run(experiment, [{}, {"content": raw}], max_retries=0)
    assert not result["pipeline_acceptance"] and result["actual"] is None
    assert result["error"]["kind"] == "body_control_characters"
    assert result["stages"][1]["raw_output"] == raw and result["stages"][1]["stop"]


@pytest.mark.parametrize(
    "body", ["鱼宝，\n给你留了热饭。", "UID300是你的门牌呀？", "action只是个英文单词嘛。"]
)
async def test_oc_speech_has_no_control_word_or_phrase_gate(experiment, body):
    result = await run(experiment, [{}, {"content": body}], max_retries=0)
    assert result["pipeline_acceptance"] and result["actual"]["text"] == body
    assert result["actual"]["action"] == "reply_public"


async def test_control_character_failure_needs_an_actual_directed_body_retry(experiment):
    result = await run(experiment, [{}, {"content": "好呀\x00"}, {"content": "好呀"}])
    assert result["pipeline_acceptance"] and result["retries_used"] == 1 and result["model_calls"] == 3
    assert result["stages"][1]["error"]["kind"] == "body_control_characters"
    assert result["stages"][1]["raw_output"] == "好呀\x00"
    assert result["stages"][2]["retry"]["kind"] == "body_format_repair"
    assert result["actual"]["text"] == "好呀"


async def test_printable_ascii_boundary_and_spaces_are_preserved(experiment):
    raw = " 好呀 ~"
    result = await run(experiment, [{}, {"content": raw}])
    assert result["pipeline_acceptance"] and result["actual"]["text"] == raw
