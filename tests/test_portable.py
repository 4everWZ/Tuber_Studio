import asyncio
import hashlib
import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest

from tuber_studio import portable


def payload(builtin, name, content, *, policy=None, mode=0o444):
    path = builtin / "files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    entry = {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
    if policy:
        entry["policy"] = policy
    return entry


def manifest(builtin, entries):
    builtin.mkdir(parents=True, exist_ok=True)
    (builtin / "manifest.json").write_text(json.dumps({"format": 1, "files": entries}))


def test_initialize_is_repeatable_preserves_operator_config_and_generates_unique_private_credentials(
    tmp_path,
):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    entries = [payload(builtin, "models/model.gguf", b"model")]
    entries.append(payload(builtin, "studio.json", b'{"preset":true}', policy="preserve", mode=0o600))
    entries.append(payload(builtin, "tts/tts_infer.yaml", b"custom: preset", policy="preserve", mode=0o600))
    manifest(builtin, entries)
    first = portable.initialize_runtime(builtin, runtime)
    assert set(first["created_credentials"]) == set(portable.TOKENS)
    tokens = {name: (runtime / name).read_bytes() for name in portable.TOKENS}
    assert all(len(value) >= 32 for value in tokens.values()) and len(set(tokens.values())) == 3
    (runtime / "studio.json").write_bytes(b'{"operator":true}')
    (runtime / "tts/tts_infer.yaml").write_bytes(b"custom: operator")
    second = portable.initialize_runtime(builtin, runtime)
    assert second["copied"] == [] and second["created_credentials"] == []
    assert (runtime / "studio.json").read_bytes() == b'{"operator":true}'
    assert (runtime / "tts/tts_infer.yaml").read_bytes() == b"custom: operator"
    assert {name: (runtime / name).read_bytes() for name in portable.TOKENS} == tokens
    other = tmp_path / "another-install"
    portable.initialize_runtime(builtin, other)
    assert all((other / name).read_bytes() != tokens[name] for name in portable.TOKENS)
    assert not any(value.decode() in json.dumps(first) for value in tokens.values())


