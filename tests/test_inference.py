import asyncio
import importlib.util
import json
import os
import signal
import struct
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from tuber_studio.inference import (
    HEADER,
    MAX_BODY,
    RESOURCES,
    InferenceBroker,
    ProcessRunner,
    create_app,
    native_context,
)


class Control:
    def __init__(self):
        self.alive = {key: False for key in RESOURCES if key != "llm1bq8"}
        self.events = []
        self.stopping = asyncio.Event()
        self.stop_gate = None

    async def validate_all(self):
        return {
            key: {"command": ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--no-mmap"]}
            for key in self.alive
        }

    async def running(self, key):
        return self.alive.get(key, False)

    async def stop(self, key):
        if self.alive.get(key):
            self.stopping.set()
            if self.stop_gate:
                await self.stop_gate.wait()
            self.alive[key] = False
            self.events.append(("stop", key))

    async def start(self, key):
        self.alive[key] = True
        self.events.append(("start", key))


def gguf(path, context=131072, architecture="llama", *, vision_context=None, architecture_last=False):
    def string(value):
        value = value.encode()
        return struct.pack("<Q", len(value)) + value

    arch = string("general.architecture") + struct.pack("<I", 8) + string(architecture)
    window = string(architecture + ".context_length") + struct.pack("<II", 4, context)
    entries = [window, arch] if architecture_last else [arch, window]
    if vision_context is not None:
        entries.insert(0, string("clip.context_length") + struct.pack("<II", 4, vision_context))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(entries)) + b"".join(entries))


def setup_broker(root, post=None, mode="text", selected="llm2b", context_tokens=131072):
    for resource in RESOURCES.values():
        if resource.model:
            gguf(
                root / "runtime/models" / resource.model,
                262144 if resource.alias == "Qwen3.5-2B" else 131072,
                "qwen35" if resource.alias == "Qwen3.5-2B" else "llama",
            )
    control = Control()
    control.context_tokens = context_tokens

    async def handle(request):
        key = next(key for key, spec in RESOURCES.items() if request.url.port == httpx.URL(spec.url).port)
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "model_alias": RESOURCES[key].alias,
                    "default_generation_settings": {"n_ctx": control.context_tokens},
                },
            )
        if request.method == "POST" and post:
            return await post(request, key)
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return InferenceBroker(root, control, client, mode=mode, selected=selected), control


async def test_initialize_warms_real_2b_and_stops_other_processes(tmp_path):
    broker, control = setup_broker(tmp_path)
    control.alive["tts"] = True
    await broker.initialize()
    assert broker.natives["llm2b"] == 131072
    assert broker.properties["llm2b"]["model_alias"] == "MiniCPM5-2B"
    assert control.events == [("stop", "tts"), ("start", "llm2b")]
    await broker.close()


async def test_insufficient_native_window_rejected_before_resource_changes(tmp_path):
    broker, control = setup_broker(tmp_path)
    gguf(tmp_path / "runtime/models" / RESOURCES["llm2b"].model, 65536)
    with pytest.raises(RuntimeError, match="natively"):
        await broker.initialize()
    assert control.events == []
    await broker.close()


async def test_buffered_upstream_body_holds_global_gpu_lease(tmp_path):
    first_chunk, finish = asyncio.Event(), asyncio.Event()
    tts_entered = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            first_chunk.set()
            await finish.wait()
            yield b"last"

    async def post(request, key):
        if key == "tts":
            tts_entered.set()
        return httpx.Response(200, stream=Body()) if key == "llm2b" else httpx.Response(200, content=b"wav")

    broker, control = setup_broker(tmp_path, post, mode="live")
    await broker.initialize()
    llm = asyncio.create_task(broker.forward("llm2b", "completion", b"{}"))
    await first_chunk.wait()
    tts = asyncio.create_task(broker.forward("tts", "tts", b"{}"))
    await asyncio.sleep(0)
    assert control.alive["tts"] and not tts_entered.is_set()
    assert not llm.done() and not tts.done()
    finish.set()
    assert (await llm).body == b"firstlast"
    assert (await tts).body == b"wav"
    assert control.events == [("start", "tts"), ("start", "llm2b")]
    await broker.close()


