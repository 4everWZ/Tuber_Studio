import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tuber_studio.config import Settings
from tuber_studio.deployment_profiles import business_configuration
from tuber_studio.inference_profiles import GEMMA_ARTIFACTS, GEMMA_ENGINE_SHA256, GEMMA_KEY
from tuber_studio.providers import LlamaCppProvider
from tuber_studio.runtime import Runtime
from tuber_studio.runtime_modes import apply_mode_profile

NODE_URL = "http://jetson.local:9012"


@pytest.fixture
async def distributed(settings, store, monkeypatch):
    settings.data = business_configuration(settings.data, NODE_URL)
    settings.save_public(settings.data)
    state = {
        "mode": "text",
        "failure": None,
        "requests": [],
        "clients": [],
        "block_props": False,
        "props_entered": asyncio.Event(),
    }

    def observed():
        selected = "llm2b" if state["mode"] == "live" else "llmgemma4e2b"
        return {
            "mode": state["mode"],
            "selected_provider": selected,
            "active_provider": selected,
            "state": "active",
            "busy": False,
            "model": "MiniCPM5-2B" if state["mode"] == "live" else "Gemma4-E2B",
        }

    async def handle(request):
        state["requests"].append(request)
        if state.get("http_handler"):
            response = await state["http_handler"](request)
            if response is not None:
                return response
        if request.url.path == "/capabilities":
            return httpx.Response(
                200,
                json={
                    "protocol": "tuber-inference-node",
                    "protocol_version": 1,
                    "mode": state["mode"],
                    "profiles": {
                        GEMMA_KEY: {
                            "loaded": True,
                            "model_sha256": GEMMA_ARTIFACTS["model"]["sha256"],
                            "projector_sha256": GEMMA_ARTIFACTS["vision_only_projector"]["sha256"],
                            "runtime_sha256": GEMMA_ENGINE_SHA256,
                        }
                    },
                    "resource": {"available_bytes": 1024 * 2**20, "swap_used_bytes": 0},
                },
            )
        if request.url.path == "/mode":
            mode = json.loads(request.content)["mode"]
            if state["failure"] == "node" and mode == "live":
                return httpx.Response(503, json={"detail": "model load failed"})
            state["mode"] = mode
            return httpx.Response(200, json=observed())
        if request.url.path == "/health":
            return httpx.Response(200, json=observed())
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("/props"):
            if state["block_props"]:
                state["props_entered"].set()
                await asyncio.Event().wait()
            alias = (
                "MiniCPM5-2B"
                if state["mode"] == "live" or request.url.host == "outside.invalid"
                else "Gemma4-E2B"
            )
            if state["mode"] == "live" and state["failure"] == "alias":
                alias = "Wrong-Model"
            window = 65536 if state["mode"] == "live" and state["failure"] == "capacity" else 131072
            return httpx.Response(
                200,
                json={
                    "model_alias": alias,
                    "default_generation_settings": {"n_ctx": window},
                    "modalities": {"vision": alias == "Gemma4-E2B"},
                },
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "Gemma4-E2B"}]})
        if request.url.path.endswith("/docs"):
            return httpx.Response(
                200, json={"status": "ready", "backend": "genie_onnx_int8", "bundle_identity": "0" * 64}
            )
        return httpx.Response(404)

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        value = original(*args, **kwargs)
        state["clients"].append(value)
        return value

    monkeypatch.setattr(httpx, "AsyncClient", client)
    runtime = Runtime(settings, store)
    await apply_mode_profile(runtime, "text")
    await runtime.refresh_inference()
    state["requests"].clear()
    try:
        yield runtime, state
    finally:
        await runtime.close()


