import asyncio
import hashlib
import io
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from test_distributed_modes import distributed as distributed

from tuber_studio import video_vision_service
from tuber_studio.config import atomic_json
from tuber_studio.inference_profiles import GEMMA_KEY
from tuber_studio.video_vision import ApiVideoEvidenceProvider


@pytest.fixture
async def visual_runtime(distributed):
    runtime, state = distributed
    provider = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing = SimpleNamespace(visual_provider=provider)
    state.update(capability_calls=0, memory_bytes=1024 * 2**20, runtime_sha=provider.config.runtime_revision)

    async def http(request):
        if request.url.path == "/capabilities":
            state["capability_calls"] += 1
            if state.get("fail_capabilities"):
                return httpx.Response(503, json={"detail": "node unavailable"})
            return httpx.Response(
                200,
                json={
                    "protocol": "tuber-inference-node",
                    "protocol_version": 1,
                    "mode": "text",
                    "profiles": {
                        GEMMA_KEY: {
                            "loaded": True,
                            "model_sha256": provider.config.model_revision,
                            "projector_sha256": provider.config.projector_revision,
                            "runtime_sha256": state["runtime_sha"],
                            **state.get("identifier_overrides", {}),
                        }
                    },
                    "resource": {
                        "available_bytes": state["memory_bytes"],
                        "swap_used_bytes": state.get("swap", 0),
                    },
                },
            )
        if request.url.path.endswith("/chat/completions"):
            if state.get("increase_swap_after_generation"):
                state["swap"] = state["increase_swap_after_generation"]
            payload = json.loads(request.content)
            if payload.get("response_format"):
                content = json.dumps(
                    {
                        "sufficient": True,
                        "used_frames": [1, 2, 3],
                        "summary": "蓝发角色在三张抽样图片中出现。",
                        "limitations": [],
                    }
                )
            else:
                content = "蓝发角色，白色发饰和深色衣裙。"
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": min(20, payload["max_tokens"])},
                },
            )
        return None

    state["http_handler"] = http
    try:
        yield runtime, provider, state
    finally:
        await runtime.browsing.visual_provider.close()
        if runtime.browsing.visual_provider is not provider:
            await provider.close()


def observer_factory(state):
    state["observer_clients"], state["observer_configs"] = [], []

    async def mark(request):
        request.extensions["probe_observer"] = True

    def factory(**kwargs):
        state["observer_configs"].append(kwargs)
        client = httpx.AsyncClient(**kwargs, event_hooks={"request": [mark]})
        state["observer_clients"].append(client)
        return client

    return factory


async def test_probe_owns_short_connections_and_records_a_recovered_disconnect(visual_runtime):
    runtime, provider, state = visual_runtime
    factory, original = observer_factory(state), state["http_handler"]
    failed = False

    async def fault(request):
        nonlocal failed
        if request.extensions.get("probe_observer") and not failed:
            failed = True
            raise httpx.RemoteProtocolError(
                "Disconnected " + runtime.settings.inference_token, request=request
            )
        return await original(request)

    state["http_handler"] = fault
    result = await video_vision_service.probe_visual(runtime, client_factory=factory)
    assert result["capabilities"]["admitted"]
    measurement = result["measurement"]
    assert measurement["request_retries"] == measurement["request_error_count"] == 1
    assert measurement["request_errors"][0]["error"] == "RemoteProtocolError"
    assert runtime.settings.inference_token not in json.dumps(measurement)
    assert "[redacted]" in measurement["request_errors"][0]["detail"]
    options = state["observer_configs"][0]
    assert options["trust_env"] is False and options["follow_redirects"] is False
    assert options["limits"].max_keepalive_connections == 0
    assert options["timeout"].read == options["timeout"].connect == 5
    assert options["headers"]["Connection"] == "close"
    assert options["headers"]["X-Tuber-Inference-Key"] == runtime.settings.inference_token
    assert state["observer_clients"][0].is_closed
    assert not runtime.inference.client.is_closed and not provider.api.client.is_closed
    generations = [
        json.loads(request.content)
        for request in state["requests"]
        if request.url.path.endswith("/chat/completions") and json.loads(request.content)["max_tokens"] > 1
    ]
    assert len(generations) == 4


