import asyncio
import base64
import json
import os
import threading
import time

import httpx
import pytest
from test_inference import setup_broker

from tuber_studio.inference import HEADER, ProcessRunner, create_app
from tuber_studio.inference_cache import cache_summary, scan_cache
from tuber_studio.inference_profiles import (
    DAFEIYU_KEY,
    DAFEIYU_MODEL,
    GEMMA_ENGINE,
    GEMMA_KEY,
    GROUNDED_DAFEIYU_KEY,
    GROUNDED_DAFEIYU_MODEL,
    project_asset,
    verify_artifact,
)
from tuber_studio.inference_validation import validate_vision_request


def vision_body(url=None):
    return {
        "model": "Gemma4-E2B",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "大肥鱼在画面里做什么？"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": url
                            or (
                                "data:image/png;base64,"
                                + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
                            )
                        },
                    },
                ],
            }
        ],
        "max_tokens": 8192,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": True},
    }


async def mode_broker(root, key=GEMMA_KEY, alias="Gemma4-E2B"):
    broker, control = setup_broker(root, selected=key)
    broker.operations_selected, broker.live_selected = key, "llm2b"
    configuration = root / "runtime/inference-node.json"
    configuration.write_text(json.dumps({"mode": "text", "operations_model": alias, "keep": "node"}))
    broker.configuration_path = configuration
    await broker.initialize()
    control.events.clear()
    return broker, control, configuration


async def test_node_mode_switches_gemma_to_minicpm_genie_and_returns_to_gemma(tmp_path):
    broker, control, configuration = await mode_broker(tmp_path)
    result = await broker.switch_mode("live")
    assert result["model"] == "MiniCPM5-2B" and result["selected_provider"] == "llm2b"
    assert control.events == [("stop", GEMMA_KEY), ("start", "tts"), ("start", "llm2b")]
    assert control.alive["llm2b"] and control.alive["tts"] and not control.alive[GEMMA_KEY]
    control.events.clear()
    result = await broker.switch_mode("text")
    assert result["model"] == "Gemma4-E2B" and result["selected_provider"] == GEMMA_KEY
    assert control.events == [("stop", "llm2b"), ("stop", "tts"), ("start", GEMMA_KEY)]
    assert json.loads(configuration.read_text()) == {
        "mode": "text",
        "operations_model": "Gemma4-E2B",
        "keep": "node",
    }
    assert not (tmp_path / "runtime/studio.json").exists()
    await broker.close()


async def test_unified_node_model_switch_commits_the_operations_profile(tmp_path):
    broker, _, configuration = await mode_broker(tmp_path)
    application = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://node", headers={HEADER: token}
    ) as client:
        response = await client.post("/llm/warmup", json={"model": DAFEIYU_MODEL})
        assert response.status_code == 200 and response.json()["model_alias"] == DAFEIYU_MODEL
    assert broker.operations_selected == DAFEIYU_KEY
    assert json.loads(configuration.read_text())["operations_model"] == DAFEIYU_MODEL
    restored = create_app(tmp_path, node_config=configuration, warm_start=False).state.broker
    assert restored.selected == restored.operations_selected == DAFEIYU_KEY
    await restored.close()
    await broker.close()


@pytest.mark.parametrize("failure", ["tts", "llm2b", "persist"])
async def test_node_failed_live_switch_restores_gemma_and_original_configuration(tmp_path, failure):
    broker, control, configuration = await mode_broker(tmp_path)
    original = configuration.read_bytes()
    start = control.start

    async def failing(key):
        if key == failure:
            raise RuntimeError("load failed")
        await start(key)

    control.start = failing
    if failure == "persist":
        broker._persist_mode = lambda mode: (_ for _ in ()).throw(OSError("disk failed"))
    with pytest.raises(Exception) as error:
        await broker.switch_mode("live")
    assert error.value.status_code == 503
    assert broker.mode == "text" and broker.selected == GEMMA_KEY
    assert control.alive[GEMMA_KEY] and not control.alive["tts"] and not control.alive["llm2b"]
    assert configuration.read_bytes() == original
    await broker.close()


async def test_node_cancelled_live_switch_restores_operations_selection_before_resuming(tmp_path):
    broker, control, configuration = await mode_broker(tmp_path)
    original, entered = configuration.read_bytes(), asyncio.Event()
    start = control.start

    async def loading(key):
        if key == "llm2b":
            entered.set()
            await asyncio.Event().wait()
        await start(key)

    control.start = loading
    switching = asyncio.create_task(broker.switch_mode("live"))
    await entered.wait()
    switching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await switching
    assert broker.mode == "text" and broker.selected == GEMMA_KEY
    assert control.alive[GEMMA_KEY] and not control.alive["tts"] and not control.alive["llm2b"]
    assert configuration.read_bytes() == original and not broker.lock.locked()
    await broker.close()


