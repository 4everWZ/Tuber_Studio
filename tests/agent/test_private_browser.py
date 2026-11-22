import asyncio
from uuid import uuid4

import httpx
import pytest

from tuber_studio.agent.creative import browser_document, private_browser
from tuber_studio.agent.creative.browser_document import MAX_DOCUMENT, read_document, source_list
from tuber_studio.agent.creative.private_browser import BrowserAction, Command, Control, PrivateBrowser
from tuber_studio.agent.tasks.identity import TaskIdentity
from tuber_studio.agent.tasks.runtime import TaskRuntime
from tuber_studio.app import create_app
from tuber_studio.core.store import Store
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn

SOURCES = ["https://reading.example/doc", "https://reading.example/next"]


@pytest.mark.parametrize("url", ["http://reading.example/", "file:///text", "https://user@example.org/",
                                 "https://127.0.0.1/", "https://[::1]/", "https://test.local/", "https://example.org:9000/"])
def test_disallowed_sources(url):
    with pytest.raises(ValueError):
        source_list([url])


@pytest.mark.parametrize("case", ["ok", "redirect", "large", "encoded", "dns"])
async def test_document_fetch_pins_verified_ip_and_only_reads_exact_source(monkeypatch, case):
    requests = []
    async def resolve(self, host, *args, **kwargs):
        import socket
        assert host == "reading.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1" if case == "dns" else "1.1.1.1", 443))]

    def receive(request):
        requests.append(request)
        assert request.method == "GET" and str(request.url) == "https://1.1.1.1/doc"
        assert request.headers["host"] == request.extensions["sni_hostname"] == "reading.example"
        assert not {"cookie", "authorization", "referer"} & set(request.headers)
        if case == "redirect":
            return httpx.Response(302, headers={"location": SOURCES[1]})
        headers = {"content-type": "text/html"}
        if case == "encoded":
            headers["content-encoding"] = "br"
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"x" * (MAX_DOCUMENT + 1) if case == "large" else b"doc"))

    original = httpx.AsyncClient
    def client(**options):
        assert options["trust_env"] is options["follow_redirects"] is False
        return original(transport=httpx.MockTransport(receive), **options)
    monkeypatch.setattr(type(asyncio.get_running_loop()), "getaddrinfo", resolve)
    monkeypatch.setattr(browser_document.httpx, "AsyncClient", client)
    if case == "ok":
        assert await read_document(SOURCES[0]) == "doc"
    else:
        with pytest.raises(ValueError):
            await read_document(SOURCES[0])
    assert len(requests) == int(case != "dns")


@pytest.fixture
async def studio(settings, monkeypatch):
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture-only")
    app = create_app(settings)
    reads = []
    async def source(url, *, guard):
        guard()
        assert url in SOURCES
        reads.append(url)
        return "<p>fixture</p>"
    async def render(identity, payload):
        return {"text": "已读文字", "opened": [], "checked": [], "scroll": 0, "image": "image", "controls": []}
    monkeypatch.setattr(private_browser, "read_document", source)
    monkeypatch.setattr(app.state.runtime.tasks.private_browser.readers, "render", render)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as owner:
        yield app.state.runtime.tasks, owner, reads
    app.state.store.close()


async def create_browser_task(studio):
    tasks, owner, reads = studio
    identity = str(uuid4())
    path = f"/api/agent/tasks/{identity}/browser"
    assert (await owner.get(path)).status_code == 401
    owner.headers["Authorization"] = "Bearer fixture-only"
    result = await owner.post("/api/agent/tasks", json={"request_id": identity, "goal": "读指定来源", "browser_sources": SOURCES})
    assert result.status_code == 200, result.text
    return identity, path