@pytest.mark.parametrize("error_type", [httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError])
async def test_capability_transport_failure_has_at_most_one_recorded_retry(visual_runtime, error_type):
    runtime, provider, state = visual_runtime
    factory = observer_factory(state)
    attempts = 0

    async def fault(request):
        nonlocal attempts
        attempts += 1
        raise error_type("temporary link failure", request=request)

    state["http_handler"] = fault
    async with video_vision_service._probe_client(runtime, factory) as client:
        sampler = video_vision_service.ProbeSampler(client, provider)
        with pytest.raises(error_type):
            await sampler.sample()
        measured = sampler.measurement()
    assert attempts == 2 and measured["request_retries"] == 1 and measured["request_error_count"] == 2
    assert [detail["retry"] for detail in measured["request_errors"]] == [True, False]
    assert state["observer_clients"][0].is_closed


async def test_read_timeout_is_not_retried_and_cannot_admit_the_probe(visual_runtime):
    runtime, provider, state = visual_runtime
    factory = observer_factory(state)
    attempts = 0

    async def fault(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("still waiting at five seconds", request=request)

    state["http_handler"] = fault
    with pytest.raises(httpx.ReadTimeout):
        await video_vision_service.probe_visual(runtime, client_factory=factory)
    assert attempts == 1 and not provider.capabilities()["admitted"]
    proof = json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())["proof"]
    assert proof["passed"] is False and proof["measurement"]["request_retries"] == 0
    assert proof["measurement"]["request_errors"][0]["error"] == "ReadTimeout"
    assert state["observer_clients"][0].is_closed


async def test_model_post_failure_is_not_retried_by_the_get_observer(visual_runtime):
    runtime, provider, state = visual_runtime
    factory, original = observer_factory(state), state["http_handler"]
    generations = 0

    async def fault(request):
        nonlocal generations
        if request.url.path.endswith("/chat/completions") and json.loads(request.content)["max_tokens"] > 1:
            generations += 1
            raise httpx.RemoteProtocolError("model connection ended", request=request)
        return await original(request)

    state["http_handler"] = fault
    with pytest.raises(httpx.RemoteProtocolError):
        await video_vision_service.probe_visual(runtime, client_factory=factory)
    assert generations == 1 and not provider.capabilities()["admitted"]
    proof = json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())["proof"]
    assert proof["measurement"]["request_retries"] == 0
    assert state["observer_clients"][0].is_closed


async def test_resource_sampling_covers_generation_and_records_monotonic_intervals(visual_runtime):
    runtime, _, state = visual_runtime
    factory, original = observer_factory(state), state["http_handler"]
    generation_times, observation_times = [], []

    async def slow_generation(request):
        if request.extensions.get("probe_observer"):
            observation_times.append(time.monotonic())
        if request.url.path.endswith("/chat/completions") and json.loads(request.content)["max_tokens"] > 1:
            start = time.monotonic()
            await asyncio.sleep(0.35)
            generation_times.append((start, time.monotonic()))
        return await original(request)

    state["http_handler"] = slow_generation
    result = await video_vision_service.probe_visual(runtime, client_factory=factory)
    measured = result["measurement"]
    assert result["capabilities"]["admitted"] and len(generation_times) == 4
    assert measured["first_sample_monotonic"] <= generation_times[0][0]
    assert measured["last_sample_monotonic"] >= generation_times[-1][1]
    assert any(generation_times[0][0] < moment < generation_times[-1][1] for moment in observation_times)
    assert len(measured["sample_intervals_seconds"]) == measured["sample_count"] - 1
    assert all(interval >= 0 for interval in measured["sample_intervals_seconds"])
    assert measured["max_sample_interval_seconds"] >= max(measured["sample_intervals_seconds"]) - 0.000001
    assert state["observer_clients"][0].is_closed


