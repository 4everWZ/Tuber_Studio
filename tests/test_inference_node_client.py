import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_distributed_modes import NODE_URL
from test_distributed_modes import distributed as distributed

from tuber_studio import inference_node_client
from tuber_studio.config import atomic_bytes
from tuber_studio.video_vision import ApiVideoEvidenceProvider

OTHER_NODE = "http://second-jetson.local:9012"
OTHER_TOKEN = "second-node-test-token-0123456789abcdef"


async def test_pair_rebinds_text_vision_and_tts_clients_to_the_new_endpoint_and_token(distributed):
    runtime, state = distributed
    old_llm, old_voices, old_client = runtime.llm, runtime.voices, runtime.inference_client
    result = await inference_node_client.pair_node(runtime, {"base_url": OTHER_NODE, "token": OTHER_TOKEN})
    assert result["paired"] and result["base_url"] == OTHER_NODE and result["model"] == "Gemma4-E2B"
    assert runtime.voices is not old_voices and runtime.voices.settings is runtime.settings
    assert runtime.voices.cache is runtime.cache
    assert str(runtime.voices.client.base_url).rstrip("/") == OTHER_NODE + "/tts"
    assert runtime.voices.client.headers["X-Tuber-Inference-Key"] == OTHER_TOKEN
    assert runtime.vision.client.headers["Authorization"] == "Bearer " + OTHER_TOKEN
    assert runtime.context.provider is runtime.llm and runtime.llm is not old_llm
    assert (runtime.settings.runtime / "inference-token").read_text() == OTHER_TOKEN
    assert runtime.settings.inference_token == OTHER_TOKEN
    assert json.loads(runtime.settings.path.read_text())["inference_broker"]["base_url"] == OTHER_NODE
    await runtime.voices._genie_identity()
    voice_requests = [request for request in state["requests"] if request.url.path.endswith("/tts/docs")]
    assert voice_requests and voice_requests[-1].url.host == "second-jetson.local"
    assert voice_requests[-1].headers["X-Tuber-Inference-Key"] == OTHER_TOKEN
    await asyncio.gather(*runtime.client_cleanup, return_exceptions=True)
    assert old_llm.client.is_closed and old_voices.client.is_closed and old_client.is_closed


@pytest.mark.parametrize(
    "base", [OTHER_NODE, NODE_URL + "/extra", "https://jetson.local.attacker.invalid:9012"]
)
async def test_similar_or_new_endpoint_requires_its_own_explicit_pairing_token(distributed, base):
    runtime, state = distributed
    before = runtime.settings.path.read_bytes()
    with pytest.raises(ValueError, match="格式"):
        await inference_node_client.pair_node(runtime, {"base_url": base})
    assert state["requests"] == [] and runtime.settings.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["token", "settings"])
async def test_pair_file_failure_restores_both_files_old_clients_and_candidate_node_mode(
    distributed, monkeypatch, failure
):
    runtime, state = distributed
    state["mode"] = "live"
    old = runtime.llm, runtime.vision, runtime.voices, runtime.inference_client
    snapshots = (
        runtime.settings.path.read_bytes(),
        (runtime.settings.runtime / "inference-token").read_bytes(),
    )
    original_key = runtime.settings.inference_token
    count = len(state["clients"])
    if failure == "settings":
        monkeypatch.setattr(
            runtime.settings, "save_public", lambda data: (_ for _ in ()).throw(OSError("disk failed"))
        )
    else:
        writes = 0

        def fail_first(path, value, *, private=False):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError("token write failed")
            atomic_bytes(path, value, private=private)

        monkeypatch.setattr(inference_node_client, "atomic_bytes", fail_first)
    with pytest.raises(OSError):
        await inference_node_client.pair_node(runtime, {"base_url": OTHER_NODE, "token": OTHER_TOKEN})
    assert (runtime.llm, runtime.vision, runtime.voices, runtime.inference_client) == old
    assert runtime.settings.inference_token == original_key
    assert runtime.settings.data["inference_broker"]["base_url"] == NODE_URL
    assert (
        runtime.settings.path.read_bytes(),
        (runtime.settings.runtime / "inference-token").read_bytes(),
    ) == snapshots
    assert state["mode"] == "live"
    assert all(client.is_closed for client in state["clients"][count:])
    assert not runtime.generation_lock.locked() and not runtime.mode_lock.locked()