async def test_cancelled_generation_confirms_stop_before_next_resource(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def post(request, key):
        if key == "llm2b":
            entered.set()
            await release.wait()
        return httpx.Response(200, content=b"done")

    broker, control = setup_broker(tmp_path, post)
    await broker.initialize()
    work = asyncio.create_task(broker.forward("llm2b", "completion", b"{}"))
    await entered.wait()
    control.stop_gate = asyncio.Event()
    work.cancel()
    await control.stopping.wait()
    following = asyncio.create_task(broker.forward("llm1b", "completion", b"{}"))
    await asyncio.sleep(0)
    assert broker.lock.locked() and not control.alive["tts"]
    control.stop_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert (await following).body == b"done"
    assert control.events[-2:] == [("stop", "llm2b"), ("start", "llm1b")]
    await broker.close()


def test_unvalidated_resident_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        InferenceBroker(tmp_path, mode="resident_pair")


async def test_modes_preserve_selected_pair_and_only_persist_mode(tmp_path):
    broker, control = setup_broker(tmp_path)
    configuration = {
        "secret": "operator-secret",
        "llm": {"model": "MiniCPM5-2B"},
        "inference_broker": {"mode": "exclusive", "kv_cache_type": "q4_0"},
    }
    path = tmp_path / "runtime/studio.json"
    path.write_text(json.dumps(configuration))
    await broker.forward("llm1b", "completion", b"{}")
    control.events.clear()
    result = await broker.switch_mode("live")
    assert result["mode"] == "live" and result["state"] == "active" and result["busy"] is False
    assert control.events == [("stop", "llm1b"), ("start", "tts"), ("start", "llm2b")]
    expected = json.loads(json.dumps(configuration))
    expected["inference_broker"]["mode"] = "live"
    assert json.loads(path.read_text()) == expected
    control.events.clear()
    result = await broker.switch_mode("text")
    assert control.events == [("stop", "tts")]
    assert result["mode"] == "text" and result["providers"]["llm2b"]["running"]
    assert not list(path.parent.glob(".studio-mode-*"))
    await broker.close()


async def test_live_cold_switch_releases_llm_until_tts_is_fully_ready(tmp_path):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    control.events.clear()
    entered, ready = asyncio.Event(), asyncio.Event()
    original_get = broker.client.get

    async def loading(url, **options):
        if url == RESOURCES["tts"].url + "/docs":
            entered.set()
            await ready.wait()
        return await original_get(url, **options)

    broker.client.get = loading
    switching = asyncio.create_task(broker.switch_mode("live"))
    await entered.wait()
    assert control.events == [("stop", "llm2b"), ("start", "tts")]
    assert not control.alive["llm2b"] and broker.mode == "text"
    ready.set()
    assert (await switching)["mode"] == "live"
    assert control.events[-1] == ("start", "llm2b")
    await broker.close()


async def test_live_missing_tts_rebuilds_tts_first_and_hot_requests_keep_both(tmp_path):
    posts = []

    async def post(request, key):
        assert control.alive["llm2b"] and control.alive["tts"]
        posts.append(key)
        return httpx.Response(200, content=b"done")

    broker, control = setup_broker(tmp_path, post, mode="live")
    await broker.initialize()
    control.alive["tts"] = False
    control.events.clear()
    await broker.forward("llm2b", "completion", b"{}")
    assert control.events == [("stop", "llm2b"), ("start", "tts"), ("start", "llm2b")]
    control.events.clear()
    await broker.forward("tts", "tts", b"{}")
    await broker.forward("llm2b", "completion", b"{}")
    assert control.events == [] and posts == ["llm2b", "tts", "llm2b"]
    await broker.close()


@pytest.mark.parametrize("failed", ["tts", "llm2b"])
async def test_live_server_failure_retires_worker_before_a_following_request(tmp_path, failed):
    first = True

    async def post(request, key):
        nonlocal first
        if first:
            first = False
            return httpx.Response(500, json={"detail": "CUDA inference failed"})
        return httpx.Response(200, content=b"fresh result")

    broker, control = setup_broker(tmp_path, post, mode="live")
    await broker.initialize()
    control.events.clear()
    response = await broker.forward(failed, "tts" if failed == "tts" else "completion", b"{}")
    assert response.status_code == 500
    assert not control.alive[failed] and broker.active is None and broker.state == "error"
    control.events.clear()
    response = await broker.forward("llm2b", "completion", b"{}")
    assert response.status_code == 200
    assert control.events == (
        [("stop", "llm2b"), ("start", "tts"), ("start", "llm2b")] if failed == "tts" else [("start", "llm2b")]
    )
    assert control.alive["tts"] and control.alive["llm2b"]
    await broker.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_live_rebuild_restores_prior_residents(tmp_path, cancel):
    broker, control = setup_broker(tmp_path, mode="live")
    await broker.initialize()
    control.alive["tts"] = False
    entered = asyncio.Event()
    original_get = broker.client.get

    async def loading(url, **options):
        if url == RESOURCES["tts"].url + "/docs":
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("TTS load failed")
        return await original_get(url, **options)

    broker.client.get = loading
    work = asyncio.create_task(broker.forward("llm2b", "completion", b"{}"))
    await entered.wait()
    if cancel:
        work.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else HTTPException):
        await work
    assert broker.mode == "live" and broker.active == "llm2b" and not broker.lock.locked()
    assert {key for key, alive in control.alive.items() if alive} == {"llm2b"}
    assert (await broker.observe())["state"] == "error"  # The prior pair was already incomplete.
    await broker.close()


async def test_live_to_text_persist_failure_restores_pair_tts_first(tmp_path, monkeypatch):
    broker, control = setup_broker(tmp_path, mode="live")
    await broker.initialize()
    broker.active = "tts"
    path = tmp_path / "runtime/studio.json"
    original = '{"inference_broker":{"mode":"live"},"secret":"keep"}'
    path.write_text(original)
    control.events.clear()

    def fail_replace(*args):
        raise PermissionError("cannot persist")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(HTTPException):
        await broker.switch_mode("text")
    assert control.events == [("stop", "tts"), ("stop", "llm2b"), ("start", "tts"), ("start", "llm2b")]
    assert broker.mode == "live" and broker.active == "tts" and path.read_text() == original
    assert (await broker.observe())["state"] == "active"
    await broker.close()


async def test_health_reports_each_fixed_resource_cache_and_current_llm(tmp_path):
    broker, control = setup_broker(tmp_path)
    control.kv_cache_type = "q4_0"
    await broker.forward("llmqwen35", "completion", b"{}")
    status = await broker.observe()
    assert status["kv_cache_type"] == "q8_0"
    assert status["providers"]["llm2b"]["cache_type_k"] == "q4_0"
    assert status["providers"]["llmqwen35"]["cache_type_v"] == "q8_0"
    assert status["providers"]["tts"]["cache_type_k"] is None
    await broker.switch_mode("live")
    await broker.forward("tts", "tts", b"{}")
    status = await broker.observe()
    assert status["active_provider"] == "tts" and status["kv_cache_type"] == "q4_0"
    await broker.close()


async def test_text_rejects_tts_and_live_rejects_other_candidates_without_mutation(tmp_path):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    before = control.events[:]
    with pytest.raises(HTTPException) as rejected:
        await broker.forward("tts", "tts", b"{}")
    assert rejected.value.status_code == 409 and "live mode" in rejected.value.detail
    assert control.events == before
    await broker.switch_mode("live")
    before = control.events[:]
    with pytest.raises(HTTPException) as rejected:
        await broker.forward("llm1b", "warmup", b"{}")
    assert rejected.value.status_code == 409 and control.events == before
    assert (await broker.observe())["state"] == "active"
    control.alive["tts"] = False
    assert (await broker.observe())["state"] == "error"
    await broker.close()


@pytest.mark.parametrize("failure", ["tts", "selected_llm", "persist"])
async def test_mode_failure_restores_previous_candidate_and_configuration(tmp_path, failure, monkeypatch):
    broker, control = setup_broker(tmp_path)
    path = tmp_path / "runtime/studio.json"
    path.write_text('{"inference_broker":{"mode":"text"},"private":"keep"}')
    original = path.read_bytes()
    await broker.forward("llm1b", "completion", b"{}")
    if failure != "persist":
        start = control.start

        async def fail_start(key):
            await start(key)
            if key == ("tts" if failure == "tts" else "llm2b"):
                raise RuntimeError("failure after child creation")

        control.start = fail_start
    else:

        def fail_persist(source, target):
            raise PermissionError("read-only configuration")

        monkeypatch.setattr(os, "replace", fail_persist)
    with pytest.raises(HTTPException) as rejected:
        await broker.switch_mode("live")
    assert rejected.value.status_code == 503
    assert broker.mode == "text" and broker.active == "llm1b"
    assert {key for key, alive in control.alive.items() if alive} == {"llm1b"}
    assert path.read_bytes() == original and not broker.lock.locked()
    assert not list(path.parent.glob(".studio-mode-*"))
    await broker.close()