async def test_node_accepts_persistent_bearer_pairing_and_exposes_actual_capabilities(tmp_path):
    broker, control, _ = await mode_broker(tmp_path)
    broker.properties[GEMMA_KEY]["modalities"] = {"vision": True}
    application = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://node"
    ) as client:
        assert (await client.get("/capabilities")).status_code == 401
        response = await client.get("/capabilities", headers={"Authorization": "Bearer " + token})
        result = response.json()
        assert response.status_code == 200 and result["protocol_version"] == 1
        assert result["protocol"] == "tuber-inference-node"
        gemma = result["profiles"][GEMMA_KEY]
        assert gemma["loaded"] and gemma["vision"] and gemma["context_tokens"] == 131072
        assert result["resource"]["available_bytes"] > 0
        assert result["storage_status"]["state"] in {"scanning", "unknown"}
        assert all(item["used_bytes"] is None for item in result["storage"])
        properties = await client.get("/vlm/gemma4e2b/v1/props", headers={HEADER: token})
        assert properties.json()["modalities"] == {"vision": True}
        models = await client.get("/vlm/gemma4e2b/v1/models", headers={HEADER: token})
        assert models.json()["data"][0]["id"] == "Gemma4-E2B"
    control.alive[GEMMA_KEY] = False
    assert not (await broker.capabilities())["profiles"][GEMMA_KEY]["vision"]
    await broker.close()


@pytest.mark.parametrize("profile,key,alias", [
    ("gemma4e2b", GEMMA_KEY, "Gemma4-E2B"),
    ("dafeiyu", GROUNDED_DAFEIYU_KEY, GROUNDED_DAFEIYU_MODEL),
])
async def test_node_vision_route_is_explicit_and_shares_the_same_mode_lease(tmp_path, profile, key, alias):
    broker, _, _ = await mode_broker(tmp_path, key, alias)
    application = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    body = {**vision_body(), "model": alias}
    route = f"/vlm/{profile}/v1/chat/completions"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://node", headers={HEADER: token}
    ) as client:
        assert (
            await client.post(f"/llm/{profile}/v1/chat/completions", json=body)
        ).status_code == 404
        assert (await client.post("/llm/v1/chat/completions", json=body)).status_code == 422
        assert (await client.post(route, json=body)).status_code == 200
        await broker.switch_mode("live")
        assert (await client.post(route, json=body)).status_code == 409
        assert (await client.post(f"/vlm/{profile}/command", json={})).status_code == 404
    await broker.close()


@pytest.mark.parametrize(
    "url", ["https://outside.invalid/image.png", "file:///etc/passwd", "data:image/png;base64,!!!!"]
)
def test_node_does_not_fetch_remote_or_local_image_addresses(url):
    with pytest.raises(ValueError):
        validate_vision_request(vision_body(url))


def test_node_gemma_command_preserves_measured_context_quantization_and_no_mtp(tmp_path):
    runner = ProcessRunner(tmp_path, kv_cache_type="q4_0", node_settings={})
    command = runner.command(GEMMA_KEY)
    assert command[0] == str((tmp_path / GEMMA_ENGINE).resolve())
    for option, value in [
        ("--ctx-size", "131072"),
        ("--cache-type-k", "q4_0"),
        ("--cache-type-v", "q4_0"),
        ("--reasoning", "on"),
        ("--load-mode", "dio"),
        ("--lazy-mode", "on"),
        ("--ubatch-size", "64"),
        ("--temp", "0.2"),
    ]:
        assert command[command.index(option) + 1] == value
    assert "--spec-type" not in command and "--reasoning-budget" not in command
    assert command[command.index("--mmproj") + 1].endswith("Q8_0-visiononly.gguf")