def test_changed_immutable_file_is_preserved_and_fails_visibly(tmp_path):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    manifest(builtin, [payload(builtin, "tts/genie/oc/model.bin", b"original")])
    portable.initialize_runtime(builtin, runtime)
    target = runtime / "tts/genie/oc/model.bin"
    target.chmod(0o600)
    target.write_bytes(b"operator change")
    with pytest.raises(ValueError, match="preserved"):
        portable.initialize_runtime(builtin, runtime)
    assert target.read_bytes() == b"operator change"


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "C:\\outside", "a//b", "admin-token", "tts/inference-token"]
)
def test_manifest_cannot_escape_or_seed_credentials(tmp_path, name):
    builtin = tmp_path / "builtin"
    entry = {"path": name, "bytes": 0, "sha256": "0" * 64, "mode": 0o600}
    manifest(builtin, [entry])
    with pytest.raises(ValueError):
        portable.initialize_runtime(builtin, tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()


def test_preserve_policy_cannot_hide_a_changed_model_and_duplicate_paths_fail(tmp_path):
    builtin = tmp_path / "builtin"
    entry = payload(builtin, "models/unsafe.gguf", b"model", policy="preserve")
    manifest(builtin, [entry])
    with pytest.raises(ValueError, match="Operator"):
        portable.initialize_runtime(builtin, tmp_path / "runtime")
    entry.pop("policy")
    manifest(builtin, [entry, entry])
    with pytest.raises(ValueError, match="Duplicate"):
        portable.initialize_runtime(builtin, tmp_path / "runtime")


@pytest.mark.parametrize("where", ["source", "target", "ancestor"])
def test_symlinks_are_rejected_without_writing_outside_runtime(tmp_path, where):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    entry = payload(builtin, "models/file.bin", b"expected")
    manifest(builtin, [entry])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.bin").write_bytes(b"outside")
    try:
        if where == "source":
            source = builtin / "files/models/file.bin"
            source.unlink()
            source.symlink_to(outside / "file.bin")
        elif where == "ancestor":
            runtime.mkdir()
            (runtime / "models").symlink_to(outside, target_is_directory=True)
        else:
            (runtime / "models").mkdir(parents=True)
            (runtime / "models/file.bin").symlink_to(outside / "file.bin")
    except OSError:
        pytest.skip("Unprivileged symlinks unavailable on this platform")
    with pytest.raises(ValueError, match="symbolic"):
        portable.initialize_runtime(builtin, runtime)
    assert (outside / "file.bin").read_bytes() == b"outside"


def test_bad_payload_and_open_failure_remove_partial_files_and_close_fds(tmp_path, monkeypatch):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    entry = payload(builtin, "models/broken.bin", b"bytes")
    entry["sha256"] = "0" * 64
    manifest(builtin, [entry])
    with pytest.raises(ValueError, match="manifest"):
        portable.initialize_runtime(builtin, runtime)
    assert not (runtime / entry["path"]).exists() and not list(runtime.rglob(".tuber-init-*"))
    streams = []
    original_fdopen, original_open = portable.os.fdopen, portable._open_regular

    def fdopen(*args, **kwargs):
        stream = original_fdopen(*args, **kwargs)
        streams.append(stream)
        return stream

    def fail_model(path):
        if Path(path).name == "broken.bin":
            raise OSError("injected file open failure")
        return original_open(path)

    monkeypatch.setattr(portable.os, "fdopen", fdopen)
    monkeypatch.setattr(portable, "_open_regular", fail_model)
    with pytest.raises(OSError, match="injected"):
        portable.initialize_runtime(builtin, runtime)
    assert streams and all(stream.closed for stream in streams)
    assert not list(runtime.rglob(".tuber-init-*"))


def test_large_copy_and_existing_hash_use_bounded_reads(tmp_path, monkeypatch):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    content = b"a" * (portable.CHUNK_BYTES * 2 + 7)
    manifest(builtin, [payload(builtin, "models/large.bin", content)])
    calls, original = [], portable._open_regular

    class Guarded:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            calls.append(size)
            assert 0 < size <= portable.CHUNK_BYTES
            return self.stream.read(size)

        def fileno(self):
            return self.stream.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

    monkeypatch.setattr(
        portable,
        "_open_regular",
        lambda path: Guarded(original(path)) if Path(path).name == "large.bin" else original(path),
    )
    portable.initialize_runtime(builtin, runtime)
    portable.initialize_runtime(builtin, runtime)
    assert len(calls) >= 6 and (runtime / "models/large.bin").read_bytes() == content


def remote_entry(name, content, urls):
    return {
        "path": name,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mode": 0o444,
        "urls": urls,
    }


def mock_download(monkeypatch, route):
    original = httpx.Client
    monkeypatch.delenv("TUBER_HF_BASE", raising=False)
    monkeypatch.delenv("TUBER_DOWNLOAD_PROXY", raising=False)
    monkeypatch.setattr(
        portable.httpx, "Client", lambda *a, **kw: original(*a, transport=httpx.MockTransport(route), **kw)
    )


def test_downloads_are_checked_published_once_and_existing_assets_do_not_access_network(
    tmp_path, monkeypatch, capsys
):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    expected, requests = b"verified model", []
    manifest(builtin, [remote_entry("models/new.gguf", expected, ["https://huggingface.co/example/model"])])

    def route(request):
        requests.append(request)
        return httpx.Response(200, content=expected)

    mock_download(monkeypatch, route)
    portable.initialize_runtime(builtin, runtime)
    portable.initialize_runtime(builtin, runtime)
    assert (runtime / "models/new.gguf").read_bytes() == expected and len(requests) == 1
    assert not list(runtime.rglob(".tuber-download-*"))
    assert '"received_bytes": 14' in capsys.readouterr().out
    target = runtime / "models/new.gguf"
    target.chmod(0o600)
    target.write_bytes(b"preserve corruption for inspection")
    with pytest.raises(ValueError, match="preserved"):
        portable.initialize_runtime(builtin, runtime)
    assert len(requests) == 1 and target.read_bytes() == b"preserve corruption for inspection"


def test_interrupted_candidate_is_cleaned_before_one_bounded_fallback(tmp_path, monkeypatch):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    expected, requests = b"complete", []
    urls = ["https://huggingface.co/first", "https://hf-mirror.com/second"]
    manifest(builtin, [remote_entry("tts/weights/source.pth", expected, urls)])

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b"part"
            raise httpx.ReadError("private upstream address must not be logged")

    def route(request):
        requests.append(str(request.url))
        return (
            httpx.Response(200, stream=Interrupted())
            if len(requests) == 1
            else httpx.Response(200, content=expected)
        )

    mock_download(monkeypatch, route)
    portable.initialize_runtime(builtin, runtime)
    assert requests == urls and (runtime / "tts/weights/source.pth").read_bytes() == expected
    assert not list(runtime.rglob(".tuber-download-*"))


@pytest.mark.parametrize("failure", ["hash", "oversize", "status", "redirect_loop", "redirect_http"])
def test_failed_downloads_never_publish_and_do_not_log_secret_urls(
    tmp_path, monkeypatch, capsys, caplog, failure
):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    url = "https://example.com/public/model?signature=PRIVATE_MARKER"
    manifest(builtin, [remote_entry("models/download.bin", b"good", [url])])
    requests = []

    def route(request):
        requests.append(request)
        if failure == "hash":
            return httpx.Response(200, content=b"evil")
        if failure == "oversize":
            return httpx.Response(200, content=b"excessive bytes")
        if failure == "status":
            return httpx.Response(403)
        if failure == "redirect_http":
            return httpx.Response(302, headers={"location": "http://unsafe.local/file"})
        return httpx.Response(302, headers={"location": str(request.url)})

    mock_download(monkeypatch, route)
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError, match="Bounded asset download failed") as error:
        portable.initialize_runtime(builtin, runtime)
    assert len(requests) <= 6 and all(request.url.scheme == "https" for request in requests)
    assert not (runtime / "models/download.bin").exists() and not list(runtime.rglob(".tuber-download-*"))
    visible = str(error.value) + capsys.readouterr().out + caplog.text
    assert "PRIVATE_MARKER" not in visible and "example.com" not in visible


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "https://name:secret@example.com/a",
        "file:///etc/passwd",
        "https://example.com/a#fragment",
    ],
)
def test_manifest_download_urls_cannot_acquire_other_protocols_or_credentials(tmp_path, url):
    builtin = tmp_path / "builtin"
    manifest(builtin, [remote_entry("models/new.bin", b"x", [url])])
    with pytest.raises(ValueError):
        portable.initialize_runtime(builtin, tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()


def test_mirror_rewrites_only_known_hf_origins_and_preserves_exact_file_path(monkeypatch):
    monkeypatch.setenv("TUBER_HF_BASE", "https://mirror.example:8443")
    for source in ("huggingface.co", "hf-mirror.com"):
        assert (
            str(portable._download_url(f"https://{source}/org/model/resolve/main/file?download=true"))
            == "https://mirror.example:8443/org/model/resolve/main/file?download=true"
        )
    assert str(portable._download_url("https://models.example/file")) == "https://models.example/file"


@pytest.mark.parametrize(
    "base",
    [
        "http://mirror.example",
        "https://user:secret@mirror.example",
        "https://mirror.example/prefix",
        "https://mirror.example/?secret=x",
        "https://mirror.example/#fragment",
    ],
)
def test_mirror_override_cannot_add_credentials_paths_queries_or_plain_http(monkeypatch, base):
    monkeypatch.setenv("TUBER_HF_BASE", base)
    with pytest.raises(ValueError):
        portable._download_url("https://huggingface.co/org/model")


def test_explicit_proxy_reaches_download_client_without_downgrading_https_or_logging_origin(
    tmp_path, monkeypatch, capsys
):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    manifest(builtin, [remote_entry("models/proxy.bin", b"ok", ["https://huggingface.co/org/file"])])
    monkeypatch.setenv("TUBER_DOWNLOAD_PROXY", "http://127.0.0.1:19099")
    monkeypatch.setenv("TUBER_HF_BASE", "https://hf-mirror.com")
    captured, original = [], httpx.Client

    def factory(*args, proxy=None, **kwargs):
        captured.append((proxy, kwargs["trust_env"]))

        def route(request):
            assert request.url.scheme == "https" and request.url.host == "hf-mirror.com"
            return httpx.Response(200, content=b"ok")

        return original(*args, transport=httpx.MockTransport(route), **kwargs)

    monkeypatch.setattr(portable.httpx, "Client", factory)
    portable.initialize_runtime(builtin, runtime)
    assert captured == [("http://127.0.0.1:19099", False)]
    assert "19099" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "proxy",
    [
        "socks5://localhost:8080",
        "http://user:secret@localhost:8080",
        "http://localhost:8080/path",
        "https://localhost:8080?secret=1",
    ],
)
def test_proxy_is_an_explicit_origin_and_rejects_credentials_without_echo(monkeypatch, proxy):
    monkeypatch.setenv("TUBER_DOWNLOAD_PROXY", proxy)
    with pytest.raises(ValueError) as error:
        with portable._download_client():
            pytest.fail("Invalid proxy must not create a network client")
    assert proxy not in str(error.value) and "secret" not in str(error.value)


