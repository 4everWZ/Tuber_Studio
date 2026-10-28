import asyncio
import json
import os
import signal
import struct

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


def gguf(path, context=131072):
    key = b"llama.context_length"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + struct.pack("<IQQQ", 3, 0, 1, len(key)) + key + struct.pack("<II", 4, context))


def setup_broker(root, post=None, mode="text", selected="llm2b"):
    for resource in RESOURCES.values():
        if resource.model:
            gguf(root / "runtime/models" / resource.model)
    control = Control()

    async def handle(request):
        key = next(key for key, spec in RESOURCES.items() if request.url.port == httpx.URL(spec.url).port)
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={"model_alias": RESOURCES[key].alias, "default_generation_settings": {"n_ctx": 131072}},
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
    assert control.events == [("start", "llm2b"), ("start", "tts")]
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
    assert control.events == [("stop", "llm1b"), ("start", "llm2b"), ("start", "tts")]
    expected = json.loads(json.dumps(configuration))
    expected["inference_broker"]["mode"] = "live"
    assert json.loads(path.read_text()) == expected
    control.events.clear()
    result = await broker.switch_mode("text")
    assert control.events == [("stop", "tts")]
    assert result["mode"] == "text" and result["providers"]["llm2b"]["running"]
    assert not list(path.parent.glob(".studio-mode-*"))
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


@pytest.mark.parametrize("failure", ["tts", "persist"])
async def test_mode_failure_restores_previous_candidate_and_configuration(tmp_path, failure, monkeypatch):
    broker, control = setup_broker(tmp_path)
    path = tmp_path / "runtime/studio.json"
    path.write_text('{"inference_broker":{"mode":"text"},"private":"keep"}')
    original = path.read_bytes()
    await broker.forward("llm1b", "completion", b"{}")
    if failure == "tts":
        start = control.start

        async def fail_start(key):
            await start(key)
            if key == "tts":
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

    async def blocked_activate(key, **options):
        if broker.state != "restoring" and key == "llm2b":
            entered.set()
            await finish.wait()
        await activate(key, **options)

    broker._activate = blocked_activate
    restore = broker._restore

    async def restore_before_unlock(*args):
        broker.state = "restoring"
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
    assert control.events == [("start", "llm1b"), ("start", "tts")]
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


@pytest.mark.parametrize("operation", ["generation", "mode"])
async def test_real_asgi_disconnect_cancels_owned_upstream_and_unlocks(tmp_path, operation):
    entered = asyncio.Event()

    async def post(request, key):
        entered.set()
        await asyncio.Event().wait()

    broker, control = setup_broker(tmp_path, post)
    await broker.initialize()
    if operation == "mode":
        start = control.start

        async def loading(key):
            await start(key)
            if key == "tts":
                entered.set()
                await asyncio.Event().wait()

        control.start = loading
    app = create_app(tmp_path, broker=broker, warm_start=False)
    token = (tmp_path / "runtime/inference-token").read_text()
    incoming, sent = asyncio.Queue(), []
    body = b'{"mode":"live"}' if operation == "mode" else b"{}"
    path = "/mode" if operation == "mode" else "/llm/2b/completion"
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
    assert control.alive["llm2b"] is (operation == "mode")
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
    with pytest.raises(KeyError):
        runner.command("arbitrary; command")


def test_native_window_is_read_from_actual_gguf(tmp_path):
    path = tmp_path / "model.gguf"
    gguf(path, 131072)
    assert native_context(path) == 131072


async def test_optional_1b_models_do_not_block_default_2b_prerequisites(tmp_path):
    binary = tmp_path / "fake-binary"
    binary.write_bytes(b"executable placeholder")
    gguf(tmp_path / "runtime/models" / RESOURCES["llm2b"].model)
    for relative in ("tools/jetson/tts/serve.py", "runtime/tts/tts_infer.yaml"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder")
    runner = ProcessRunner(tmp_path, llama_binary=binary, tts_python=binary)
    assert set(await runner.validate_all()) == {"llm2b", "tts"}


async def test_selected_profile_has_one_studio_configuration_owner(tmp_path):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(
        json.dumps(
            {
                "inference_broker": {
                    "mode": "exclusive",
                    "kv_cache_type": "q4_0",
                    "cuda_managed_memory": False,
                }
            }
        )
    )
    app = create_app(tmp_path, warm_start=False)
    manager = app.state.broker
    args = manager.runner.command("llm2b")
    assert args[args.index("--cache-type-k") + 1] == "q4_0"
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