async def test_mode_switch_waits_for_generation_to_release_global_lock(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def post(request, key):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=b"finished")

    broker, control = setup_broker(tmp_path, post)
    await broker.initialize()
    generation = asyncio.create_task(broker.forward("llm2b", "completion", b"{}"))
    await entered.wait()
    switching = asyncio.create_task(broker.switch_mode("live"))
    await asyncio.sleep(0)
    assert not control.alive["tts"] and not switching.done()
    release.set()
    await generation
    assert (await switching)["mode"] == "live"
    assert control.alive["tts"]
    await broker.close()


async def test_cancelled_mode_load_restores_previous_live_residents_before_unlock(tmp_path):
    broker, control = setup_broker(tmp_path, mode="live")
    await broker.initialize()
    entered, finish = asyncio.Event(), asyncio.Event()
    activate = broker._activate
    restoring = False

    async def blocked_activate(key, **options):
        if not restoring and key == "llm2b":
            entered.set()
            await finish.wait()
        await activate(key, **options)

    broker._activate = blocked_activate
    restore = broker._restore

    async def restore_before_unlock(*args):
        nonlocal restoring
        restoring = True
        await restore(*args)

    broker._restore = restore_before_unlock
    task = asyncio.create_task(broker.switch_mode("text"))
    await entered.wait()
    assert not control.alive["tts"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.mode == "live" and not broker.lock.locked()
    assert {key for key, alive in control.alive.items() if alive} == {"llm2b", "tts"}
    assert (await broker.observe())["state"] == "active"
    assert not (tmp_path / "runtime/studio.json").exists()
    await broker.close()


async def test_mode_endpoint_authentication_validation_and_read_only_observers(tmp_path):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.post("/mode", json={"mode": "live"})).status_code == 401
        client.headers[HEADER] = token
        for body in ({"mode": "exclusive"}, {"mode": "live", "path": "custom"}, {"mode": []}):
            assert (await client.post("/mode", json=body)).status_code == 422
        assert control.events == []
        response = await client.post("/mode", json={"mode": "live"})
        assert response.status_code == 200 and response.json()["mode"] == "live"
        before = control.events[:]
        assert (await client.get("/health")).json()["state"] == "active"
        assert (await client.get("/tts/docs")).status_code == 200
        assert (await client.get("/llm/1b/health")).status_code == 200
        assert control.events == before
        assert (await client.post("/llm/1b/warmup", json={})).status_code == 409
    await broker.close()


async def test_configured_model_and_mode_initialize_the_selected_pair(tmp_path):
    broker, control = setup_broker(tmp_path, mode="live", selected="llm1b")
    path = tmp_path / "runtime/studio.json"
    path.write_text(json.dumps({"llm": {"model": "MiniCPM5-1B"}, "inference_broker": {"mode": "live"}}))
    app = create_app(tmp_path, warm_start=False)
    assert app.state.broker.selected == "llm1b" and app.state.broker.mode == "live"
    await app.state.broker.close()
    await broker.initialize()
    assert control.events == [("start", "tts"), ("start", "llm1b")]
    assert (await broker.observe())["selected_provider"] == "llm1b"
    legacy = InferenceBroker(tmp_path, mode="exclusive")
    assert legacy.mode == "text"
    await legacy.close()
    await broker.close()


async def test_observers_do_not_wake_sleeping_resources_and_warmup_is_explicit(tmp_path):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.get("/health")).status_code == 401
        client.headers[HEADER] = token
        for path in ("/health", "/llm/2b/health", "/tts/docs"):
            assert (await client.get(path)).status_code == 200
        assert control.events == []
        assert (await client.get("/llm/2b/props")).status_code == 503
        response = await client.post("/llm/2b/warmup", json={})
        assert response.status_code == 200
        assert response.json()["default_generation_settings"]["n_ctx"] == 131072
        await broker.forward("llm1b", "completion", b"{}")
        before = control.events[:]
        assert (await client.get("/llm/2b/props")).json()["is_sleeping"] is True
        assert control.events == before
    await broker.close()


async def test_speech_observer_preserves_actual_backend_identity_without_starting_workers(tmp_path):
    broker, control = setup_broker(tmp_path)
    calls = []
    state = {"ready": True}

    async def get(url, **kwargs):
        calls.append(str(url))
        return httpx.Response(
            200 if state["ready"] else 503,
            request=httpx.Request("GET", url),
            json={
                "status": "ready",
                "backend": "genie_onnx_int8",
                "voice_id": "whale_sovits_v2",
                "bundle_identity": "a" * 64,
                "ignored_untrusted_field": "never forwarded",
            },
        )

    broker.client.get = get
    assert (await broker.read("tts", "docs"))["status"] == "cold"
    assert calls == [] and control.events == []
    control.alive["tts"] = True
    body = await broker.read("tts", "docs")
    assert body["backend"] == "genie_onnx_int8" and body["bundle_identity"] == "a" * 64
    assert body["status"] == "ready" and not body["is_sleeping"]
    assert "ignored_untrusted_field" not in body
    state["ready"] = False
    with pytest.raises(HTTPException) as error:
        await broker.read("tts", "docs")
    assert error.value.status_code == 503
    assert control.events == []
    await broker.close()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/llm/2b/arbitrary", {}),
        ("/llm/other/completion", {}),
        ("/tts/set_gpt_weights", {}),
        ("/llm/2b/completion", {"model": "another-model"}),
        ("/llm/2b/completion", {"stream": True}),
        ("/tts/tts", {"ref_audio_path": "/etc/passwd"}),
    ],
)
async def test_paths_models_streams_and_reference_files_are_restricted(tmp_path, path, body):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        assert (await client.post(path, json=body)).status_code in {404, 422}
    assert control.events == []
    await broker.close()