def test_each_download_gets_its_own_deadline_separate_from_serve_timeout(tmp_path, monkeypatch):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    manifest(
        builtin,
        [
            remote_entry(f"models/{index}.bin", b"data", [f"https://example.com/{index}"])
            for index in range(2)
        ],
    )
    clock = [0.0]

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] += 900
            yield b"data"

    mock_download(monkeypatch, lambda request: httpx.Response(200, stream=Slow()))
    monkeypatch.setattr(portable.time, "monotonic", lambda: clock[0])
    portable.initialize_runtime(builtin, runtime)
    assert clock[0] == 1800 and len(list((runtime / "models").iterdir())) == 2


def test_file_deadline_aborts_trickling_stream_and_cleans_partial(tmp_path, monkeypatch):
    builtin, runtime = tmp_path / "builtin", tmp_path / "runtime"
    manifest(
        builtin,
        [
            remote_entry(
                "models/slow.bin", b"data", ["https://example.com/first", "https://example.com/second"]
            )
        ],
    )
    clock, requests = [0.0], []

    class TooSlow(httpx.SyncByteStream):
        def __iter__(self):
            yield b"d"
            clock[0] = portable.DOWNLOAD_SECONDS + 1
            yield b"ata"

    def route(request):
        requests.append(request)
        return httpx.Response(200, stream=TooSlow())

    mock_download(monkeypatch, route)
    monkeypatch.setattr(portable.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError):
        portable.initialize_runtime(builtin, runtime)
    assert len(requests) == 1 and not list(runtime.rglob(".tuber-download-*"))