async def test_switch_rebinds_real_clients_and_context_without_changing_business_data(distributed):
    runtime, state = distributed
    runtime.store.set("mode-persistence", {"user_id": "platform-user", "affinity": 77})
    old = runtime.llm
    runtime.context.last_key = "old-model-context"
    result = await runtime.set_work_mode("live")
    assert result["mode"] == "live" and state["mode"] == "live"
    assert isinstance(runtime.llm, LlamaCppProvider) and runtime.llm is not old
    assert runtime.llm.config["model"] == "MiniCPM5-2B"
    assert runtime.context.provider is runtime.llm and runtime.context.last_key is None
    assert runtime.settings.data["vision"]["kind"] == "disabled"
    await asyncio.gather(*runtime.inference.cleanup_tasks, return_exceptions=True)
    assert old.client.is_closed
    await runtime.set_work_mode("text")
    assert runtime.llm.config["model"] == "Gemma4-E2B" and runtime.llm.server_tokens == 131072
    assert runtime.settings.data["llm"]["operations_max_tokens"] == 8192
    assert runtime.settings.data["llm"]["enable_thinking"] is True
    assert "reasoning_budget_tokens" not in runtime.settings.data["llm"]
    assert runtime.settings.data["vision"]["model"] == "Gemma4-E2B"
    assert runtime.store.get("mode-persistence") == {"user_id": "platform-user", "affinity": 77}
    persisted = json.loads(runtime.settings.path.read_text())
    assert persisted["inference_broker"]["mode"] == "text" and persisted["llm"]["model"] == "Gemma4-E2B"
    assert all(
        request.headers.get("x-tuber-inference-key") == runtime.settings.inference_token
        for request in state["requests"]
        if "/llm/" in request.url.path
    )


@pytest.mark.parametrize("failure", ["node", "alias", "capacity", "save"])
async def test_failed_switch_preserves_original_provider_settings_and_restores_node(
    distributed, monkeypatch, failure
):
    runtime, state = distributed
    old_llm, old_vision = runtime.llm, runtime.vision
    old_bytes = runtime.settings.path.read_bytes()
    state["failure"] = failure
    if failure == "save":
        monkeypatch.setattr(
            runtime.settings, "save_public", lambda data: (_ for _ in ()).throw(OSError("disk failed"))
        )
    clients_before = len(state["clients"])
    with pytest.raises(ValueError, match="工作模式切换失败"):
        await runtime.set_work_mode("live")
    assert state["mode"] == "text" and runtime.settings.data["inference_broker"]["mode"] == "text"
    assert runtime.llm is old_llm and runtime.vision is old_vision and runtime.context.provider is old_llm
    assert not old_llm.client.is_closed and runtime.settings.path.read_bytes() == old_bytes
    assert all(client.is_closed for client in state["clients"][clients_before:])
    mode_requests = [
        json.loads(request.content)["mode"] for request in state["requests"] if request.url.path == "/mode"
    ]
    assert mode_requests == ["live", "text"]
    assert not runtime.generation_lock.locked() and not runtime.inference.mode_lock.locked()


async def test_cancel_after_node_commit_waits_for_rollback_and_keeps_original_client(distributed):
    runtime, state = distributed
    old, old_bytes = runtime.llm, runtime.settings.path.read_bytes()
    state["block_props"] = True
    switching = asyncio.create_task(runtime.set_work_mode("live"))
    await state["props_entered"].wait()
    assert state["mode"] == "live"
    switching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await switching
    assert state["mode"] == "text" and runtime.llm is old and not old.client.is_closed
    assert runtime.context.provider is old and runtime.settings.path.read_bytes() == old_bytes
    assert not runtime.inference.mode_lock.locked() and not runtime.generation_lock.locked()


async def test_committed_switch_returns_without_waiting_for_old_client_cleanup(distributed):
    runtime, state = distributed
    old = runtime.llm
    close = old.close
    entered, release = asyncio.Event(), asyncio.Event()

    async def closing():
        entered.set()
        await release.wait()
        await close()

    old.close = closing
    try:
        result = await asyncio.wait_for(runtime.set_work_mode("live"), 2)
        await asyncio.wait_for(entered.wait(), 2)
        assert result["mode"] == state["mode"] == "live"
        assert runtime.llm is not old and not old.client.is_closed
        assert not runtime.generation_lock.locked() and not runtime.inference.mode_lock.locked()
        assert json.loads(runtime.settings.path.read_text())["llm"]["model"] == "MiniCPM5-2B"
    finally:
        release.set()
        await asyncio.gather(*runtime.inference.cleanup_tasks, return_exceptions=True)
    assert old.client.is_closed