def test_node_configuration_has_independent_persistence_and_mode_defaults(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    configuration = runtime / "inference-node.json"
    configuration.write_text(json.dumps({"context_tokens": 131072, "kv_cache_type": "q4_0"}))
    application = create_app(tmp_path, node_config=configuration, warm_start=False)
    manager = application.state.broker
    assert manager.selected == manager.operations_selected == GEMMA_KEY
    assert manager.live_selected == "llm2b" and manager.configuration_path == configuration
    assert manager.runner.node_settings["kv_cache_type"] == "q4_0"
    assert not (runtime / "studio.sqlite3").exists()


async def test_node_resource_guard_rejects_before_generation_and_preserves_loaded_model(tmp_path):
    broker, control, _ = await mode_broker(tmp_path)
    control.node_settings = {"min_available_mib": 384, "max_swap_growth_mib": 128}
    broker.resource_snapshot = lambda: {"available_bytes": 383 * 1024**2, "swap_growth_bytes": 0}
    with pytest.raises(Exception) as error:
        await broker.forward(GEMMA_KEY, "completion", b"{}")
    assert error.value.status_code == 503 and "available memory" in error.value.detail
    assert control.events == [] and control.alive[GEMMA_KEY]
    await broker.close()


def test_artifact_verification_caches_identity_and_rejects_replacement(tmp_path):
    import hashlib

    path = tmp_path / "weights"
    path.write_bytes(b"fixed model")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache = {}
    verify_artifact(path, digest, cache)
    verify_artifact(path, digest, cache)
    assert len(cache) == 1
    path.write_bytes(b"changed model")
    with pytest.raises(RuntimeError, match="fixed profile"):
        verify_artifact(path, digest, cache)
    with pytest.raises(ValueError, match="inside"):
        project_asset(tmp_path, str(tmp_path.parent / "outside"))


def test_node_identifiers_report_verified_bytes_and_expire_when_assets_change(tmp_path):
    import hashlib

    runner = ProcessRunner(tmp_path, node_settings={})
    for role, path in runner.gemma_assets().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(role.encode())
        verify_artifact(path, hashlib.sha256(role.encode()).hexdigest(), runner.verified_artifacts)
    identities = runner.artifact_identifiers(GEMMA_KEY)
    assert identities["model_sha256"] == hashlib.sha256(b"model").hexdigest()
    assert identities["projector_sha256"] == hashlib.sha256(b"projector").hexdigest()
    assert identities["runtime_sha256"] == hashlib.sha256(b"engine").hexdigest()
    runner.gemma_assets()["projector"].write_bytes(b"different projector")
    assert runner.artifact_identifiers(GEMMA_KEY)["projector_sha256"] is None


async def test_node_cache_cleanup_requires_authentication_idle_and_a_body_without_paths(tmp_path):
    broker, _, _ = await mode_broker(tmp_path)
    cache = tmp_path / "runtime/inference-cache"
    cache.mkdir()
    (cache / "temporary.bin").write_bytes(b"12345")
    assert broker.storage_summary()[-1]["reclaimable_bytes"] == 5
    application = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://node"
    ) as client:
        assert (await client.post("/cache/clear", json={})).status_code == 401
        assert (await client.post("/cache/clear", json={}, headers={HEADER: token})).status_code == 409
        assert (cache / "temporary.bin").exists()
        await broker.idle()
        assert (
            await client.post("/cache/clear", json={"path": "models"}, headers={HEADER: token})
        ).status_code == 422
        response = await client.post("/cache/clear", json={}, headers={HEADER: token})
        assert response.status_code == 200
        assert response.json()["removed_files"] == 1 and response.json()["removed_bytes"] == 5
        assert not (cache / "temporary.bin").exists()
        assert broker.storage_summary()[-1]["reclaimable_bytes"] == 0
        result = (await client.post("/cache/clear", json={}, headers={HEADER: token})).json()
        assert result["removed_files"] == result["removed_bytes"] == 0 and result["complete"]
        await broker.switch_mode("live")
        assert (await client.post("/cache/clear", json={}, headers={HEADER: token})).status_code == 409
    await broker.close()


async def test_cancelled_cache_cleanup_retains_model_lease_until_filesystem_work_finishes(
    tmp_path, monkeypatch
):
    from tuber_studio import inference

    broker, control, _ = await mode_broker(tmp_path)
    await broker.idle()
    cache = tmp_path / "runtime/inference-cache"
    cache.mkdir()
    temporary = cache / "disposable"
    temporary.write_bytes(b"cache")
    entered, released = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(root, *, clear):
        loop.call_soon_threadsafe(entered.set)
        if not released.wait(timeout=5):
            raise TimeoutError("Test did not release cache cleanup")
        return scan_cache(root, clear=clear)

    monkeypatch.setattr(inference, "scan_cache", blocked)
    clearing = asyncio.create_task(broker.clear_cache())
    following = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clearing.cancel()
        await asyncio.sleep(0)
        following = asyncio.create_task(broker.forward(GEMMA_KEY, "completion", b"{}"))
        await asyncio.sleep(0)
        assert broker.lock.locked() and not following.done() and not control.alive[GEMMA_KEY]
        clearing.cancel()
        await asyncio.sleep(0)
        assert broker.lock.locked()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await clearing
        assert (await following).status_code == 200
        assert not temporary.exists() and not broker.lock.locked()
    finally:
        released.set()
        if following is not None:
            following.cancel()
        clearing.cancel()
        await asyncio.gather(clearing, *([following] if following else []), return_exceptions=True)
        await broker.close()


