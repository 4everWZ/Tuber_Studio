import asyncio
import base64
import copy
import json
import time

import httpx
import pytest

from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.video_vision import ApiVideoEvidenceProvider, VideoVisionConfig

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
META = {"parts": [{"cid": "34", "duration": 40}]}
FRAMES = [
    {
        "id": "first",
        "cid": "34",
        "actual_time": 10,
        "requested_time": 15,
        "kind": "storyboard",
        "image": PNG,
        "width": 1,
        "height": 1,
        "mime_type": "image/png",
    },
    {
        "id": "second",
        "cid": "34",
        "actual_time": 30,
        "requested_time": 25,
        "kind": "storyboard",
        "image": PNG,
        "width": 1,
        "height": 1,
        "mime_type": "image/png",
    },
]
CONFIG = {
    "enabled": True,
    "model": "actual-vlm",
    "base_url": "http://127.0.0.1:18080/v1",
    "model_revision": "weights-sha",
    "projector_revision": "projector-sha",
    "runtime_revision": "runtime-sha",
    "runtime_profile": "single-slot-q4-kv",
    "max_frames": 2,
}


@pytest.fixture
async def provider(runtime, request):
    value = ApiVideoEvidenceProvider(runtime, CONFIG | getattr(request, "param", {}))
    await value.api.client.aclose()
    state = {
        "requests": [],
        "result": {
            "sufficient": True,
            "summary": "两帧有不同图形",
            "used_frames": [1, 2],
            "limitations": ["仅抽样画面"],
        },
        "captions": {"first": "红色圆形", "second": "蓝色方形"},
        "caption_requests": 0,
        "window": 131072,
        "vision": True,
        "aliases": ["actual-vlm"],
        "block": False,
        "started": asyncio.Event(),
        "cancelled": asyncio.Event(),
    }

    async def handle(request):
        state["requests"].append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "actual-vlm"}]})
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "default_generation_settings": {"n_ctx": state["window"]},
                    "modalities": {"vision": state["vision"]},
                    "model_alias": state["aliases"],
                    "build_info": "tested-runtime",
                    "model_path": "tested-weights.gguf",
                },
            )
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert "tools" not in payload and not payload["stream"]
        probe = payload["max_tokens"] == 1
        if (
            state["block"]
            or not probe
            and (
                state.get("block_assessment")
                and "response_format" in payload
                or state.get("block_at_caption") == state["caption_requests"] + 1
                and "response_format" not in payload
            )
        ):
            state["started"].set()
            try:
                await asyncio.Event().wait()
            finally:
                state["cancelled"].set()
        if probe:
            content = None
        elif "response_format" not in payload:
            index = state["caption_requests"] % len(state["captions"])
            state["caption_requests"] += 1
            content = list(state["captions"].values())[index]
        else:
            content = json.dumps(state["result"])
        message = state.get("message") or {"role": "assistant", "content": content}
        if not probe and state.get("after_generation"):
            state["after_generation"](payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length" if probe else state.get("finish", "stop"), "message": message}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1 if probe else 20},
            },
        )

    value.api.client = httpx.AsyncClient(
        base_url="http://127.0.0.1:18080/v1/", transport=httpx.MockTransport(handle)
    )
    yield value, state
    await value.close()


def proof(provider):
    return {
        "passed": True,
        "configuration": provider.identity(),
        "context_tokens": 131072,
        "max_frames": 2,
        "max_pixels": 1024 * 1024,
        "max_bytes": 1024 * 1024,
    }


async def admit(provider):
    return await provider.probe(FRAMES, metadata=META, resource_admission=proof(provider))


def generations(state):
    return [request for request in state["requests"] if request.method == "POST"]


async def test_default_disabled_never_opens_client_or_admits_by_name(runtime):
    value = ApiVideoEvidenceProvider(runtime, {"model": "MiniCPM-V-4.7-1B"})
    assert (
        value.api is None and not value.capabilities()["available"] and not value.capabilities()["admitted"]
    )
    with pytest.raises(ValueError, match="未启用"):
        await value.probe(FRAMES, metadata=META)
    await value.close()


