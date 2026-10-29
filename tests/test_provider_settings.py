import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.inference import RESOURCES
from tuber_studio.provider_settings import ProviderConfigurationError, apply_settings


def api_patch(**extra):
    return {
        "llm": {
            "kind": "openai_compatible",
            "base_url": "https://api.test/v1",
            "model": "remote-model",
            "context_tokens": 8192,
            "native_context_tokens": 131072,
        },
        **extra,
    }


def test_authenticated_settings_save_masks_persists_and_clears_keys(settings):
    settings.data["features"]["live_enabled"] = False
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.put("/api/settings/providers", json=api_patch()).status_code == 401
        headers = {"Authorization": "Bearer " + settings.admin_token}
        response = client.put(
            "/api/settings/providers",
            headers=headers,
            json=api_patch(credentials={"llm": "private-api-test-key"}),
        )
        assert response.status_code == 200
        assert response.json()["provider_credentials"]["llm"]
        assert not response.json()["restart_required"]
        assert "private-api-test-key" not in response.text
        assert "private-api-test-key" not in settings.path.read_text()
        assert json.loads(settings.credentials_path.read_text())["llm"] == "private-api-test-key"
        if os.name != "nt":
            assert settings.credentials_path.stat().st_mode & 0o077 == 0
        response = client.get("/api/settings", headers=headers)
        assert "private-api-test-key" not in response.text
        assert (
            client.put(
                "/api/settings/providers", headers=headers, json={"credentials": {"llm": ""}}
            ).status_code
            == 200
        )
        assert settings.provider_key("llm") == "private-api-test-key"
        assert (
            client.put(
                "/api/settings/providers", headers=headers, json={"clear_credentials": ["llm"]}
            ).status_code
            == 200
        )
        assert settings.provider_key("llm") is None
        assert client.post("/api/live/start", headers=headers).status_code == 400
    reloaded = Settings(runtime=settings.runtime)
    assert reloaded.data["llm"]["context_tokens"] == 8192
    assert not reloaded.provider_key("llm")


@pytest.mark.asyncio
async def test_bad_config_or_changed_endpoint_does_not_leak_or_overwrite_credentials(runtime):
    settings = runtime.settings
    await apply_settings(runtime, api_patch(credentials={"llm": "private-api-test-key"}))
    original = settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm
    with pytest.raises(ValueError, match="旧凭证"):
        await apply_settings(runtime, {"llm": {"base_url": "https://different.test/v1"}})
    with pytest.raises(ValueError, match="上限"):
        await apply_settings(runtime, {"llm": {"context_tokens": 262144}})
    with pytest.raises(ValueError, match="HTTP"):
        await apply_settings(
            runtime,
            {"vision": {"kind": "openai_compatible", "base_url": "file:///etc/passwd", "model": "bad"}},
        )
    assert (settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm) == original


@pytest.mark.asyncio
async def test_failed_public_save_rolls_back_both_files_and_runtime(runtime, monkeypatch):
    settings = runtime.settings
    await apply_settings(runtime, api_patch(credentials={"llm": "old-test-key"}))
    original = settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm

    def fail(data):
        raise OSError("injected write failure")

    monkeypatch.setattr(settings, "save_public", fail)
    with pytest.raises(OSError):
        await apply_settings(runtime, {"credentials": {"llm": "new-test-key"}})
    assert (settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm) == original
    assert settings.provider_key("llm") == "old-test-key"


@pytest.mark.asyncio
async def test_busy_generation_and_auto_policies_block_reconfiguration(runtime):
    async with runtime.generation_lock:
        with pytest.raises(RuntimeError, match="生成"):
            await apply_settings(runtime, api_patch())
    runtime.store.set("comment_policy", {"auto_reply": True})
    with pytest.raises(RuntimeError, match="自动"):
        await apply_settings(runtime, api_patch())