def test_node_cache_cleanup_protects_nested_data_and_hardlinks_and_never_walks_other_pools(tmp_path):
    cache = tmp_path / "runtime/inference-cache"
    cache.mkdir(parents=True)
    (cache / "throwaway").write_bytes(b"ok")
    nested = cache / "protected-directory/keep"
    nested.parent.mkdir()
    nested.write_bytes(b"keep nested")
    protected = []
    for relative in (
        "runtime/models/model.gguf",
        "runtime/tts/voice.onnx",
        "runtime/nltk_data/corpus",
        "runtime/studio.sqlite3",
        "assets/character.png",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"permanent")
        protected.append(path)
    os.link(protected[0], cache / "shared-model")
    summary = cache_summary(tmp_path)
    assert summary["reclaimable_bytes"] == 2 and summary["reclaimable_complete"]
    assert summary["used_bytes"] is None and not summary["usage_complete"]
    result = scan_cache(tmp_path, clear=True)
    assert result["removed_files"] == 1 and result["removed_bytes"] == 2
    assert result["protected_count"] == 2 and not result["errors"]
    assert (cache / "shared-model").exists() and nested.read_bytes() == b"keep nested"
    assert all(path.read_bytes() == b"permanent" for path in protected)


def test_node_cache_scan_is_bounded_and_does_not_claim_partial_totals_are_complete(tmp_path):
    cache = tmp_path / "runtime/inference-cache"
    cache.mkdir(parents=True)
    for index in range(5):
        (cache / str(index)).write_bytes(b"x")
    result = scan_cache(tmp_path, limit=2)
    assert result["scanned_entries"] == 2 and not result["complete"]
    assert result["used_bytes"] is result["reclaimable_bytes"] is None
    result = scan_cache(tmp_path, clear=True, limit=2)
    assert result["removed_files"] == 2 and result["removed_bytes"] == 2
    assert len(list(cache.iterdir())) == 3


def test_node_cache_symlink_cannot_redirect_cleanup_to_business_data(tmp_path):
    target = tmp_path / "business"
    target.mkdir()
    permanent = target / "studio.sqlite3"
    permanent.write_bytes(b"persistent history")
    cache = tmp_path / "runtime/inference-cache"
    cache.parent.mkdir()
    try:
        cache.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    result = scan_cache(tmp_path, clear=True)
    assert result["removed_files"] == 0 and result["protected_count"] == 1
    assert permanent.read_bytes() == b"persistent history"


async def test_node_storage_accounts_for_separately_mounted_projectors_and_nltk_assets(tmp_path):
    from tuber_studio.inference import InferenceBroker

    for relative, data in (
        ("node-assets/projector.gguf", b"Q8-projector"),
        ("nltk_data/corpora/dictionary", b"dictionary"),
    ):
        path = tmp_path / "runtime" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    broker = InferenceBroker(tmp_path)
    summary = {item["kind"]: item for item in broker.storage_summary()}
    for kind, size in (("node-assets", 12), ("nltk_data", 10)):
        assert summary[kind]["used_bytes"] == size
        assert summary[kind]["protected"] is True and summary[kind]["reclaimable_bytes"] == 0
    await broker.close()