async def test_enabled_model_name_alone_is_not_capability(provider):
    value, state = provider
    assert not value.capabilities()["available"]
    with pytest.raises(ValueError, match="准入"):
        await value.analyze_video_evidence({}, META, FRAMES, "查看", deadline=time.time() + 10)
    assert not state["requests"]
    observed = await value.probe(FRAMES, metadata=META)
    assert observed["capabilities"]["images"] and not observed["capabilities"]["admitted"]
    assert len(generations(state)) == 6


async def test_real_inline_image_probe_and_distinct_frame_evidence(provider):
    value, state = provider
    assert (await admit(value))["capabilities"]["admitted"]
    result = await value.analyze_video_evidence(
        {"id": "task"}, META, FRAMES, "分别观察", deadline=time.time() + 10
    )
    assert result["frame_observations"] == {"first": "红色圆形", "second": "蓝色方形"}
    assert result["coverage"]["frames"] == [
        {"id": "first", "cid": "34", "actual_time": 10},
        {"id": "second", "cid": "34", "actual_time": 30},
    ]
    assert result["elapsed_ms"] >= 0 and result["usage"]["final_generation"]["prompt_tokens"] == 100
    payloads = [json.loads(request.content) for request in generations(state)]
    captions = [body for body in payloads if body["max_tokens"] != 1 and "response_format" not in body]
    assert len(captions) == 4  # Two sequential frame calls in probe and analysis.
    for body in captions:
        content = body["messages"][0]["content"]
        assert len([item for item in content if item["type"] == "image_url"]) == 1
        assert all(frame["id"] not in json.dumps(body) for frame in FRAMES)
    assessment = json.loads(payloads[-1]["messages"][-1]["content"])
    assert assessment == {
        "question": "分别观察",
        "observations": [
            {"number": 1, "cid": "34", "actual_time": 10, "text": "红色圆形"},
            {"number": 2, "cid": "34", "actual_time": 30, "text": "蓝色方形"},
        ],
    }
    assert all(frame["id"] not in json.dumps(payloads[-1]) for frame in FRAMES)


@pytest.mark.parametrize("field,changed", [("window", 8192), ("vision", False), ("aliases", ["wrong-model"])])
async def test_runtime_window_projector_and_alias_are_proved_not_assumed(provider, field, changed):
    value, state = provider
    state[field] = changed
    with pytest.raises(ValueError):
        await admit(value)
    assert not value.capabilities()["available"] and not generations(state)


@pytest.mark.parametrize(
    "patch",
    [
        {"passed": False},
        {"context_tokens": 8192},
        {"max_frames": 1},
        {"max_bytes": 1024},
        {"configuration": {"model": "different"}},
    ],
)
async def test_resource_proof_must_match_measured_configuration_and_bounds(provider, patch):
    value, _ = provider
    report = await value.probe(FRAMES, metadata=META, resource_admission=proof(value) | patch)
    assert report["capabilities"]["images"] and not report["capabilities"]["admitted"]


async def test_runtime_change_revokes_prior_admission_before_generation(provider):
    value, state = provider
    await admit(value)
    before = len(generations(state))
    state["window"] *= 2
    with pytest.raises(ValueError, match="变化"):
        await value.analyze_video_evidence({}, META, FRAMES, "观察", deadline=time.time() + 10)
    assert len(generations(state)) == before and not value.capabilities()["admitted"]


@pytest.mark.parametrize(
    "patch",
    [
        {"used_frames": [3]},
        {"used_frames": [0]},
        {"used_frames": [-1]},
        {"used_frames": [True]},
        {"used_frames": [1.0]},
        {"used_frames": ["1"]},
        {"used_frames": [1, 1]},
        {"used_frames": []},
        {"summary": 123},
        {"summary": ""},
        {"sufficient": 1},
        {"limitations": ["缺项"] * 21},
        {"evidence_ids": ["invented"]},
    ],
)
async def test_unknown_refs_missing_map_and_structural_errors_are_rejected(provider, patch):
    value, state = provider
    await admit(value)
    state["result"].update(patch)
    with pytest.raises(ValueError):
        await value.analyze_video_evidence({}, META, FRAMES, "观察", deadline=time.time() + 10)