async def test_pair_second_cancellation_keeps_transition_locks_until_candidate_is_restored(distributed):
    runtime, state = distributed
    state["mode"], state["block_props"] = "live", True
    rollback_entered, rollback_release = asyncio.Event(), asyncio.Event()
    old = runtime.llm
    snapshots = (
        runtime.settings.path.read_bytes(),
        (runtime.settings.runtime / "inference-token").read_bytes(),
    )

    async def http(request):
        if request.url.path == "/mode" and json.loads(request.content)["mode"] == "live":
            rollback_entered.set()
            await rollback_release.wait()
        return None

    state["http_handler"] = http
    pairing = asyncio.create_task(
        inference_node_client.pair_node(runtime, {"base_url": OTHER_NODE, "token": OTHER_TOKEN})
    )
    try:
        await asyncio.wait_for(state["props_entered"].wait(), 2)
        assert state["mode"] == "text"
        pairing.cancel()
        await asyncio.wait_for(rollback_entered.wait(), 2)
        pairing.cancel()
        await asyncio.sleep(0)
        assert runtime.mode_lock.locked() and runtime.generation_lock.locked() and not pairing.done()
        assert runtime.llm is old and not old.client.is_closed
        rollback_release.set()
        with pytest.raises(asyncio.CancelledError):
            await pairing
        assert state["mode"] == "live" and runtime.llm is old
        assert not runtime.mode_lock.locked() and not runtime.generation_lock.locked()
        assert (
            runtime.settings.path.read_bytes(),
            (runtime.settings.runtime / "inference-token").read_bytes(),
        ) == snapshots
    finally:
        rollback_release.set()
        pairing.cancel()
        await asyncio.gather(pairing, return_exceptions=True)


async def test_pair_rechecks_live_state_after_readonly_capability_lookup(distributed):
    runtime, state = distributed
    entered, release = asyncio.Event(), asyncio.Event()

    async def http(request):
        if request.url.path == "/capabilities":
            entered.set()
            await release.wait()
        return None

    state["http_handler"] = http
    pairing = asyncio.create_task(
        inference_node_client.pair_node(runtime, {"base_url": OTHER_NODE, "token": OTHER_TOKEN})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runtime.state = "live"
        release.set()
        with pytest.raises(ValueError, match="停止直播"):
            await pairing
        assert not any(request.url.path == "/mode" for request in state["requests"])
        assert runtime.settings.data["inference_broker"]["base_url"] == NODE_URL
    finally:
        release.set()
        pairing.cancel()
        await asyncio.gather(pairing, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_observation_of_old_node_cannot_replace_a_new_pair_or_mark_it_failed(distributed, outcome):
    runtime, state = distributed
    entered, release = asyncio.Event(), asyncio.Event()

    async def old_capability(request):
        if request.url.host == "jetson.local" and request.url.path == "/capabilities":
            entered.set()
            await release.wait()
            if outcome == "error":
                raise httpx.RemoteProtocolError("old node closed connection", request=request)
            return httpx.Response(
                200,
                json={
                    "protocol": "tuber-inference-node",
                    "protocol_version": 1,
                    "mode": "live",
                    "obsolete_node": True,
                },
            )
        return None

    state["http_handler"] = old_capability
    refreshing = asyncio.create_task(inference_node_client.refresh_capabilities(runtime))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await inference_node_client.pair_node(runtime, {"base_url": OTHER_NODE, "token": OTHER_TOKEN})
        committed = runtime.node_capabilities
        if outcome == "cancel":
            refreshing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await refreshing
        else:
            release.set()
            assert await refreshing is committed
        assert runtime.node_capabilities is committed and "obsolete_node" not in committed
        assert runtime.settings.data["inference_broker"]["base_url"] == OTHER_NODE
        assert runtime.inference_status["state"] == "active" and not runtime.error
    finally:
        release.set()
        refreshing.cancel()
        await asyncio.gather(refreshing, return_exceptions=True)


async def test_closed_old_visual_observation_cannot_mark_a_new_pair_failed(distributed, monkeypatch):
    runtime, _ = distributed
    provider = ApiVideoEvidenceProvider(runtime, runtime.settings.data["video_vision"])
    runtime.browsing = SimpleNamespace(evidence=SimpleNamespace(visual=provider))
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_old_restore(runtime):
        entered.set()
        await release.wait()
        completed.set()
        raise httpx.ReadError("old visual connection closed")

    monkeypatch.setattr("tuber_studio.runtime.restore_admission", delayed_old_restore)
    observer = asyncio.create_task(runtime._observe_inference())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await inference_node_client.pair_node(runtime, {"base_url": OTHER_NODE, "token": OTHER_TOKEN})
        committed = runtime.node_capabilities
        release.set()
        await asyncio.wait_for(completed.wait(), 2)
        assert runtime.node_capabilities is committed and "monitoring_error" not in committed
        assert runtime.inference_status["state"] == "active"
    finally:
        release.set()
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        await runtime.browsing.evidence.visual.close()