@pytest.mark.parametrize("chunked", [False, True])
async def test_body_limit_rejects_before_resource_activation(tmp_path, chunked):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()

    async def chunks():
        yield b"x" * MAX_BODY
        yield b"x"

    data = chunks() if chunked else b"x" * (MAX_BODY + 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        assert (await client.post("/llm/2b/completion", content=data)).status_code == 413
    assert control.events == []
    await broker.close()


@pytest.mark.parametrize("operation", ["generation", "mode", "mode_llm"])
async def test_real_asgi_disconnect_cancels_owned_upstream_and_unlocks(tmp_path, operation):
    entered = asyncio.Event()

    async def post(request, key):
        entered.set()
        await asyncio.Event().wait()

    broker, control = setup_broker(tmp_path, post)
    await broker.initialize()
    mode_change = operation != "generation"
    if mode_change:
        start = control.start
        blocked = False

        async def loading(key):
            nonlocal blocked
            await start(key)
            if key == ("tts" if operation == "mode" else "llm2b") and not blocked:
                blocked = True
                entered.set()
                await asyncio.Event().wait()

        control.start = loading
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    incoming, sent = asyncio.Queue(), []
    body = b'{"mode":"live"}' if mode_change else b"{}"
    path = "/mode" if mode_change else "/llm/2b/completion"
    incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (HEADER.lower().encode(), token.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("broker", 80),
        "client": ("operator", 1234),
    }
    work = asyncio.create_task(app(scope, incoming.get, send))
    await entered.wait()
    incoming.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(work, 2)
    assert sent[0]["status"] == 499
    assert not broker.lock.locked()
    assert control.alive["llm2b"] is mode_change
    assert not control.alive["tts"] and broker.mode == "text"
    await broker.close()


async def test_process_runner_owns_session_and_confirms_group_stop(tmp_path, monkeypatch):
    calls = []

    class Process:
        pid = 12345
        returncode = None

        async def wait(self):
            calls.append("wait")
            self.returncode = 0
            return 0

    process = Process()
    runner = ProcessRunner(tmp_path)
    (tmp_path / "runtime").mkdir()
    gguf(tmp_path / "runtime/models" / RESOURCES["llm2b"].model)

    async def profiles():
        return {"llm2b": {"command": runner.command("llm2b")}}

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    runner._advise_owned_model_cache = lambda: calls.append("cache_advice")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    runner.validate_all = profiles
    await runner.start("llm2b")
    assert calls[0] == "cache_advice"
    args, options = calls[1]
    assert args[0] == str(runner.llama_binary)
    assert options["start_new_session"] is True and "shell" not in options
    assert options["env"]["TUBER_GPT_SOVITS_ROOT"] == "/opt/GPT-SoVITS-v2"
    await runner.stop("llm2b")
    assert (process.pid, signal.SIGTERM) in calls and (process.pid, signal.SIGKILL) in calls
    assert "wait" in calls and not runner.processes


async def test_cancel_during_spawn_acquires_and_stops_child_before_return(tmp_path, monkeypatch):
    spawned, finish_spawn = asyncio.Event(), asyncio.Event()
    signals = []

    class Process:
        pid = 12346
        returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

    async def spawn(*args, **kwargs):
        spawned.set()
        await finish_spawn.wait()
        return Process()

    runner = ProcessRunner(tmp_path)
    (tmp_path / "runtime").mkdir()
    gguf(tmp_path / "runtime/models" / RESOURCES["llm2b"].model)

    async def profiles():
        return {"llm2b": {"command": runner.command("llm2b")}}

    runner.validate_all = profiles
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    work = asyncio.create_task(runner.start("llm2b"))
    await spawned.wait()
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done()
    finish_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert signals == [(12346, signal.SIGTERM), (12346, signal.SIGKILL)]
    assert not runner.processes and not runner.logs


@pytest.mark.parametrize(
    "configured,location,valid_wave,expected",
    [
        (True, "assets/oc/reference.wav", True, 200),
        (True, "runtime/tts/ref/new-oc.wav", True, 200),
        (False, "assets/oc/reference.wav", True, 422),
        (True, "runtime/private.wav", True, 422),
        (True, "assets/oc/reference.wav", False, 422),
    ],
)
async def test_tts_uses_operator_reference_whitelist_with_project_bounds(
    tmp_path,
    configured,
    location,
    valid_wave,
    expected,
):
    broker, control = setup_broker(tmp_path, mode="live")
    reference = tmp_path / location
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(b"RIFF\x04\x00\x00\x00WAVE" if valid_wave else b"not audio")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/voices.json").write_text(
        json.dumps(
            [{"id": "new-oc", "server_reference_path": str(reference)}] if configured else [],
        )
    )
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        response = await client.post("/tts/tts", json={"ref_audio_path": str(reference), "text": "你好"})
    assert response.status_code == expected
    assert control.alive["tts"] is (expected == 200)
    await broker.close()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/llm/2b/v1/chat/completions", {"tools": [{"function": {"name": "shell"}}]}),
        ("/llm/2b/completion", {"prompt": {"path": "/etc/passwd"}}),
        ("/llm/2b/apply-template", {"messages": [{"role": "tool", "content": "anything"}]}),
        ("/llm/2b/warmup", {"model_path": "/tmp/foreign.gguf"}),
        ("/tts/tts", {"weights_path": "/tmp/foreign.ckpt"}),
        ("/tts/control", {"command": "restart"}),
    ],
)
async def test_unsafe_inference_shapes_are_rejected_before_any_child_is_started(tmp_path, path, body):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        response = await client.post(path, json=body)
    assert response.status_code in {404, 422}
    assert control.events == [] and not any(control.alive.values())
    assert "/etc/passwd" not in response.text and "/tmp/foreign" not in response.text
    await broker.close()


async def test_model_symlink_outside_project_is_rejected(tmp_path):
    runner = ProcessRunner(tmp_path)
    model = tmp_path / "runtime/models" / RESOURCES["llm2b"].model
    outside = tmp_path.parent / "foreign-model.gguf"
    outside.write_bytes(b"not a project model")
    model.parent.mkdir(parents=True)
    try:
        model.symlink_to(outside)
    except OSError:
        pytest.skip("This platform does not permit unprivileged symlinks")
    runner.llama_binary = outside
    with pytest.raises(RuntimeError, match="outside"):
        await runner.validate_all()