@pytest.mark.parametrize(
    "summary,per_frame,limitations",
    [
        (
            "背景模糊，但前景红杯清楚，两个画面中杯子都在桌上。",
            {"first": "背景看不清，前景可见桌上的红杯。", "second": "红杯仍在桌上，背景不可辨。"},
            ["无法判断背景墙上的文字；当前问题只问杯子颜色。"],
        ),
        (
            "两帧都显示桌上放着红杯。",
            {"first": "桌上放着红杯。", "second": "桌上放着红杯。"},
            [],
        ),
        (
            "A red cup is visible in the foreground; the background is blurry.",
            {"first": "A red cup on a table.", "second": "A red cup on a table."},
            ["Cannot determine the lettering on the background wall."],
        ),
    ],
)
async def test_local_blur_and_same_scene_observations_preserve_model_assessment(
    provider, summary, per_frame, limitations
):
    value, state = provider
    await admit(value)
    original = {
        "sufficient": True,
        "observations": summary,
        "evidence_ids": ["first", "second"],
        "frame_observations": per_frame,
        "limitations": limitations,
    }
    state["captions"] = copy.deepcopy(per_frame)
    state["result"] = {
        "sufficient": True,
        "summary": summary,
        "used_frames": [1, 2],
        "limitations": limitations,
    }
    result = await value.analyze_video_evidence(
        {}, META, FRAMES, "前景杯子是什么颜色？", deadline=time.time() + 10
    )
    assert {key: result[key] for key in original} == original
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["frames"] == [
        {"id": frame["id"], "cid": frame["cid"], "actual_time": frame["actual_time"]} for frame in FRAMES
    ]
    payload = json.loads(generations(state)[-1].content)
    system = next(message["content"] for message in payload["messages"] if message["role"] == "system")
    assert "围绕当前问题" in system and "limitations" in system
    assert "禁止把综合观察复制到多帧" not in system and "sufficient=false" not in system


async def test_sufficiency_is_retained_as_model_self_assessment_not_a_word_filter(provider):
    value, state = provider
    await admit(value)
    # A self-assessment can itself be wrong. Structural validation preserves it
    # and its original observations; actual image/question semantics need review.
    state["result"]["summary"] = "画面模糊无法判断。"
    result = await value.analyze_video_evidence({}, META, FRAMES, "观察", deadline=time.time() + 10)
    assert result["sufficient"] is True
    assert result["observations"] == "画面模糊无法判断。"


async def test_model_can_report_insufficient_without_inventing_frame_observations(provider):
    value, state = provider
    await admit(value)
    state["result"] = {
        "sufficient": False,
        "summary": "",
        "used_frames": [],
        "limitations": ["画面模糊无法判断"],
    }
    result = await value.analyze_video_evidence({}, META, FRAMES, "观察", deadline=time.time() + 10)
    assert not result["sufficient"] and not result["coverage"]["frames"]


@pytest.mark.parametrize(
    "patch",
    [
        {"id": "second"},
        {"cid": "99"},
        {"actual_time": 45},
        {"actual_time": -1},
        {"actual_time": True},
        {"actual_time": float("nan")},
        {"kind": "cover"},
        {"width": 100000},
        {"image": b"invalid"},
    ],
)
async def test_invalid_inputs_do_not_reach_generation(provider, patch):
    value, state = provider
    await admit(value)
    frames = copy.deepcopy(FRAMES)
    frames[0].update(patch)
    before = len(state["requests"])
    with pytest.raises(ValueError):
        await value.analyze_video_evidence({}, META, frames, "观察", deadline=time.time() + 10)
    assert len(state["requests"]) == before