@pytest.fixture
async def managed_settings(runtime, monkeypatch):
    """Real settings/runtime plus a transport representing committed broker RPCs."""
    from tuber_studio import provider_settings

    settings = runtime.settings
    settings.data["llm"].update(kind="llama_cpp", model="MiniCPM5-2B", context_tokens=65536)
    settings.data["inference_broker"].update(enabled=True, model="MiniCPM5-2B", context_tokens=131072)
    settings.provider_keys = {"llm": "original-private-test-key"}
    settings.save_public(settings.data)
    settings.save_keys(settings.provider_keys)
    state = {
        "window": 65536,
        "resident": True,
        "selected": "llm2b",
        "busy": False,
        "mode": "text",
        "extra": set(),
        "calls": [],
        "capacity_error": False,
        "new_committed": asyncio.Event(),
        "restore_entered": asyncio.Event(),
        "restore_release": asyncio.Event(),
        "hold_restore": False,
        "lose_response": None,
    }

    async def handle(request):
        payload = json.loads(request.content) if request.content else None
        state["calls"].append((request.method, request.url.path, payload))
        if request.method == "GET":
            body = {
                "mode": state["mode"],
                "busy": state["busy"],
                "selected_provider": state["selected"],
                "providers": {
                    key: {
                        "running": (state["resident"] and key == state["selected"]) or key in state["extra"],
                        "context_tokens": state["window"],
                    }
                    for key in RESOURCES
                },
            }
            if not state.get("omit_window"):
                body["requested_context_tokens"] = state["window"]
            return httpx.Response(200, json=body)
        if state.get("reject_rpc") == request.url.path:
            return httpx.Response(422, json={"detail": "confirmed rejection before resource changes"})
        if request.url.path == "/context":
            state["window"], state["resident"] = payload["context_tokens"], True
            # A real broker commits its public config before sending the response.
            # This marker is intentionally not a second implementation of it.
            settings.path.write_bytes(b'{"committed_rpc_side_effect":true}')
            if state["window"] == 8192 and state["lose_response"]:
                state["new_committed"].set()
                if state["lose_response"] == "timeout":
                    raise httpx.ReadTimeout("simulated response loss", request=request)
                await asyncio.Event().wait()
            if state["window"] == 65536 and state["hold_restore"]:
                state["restore_entered"].set()
                await state["restore_release"].wait()
        elif request.url.path == "/idle":
            state["resident"] = False
        return httpx.Response(200, json={})

    runtime.inference_client = httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handle)
    )
    created = []

    class StagedClient:
        closed = 0

        async def ensure_capacity(self):
            if state["capacity_error"]:
                raise ValueError("simulated new provider failure")

        async def close(self):
            self.closed += 1

    def factory(*args):
        client = StagedClient()
        created.append(client)
        return client

    monkeypatch.setattr(provider_settings, "create_llm", factory)
    monkeypatch.setattr(provider_settings, "VoiceService", factory)
    monkeypatch.setattr(provider_settings, "VisionService", factory)
    return runtime, state, created


def originals(runtime):
    return (
        runtime.settings.path.read_bytes(),
        runtime.settings.credentials_path.read_bytes(),
        runtime.llm,
        runtime.voices,
        runtime.vision,
        runtime.context.provider,
        runtime.settings.data,
        dict(runtime.settings.provider_keys),
    )


def assert_rollback(runtime, previous, created):
    assert runtime.settings.path.read_bytes() == previous[0]
    assert runtime.settings.credentials_path.read_bytes() == previous[1]
    assert runtime.llm is previous[2] and runtime.voices is previous[3] and runtime.vision is previous[4]
    assert runtime.context.provider is previous[5] and runtime.settings.data is previous[6]
    assert runtime.settings.provider_keys == previous[7]
    assert len(created) == 3 and all(client.closed == 1 for client in created)
    assert not runtime.mode_lock.locked() and not runtime.generation_lock.locked()


