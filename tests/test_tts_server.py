import array
import asyncio
import copy
import importlib.util
import io
import json
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def backend(tmp_path, monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "tools/jetson/tts"
    spec = importlib.util.spec_from_file_location("compact", folder / "compact.py")
    compact = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compact)
    monkeypatch.setitem(sys.modules, "compact", compact)
    spec = importlib.util.spec_from_file_location("tts_server_test", folder / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    reference = tmp_path / "assets/oc/ref.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"a configured reference")
    identity = {
        "paths": {"reference": str(reference)},
        "reference_text": "fixed reference",
        "prompt_text": "fixed reference.",
        "prompt_lang": "en",
    }
    signature = {
        "voice": {"id": "oc", "provider": "gpt_sovits", "params": {"text_lang": "zh", "prompt_lang": "en"}},
        "revision": 1,
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config/voices.json").write_text(
        json.dumps([signature["voice"], {"id": "new_oc", "provider": "gpt_sovits"}])
    )
    monkeypatch.setattr(compact, "reference_signature", lambda *a, **k: copy.deepcopy(signature))
    monkeypatch.setattr(compact, "_splits", lambda: {".", "。"})
    calls = []

    class Pipeline:
        fixed_reference_metadata = {"identity": identity}
        fixed_reference_signature = copy.deepcopy(signature)

        def run(self, params):
            calls.append(params)
            yield 32000, array.array("h", [3, -3, 100, -100] * 100)

    pipeline = Pipeline()
    engine = server.SpeechEngine(tmp_path, "oc", pipeline)
    return server, engine, pipeline, signature, calls


@asynccontextmanager
async def client_for(server, engine, tmp_path):
    app = server.create_app(tmp_path, voice_id="oc", engine=engine)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://speech") as client,
    ):
        yield client


async def test_only_health_and_buffered_speech_are_exposed(backend, tmp_path):
    server, engine, _, _, calls = backend
    async with client_for(server, engine, tmp_path) as client:
        health = await client.get("/docs")
        assert health.status_code == 200 and health.json()["voice_id"] == "oc"
        for path in ("/control?command=restart", "/set_gpt_weights", "/set_refer_audio", "/openapi.json"):
            assert (await client.get(path)).status_code == 404
        response = await client.post("/tts", json={"text": "你好", "seed": 42})
        assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            assert wav.getframerate() == 32000 and wav.getnframes() == 400
    assert calls[0]["seed"] == 42 and calls[0]["ref_audio_path"].endswith("ref.wav")


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hi", "ref_audio_path": "/etc/passwd"},
        {"text": "hi", "prompt_text": "a different voice"},
        {"text": "hi", "prompt_lang": "zh"},
    ],
)
async def test_another_reference_voice_is_rejected_without_model_call(backend, tmp_path, body):
    server, engine, _, _, calls = backend
    async with client_for(server, engine, tmp_path) as client:
        response = await client.post("/tts", json=body)
        assert response.status_code == 409
    assert calls == [] and not engine.failed


async def test_invalid_capability_parameters_and_oversized_body_are_rejected(backend, tmp_path):
    server, engine, _, _, calls = backend
    async with client_for(server, engine, tmp_path) as client:
        for body in (
            {"text": "hi", "weights_path": "/tmp/model.pth"},
            {"text": "hi", "streaming_mode": True},
        ):
            assert (await client.post("/tts", json=body)).status_code == 422
        assert (await client.post("/tts", content=b"x" * (server.MAX_BODY + 1))).status_code == 413
    assert calls == []


async def test_source_change_fails_closed_and_health_requires_restart(backend, tmp_path):
    server, engine, _, signature, calls = backend
    signature["revision"] = 2
    async with client_for(server, engine, tmp_path) as client:
        assert (await client.post("/tts", json={"text": "hi"})).status_code == 503
        assert (await client.get("/docs")).status_code == 503
    assert calls == [] and engine.failed