def test_prepare_hook_runs_fixed_child_only_for_selected_local_speech(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"kind": "gpt_sovits"}}))
    assert portable.prepare_runtime(tmp_path)["prepared"] is False
    script = tmp_path / "tools/jetson/tts/bootstrap.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fixed program, not executed by CPU contract test")
    calls = []

    class Child:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

        def wait(self, timeout):
            assert timeout == portable.PREPARE_SECONDS
            return 0

    monkeypatch.setattr(portable.subprocess, "Popen", Child)
    assert portable.prepare_runtime(tmp_path) == {"prepared": True, "returncode": 0}
    assert calls[0][0] == [portable.sys.executable, str(script), "--root", str(tmp_path)]
    assert calls[0][1] == {"cwd": tmp_path, "start_new_session": True}
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"kind": "openai_compatible"}}))
    assert portable.prepare_runtime(tmp_path)["prepared"] is False and len(calls) == 1


def test_prepare_timeout_terminates_only_its_child_and_propagates_failure(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/studio.json").write_text(json.dumps({"tts": {"kind": "gpt_sovits"}}))
    script = tmp_path / "tools/jetson/tts/bootstrap.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fixed")
    events = []

    class Child:
        pid = 123456

        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            if timeout == portable.PREPARE_SECONDS:
                raise subprocess.TimeoutExpired("fixed bootstrap", timeout)
            events.append(("wait", timeout))
            return -15

    monkeypatch.setattr(portable.subprocess, "Popen", Child)
    if portable.os.name == "posix":
        monkeypatch.setattr(portable.os, "killpg", lambda pid, signum: events.append((pid, signum)))
    with pytest.raises(subprocess.TimeoutExpired):
        portable.prepare_runtime(tmp_path)
    assert ("wait", 8) in events