@pytest.mark.parametrize("mode", ["tool", "length"])
async def test_tool_calls_and_incomplete_generation_are_rejected(provider, mode):
    value, state = provider
    await admit(value)
    if mode == "tool":
        state["message"] = {"content": "{}", "tool_calls": [{"function": {"name": "send_message"}}]}
    else:
        state["finish"] = "length"
    with pytest.raises(ValueError):
        await value.analyze_video_evidence({}, META, FRAMES, "观察", deadline=time.time() + 10)


async def test_round_meter_counts_actual_image_token_probe_and_completion(provider, runtime):
    value, _ = provider
    await admit(value)
    repo = runtime.browsing_repository
    row, _ = repo.create_round("100", runtime.character, BrowsingPolicy().model_dump(), 0, True)
    result = await value.analyze_video_evidence(
        {"id": "task", "round_id": row["id"]}, META, FRAMES, "计量新问题", deadline=time.time() + 10
    )
    assert result["usage"]["including_token_probes"] == {
        "model_calls": 6,
        "input_tokens": 600,
        "output_tokens": 63,
    }
    assert repo.round(row["id"])["budget"]["model_calls"] == 6


async def test_probe_also_accepts_parent_meter_and_budget_blocks_second_request(provider, runtime):
    value, state = provider
    repo = runtime.browsing_repository
    config = BrowsingPolicy(max_model_calls=1).model_dump()
    row, _ = repo.create_round("100", runtime.character, config, 0, True)
    with pytest.raises(ValueError, match="预算"):
        await value.probe(
            FRAMES, metadata=META, resource_admission=proof(value), task={"round_id": row["id"]}
        )
    assert len(generations(state)) == 1 and not value.capabilities()["admitted"]
    assert repo.round(row["id"])["budget"]["input_tokens"] == 100


async def test_shared_generation_lock_and_owned_cancellation_release_everything(provider, runtime):
    value, state = provider
    await admit(value)
    state["block"] = True
    before = len(generations(state))
    async with runtime.generation_lock:
        task = asyncio.create_task(
            value.analyze_video_evidence({"id": "owned"}, META, FRAMES, "取消测试", deadline=time.time() + 10)
        )
        await asyncio.sleep(0.02)
        assert len(generations(state)) == before
    await state["started"].wait()
    await value.cancel("owned")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["cancelled"].is_set() and not value._active and not runtime.generation_lock.locked()


async def test_deadline_and_close_cancel_owned_http_only(provider, runtime):
    value, state = provider
    await admit(value)
    state["block"] = True
    with pytest.raises(TimeoutError):
        await value.analyze_video_evidence(
            {"id": "timeout"}, META, FRAMES, "期限测试", deadline=time.time() + 0.03
        )
    assert state["cancelled"].is_set() and not value._active and not runtime.generation_lock.locked()
    await value.configure({"enabled": False})
    assert value.api is None and not value.capabilities()["available"]
    assert runtime.llm is not None


async def test_cancel_signal_during_http_collects_the_owned_generation(provider, runtime):
    value, state = provider
    await admit(value)
    state["block"] = True
    signal = asyncio.Event()
    task = asyncio.create_task(
        value.analyze_video_evidence(
            {"id": "signal", "cancel_signal": signal}, META, FRAMES, "信号取消", deadline=time.time() + 10
        )
    )
    await state["started"].wait()
    signal.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["cancelled"].is_set() and not value._active and not runtime.generation_lock.locked()


async def test_past_deadline_never_reaches_health_or_generation(provider):
    value, state = provider
    await admit(value)
    before = len(state["requests"])
    with pytest.raises(TimeoutError):
        await value.analyze_video_evidence({}, META, FRAMES, "过期", deadline=time.time() - 1)
    assert len(state["requests"]) == before