@pytest.mark.parametrize("failure", ["oom", "silent"])
async def test_oom_and_silent_fallback_never_become_success_wav(backend, tmp_path, failure):
    server, engine, pipeline, _, _ = backend

    def run(params):
        if failure == "oom":
            raise RuntimeError("CUDA out of memory with a private implementation path")
        yield 32000, array.array("h", [0] * 400)

    pipeline.run = run
    async with client_for(server, engine, tmp_path) as client:
        response = await client.post("/tts", json={"text": "hi"})
        assert response.status_code == 503 and "audio/wav" not in response.headers["content-type"]
        assert "private implementation path" not in response.text
        assert (await client.get("/docs")).status_code == 503


async def test_all_http_synthesis_is_serialized(backend, tmp_path):
    server, engine, _, _, _ = backend
    active, maximum = 0, 0
    original = engine.synthesize

    def synthesize(params):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.025)
        result = original(params)
        active -= 1
        return result

    engine.synthesize = synthesize
    async with client_for(server, engine, tmp_path) as client:
        replies = await asyncio.gather(*(client.post("/tts", json={"text": str(i)}) for i in range(3)))
    assert all(response.status_code == 200 for response in replies) and maximum == 1


async def test_cancelled_http_keeps_lock_until_real_synthesis_thread_exits(backend, tmp_path):
    server, engine, _, _, _ = backend
    entered, release = threading.Event(), threading.Event()
    original, calls = engine.synthesize, []

    def synthesize(params):
        calls.append(params["text"])
        if params["text"] == "first":
            entered.set()
            assert release.wait(3)
        return original(params)

    engine.synthesize = synthesize
    async with client_for(server, engine, tmp_path) as client:
        first = asyncio.create_task(client.post("/tts", json={"text": "first"}))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        first.cancel()
        second = asyncio.create_task(client.post("/tts", json={"text": "second"}))
        try:
            await asyncio.sleep(0.025)
            assert calls == ["first"] and not first.done() and not second.done()
        finally:
            release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError) and results[1].status_code == 200


async def test_health_is_not_ready_until_startup_pipeline_finishes(backend, tmp_path, monkeypatch):
    server, engine, _, _, _ = backend
    entered, release = threading.Event(), threading.Event()

    def load(root, selected):
        entered.set()
        assert release.wait(3)
        return engine

    monkeypatch.setattr(server.SpeechEngine, "load", load)
    app = server.create_app(tmp_path, voice_id="oc")
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://speech"
        ) as client:
            assert (await client.get("/docs")).status_code == 503
            release.set()
            await startup
            assert (await client.get("/docs")).status_code == 200
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        await lifespan.__aexit__(None, None, None)


@pytest.mark.parametrize("silent", [False, True])
async def test_startup_requires_complete_non_silent_warmup_before_health_ready(
    backend, tmp_path, monkeypatch, silent
):
    server, _, pipeline, _, _ = backend
    entered, release = threading.Event(), threading.Event()
    events = []

    def run(params):
        events.append(("warmup", params.copy()))
        entered.set()
        assert release.wait(3)
        try:
            yield 32000, array.array("h", ([0] if silent else [100, -100]) * 100)
        finally:
            events.append(("closed", None))

    pipeline.run = run
    monkeypatch.setattr(server.compact, "create_pipeline", lambda *a, **k: pipeline)
    app = server.create_app(tmp_path, voice_id="oc")
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    started = False
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        assert events[0][1]["seed"] == 42 and events[0][1]["text_lang"] == "zh"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://speech"
        ) as client:
            assert (await client.get("/docs")).status_code == 503
            release.set()
            if silent:
                with pytest.raises(RuntimeError, match="silent"):
                    await startup
                assert (await client.get("/docs")).status_code == 503
            else:
                await startup
                started = True
                assert (await client.get("/docs")).status_code == 200
                assert app.state.engine.request({"text": "business text"})["seed"] == -1
            assert events[-1][0] == "closed"
            assert len(events) == 2
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        if started:
            await lifespan.__aexit__(None, None, None)


def test_operator_voice_selection_uses_runtime_config(backend, tmp_path):
    server, _, _, _, _ = backend
    assert server.select_voice(tmp_path) == "oc"
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"voice_id": "new_oc"}}))
    assert server.select_voice(tmp_path) == "new_oc"
    assert server.select_voice(tmp_path, "oc") == "oc"
    with pytest.raises(ValueError):
        server.select_voice(tmp_path, "unknown_oc")