@pytest.fixture
def ready(tmp_path):
    (tmp_path / "runtime").mkdir()
    config = {
        "llm": {"kind": "llama_cpp", "model": "MiniCPM5-2B", "context_tokens": 131072},
        "tts": {"kind": "gpt_sovits", "local_backend": "genie_onnx_int8", "voice_id": "yaoyao_sovits_v2"},
        "inference_broker": {"enabled": True, "mode": "live"},
    }
    (tmp_path / "runtime/studio.json").write_text(json.dumps(config))
    for name in portable.TOKENS:
        portable._token(tmp_path / "runtime", name)
    health = {
        "state": "active",
        "error": None,
        "busy": False,
        "mode": "live",
        "selected_provider": "llm2b",
        "providers": {"llm2b": {"running": True, "native_context_tokens": 131072}, "tts": {"running": True}},
    }
    props = {
        "model_alias": "MiniCPM5-2B",
        "default_generation_settings": {"n_ctx": 131072},
        "is_sleeping": False,
    }
    speech = {
        "status": "ready",
        "backend": "genie_onnx_int8",
        "voice_id": "yaoyao_sovits_v2",
        "bundle_identity": "a" * 64,
        "is_sleeping": False,
    }
    requests = []

    async def route(request):
        requests.append(request)
        if request.url.port == 9020:
            return httpx.Response(200, text="workbench")
        if request.url.port != 9012 and request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/health":
            return httpx.Response(200, json=health)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json=props)
        if request.url.path == "/tts/docs":
            return httpx.Response(200, json=speech)
        if request.url.path == "/mode":
            return httpx.Response(200, json=health)
        raise AssertionError(request.url)

    return config, health, props, speech, requests, route


@pytest.mark.parametrize("busy", [False, True])
async def test_readonly_health_accepts_normal_busy_and_checks_actual_voice_context(ready, tmp_path, busy):
    _, health, _, _, requests, route = ready
    health.update(busy=busy, state="switching" if busy else "active")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:9012", transport=httpx.MockTransport(route)
    ) as client:
        actual = await portable.observe_ready(client, tmp_path)
    assert actual["context_tokens"] == 131072 and actual["bundle_identity"] == "a" * 64
    assert requests and all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("failure", ["sleeping", "window", "voice", "bundle", "worker", "error"])
async def test_sleeping_or_mismatched_metadata_is_never_ready(ready, tmp_path, failure):
    _, health, props, speech, requests, route = ready
    if failure == "sleeping":
        props["is_sleeping"] = True
    if failure == "window":
        props["default_generation_settings"]["n_ctx"] = 3072
    if failure == "voice":
        speech["voice_id"] = "old-whale"
    if failure == "bundle":
        speech["bundle_identity"] = ""
    if failure == "worker":
        health["providers"]["tts"]["running"] = False
    if failure == "error":
        health["error"] = "initialization failed"
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:9012", transport=httpx.MockTransport(route)
    ) as client:
        with pytest.raises(ValueError):
            await portable.observe_ready(client, tmp_path)
    assert all(request.method == "GET" for request in requests)