async def test_cancelling_generation_closes_only_the_owned_probe_client(visual_runtime):
    runtime, _, state = visual_runtime
    factory, original = observer_factory(state), state["http_handler"]
    entered = asyncio.Event()

    async def blocked(request):
        if request.url.path.endswith("/chat/completions") and json.loads(request.content)["max_tokens"] > 1:
            entered.set()
            await asyncio.Event().wait()
        return await original(request)

    state["http_handler"] = blocked
    task = asyncio.create_task(video_vision_service.probe_visual(runtime, client_factory=factory))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["observer_clients"][0].is_closed
        assert not runtime.inference.client.is_closed and not runtime.inference.mode_lock.locked()
        proof = json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())["proof"]
        assert proof["passed"] is False and proof["failure_type"] == "CancelledError"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_probe_persists_measured_resource_proof_only_after_success(visual_runtime):
    runtime, provider, state = visual_runtime
    result = await video_vision_service.probe_visual(runtime)
    assert result["capabilities"]["available"] and result["capabilities"]["admitted"]
    assert (
        state["capability_calls"] >= 2
        and result["measurement"]["min_available_bytes"] == state["memory_bytes"]
    )
    saved = json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())
    proof = saved["proof"]
    assert proof["passed"] is True and proof["configuration"] == provider.identity()
    assert proof["input_boundaries"] == {
        "frames": 3,
        "max_pixels": 1048576,
        "total_bytes": 1048576,
        "upscale": "nearest",
        "byte_padding": "valid_PNG_ancillary_text",
        "quality_acceptance": False,
    }
    generated = [
        json.loads(request.content)
        for request in state["requests"]
        if request.url.path.endswith("/chat/completions")
    ]
    assert generated and any(payload["max_tokens"] == 8192 for payload in generated)
    assert all(payload["chat_template_kwargs"]["enable_thinking"] is True for payload in generated)


async def test_successful_admission_can_be_restored_with_matching_actual_node_identities(visual_runtime):
    runtime, provider, _ = visual_runtime
    await video_vision_service.probe_visual(runtime)
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    await video_vision_service.restore_admission(runtime)
    assert fresh.capabilities()["admitted"] and fresh._admission["configuration"] == provider.identity()


@pytest.mark.parametrize("field", ["model_revision", "projector_revision", "runtime_profile"])
async def test_restore_rejects_proof_for_a_different_provider_configuration(visual_runtime, field):
    runtime, _, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    config = runtime.settings.data["video_vision"] | {field: "new-deployed-model"}
    fresh = ApiVideoEvidenceProvider(runtime, config, api_key=runtime.settings.inference_token)
    runtime.browsing.visual_provider = fresh
    request_count = len(state["requests"])

    await video_vision_service.restore_admission(runtime)

    assert not fresh.capabilities()["admitted"]
    assert fresh._probe is None and fresh._admission is None
    assert len(state["requests"]) == request_count


@pytest.mark.parametrize("field", ["model_path", "build_info", "context_tokens"])
async def test_restore_rejects_changed_normalized_service_properties(visual_runtime, field):
    runtime, provider, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    expected, original = provider._probe["properties"], state["http_handler"]
    changes = {
        "model_path": "/runtime/models/authentic-dora/model.gguf",
        "build_info": "new-engine-build",
        "context_tokens": expected["context_tokens"] + 4096,
    }
    current = expected | {field: changes[field]}

    async def changed_properties(request):
        if request.url.path.endswith("/props"):
            return httpx.Response(
                200,
                json={
                    "model_alias": current["model_alias"],
                    "default_generation_settings": {"n_ctx": current["context_tokens"]},
                    "modalities": current["modalities"],
                    "model_path": current["model_path"],
                    "build_info": current["build_info"],
                },
            )
        return await original(request)

    state["http_handler"] = changed_properties
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    request_count = len(state["requests"])

    await video_vision_service.restore_admission(runtime)

    assert not fresh.capabilities()["admitted"]
    assert fresh._probe is None and fresh._admission is None
    assert all(request.method == "GET" for request in state["requests"][request_count:])