def test_runner_commands_are_fixed_profiles(tmp_path):
    runner = ProcessRunner(tmp_path)
    args = runner.command("llm2b")
    assert args[0] == str(runner.llama_binary)
    assert args[args.index("--ctx-size") + 1] == "131072"
    assert args[args.index("--cache-type-k") + 1] == "q8_0"
    assert "--no-mmap" in args
    assert "--direct-io" in args
    speech = runner.command("tts")
    assert speech[1] == str(tmp_path.resolve() / "tools/jetson/tts/server.py")
    assert speech[2:] == ["-a", "127.0.0.1", "-p", "9011"]
    with pytest.raises(KeyError):
        runner.command("arbitrary; command")


@pytest.mark.parametrize("window", [4096, 65536, 131072, 262144])
def test_runner_uses_explicit_context_without_rope_override(tmp_path, window):
    runner = ProcessRunner(tmp_path, context_tokens=window)
    for key in ("llm2b", "llmqwen35"):
        argv = runner.command(key)
        assert argv[argv.index("--ctx-size") + 1] == str(window)
        assert not any(argument.startswith(("--rope", "--yarn")) for argument in argv)


@pytest.mark.parametrize("window", [True, False, "65536", 4096.0, 0, -1, 4095])
def test_runner_rejects_invalid_context_before_startup(tmp_path, window):
    with pytest.raises(ValueError, match="at least 4096"):
        ProcessRunner(tmp_path, context_tokens=window)


async def test_smaller_requested_window_loads_and_reports_actual_native_separately(tmp_path):
    broker, control = setup_broker(tmp_path, context_tokens=8192)
    gguf(tmp_path / "runtime/models" / RESOURCES["llm2b"].model, context=32768)
    await broker.initialize()
    status = await broker.observe()
    assert status["requested_context_tokens"] == 8192
    assert status["providers"]["llm2b"]["native_context_tokens"] == 32768
    assert status["providers"]["llm2b"]["context_tokens"] == 8192
    assert control.events == [("start", "llm2b")]
    await broker.close()


async def test_larger_window_requires_selected_real_native_not_other_installed_profiles(tmp_path):
    broker, control = setup_broker(tmp_path, selected="llmqwen35", context_tokens=262144)
    await broker.initialize()
    assert broker.properties["llmqwen35"]["default_generation_settings"]["n_ctx"] == 262144
    control.events.clear()
    with pytest.raises(HTTPException):
        await broker.forward("llm2b", "warmup", b"{}")
    assert control.events == [] and control.alive["llmqwen35"]
    assert not control.alive["llm2b"]
    await broker.close()


async def test_actual_props_must_match_requested_window_without_caching_false_capacity(tmp_path):
    broker, control = setup_broker(tmp_path, context_tokens=8192)
    await broker.client.aclose()

    def upstream(request):
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"model_alias": "MiniCPM5-2B", "default_generation_settings": {"n_ctx": 131072}}
            )
        return httpx.Response(200, json={"status": "ok"})

    broker.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with pytest.raises(RuntimeError, match="does not match"):
        await broker.initialize()
    assert "llm2b" not in broker.properties
    assert not control.alive["llm2b"]
    await broker.close()


async def test_studio_claim_cannot_expand_the_gguf_native_limit(tmp_path):
    broker, control = setup_broker(tmp_path, context_tokens=262144)
    (tmp_path / "runtime/studio.json").write_text(
        json.dumps({"llm": {"context_tokens": 262144, "native_context_tokens": 262144}})
    )
    with pytest.raises(RuntimeError, match="natively"):
        await broker.initialize()
    assert broker.natives["llm2b"] == 131072 and not control.events
    await broker.close()


@pytest.mark.parametrize("kind", ["openai_compatible", "demo"])
async def test_external_llm_boot_keeps_broker_cold_and_uses_its_own_model_window(tmp_path, kind):
    path = tmp_path / "runtime/studio.json"
    path.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "llm": {"kind": kind, "model": "remote-model", "context_tokens": 262144},
            "inference_broker": {"model": "MiniCPM5-1B", "context_tokens": 65536, "mode": "live"},
        }
    )
    path.write_text(original)
    app = create_app(tmp_path)
    manager = app.state.broker

    async def forbidden():
        pytest.fail("External API boot must not warm local workers")

    manager.initialize = forbidden
    async with app.router.lifespan_context(app):
        assert manager.selected == "llm1b" and manager.context_tokens == 65536
        assert (
            manager.runner.command("llm1b")[manager.runner.command("llm1b").index("--ctx-size") + 1]
            == "65536"
        )
        token = (path.parent / "inference-token").read_text()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
        ) as client:
            health = (await client.get("/health")).json()
            assert health["state"] == "cold" and health["mode"] == "text"
            assert health["requested_context_tokens"] == 65536
            assert all(
                not item["running"] and item["native_context_tokens"] is None
                for item in health["providers"].values()
            )
            assert (await client.get("/llm/1b/props")).status_code == 503
    assert path.read_text() == original


async def test_external_llm_uses_default_local_profile_without_copying_remote_window(tmp_path):
    path = tmp_path / "runtime/studio.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"llm": {"kind": "openai_compatible", "model": "remote-256k", "context_tokens": 262144}})
    )
    app = create_app(tmp_path, warm_start=False)
    assert app.state.broker.selected == "llm2b"
    assert app.state.broker.context_tokens == 131072
    await app.state.broker.close()