async def test_healthcheck_is_readonly_and_token_is_only_sent_to_broker(ready, tmp_path, monkeypatch):
    _, _, _, _, requests, route = ready
    original = httpx.AsyncClient
    monkeypatch.setattr(
        portable.httpx,
        "AsyncClient",
        lambda *a, **kw: original(*a, transport=httpx.MockTransport(route), **kw),
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    await portable.healthcheck(tmp_path)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    token = (tmp_path / "runtime/inference-token").read_text()
    assert all(request.method == "GET" for request in requests)
    for request in requests:
        assert request.headers.get("X-Tuber-Inference-Key") == (token if request.url.port == 9012 else None)


async def test_alive_pid_and_cached_props_do_not_hide_an_unready_model_server(ready, tmp_path):
    *_, route = ready

    async def unavailable(request):
        if request.url.port != 9012 and request.url.path == "/health":
            return httpx.Response(503, json={"status": "loading"})
        return await route(request)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:9012", transport=httpx.MockTransport(unavailable)
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await portable.observe_ready(client, tmp_path)


async def test_external_llm_startup_never_posts_mode_or_warms_local_workers(ready, tmp_path):
    config, health, _, _, requests, route = ready
    config["llm"].update(kind="openai_compatible", model="remote-model", context_tokens=262144)
    (tmp_path / "runtime/studio.json").write_text(json.dumps(config))
    health.update(
        state="cold", mode="text", providers={"llm2b": {"running": False}, "tts": {"running": False}}
    )
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:9012", transport=httpx.MockTransport(route)
        ) as client:
            result = await portable._startup_once(tmp_path, task, client)
        assert result["mode"] == "text" and all(request.method == "GET" for request in requests)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_local_llm_with_external_tts_establishes_only_text_mode(ready, tmp_path):
    config, health, _, _, requests, route = ready
    config["tts"]["kind"] = "openai_compatible"
    (tmp_path / "runtime/studio.json").write_text(json.dumps(config))
    health.update(mode="text")
    health["providers"]["tts"]["running"] = False
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:9012", transport=httpx.MockTransport(route)
        ) as client:
            actual = await portable._startup_once(tmp_path, task, client)
        posts = [request for request in requests if request.method == "POST"]
        assert len(posts) == 1 and json.loads(posts[0].content) == {"mode": "text"}
        assert actual["mode"] == "text" and not any(r.url.path == "/tts/docs" for r in requests)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("hang", [False, True])
async def test_failed_or_timed_out_establishment_keeps_ui_alive_and_never_retries(
    ready, tmp_path, monkeypatch, hang
):
    *_, route = ready
    mode_calls = []

    async def failing(request):
        if request.url.path == "/mode":
            mode_calls.append(request)
            if hang:
                await asyncio.Event().wait()
            return httpx.Response(503, json={"detail": "resource unavailable"})
        return await route(request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        portable.httpx,
        "AsyncClient",
        lambda *a, **kw: original(*a, transport=httpx.MockTransport(failing), **kw),
    )
    stopped, exited = asyncio.Event(), asyncio.Event()

    async def serve(stop):
        await stop.wait()
        exited.set()

    task = asyncio.create_task(
        portable.serve_portable(tmp_path, serve=serve, stopped=stopped, startup_timeout=0.04)
    )
    try:
        for _ in range(100):
            path = tmp_path / "runtime/portable-status.json"
            if path.exists() and json.loads(path.read_text())["state"] == "error":
                break
            await asyncio.sleep(0.005)
        assert json.loads(path.read_text())["state"] == "error"
        await asyncio.sleep(0.02)
        assert len(mode_calls) == 1 and not task.done() and not exited.is_set()
    finally:
        stopped.set()
        await asyncio.wait_for(task, 1)
    assert exited.is_set()