async def test_inflight_restore_cannot_revive_proof_invalidated_by_a_new_failed_probe(
    visual_runtime, monkeypatch
):
    runtime, provider, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    original = state["http_handler"]
    entered, release = asyncio.Event(), asyncio.Event()
    held = False

    async def delayed_properties(request):
        nonlocal held
        if request.url.path.endswith("/props") and not held:
            held = True
            entered.set()
            await release.wait()
        return await original(request)

    state["http_handler"] = delayed_properties
    restoring = asyncio.create_task(video_vision_service.restore_admission(runtime))
    try:
        await asyncio.wait_for(entered.wait(), 2)

        def fail_preparation(*args):
            raise ValueError("new probe preparation failed")

        monkeypatch.setattr(video_vision_service, "boundary_frames", fail_preparation)
        with pytest.raises(ValueError, match="new probe preparation"):
            await video_vision_service.probe_visual(runtime)
        assert not runtime.inference.mode_lock.locked() and not provider.capabilities()["admitted"]
        release.set()
        await restoring
        assert provider._probe is None and not provider.capabilities()["admitted"]
        saved = json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())
        assert saved["proof"]["passed"] is False
    finally:
        release.set()
        restoring.cancel()
        await asyncio.gather(restoring, return_exceptions=True)


async def test_admission_restore_skips_busy_probe_without_waiting_or_calling_the_node(visual_runtime):
    runtime, _, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    request_count = len(state["requests"])
    async with runtime.inference.mode_lock:
        await asyncio.wait_for(video_vision_service.restore_admission(runtime), 0.5)
    assert len(state["requests"]) == request_count


async def test_inflight_restore_cannot_apply_after_a_complete_mode_roundtrip(visual_runtime):
    runtime, provider, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    provider._probe = provider._admission = None
    original, held = state["http_handler"], False
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_properties(request):
        nonlocal held
        if request.url.path.endswith("/props") and not held:
            held = True
            entered.set()
            await release.wait()
        return await original(request)

    state["http_handler"] = delayed_properties
    restoring = asyncio.create_task(video_vision_service.restore_admission(runtime))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.inference.set_mode("live")
        await runtime.inference.set_mode("text")
        assert runtime.browsing.visual_provider is provider and not runtime.inference.mode_lock.locked()
        release.set()
        await restoring
        assert provider._probe is None and not provider.capabilities()["admitted"]
        # A fresh observation of the same still-valid proof can admit normally.
        await video_vision_service.restore_admission(runtime)
        assert provider.capabilities()["admitted"]
    finally:
        release.set()
        restoring.cancel()
        await asyncio.gather(restoring, return_exceptions=True)


@pytest.mark.parametrize("field", ["model_sha256", "projector_sha256", "runtime_sha256"])
async def test_restore_accepts_changed_reported_digest_when_alias_and_properties_match(visual_runtime, field):
    runtime, _, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    state["identifier_overrides"] = {field: "f" * 64}
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    request_count = len(state["requests"])
    await video_vision_service.restore_admission(runtime)
    # Restore checks actual capabilities; reported byte digests are no longer a gate.
    assert fresh.capabilities()["admitted"] and fresh._probe is not None
    assert all(request.method == "GET" for request in state["requests"][request_count:])


@pytest.mark.parametrize("failure", ["unloaded", "context", "vision", "alias"])
async def test_restore_still_requires_a_loaded_capable_visual_model(visual_runtime, failure):
    runtime, _, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    original = state["http_handler"]
    if failure == "unloaded":
        state["identifier_overrides"] = {"loaded": False}

    async def incompatible_properties(request):
        if request.url.path.endswith("/props"):
            return httpx.Response(
                200,
                json={
                    "model_alias": "Wrong-Model" if failure == "alias" else "Gemma4-E2B",
                    "default_generation_settings": {"n_ctx": 4096 if failure == "context" else 131072},
                    "modalities": {"vision": failure != "vision"},
                },
            )
        return await original(request)

    state["http_handler"] = incompatible_properties
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    if failure == "unloaded":
        await video_vision_service.restore_admission(runtime)
    else:
        with pytest.raises(ValueError):
            await video_vision_service.restore_admission(runtime)
    assert not fresh.capabilities()["admitted"] and fresh._probe is None