@pytest.mark.parametrize("kind", ["openai_compatible", "prerecorded", None])
async def test_factory_external_speech_starts_text_without_rewriting_live_configuration(
    tmp_path, monkeypatch, kind
):
    from tuber_studio import inference

    original_broker, control = setup_broker(tmp_path)
    config = {"llm": {"model": "MiniCPM5-2B"}, "inference_broker": {"mode": "live"}}
    if kind is not None:
        config["tts"] = {"kind": kind}
    path = tmp_path / "runtime/studio.json"
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    monkeypatch.setattr(inference, "ProcessRunner", lambda *a, **k: control)
    monkeypatch.setattr(
        inference,
        "InferenceBroker",
        lambda root, **kwargs: InferenceBroker(root, client=original_broker.client, **kwargs),
    )
    app = inference.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        expected = [("start", "tts"), ("start", "llm2b")] if kind is None else [("start", "llm2b")]
        assert control.events == expected
        assert app.state.broker.mode == ("live" if kind is None else "text")
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", [None, "llama_cpp", "openai_compatible"])
async def test_context_transition_reloads_actual_window_and_only_updates_its_owned_settings(tmp_path, kind):
    broker, control = setup_broker(tmp_path)
    llm = {"model": "remote" if kind == "openai_compatible" else "MiniCPM5-2B", "context_tokens": 262144}
    if kind is not None:
        llm["kind"] = kind
    config = {
        "llm": llm,
        "inference_broker": {"model": "MiniCPM5-2B", "context_tokens": 131072},
        "secret": "preserved",
        "other_setting": [1, 2],
    }
    path = tmp_path / "runtime/studio.json"
    path.write_text(json.dumps(config))
    await broker.forward("llm1b", "warmup", b"{}")
    assert "llm1b" in broker.properties
    control.events.clear()
    result = await broker.change_context(65536)
    assert result["requested_context_tokens"] == 65536 and result["busy"] is False
    assert result["providers"]["llm2b"]["context_tokens"] == 65536
    assert broker.context_tokens == control.context_tokens == 65536
    assert control.events == [("stop", "llm1b"), ("start", "llm2b")]
    assert set(broker.properties) == {"llm2b"}
    expected = json.loads(json.dumps(config))
    expected["inference_broker"]["context_tokens"] = 65536
    if kind != "openai_compatible":
        expected["llm"]["context_tokens"] = 65536
    assert json.loads(path.read_text()) == expected
    await broker.close()


async def test_context_over_native_is_rejected_before_stopping_resident_model(tmp_path):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    control.events.clear()
    with pytest.raises(HTTPException) as rejected:
        await broker.change_context(262144)
    assert rejected.value.status_code == 422
    assert not control.events and control.alive["llm2b"]
    assert broker.context_tokens == 131072
    assert broker.properties["llm2b"]["default_generation_settings"]["n_ctx"] == 131072
    await broker.close()


async def test_context_cannot_use_stale_cached_native_when_artifact_is_invalid(tmp_path):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    control.events.clear()
    (tmp_path / "runtime/models" / RESOURCES["llm2b"].model).write_bytes(b"invalid")
    with pytest.raises(HTTPException) as rejected:
        await broker.change_context(8192)
    assert rejected.value.status_code == 503
    assert not control.events and control.alive["llm2b"]
    assert broker.context_tokens == 131072
    await broker.close()


@pytest.mark.parametrize("failure", ["load", "persist"])
async def test_context_failure_restores_old_process_window_props_and_config(tmp_path, failure):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    path = tmp_path / "runtime/studio.json"
    original = '{"llm":{"kind":"llama_cpp","context_tokens":131072},"secret":"unchanged"}'
    path.write_text(original)
    start = control.start

    async def failing_start(key):
        await start(key)
        if failure == "load" and control.context_tokens == 8192:
            raise RuntimeError("new worker failed")

    def failing_persist():
        raise OSError("read-only or full filesystem")

    control.start = failing_start
    if failure == "persist":
        broker._persist_context = failing_persist
    with pytest.raises(HTTPException) as rejected:
        await broker.change_context(8192)
    assert rejected.value.status_code == 503
    assert broker.context_tokens == control.context_tokens == 131072
    assert control.alive["llm2b"] and not control.alive["tts"]
    assert broker.properties["llm2b"]["default_generation_settings"]["n_ctx"] == 131072
    assert control.events[-1] == ("start", "llm2b")
    assert path.read_text() == original and not broker.lock.locked()
    await broker.close()


async def test_context_cancellation_restores_old_window_and_never_advertises_stale_new_props(tmp_path):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    path = tmp_path / "runtime/studio.json"
    original = '{"llm":{"context_tokens":131072}}'
    path.write_text(original)
    get = broker.client.get
    entered = asyncio.Event()

    async def loading(url, **options):
        if control.context_tokens == 8192:
            entered.set()
            await asyncio.Event().wait()
        return await get(url, **options)

    broker.client.get = loading
    work = asyncio.create_task(broker.change_context(8192))
    await entered.wait()
    assert broker.properties == {} and broker.lock.locked()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert control.context_tokens == broker.context_tokens == 131072
    assert control.alive["llm2b"] and not broker.lock.locked()
    assert broker.properties["llm2b"]["default_generation_settings"]["n_ctx"] == 131072
    assert path.read_text() == original
    await broker.close()


@pytest.mark.parametrize("operation", ["context", "idle"])
@pytest.mark.parametrize("blocked_by", ["busy", "live"])
async def test_context_and_idle_reject_busy_or_live_mode_without_resource_changes(
    tmp_path, operation, blocked_by
):
    broker, control = setup_broker(tmp_path, mode="live" if blocked_by == "live" else "text")
    if blocked_by == "busy":
        await broker.lock.acquire()
    try:
        with pytest.raises(HTTPException) as rejected:
            await (broker.idle() if operation == "idle" else broker.change_context(8192))
        assert rejected.value.status_code == 409 and not control.events
    finally:
        if broker.lock.locked():
            broker.lock.release()
        await broker.close()


async def test_idle_stops_only_owned_workers_and_does_not_change_configuration(tmp_path):
    broker, control = setup_broker(tmp_path)
    await broker.initialize()
    path = tmp_path / "runtime/studio.json"
    original = '{"llm":{"kind":"openai_compatible","context_tokens":262144},"secret":"preserve"}'
    path.write_text(original)
    control.events.clear()
    result = await broker.idle()
    assert result["state"] == "cold" and result["active_provider"] is None and result["busy"] is False
    assert control.events == [("stop", "llm2b")]
    assert all(not state["running"] for state in result["providers"].values())
    assert path.read_text() == original
    assert (await broker.read("llm2b", "props"))["is_sleeping"]
    await broker.close()


@pytest.mark.parametrize("operation,body", [("context", {"context_tokens": 8192}), ("idle", {})])
async def test_authenticated_context_and_idle_routes_return_resource_health(tmp_path, operation, body):
    broker, _ = setup_broker(tmp_path)
    await broker.initialize()
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        response = await client.post("/" + operation, json=body)
    assert response.status_code == 200
    health = response.json()
    assert health["busy"] is False and health["mode"] == "text"
    if operation == "context":
        assert health["requested_context_tokens"] == 8192
        assert health["providers"]["llm2b"]["context_tokens"] == 8192
    else:
        assert health["state"] == "cold" and health["active_provider"] is None
    await broker.close()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/context", {}),
        ("/context", {"context_tokens": True}),
        ("/context", {"context_tokens": 4095}),
        ("/context", {"context_tokens": 8192, "command": "x"}),
        ("/idle", {"pid": 123}),
        ("/idle", {"command": "stop"}),
    ],
)
async def test_context_and_idle_require_auth_and_exact_typed_bodies(tmp_path, path, body):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.post(path, json=body)).status_code == 401
        response = await client.post(path, json=body, headers={HEADER: token})
        assert response.status_code == 422 and not control.events
    await broker.close()