@pytest.mark.parametrize(
    "provider",
    [
        {"enable_thinking": thinking, "timeout_seconds": 300, "max_output_tokens": 4096}
        for thinking in (None, True, False)
    ],
    indirect=True,
)
async def test_public_configuration_controls_outbound_thinking_timeout_and_budget(provider):
    value, state = provider
    observed = await value.probe(FRAMES, metadata=META, resource_admission=proof(value))
    assert observed["capabilities"]["admitted"]
    assert observed["capabilities"]["timeout_seconds"] == 300
    result = await value.analyze_video_evidence({}, META, FRAMES, "分别观察杯子", deadline=time.time() + 400)
    assert result["evidence_ids"] == ["first", "second"]
    posts = generations(state)
    assert [json.loads(request.content)["max_tokens"] for request in posts] == [1, 4096] * 6
    for request in posts:
        assert request.extensions["timeout"]["read"] == 300
        body = json.loads(request.content)
        assert "tools" not in body
        if value.config.enable_thinking is None:
            assert "chat_template_kwargs" not in body
        else:
            assert body["chat_template_kwargs"] == {"enable_thinking": value.config.enable_thinking}


def test_vision_defaults_remain_compatible_and_explicit_bounds_are_validated():
    defaults = VideoVisionConfig()
    assert defaults.timeout_seconds == 60 and defaults.enable_thinking is None
    assert defaults.max_output_tokens == 2048
    assert defaults.sampling.temperature == 0.2
    assert all(value is None for key, value in defaults.sampling.model_dump().items() if key != "temperature")
    assert VideoVisionConfig(timeout_seconds=600).timeout_seconds == 600
    for patch in ({"timeout_seconds": 601}, {"timeout_seconds": 0}, {"enable_thinking": "on"}):
        with pytest.raises(ValueError):
            VideoVisionConfig(**patch)


@pytest.mark.parametrize(
    "provider", [{"enable_thinking": True, "timeout_seconds": 300, "max_output_tokens": 4096}], indirect=True
)
async def test_long_transport_timeout_keeps_parent_budget_and_cancel_deadline(provider, runtime):
    value, state = provider
    repo = runtime.browsing_repository
    policy = BrowsingPolicy(max_model_calls=1, decision_max_tokens=4096).model_dump()
    row, _ = repo.create_round("isolated", runtime.character, policy, 0, True)
    with pytest.raises(ValueError, match="预算"):
        await value.probe(
            FRAMES, metadata=META, resource_admission=proof(value), task={"round_id": row["id"]}
        )
    assert len(generations(state)) == 1
    assert not value.capabilities()["admitted"]
    await admit(value)
    state["block"] = True
    with pytest.raises(TimeoutError):
        await value.analyze_video_evidence({}, META, FRAMES, "短父期限", deadline=time.time() + 0.03)
    assert state["cancelled"].is_set() and not value._active and not runtime.generation_lock.locked()


@pytest.mark.parametrize("provider", [{"enable_thinking": True}], indirect=True)
async def test_resource_proof_for_different_thinking_setting_does_not_admit(provider):
    value, _ = provider
    resource = proof(value)
    resource["configuration"] = resource["configuration"] | {"enable_thinking": False}
    observed = await value.probe(FRAMES, metadata=META, resource_admission=resource)
    assert observed["capabilities"]["images"] and not observed["capabilities"]["admitted"]


@pytest.mark.parametrize(
    "provider",
    [
        {},
        {"sampling": {"temperature": None}},
        {
            "enable_thinking": True,
            "max_output_tokens": 8192,
            "sampling": {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
                "repeat_penalty": 1.0,
                "seed": 42,
                "reasoning_budget_tokens": 1024,
            },
        },
    ],
    indirect=True,
)
async def test_video_sampling_public_probe_analysis_and_compatible_defaults(provider):
    value, state = provider
    observed = await value.probe(FRAMES, metadata=META, resource_admission=proof(value))
    assert observed["capabilities"]["admitted"]
    await value.analyze_video_evidence({}, META, FRAMES, "观察这些杯子", deadline=time.time() + 10)
    expected = value.config.sampling.model_dump(exclude_none=True)
    for request in generations(state):
        body = json.loads(request.content)
        probe = body["max_tokens"] == 1
        for key in type(value.config.sampling).model_fields:
            if key == "temperature" and probe or key not in expected:
                assert key not in body
            else:
                assert body[key] == expected[key]
        assert body["max_tokens"] == (1 if probe else value.config.max_output_tokens)
        assert body.get("chat_template_kwargs") == (
            None
            if value.config.enable_thinking is None
            else {"enable_thinking": value.config.enable_thinking}
        )
    assert value.identity()["sampling"] == value.config.sampling.model_dump()