async def test_idle_live_mode_keeps_operations_waiting_until_gemma_is_restored(distributed):
    runtime, _ = distributed
    await runtime.control("start")
    assert runtime.live.state == "live" and runtime.live.current_id is None and runtime.live.queue == []
    assert runtime.llm.config["model"] == "MiniCPM5-2B"
    entered, selected = asyncio.Event(), []

    async def operation():
        async with runtime.offline_generation():
            selected.append(runtime.llm.config["model"])
            entered.set()

    pending = asyncio.create_task(operation())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set() and not pending.done()
        assert not runtime.generation_lock.locked()
        await runtime.set_work_mode("text")
        await asyncio.wait_for(pending, 2)
        assert entered.is_set() and selected == ["Gemma4-E2B"]
        assert runtime.live.state == "stopped" and not runtime.generation_lock.locked()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_restored_active_node_allows_operations_despite_a_previous_transition_error(distributed):
    runtime, _ = distributed
    runtime.inference.status = {
        "mode": "text",
        "state": "active",
        "model": "Gemma4-E2B",
        "selected_provider": "llmgemma4e2b",
        "active_provider": "llmgemma4e2b",
        "error": "Resource transition cancelled",
    }

    async def operation():
        async with runtime.offline_generation():
            return runtime.llm.config["model"]

    assert await asyncio.wait_for(operation(), 2) == "Gemma4-E2B"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("late_error", [False, True])
async def test_late_health_observation_cannot_overwrite_a_committed_live_mode(
    distributed, legacy, late_error
):
    runtime, state = distributed
    if legacy:
        runtime.settings.data.pop("work_mode_profiles")
    original_data = runtime.settings.data
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    held = False

    async def delayed_old_health(request):
        nonlocal held
        if request.url.path == "/health" and not held:
            held = True
            entered.set()
            await release.wait()
            if late_error:
                raise httpx.ReadError("old health connection closed", request=request)
            return httpx.Response(
                200,
                json={"mode": "text", "state": "active", "error": None, "active_provider": "llmgemma4e2b"},
            )
        if request.url.path == "/capabilities":
            completed.set()
        return None

    state["http_handler"] = delayed_old_health
    observer = asyncio.create_task(runtime.inference.observe())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.set_work_mode("live")
        if legacy:
            assert runtime.settings.data is original_data
        else:
            assert runtime.llm.config["model"] == "MiniCPM5-2B"
            assert runtime.settings.data is not original_data
        committed = runtime.inference.status
        release.set()
        await asyncio.wait_for(completed.wait(), 2)
        assert runtime.inference.status is committed
        assert runtime.inference.status["mode"] == "live" and runtime.inference.status["state"] == "active"
        assert runtime.settings.data["inference_broker"]["mode"] == "live"
        if not legacy:
            assert runtime.operations_waiting() is True
    finally:
        release.set()
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)


async def test_restart_synchronizes_business_profile_to_actual_node_mode(distributed):
    runtime, state = distributed
    await runtime.set_work_mode("live")
    state["mode"] = "text"
    restored = Settings(runtime=runtime.settings.runtime)
    assert restored.data["llm"]["model"] == "MiniCPM5-2B"
    restarted = Runtime(restored, runtime.store)
    try:
        await restarted.open()
        assert restarted.error is None and restarted.llm.config["model"] == "Gemma4-E2B"
        assert restarted.context.provider is restarted.llm
        assert restored.data["inference_broker"]["mode"] == "text"
        assert json.loads(restored.path.read_text())["llm"]["model"] == "Gemma4-E2B"
    finally:
        await restarted.close()