async def test_runner_checks_native_window_before_stopping_or_spawning(tmp_path, monkeypatch):
    runner = ProcessRunner(tmp_path, context_tokens=65536)
    gguf(tmp_path / "runtime/models" / RESOURCES["llm2b"].model, context=32768)
    events = []

    async def profiles():
        return {"llm2b": {"command": runner.command("llm2b")}}

    async def stop(key):
        events.append(key)

    runner.validate_all = profiles
    runner.stop = stop
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: pytest.fail("Must not spawn"))
    with pytest.raises(RuntimeError, match="natively"):
        await runner.start("llm2b")
    assert not events


async def test_broker_rejects_output_budget_above_configured_actual_window_before_activation(tmp_path):
    broker, control = setup_broker(tmp_path, context_tokens=4096)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        rejected = await client.post("/llm/2b/completion", json={"prompt": "hello", "n_predict": 4097})
        assert rejected.status_code == 422 and not control.events
        accepted = await client.post("/llm/2b/completion", json={"prompt": "hello", "n_predict": 4096})
        assert accepted.status_code == 200
    await broker.close()


def test_native_window_is_read_from_actual_gguf(tmp_path):
    path = tmp_path / "model.gguf"
    gguf(path, 131072)
    assert native_context(path) == 131072


@pytest.mark.parametrize("architecture_last", [False, True])
def test_native_window_matches_architecture_and_ignores_vision_context(tmp_path, architecture_last):
    path = tmp_path / "qwen35.gguf"
    gguf(path, 262144, "qwen35", vision_context=8192, architecture_last=architecture_last)
    assert native_context(path) == 262144


def test_vision_window_cannot_stand_in_for_missing_language_window(tmp_path):
    path = tmp_path / "vision-only-metadata.gguf"
    gguf(path, 262144, "qwen35", vision_context=8192)
    path.write_bytes(path.read_bytes().replace(b"qwen35.context_length", b"vision.context_length"))
    with pytest.raises(ValueError, match="no native context"):
        native_context(path)


def test_candidate_identity_cannot_use_another_architecture_with_the_same_window(tmp_path):
    path = tmp_path / "wrong-model.gguf"
    gguf(path, 262144)
    with pytest.raises(ValueError, match="architecture does not match"):
        native_context(path, expected_architecture="qwen35")


async def test_fixed_qwen_text_route_is_explicit_and_respects_live_selection(tmp_path):
    broker, control = setup_broker(tmp_path)
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
    ) as client:
        assert (await client.get("/llm/qwen35/health")).status_code == 200
        assert control.events == []
        wrong = await client.post("/llm/qwen35/completion", json={"model": "MiniCPM5-2B"})
        assert wrong.status_code == 422 and control.events == []
        response = await client.post("/llm/qwen35/warmup", json={})
        assert response.status_code == 200 and response.json()["model_alias"] == "Qwen3.5-2B"
        assert broker.natives["llmqwen35"] == 262144
        assert not control.alive["tts"] and control.alive["llmqwen35"]
        await broker.switch_mode("live")
        assert (await client.post("/llm/qwen35/warmup", json={})).status_code == 409
        assert not control.alive["llmqwen35"]
    await broker.close()


async def test_qwen_alias_can_be_selected_without_changing_default_configuration(tmp_path):
    (tmp_path / "runtime").mkdir()
    path = tmp_path / "runtime/studio.json"
    original = json.dumps({"llm": {"model": "Qwen3.5-2B"}, "inference_broker": {"mode": "text"}})
    path.write_text(original)
    app = create_app(tmp_path, warm_start=False)
    assert app.state.broker.selected == "llmqwen35"
    assert path.read_text() == original
    await app.state.broker.close()


def test_qwen_profile_uses_fixed_text_weights_full_window_and_documented_sampling(tmp_path):
    runner = ProcessRunner(tmp_path, kv_cache_type="q4_0")
    command = runner.command("llmqwen35")
    expected = {
        "--alias": "Qwen3.5-2B",
        "--port": "9015",
        "--ctx-size": "131072",
        "--cache-type-k": "q8_0",
        "--cache-type-v": "q8_0",
        "--reasoning": "auto",
        "--temp": "1.0",
        "--top-p": "1.0",
        "--top-k": "20",
        "--min-p": "0.0",
        "--presence-penalty": "2.0",
        "--repeat-penalty": "1.0",
        "--cache-ram": "0",
        "--ctx-checkpoints": "0",
        "--cache-reuse": "0",
    }
    for flag, value in expected.items():
        assert command[command.index(flag) + 1] == value
    assert command[command.index("-m") + 1] == str(
        tmp_path.resolve() / "runtime/models/Qwen_Qwen3.5-2B-Q4_K_M.gguf"
    )
    assert "--mmproj" not in command
    assert "--no-context-shift" in command
    assert "--ctx-checkpoints" not in runner.command("llm2b")


@pytest.mark.parametrize("candidate,use_runtime", [(False, True), (False, False), (True, True)])
def test_initializer_only_prepares_selected_or_explicit_candidate(
    tmp_path, monkeypatch, candidate, use_runtime
):
    source = Path(__file__).resolve().parents[1] / "tools/jetson/initialize-data.py"
    spec = importlib.util.spec_from_file_location("initialize_data", source)
    initializer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(initializer)
    configuration = tmp_path / ("runtime/studio.json" if use_runtime else "config/studio.example.json")
    configuration.parent.mkdir(parents=True)
    alias = "MiniCPM5-2B" if candidate else "Qwen3.5-2B"
    data = {"llm": {"model": alias}, "inference_broker": {"mode": "text"}, "secret": "retained"}
    original = json.dumps(data)
    configuration.write_text(original)
    events = []
    monkeypatch.setenv("TUBER_ROOT", str(tmp_path))
    monkeypatch.setattr(
        sys, "argv", ["initialize-data.py", "--offline"] + (["--candidate", "qwen35"] if candidate else [])
    )
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(initializer, "ensure_nltk_data", lambda *args: events.append("nltk"))
    monkeypatch.setattr(initializer, "verify_nltk_consumers", lambda *args: None)
    monkeypatch.setattr(initializer, "prepare_llm", lambda root, key, offline: events.append((key, offline)))
    monkeypatch.setattr(initializer.runpy, "run_path", lambda *args, **kwargs: events.append("tts"))
    initializer.main()
    assert events == ([("llmqwen35", True)] if candidate else ["nltk", ("llmqwen35", True), "tts"])
    assert configuration.read_text() == original
    if not candidate:
        assert json.loads((tmp_path / "runtime/studio.json").read_text()) == data