async def test_sampling_budget_change_invalidates_resource_identity(provider):
    value, _ = provider
    original_version = value.version
    resource = proof(value)
    resource["configuration"]["sampling"]["reasoning_budget_tokens"] = 1024
    observed = await value.probe(FRAMES, metadata=META, resource_admission=resource)
    assert not observed["capabilities"]["admitted"] and value.version == original_version


async def test_assessment_numbers_map_exact_captions_without_model_source_ids(provider):
    value, state = provider
    await admit(value)
    state["result"]["used_frames"] = [2, 1]
    result = await value.analyze_video_evidence({}, META, FRAMES, "比较", deadline=time.time() + 10)
    assert result["evidence_ids"] == ["second", "first"]
    assert result["frame_observations"] == {"second": "蓝色方形", "first": "红色圆形"}
    assert [row["frame_id"] for row in result["usage"]["stages"][:-1]] == ["first", "second"]
    assert all(row["elapsed_ms"] >= 0 for row in result["usage"]["stages"])


@pytest.mark.parametrize(
    "question", ["杯子是什么颜色？", "请读出牌子上的字幕。", "20秒后杯子的颜色是否变化？"]
)
async def test_each_caption_request_receives_current_question_as_its_observation_focus(provider, question):
    value, state = provider
    await admit(value)
    before = len(generations(state))
    await value.analyze_video_evidence({}, META, FRAMES, question, deadline=time.time() + 10)
    bodies = [json.loads(request.content) for request in generations(state)[before:]]
    captions = [body for body in bodies if body["max_tokens"] != 1 and "response_format" not in body]
    assert len(captions) == 2
    for frame, body in zip(FRAMES, captions, strict=True):
        content = body["messages"][0]["content"]
        texts = [item["text"] for item in content if item["type"] == "text"]
        assert any(question in text and "人物、物品和动作" in text for text in texts)
        assert any(f"{frame['actual_time']} 秒" in text and f"分P {frame['cid']}" in text for text in texts)
        assert all(frame["id"] not in json.dumps(body) for frame in FRAMES)


async def test_source_snapshot_stays_bound_when_callers_mutate_frames_during_http(provider):
    value, state = provider
    await admit(value)
    frames = copy.deepcopy(FRAMES)

    def mutate(_):
        frames[0].update(id="forged", cid="other", actual_time=999)

    state["after_generation"] = mutate
    result = await value.analyze_video_evidence({}, META, frames, "观察", deadline=time.time() + 10)
    assert result["evidence_ids"] == ["first", "second"]
    assert result["coverage"]["frames"][0] == {"id": "first", "cid": "34", "actual_time": 10}


async def test_old_nested_json_protocol_proof_does_not_admit_new_call_graph(provider):
    value, _ = provider
    resource = proof(value)
    assert resource["configuration"].pop("protocol") == "sequential_captions_indexed_assessment_v2"
    observed = await value.probe(FRAMES, metadata=META, resource_admission=resource)
    assert observed["capabilities"]["images"] and not observed["capabilities"]["admitted"]


@pytest.mark.parametrize("stage", ["second_caption", "assessment"])
async def test_cancelling_later_stage_collects_tasks_and_never_finishes_pipeline(provider, runtime, stage):
    value, state = provider
    await admit(value)
    if stage == "second_caption":
        state["block_at_caption"] = state["caption_requests"] + 2
    else:
        state["block_assessment"] = True
    task = asyncio.create_task(
        value.analyze_video_evidence({"id": "later"}, META, FRAMES, "稍后取消", deadline=time.time() + 10)
    )
    await asyncio.wait_for(state["started"].wait(), timeout=2)
    await value.cancel("later")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["cancelled"].is_set() and not value._active and not runtime.generation_lock.locked()