@pytest.mark.asyncio
async def test_committed_context_with_lost_response_is_restored_before_return(managed_settings):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state["lose_response"] = "timeout"
    with pytest.raises(httpx.ReadTimeout):
        await apply_settings(
            runtime, {"llm": {"context_tokens": 8192}, "credentials": {"llm": "new-test-key"}}
        )
    assert state["window"] == 65536 and state["resident"]
    assert [call[2] for call in state["calls"] if call[1] == "/context"] == [
        {"context_tokens": 8192},
        {"context_tokens": 65536},
    ]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_cancel_after_commit_and_repeated_cancel_wait_for_confirmed_rollback(managed_settings):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state.update(lose_response="cancel", hold_restore=True)
    work = asyncio.create_task(apply_settings(runtime, {"llm": {"context_tokens": 8192}}))
    await state["new_committed"].wait()
    work.cancel()
    await state["restore_entered"].wait()
    work.cancel()
    await asyncio.sleep(0)
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done() and runtime.mode_lock.locked() and runtime.generation_lock.locked()
    state["restore_release"].set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert state["window"] == 65536 and state["resident"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
@pytest.mark.parametrize("was_local", [True, False])
async def test_failed_local_setting_restores_original_idle_not_extra_mini_residency(
    managed_settings, was_local
):
    runtime, state, created = managed_settings
    state["resident"] = False
    if not was_local:
        runtime.settings.data["llm"].update(
            kind="openai_compatible", model="old-remote", context_tokens=262144, native_context_tokens=262144
        )
        runtime.settings.data["inference_broker"]["context_tokens"] = 65536
        runtime.settings.save_public(runtime.settings.data)
    state["capacity_error"] = True
    previous = originals(runtime)
    with pytest.raises(ValueError, match="provider failure"):
        await apply_settings(
            runtime, {"llm": {"kind": "llama_cpp", "model": "MiniCPM5-2B", "context_tokens": 8192}}
        )
    assert state["window"] == 65536 and not state["resident"]
    assert ("POST", "/idle", {}) in state["calls"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_local_fallback_window_ignores_stale_broker_field_on_write_failure(
    managed_settings, monkeypatch
):
    runtime, state, created = managed_settings
    state["omit_window"] = True
    previous = originals(runtime)

    def fail(_):
        raise OSError("public configuration write failure")

    monkeypatch.setattr(runtime.settings, "save_public", fail)
    with pytest.raises(OSError):
        await apply_settings(
            runtime, {"llm": {"context_tokens": 8192}, "credentials": {"llm": "changed-test-key"}}
        )
    assert state["window"] == 65536 and state["resident"]
    assert runtime.settings.data["inference_broker"]["context_tokens"] == 131072
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_failed_external_setting_restores_original_local_resident(managed_settings, monkeypatch):
    runtime, state, created = managed_settings
    previous = originals(runtime)

    def fail(_):
        raise OSError("public configuration write failure")

    monkeypatch.setattr(runtime.settings, "save_public", fail)
    with pytest.raises(OSError):
        await apply_settings(runtime, api_patch(credentials={"llm": "changed-test-key"}))
    assert state["window"] == 65536 and state["resident"]
    assert state["calls"][1][1] == "/idle"
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["live", "busy", "other_worker"])
async def test_provider_save_does_not_claim_active_resource_experiments(managed_settings, conflict):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    if conflict == "live":
        state["mode"] = "live"
    elif conflict == "busy":
        state["busy"] = True
    else:
        state["extra"].add("llm1bq8")
    with pytest.raises(RuntimeError):
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert not any(call[0] == "POST" for call in state["calls"])
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_unconfirmed_broker_rollback_is_explicit_but_restores_secret_and_runtime(managed_settings):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state["capacity_error"] = True
    # The restoration RPC can fail independently after the initial commit.
    post = runtime.inference_client.post

    async def fail_restore(path, **kwargs):
        if path == "/context" and kwargs["json"]["context_tokens"] == 65536:
            raise httpx.ConnectError("restoration connection failed")
        return await post(path, **kwargs)

    runtime.inference_client.post = fail_restore
    with pytest.raises(ProviderConfigurationError, match="恢复结果未确认"):
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert state["window"] == 8192
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/context", "/idle"])
async def test_confirmed_rejection_with_unchanged_resources_does_not_reload_mini(managed_settings, path):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state["reject_rpc"] = path
    patch = (
        {"llm": {"context_tokens": 8192}}
        if path == "/context"
        else api_patch(credentials={"llm": "new-test-key"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await apply_settings(runtime, patch)
    posts = [call for call in state["calls"] if call[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == path
    assert state["window"] == 65536 and state["resident"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_external_runtime_error_is_not_marked_safe_operator_guidance(managed_settings, monkeypatch):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    from tuber_studio import provider_settings

    factory = provider_settings.create_llm

    async def fail():
        raise RuntimeError("untrusted provider message must stay private")

    def new_client(settings):
        client = factory(settings)
        client.ensure_capacity = fail
        return client

    monkeypatch.setattr(provider_settings, "create_llm", new_client)
    with pytest.raises(RuntimeError) as failure:
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert not isinstance(failure.value, ProviderConfigurationError)
    assert state["window"] == 65536 and state["resident"]
    assert_rollback(runtime, previous, created)