def test_reference_prepare_cli_never_starts_http_and_default_seed_is_preserved(
    backend, tmp_path, monkeypatch
):
    server, engine, _, _, _ = backend
    assert engine.request({"text": "hello"})["seed"] == -1
    calls = []
    monkeypatch.setenv("TUBER_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["server.py", "--prepare-reference", "--voice-id", "oc"])
    monkeypatch.setattr(server.compact, "prepare", lambda root, **kwargs: calls.append((root, kwargs)))
    monkeypatch.setattr(server, "create_app", lambda *a, **k: pytest.fail("Preparation must not start HTTP"))
    server.main()
    assert calls == [(tmp_path, {"voice_id": "oc"})]


def test_genie_factory_uses_selected_backend_and_retains_complete_startup_warmup(
    backend, tmp_path, monkeypatch
):
    server, _, pipeline, _, calls = backend
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"local_backend": "genie_onnx_int8"}}))
    pipeline.backend = "genie_onnx_int8"
    pipeline.backend_identity = "a" * 64
    selected = []

    def load(root, voice_id):
        selected.append((root, voice_id))
        return pipeline

    monkeypatch.setattr(server, "_genie_pipeline", load)
    monkeypatch.setattr(server.compact, "create_pipeline", lambda *a, **k: pytest.fail("Wrong backend"))
    engine = server.SpeechEngine.load(tmp_path, "oc")
    assert selected == [(tmp_path, "oc")]
    assert engine.backend == "genie_onnx_int8" and engine.bundle_identity == "a" * 64
    assert len(calls) == 1 and calls[0]["text"] == "海底的小灯亮起来了。" and calls[0]["seed"] == 42


async def test_genie_health_exposes_identity_and_checks_bundle_before_cache_hits(backend, tmp_path):
    server, _, pipeline, _, calls = backend
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"local_backend": "genie_onnx_int8"}}))
    pipeline.backend, pipeline.backend_identity = "genie_onnx_int8", "b" * 64
    changed, checks = False, []

    def verify():
        checks.append(True)
        if changed:
            raise ValueError("private bundle path changed")

    pipeline.verify_assets = verify
    engine = server.SpeechEngine(tmp_path, "oc", pipeline)
    async with client_for(server, engine, tmp_path) as client:
        result = await client.get("/docs")
        assert result.status_code == 200
        assert result.json()["backend"] == "genie_onnx_int8"
        assert result.json()["bundle_identity"] == "b" * 64
        changed = True
        failed = await client.get("/docs")
        assert failed.status_code == 503 and engine.failed
        assert "private bundle" not in failed.text
        assert (await client.post("/tts", json={"text": "你好"})).status_code == 503
    assert len(checks) == 2 and calls == []


async def test_backend_parameter_rejection_is_422_without_poisoning_worker(backend, tmp_path):
    server, engine, pipeline, _, calls = backend

    def validate(params):
        if params.get("top_k", 5) != 5:
            raise ValueError("Genie backend requires top_k=5")

    pipeline.validate_params = validate
    async with client_for(server, engine, tmp_path) as client:
        assert (await client.post("/tts", json={"text": "你好", "top_k": 15})).status_code == 422
        assert (await client.get("/docs")).status_code == 200
    assert not engine.failed and calls == []


async def test_changing_backend_requires_restart_before_speech_or_health(backend, tmp_path):
    server, engine, _, _, calls = backend
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"local_backend": "genie_onnx_int8"}}))
    async with client_for(server, engine, tmp_path) as client:
        assert (await client.get("/docs")).status_code == 503
        assert (await client.post("/tts", json={"text": "你好"})).status_code == 503
    assert engine.failed and calls == []


def test_unknown_local_backend_never_falls_back_to_compact(backend, tmp_path, monkeypatch):
    server, _, _, _, calls = backend
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"local_backend": "unknown"}}))
    monkeypatch.setattr(server.compact, "create_pipeline", lambda *a, **k: pytest.fail("Silent fallback"))
    with pytest.raises(ValueError, match="Unknown configured"):
        server.SpeechEngine.load(tmp_path, "oc")
    assert calls == []