async def test_successful_generation_does_not_admit_when_proof_save_fails(visual_runtime, monkeypatch):
    runtime, provider, _ = visual_runtime
    writes = 0

    def fail_final(path, value, *, private=False):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("proof save failed")
        atomic_json(path, value, private=private)

    monkeypatch.setattr(video_vision_service, "atomic_json", fail_final)
    with pytest.raises(OSError, match="proof save"):
        await video_vision_service.probe_visual(runtime)
    assert writes == 2 and not provider.capabilities()["admitted"]
    saved = json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())
    assert saved["proof"]["passed"] is False


async def test_preparation_failure_invalidates_old_proof_and_cannot_reappear_on_restart(
    visual_runtime, monkeypatch
):
    runtime, provider, _ = visual_runtime
    await video_vision_service.probe_visual(runtime)

    def fail(*args):
        raise ValueError("input preparation failed")

    monkeypatch.setattr(video_vision_service, "boundary_frames", fail)
    with pytest.raises(ValueError, match="preparation"):
        await video_vision_service.probe_visual(runtime)
    assert not provider.capabilities()["admitted"]
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    await video_vision_service.restore_admission(runtime)
    assert not fresh.capabilities()["admitted"]
    assert (
        json.loads((runtime.settings.runtime / "video-vision-admission.json").read_text())["proof"]["passed"]
        is False
    )


async def test_initial_node_failure_invalidates_previous_admission_before_reading_a_new_sample(
    visual_runtime,
):
    runtime, provider, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    state["fail_capabilities"] = True
    with pytest.raises(httpx.HTTPStatusError):
        await video_vision_service.probe_visual(runtime)
    assert not provider.capabilities()["admitted"]
    state["fail_capabilities"] = False
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    await video_vision_service.restore_admission(runtime)
    assert not fresh.capabilities()["admitted"]


async def test_insufficient_measured_memory_cannot_revive_a_previous_pass(visual_runtime):
    runtime, provider, state = visual_runtime
    await video_vision_service.probe_visual(runtime)
    state["memory_bytes"] = 300 * 2**20
    result = await video_vision_service.probe_visual(runtime)
    assert result["measurement"]["min_available_bytes"] == 300 * 2**20
    assert not result["capabilities"]["admitted"] and provider._admission is None
    fresh = ApiVideoEvidenceProvider(
        runtime, runtime.settings.data["video_vision"], api_key=runtime.settings.inference_token
    )
    runtime.browsing.visual_provider = fresh
    await video_vision_service.restore_admission(runtime)
    assert not fresh.capabilities()["admitted"]


def test_boundary_images_have_exact_declared_pixels_bytes_and_are_actually_decodable(settings):
    Image = pytest.importorskip("PIL.Image")
    from tuber_studio.deployment_profiles import business_configuration
    from tuber_studio.video_vision import VideoVisionConfig

    config = VideoVisionConfig.model_validate(
        business_configuration(settings.data, "http://jetson.local:9012")["video_vision"]
    )
    frames = video_vision_service.boundary_frames(settings, config)
    assert len(frames) == config.max_frames == 3
    assert sum(len(frame["image"]) for frame in frames) == config.max_bytes
    pixels = []
    for frame in frames:
        with Image.open(io.BytesIO(frame["image"])) as image:
            image.verify()
        with Image.open(io.BytesIO(frame["image"])) as image:
            image.load()
            assert image.size == (1024, 1024)
            assert image.width * image.height == config.max_pixels
            pixels.append(hashlib.sha256(image.convert("RGB").tobytes()).hexdigest())
    assert len(set(pixels)) >= 2