@pytest.mark.parametrize("model_key", ["llm2b", "llmqwen35"])
async def test_only_selected_model_is_required_on_fresh_installation(tmp_path, model_key):
    binary = tmp_path / "fake-binary"
    binary.write_bytes(b"executable placeholder")
    gguf(
        tmp_path / "runtime/models" / RESOURCES[model_key].model,
        262144 if model_key == "llmqwen35" else 131072,
        "qwen35" if model_key == "llmqwen35" else "llama",
    )
    for relative in (
        "tools/jetson/tts/server.py",
        "tools/jetson/tts/compact.py",
        "tools/jetson/tts/inference_only.py",
        "tools/jetson/tts/serve.py",
        "runtime/tts/tts_infer.yaml",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder")
    runner = ProcessRunner(tmp_path, llama_binary=binary, tts_python=binary)
    assert set(await runner.validate_all()) == {model_key, "tts"}


async def test_missing_selected_model_still_fails_without_starting_another_model(tmp_path):
    broker, control = setup_broker(tmp_path)
    control.alive.pop("llm2b")
    with pytest.raises(RuntimeError, match="not installed"):
        await broker.initialize()
    assert control.events == []
    await broker.close()


async def test_selected_profile_has_one_studio_configuration_owner(tmp_path):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(
        json.dumps(
            {
                "inference_broker": {
                    "mode": "exclusive",
                    "kv_cache_type": "q4_0",
                    "cuda_managed_memory": False,
                },
                "llm": {"context_tokens": 65536},
            }
        )
    )
    app = create_app(tmp_path, warm_start=False)
    manager = app.state.broker
    args = manager.runner.command("llm2b")
    assert args[args.index("--cache-type-k") + 1] == "q4_0"
    assert args[args.index("--ctx-size") + 1] == "65536"
    assert manager.context_tokens == 65536
    assert manager.runner.cuda_managed_memory is False
    assert (await manager.observe())["kv_cache_type"] == "q4_0"
    assert not (tmp_path / "runtime/inference-profile.json").exists()
    await manager.close()


async def test_installed_package_uses_explicit_stack_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TUBER_ROOT", str(tmp_path))
    app = create_app(warm_start=False)
    assert app.state.broker.root == tmp_path.resolve()
    assert (tmp_path / "runtime/inference-token").is_file()
    await app.state.broker.close()


async def test_resource_startup_failure_keeps_authenticated_observer_available(tmp_path):
    manager, _ = setup_broker(tmp_path)

    async def fail():
        raise RuntimeError("resource unavailable")

    manager.initialize = fail
    app = create_app(tmp_path, broker=manager)
    token = (tmp_path / "runtime/inference-token").read_text()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={HEADER: token}
        ) as client:
            response = await client.get("/health")
            assert response.status_code == 200 and response.json()["state"] == "error"
            assert (await client.get("/llm/2b/props")).status_code == 503
            assert manager.properties == {}


def cache_advice_support(monkeypatch, implementation):
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    monkeypatch.setattr(os, "posix_fadvise", implementation, raising=False)


def test_cache_advice_only_touches_fixed_model_directories_and_suffixes(tmp_path, monkeypatch):
    wanted = {
        "models/a.gguf",
        "tts/weights/b.pth",
        "tts/weights/c.ckpt",
        "tts/pretrained/d.bin",
        "tts/pretrained/e.safetensors",
    }
    ignored = {"models/config.json", "tts/ref/reference.wav", "private/other.gguf"}
    for relative in wanted | ignored:
        path = tmp_path / "runtime" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    seen, descriptors = [], []

    def advise(descriptor, offset, length, advice):
        assert (offset, length, advice) == (0, 0, 4)
        seen.append(os.read(descriptor, 200).decode())
        descriptors.append(descriptor)

    cache_advice_support(monkeypatch, advise)
    runner = ProcessRunner(tmp_path)
    assert runner._advise_owned_model_cache() == len(wanted)
    assert set(seen) == wanted
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_cache_advice_refuses_escaped_and_looping_links(tmp_path, monkeypatch):
    models = tmp_path / "runtime/models"
    models.mkdir(parents=True)
    (models / "good.gguf").write_text("owned")
    foreign = tmp_path / "outside.gguf"
    foreign.write_text("foreign")
    try:
        (models / "escape.gguf").symlink_to(foreign)
        (models / "loop.gguf").symlink_to("loop.gguf")
    except OSError:
        pytest.skip("This platform does not permit unprivileged symlinks")
    seen = []
    cache_advice_support(monkeypatch, lambda fd, *_: seen.append(os.read(fd, 200)))
    assert ProcessRunner(tmp_path)._advise_owned_model_cache() == 1
    assert seen == [b"owned"]


def test_cache_advice_closes_file_when_hint_fails(tmp_path, monkeypatch):
    model = tmp_path / "runtime/models/model.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    descriptors = []

    def fail(descriptor, *_):
        descriptors.append(descriptor)
        raise OSError("Cache hint unsupported")

    cache_advice_support(monkeypatch, fail)
    assert ProcessRunner(tmp_path)._advise_owned_model_cache() == 0
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_cache_advice_ignores_open_failure(tmp_path, monkeypatch):
    model = tmp_path / "runtime/models/model.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    cache_advice_support(monkeypatch, lambda *_: pytest.fail("No FD was opened"))

    def denied(*_):
        raise PermissionError("Model is not readable")

    monkeypatch.setattr(os, "open", denied)
    assert ProcessRunner(tmp_path)._advise_owned_model_cache() == 0