async def test_assessment_deadline_cancels_after_captions_without_partial_return(provider, runtime):
    value, state = provider
    await admit(value)
    state["block_assessment"] = True
    with pytest.raises(TimeoutError):
        await value.analyze_video_evidence({}, META, FRAMES, "限时汇总", deadline=time.time() + 0.05)
    assert state["started"].is_set() and state["cancelled"].is_set()
    assert not value._active and not runtime.generation_lock.locked()


@pytest.mark.parametrize("stage", ["caption", "assessment"])
async def test_runtime_switch_between_stages_or_before_return_revokes_admission(provider, stage):
    value, state = provider
    await admit(value)

    def switch(body):
        if ("response_format" in body) == (stage == "assessment"):
            state["window"] *= 2

    state["after_generation"] = switch
    before = len(generations(state))
    with pytest.raises(ValueError, match="变化"):
        await value.analyze_video_evidence({}, META, FRAMES, "模型切换", deadline=time.time() + 10)
    assert not value.capabilities()["admitted"]
    if stage == "caption":
        assert len(generations(state)) == before + 2


async def test_endpoint_switch_with_identical_model_properties_revokes_admission(provider):
    value, state = provider
    await admit(value)
    value.api.client.base_url = "http://127.0.0.1:18081/v1/"
    before = len(generations(state))
    with pytest.raises(ValueError, match="变化"):
        await value.analyze_video_evidence({}, META, FRAMES, "端点切换", deadline=time.time() + 10)
    assert len(generations(state)) == before and not value.capabilities()["admitted"]


async def test_public_probe_and_analysis_share_real_parent_meter_for_all_stages(provider, runtime):
    value, state = provider
    repo = runtime.browsing_repository
    policy = BrowsingPolicy(
        max_model_calls=16, max_input_tokens=1048576, max_output_tokens=65536
    ).model_dump()
    row, _ = repo.create_round("100", runtime.character, policy, 0, True)
    task = {"id": "metered", "round_id": row["id"]}
    observed = await value.probe(FRAMES, metadata=META, resource_admission=proof(value), task=task)
    result = await value.analyze_video_evidence(task, META, FRAMES, "新的问题", deadline=time.time() + 10)
    assert len(generations(state)) == repo.round(row["id"])["budget"]["model_calls"] == 12
    assert repo.round(row["id"])["budget"]["input_tokens"] == 1200
    assert repo.round(row["id"])["budget"]["output_tokens"] == 126
    for output in (observed["result"], result):
        assert len(output["usage"]["stages"]) == 3
        for key in ("model_calls", "input_tokens", "output_tokens"):
            assert (
                sum(stage["including_token_probes"][key] for stage in output["usage"]["stages"])
                == output["usage"]["including_token_probes"][key]
            )


async def test_tight_budget_stops_before_assessment_post_without_overrun(provider, runtime):
    value, state = provider
    repo = runtime.browsing_repository
    policy = BrowsingPolicy(max_model_calls=4).model_dump()
    row, _ = repo.create_round("100", runtime.character, policy, 0, True)
    with pytest.raises(ValueError, match="预算"):
        await value.probe(
            FRAMES, metadata=META, resource_admission=proof(value), task={"round_id": row["id"]}
        )
    assert len(generations(state)) == repo.round(row["id"])["budget"]["model_calls"] == 4
    assert all("response_format" not in json.loads(request.content) for request in generations(state))
    assert not value.capabilities()["admitted"]


@pytest.mark.parametrize("caption", ["", "x" * 4001])
async def test_empty_or_oversize_caption_never_reaches_assessment(provider, caption):
    value, state = provider
    state["captions"]["first"] = caption
    with pytest.raises(ValueError):
        await admit(value)
    assert all("response_format" not in json.loads(request.content) for request in generations(state))