async def test_unpaired_url_never_receives_the_node_credential(distributed):
    runtime, state = distributed
    settings = runtime.settings
    assert settings.endpoint_key("vision") == settings.inference_token
    for url in ("https://outside.invalid/v1", NODE_URL + "/llm/gemma4e2b-extra", NODE_URL + "/unregistered"):
        assert settings.broker_key_for(url) is None
    settings.data["work_mode_profiles"]["live"]["llm"]["base_url"] = "https://outside.invalid/v1"
    await runtime.set_work_mode("live")
    outside = [request for request in state["requests"] if request.url.host == "outside.invalid"]
    assert outside
    assert all(settings.inference_token not in str(request.headers) for request in outside)
    assert all("x-tuber-inference-key" not in request.headers for request in outside)


def test_business_voice_reference_resolves_with_independent_runtime(settings):
    settings.data = business_configuration(settings.data, NODE_URL)
    voice = next(voice for voice in settings.voices() if voice["id"] == "yaoyao_sovits_v2")
    reference = settings.runtime / Path(voice["reference_asset"]).relative_to("runtime")
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    assert settings.voice_reference_path(voice) == reference.resolve()


async def test_unconfirmed_mode_rollback_blocks_mismatched_business_provider(distributed, monkeypatch):
    runtime, state = distributed
    await runtime.set_work_mode("live")
    original = runtime.llm

    def disk_failed(data):
        raise OSError("disk full")

    async def failed_live_rollback(request):
        if request.url.path == "/mode" and json.loads(request.content)["mode"] == "live":
            return httpx.Response(503, json={"detail": "model OOM; restored Gemma/text"})
        return None

    save_public = runtime.settings.save_public
    monkeypatch.setattr(runtime.settings, "save_public", disk_failed)
    state["http_handler"] = failed_live_rollback
    with pytest.raises(ValueError, match="工作模式切换失败"):
        await runtime.set_work_mode("text")
    assert runtime.llm is original and original.config["model"] == "MiniCPM5-2B"
    assert state["mode"] == runtime.inference.status["mode"] == "text"
    assert runtime.operations_waiting() is True
    await runtime.refresh_inference()
    assert runtime.inference.status["state"] == "error" and runtime.operations_waiting()
    monkeypatch.setattr(runtime.settings, "save_public", save_public)
    await runtime.set_work_mode("text")
    assert runtime.llm.config["model"] == "Gemma4-E2B" and not runtime.operations_waiting()


async def test_wrong_node_alias_requires_explicit_retry_even_after_health_looks_healthy(distributed):
    runtime, state = distributed

    async def wrong_alias(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"mode": "text", "state": "active", "model": "Wrong-Model"})
        return None

    state["http_handler"] = wrong_alias
    await runtime.refresh_inference()
    assert runtime.operations_waiting() and runtime.inference.status["provider_alignment_error"]
    state.pop("http_handler")
    await runtime.refresh_inference()
    assert runtime.inference.status["state"] == "error" and runtime.operations_waiting()
    await runtime.set_work_mode("text")
    assert not runtime.operations_waiting() and not runtime.inference.profile_alignment_failed


async def test_different_vision_profile_also_blocks_operations_until_a_successful_retry(distributed):
    runtime, _ = distributed
    runtime.settings.data["vision"] = dict(runtime.settings.data["work_mode_profiles"]["live"]["vision"])
    await runtime.refresh_inference()
    assert runtime.inference.status["state"] == "error" and runtime.operations_waiting()
    await runtime.set_work_mode("text")
    assert runtime.settings.data["vision"] == runtime.settings.data["work_mode_profiles"]["text"]["vision"]
    assert not runtime.operations_waiting()


async def test_external_api_profile_is_not_rejected_for_a_different_node_local_model(distributed):
    runtime, state = distributed
    runtime.settings.data["work_mode_profiles"]["text"]["llm"].update(
        kind="openai_compatible", base_url="https://cloud.invalid/v1", model="External-Model"
    )
    await runtime.set_work_mode("text")
    await runtime.refresh_inference()
    assert runtime.llm.config["model"] == "External-Model"
    assert runtime.inference.status["model"] == "Gemma4-E2B" and not runtime.operations_waiting()
    assert all(request.url.host != "cloud.invalid" for request in state["requests"])