async def test_tool_menu_step_identity_takeover_and_restart(studio, settings):
    tasks, owner, reads = studio
    identity, path = await create_browser_task(studio)
    assert not reads
    async def model(*, tools, **kwargs):
        assert {t["name"] for t in tools} == {"private_browser", "delegate_text", "ask_owner", "submit_materials"}
        schema = next(t for t in tools if t["name"] == "private_browser")["parameters"]["properties"]
        assert schema["task_id"]["enum"] == [identity]
        call = FunctionCall("read", "private_browser", {"task_id": identity, "kind": "read", "index": 0,
                            "text": "", "version": schema["version"]["enum"][0]})
        return ResponseTurn("turn", (call,), "", {})
    tasks.responses_client = lambda: model
    for _ in range(2):
        await tasks._advance(tasks.current(identity))
    step = tasks.current(identity)["steps"][-1]
    assert step["result"]["excerpt"] == "已读文字" and reads == SOURCES[:1]
    state = (await owner.get(path)).json()
    state = (await owner.patch(path, json={"action": "takeover", "version": state["version"]})).json()
    assert state["mode"] == "owner" and state["view"]["text"] == "已读文字"
    await tasks._advance(tasks.current(identity))
    assert tasks.current(identity)["status"] == "waiting"
    assert (await owner.post(path + "/actions", json={"kind": "open", "index": 3, "version": state["version"]})).status_code == 409
    state = (await owner.post(path + "/actions", json={"kind": "open", "index": 2, "version": state["version"]})).json()
    assert state["position"] == 2 and reads == SOURCES
    with pytest.raises(ValueError):
        await tasks.private_browser.turn(identity, Command(kind="read", version=state["version"]), "agent")
    reopened = Store(settings.runtime / "studio.sqlite3", [])
    try:
        original = tasks.scope()
        authority = TaskIdentity(reopened, lambda: original[1], lambda: original[:-2])
        resumed = PrivateBrowser(TaskRuntime(reopened, authority, None))
        assert resumed.state(identity) == state
        restored = resumed.control(identity, Control(action="handback", version=state["version"]))
        assert restored["mode"] == "agent"
    finally:
        reopened.close()
    assert (await owner.post(path + "/actions", json={"kind": "read", "version": state["version"]})).status_code == 409
    assert not tasks.store.all("SELECT * FROM account_writes")


@pytest.mark.parametrize("stop", ["end", "switch", "steer", "cancel"])
async def test_inflight_document_has_no_receipt_after_revocation(studio, monkeypatch, stop):
    tasks, owner, _ = studio
    identity, path = await create_browser_task(studio)
    state = (await owner.patch(path, json={"action": "takeover", "version": 1})).json()
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(url, *, guard):
        entered.set()
        await release.wait()
        return "<p>旧授权读取</p>"
    monkeypatch.setattr(private_browser, "read_document", delayed)
    job = asyncio.create_task(owner.post(path + "/actions", json={"kind": "read", "version": state["version"]}))
    await asyncio.wait_for(entered.wait(), 3)
    latest = (await owner.get(path)).json()
    if stop == "end":
        assert (await owner.patch(path, json={"action": "end", "version": latest["version"]})).json()["mode"] == "ended"
    elif stop == "switch":
        tasks.store.set("browsing_discovery_account", {"account_id": "123"})
        tasks.scope()
    else:
        await tasks.control(identity, 1, stop, goal="新目标" if stop == "steer" else None)
    release.set()
    assert (await job).status_code in {404, 409}
    assert not tasks.store.all("SELECT * FROM task_browser_documents")
    assert not tasks.store.all("SELECT * FROM task_browser_receipts")


async def test_foreign_step_and_separate_capabilities(studio):
    tasks, owner, reads = studio
    identity, path = await create_browser_task(studio)
    second = tasks.create(str(uuid4()), "另一项任务")
    from tuber_studio.agent.tasks.runtime import ToolRequest
    args = BrowserAction(task_id=identity, kind="read", version=1)
    result = await tasks.private_browser.tool(ToolRequest(str(uuid4()), second["goal"], second["scope"], None, None), args)
    assert "error" in result and not reads
    assert "private_browser" not in tasks.private_browser.menu(second, tasks.tools)
    mixed = await owner.post("/api/agent/tasks", json={"request_id": str(uuid4()), "goal": "混合", "browser_sources": SOURCES,
                             "attachments": [{"name": "private.txt", "text": "私人内容"}]})
    assert mixed.status_code == 409


async def test_regrant_requires_fresh_version_and_expiry_clears_document(studio):
    tasks, owner, reads = studio
    identity, path = await create_browser_task(studio)
    state = (await owner.patch(path, json={"action": "takeover", "version": 1})).json()
    await owner.post(path + "/actions", json={"kind": "read", "version": state["version"]})
    tasks.store.execute("UPDATE task_browser_sessions SET expires=0")
    ended = (await owner.get(path)).json()
    assert ended["mode"] == "ended" and not tasks.store.all("SELECT * FROM task_browser_documents")
    grant = {"revision": 1, "sources": [SOURCES[1]], "version": ended["version"]}
    assert (await owner.put(path, json=grant | {"version": state["version"]})).status_code == 409
    new = (await owner.put(path, json=grant)).json()
    assert new["version"] > ended["version"] and new["sources"] == [SOURCES[1]] and new["view"] == {}
    assert reads == SOURCES[:1]


async def test_unopened_grant_expires_from_owner_authorization(studio):
    tasks, owner, reads = studio
    identity, path = await create_browser_task(studio)
    tasks.store.execute("UPDATE agent_tasks SET created=0 WHERE id=?", (identity,))
    assert (await owner.get(path)).json()["mode"] == "ended" and not reads