async def test_slow_inventory_is_single_flight_and_does_not_delay_capabilities_or_health(
    tmp_path, monkeypatch
):
    broker, _, _ = await mode_broker(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original, calls = broker.storage_summary, []

    def slow_inventory():
        calls.append(1)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release disk inventory")
        return original()

    monkeypatch.setattr(broker, "storage_summary", slow_inventory)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://node", headers={HEADER: token}
        ) as client:
            first = await asyncio.wait_for(client.get("/capabilities"), 0.5)
            assert first.json()["storage_status"]["state"] == "scanning"
            assert first.json()["profiles"][GEMMA_KEY]["loaded"] is True
            assert await asyncio.to_thread(entered.wait, 2)
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(client.get("/capabilities" if index % 2 else "/health") for index in range(20))
                ),
                1,
            )
            assert all(response.status_code == 200 for response in results)
            assert calls == [1]
            for _ in range(10):
                refreshed = await client.post("/capabilities/refresh", json={})
                assert refreshed.status_code == 200
            assert calls == [1] and not broker._storage_observation.task.done()
            release.set()
            await asyncio.wait_for(asyncio.shield(broker._storage_observation.task), 2)
            cached = (await client.get("/capabilities")).json()
            assert cached["storage_status"]["state"] == "cached"
            assert cached["storage_status"]["checked_at"] is not None
            assert calls == [1]
    finally:
        release.set()
        await broker.close()


async def test_freshly_loaded_verified_identities_are_available_while_storage_is_still_unknown(tmp_path):
    from test_inference import setup_broker

    broker, control = setup_broker(tmp_path, selected=GEMMA_KEY)
    expected = {"model_sha256": "a" * 64, "projector_sha256": "b" * 64, "runtime_sha256": "c" * 64}
    control.artifact_identifiers = lambda key: expected if key == GEMMA_KEY else {}
    await broker.initialize()
    try:
        result = await broker.capabilities()
        assert result["profiles"][GEMMA_KEY]["loaded"] is True
        assert result["profiles"][GEMMA_KEY]["context_tokens"] == 131072
        assert all(result["profiles"][GEMMA_KEY][key] == value for key, value in expected.items())
        assert result["storage_status"]["state"] == "scanning"
        assert all(item["used_bytes"] is None for item in result["storage"])
    finally:
        await broker.close()


async def test_profile_metadata_validation_runs_off_the_event_loop(tmp_path, monkeypatch):
    runner = ProcessRunner(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow_validation():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release profile validation")
        return {}

    monkeypatch.setattr(runner, "_validate_all_sync", slow_validation)
    validation = asyncio.create_task(runner.validate_all())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not validation.done()
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
        release.set()
        assert await validation == {}
    finally:
        release.set()
        await asyncio.gather(validation, return_exceptions=True)


async def test_asset_statistics_can_refresh_after_changes_without_repeated_immediate_scans(tmp_path):
    broker, _, _ = await mode_broker(tmp_path)
    asset = tmp_path / "runtime/node-assets/projector.gguf"
    asset.parent.mkdir()
    asset.write_bytes(b"old")
    try:
        await broker.capabilities()
        await broker._storage_observation.task
        first = await broker.capabilities()
        row = next(item for item in first["storage"] if item["kind"] == "node-assets")
        assert row["used_bytes"] == 3
        first_task = broker._storage_observation.task
        asset.write_bytes(b"new-byte-content")
        broker.request_inventory_refresh()
        assert broker._storage_observation.task is first_task
        assert broker._storage_observation.status()["refresh_after_seconds"] > 0
        broker._storage_observation.clock = lambda: time.monotonic() + 31
        broker.request_inventory_refresh()
        assert broker._storage_observation.task is not first_task
        await broker._storage_observation.task
        refreshed = await broker.capabilities()
        row = next(item for item in refreshed["storage"] if item["kind"] == "node-assets")
        assert row["used_bytes"] == len(b"new-byte-content")
        assert refreshed["storage_status"]["checked_at"] >= first["storage_status"]["checked_at"]
    finally:
        await broker.close()


async def test_background_identity_audit_updates_changed_assets_without_waiting_for_disk_inventory(tmp_path):
    from test_inference import setup_broker

    broker, control = setup_broker(tmp_path, selected=GEMMA_KEY)
    identities = {"model_sha256": "a" * 64, "projector_sha256": "b" * 64, "runtime_sha256": "c" * 64}
    control.artifact_identifiers = lambda key: dict(identities) if key == GEMMA_KEY else {}
    await broker.initialize()
    try:
        await broker.capabilities()
        await broker._metadata_observation.task
        identities["projector_sha256"] = None
        old_storage_task = broker._storage_observation.task
        broker._metadata_observation.clock = lambda: time.monotonic() + 6
        await broker.capabilities()
        await broker._metadata_observation.task
        result = await broker.capabilities()
        assert result["profiles"][GEMMA_KEY]["projector_sha256"] is None
        assert result["profiles"][GEMMA_KEY]["model_sha256"] == "a" * 64
        assert broker._storage_observation.task is old_storage_task
    finally:
        await broker.close()
