"""Account management reads fixed endpoints and never exposes platform credentials."""

import asyncio
import base64
import json
import os
import stat
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.bilibili_account import avatar_url
from tuber_studio.bilibili_browsing import BrowsingAdapter

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6LQAAAABJRU5ErkJggg=="
)
FACE = "https://i0.hdslb.com/bfs/face/fixture.png"
SECRET = {"sessdata": "fixture-sessdata-private", "csrf": "fixture-csrf-private"}


@pytest.fixture
def network(monkeypatch):
    calls = []
    state = {
        "nav_code": 0,
        "stats_code": 0,
        "face": FACE,
        "cdn_status": 200,
        "image": PNG,
        "mime": "image/png",
        "network_error": False,
    }

    def handler(request):
        calls.append(request)
        if request.url.host in {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}:
            assert "cookie" not in request.headers
            assert "authorization" not in request.headers
            return httpx.Response(
                state["cdn_status"],
                content=state["image"],
                headers={"content-type": state["mime"], "location": "https://evil.invalid/"},
            )
        assert request.url.host == "api.bilibili.com"
        assert request.method == "GET"
        if state["network_error"]:
            raise httpx.ConnectError("private error " + SECRET["sessdata"], request=request)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": state["nav_code"],
                    "data": {
                        "isLogin": True,
                        "mid": 123,
                        "uname": "大肥鱼账号",
                        "face": state["face"],
                        "money": 3.5,
                    },
                },
            )
        assert request.url.path == "/x/relation/stat"
        assert request.url.params["vmid"] == "123"
        return httpx.Response(200, json={"code": state["stats_code"], "data": {"follower": 42}})

    original = httpx.AsyncClient

    def controlled(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    async def no_pace(self):
        pass

    monkeypatch.setattr(httpx, "AsyncClient", controlled)
    monkeypatch.setattr(BrowsingAdapter, "_pace", no_pace)
    return calls, state


@pytest.fixture
def account_client(settings, network):
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        yield client, app, network[0], network[1]


def configure(client):
    response = client.put("/api/account/bilibili", json=SECRET)
    assert response.status_code == 200
    assert response.json()["configured"] == {"sessdata": True, "csrf": True}
    assert all(secret not in response.text for secret in SECRET.values())
    return response


def test_get_configure_blank_clear_and_legacy_policy_are_local_and_redacted(account_client, settings):
    client, app, calls, _ = account_client
    assert client.get("/api/account/bilibili").json()["state"] == "not_configured"
    configure(client)
    assert client.get("/api/account/bilibili").json()["state"] == "unverified"
    blank = client.put("/api/account/bilibili", json={"sessdata": " ", "bili_jct": ""})
    assert blank.status_code == 200 and app.state.comments.credentials == SECRET
    changed = client.put("/api/comments/policy", json={"csrf": "legacy-csrf"})
    assert changed.status_code == 200 and "legacy-csrf" not in changed.text
    assert app.state.comments.credentials == SECRET | {"csrf": "legacy-csrf"}
    path = settings.runtime / "bilibili-credentials.json"
    assert json.loads(path.read_text()) == SECRET | {"csrf": "legacy-csrf"}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert client.put("/api/account/bilibili", json={"clear": True, "sessdata": "x"}).status_code == 400
    assert client.put("/api/account/bilibili", json={"csrf": "a", "bili_jct": "b"}).status_code == 400
    clear = client.put("/api/account/bilibili", json={"clear": True})
    assert clear.json()["configured"] == {"sessdata": False, "csrf": False}
    assert json.loads(path.read_text()) == {}
    assert calls == []  # Neither GET nor credential editing may trigger platform reads.


def test_credentials_survive_restart_but_login_status_requires_new_verification(settings, network):
    with TestClient(create_app(settings)) as first:
        first.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        configure(first)
        assert first.post("/api/account/bilibili/refresh").json()["state"] == "logged_in"
    count = len(network[0])
    with TestClient(create_app(settings)) as second:
        second.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        response = second.get("/api/account/bilibili")
        assert response.json()["state"] == "unverified"
        assert response.json()["configured"] == {"sessdata": True, "csrf": True}
        assert response.json()["profile"] is None
        assert len(network[0]) == count


def test_refresh_reads_actual_nav_stats_and_profile_cache_does_not_poll(account_client):
    client, app, calls, _ = account_client
    configure(client)
    response = client.post("/api/account/bilibili/refresh")
    data = response.json()
    assert data["state"] == "logged_in"
    assert data["profile"] == {
        "uid": "123",
        "name": "大肥鱼账号",
        "avatar_url": FACE,
        "coins": 3.5,
        "fans": 42,
    }
    assert app.state.comments.display_account_id() == "123"
    assert len(calls) == 2
    assert "SESSDATA=" + SECRET["sessdata"] in calls[0].headers["cookie"]
    for _ in range(3):
        assert client.get("/api/account/bilibili").json()["state"] == "logged_in"
    assert len(calls) == 2
    assert all(secret not in response.text for secret in SECRET.values())


def test_partial_stats_keep_verified_profile_with_explicit_missing_count(account_client):
    client, _, _, state = account_client
    configure(client)
    state["stats_code"] = -352
    data = client.post("/api/account/bilibili/refresh").json()
    assert data["state"] == "logged_in"
    assert data["profile"]["fans"] is None and data["warnings"]
    assert data["profile"]["coins"] == 3.5


@pytest.mark.parametrize(
    ("code", "error", "expected"), [(-101, False, "expired"), (-352, False, "restricted"), (0, True, "error")]
)
def test_expired_restricted_and_exception_states_are_sanitized(account_client, code, error, expected):
    client, _, _, state = account_client
    configure(client)
    state.update(nav_code=code, network_error=error)
    response = client.post("/api/account/bilibili/refresh")
    assert response.status_code == 200
    assert response.json()["state"] == expected
    assert response.json()["profile"] is None
    assert all(secret not in response.text for secret in SECRET.values())


def test_avatar_proxy_has_no_platform_cookies_and_caches_only_current_identity(account_client):
    client, _, calls, _ = account_client
    configure(client)
    client.post("/api/account/bilibili/refresh").raise_for_status()
    response = client.get("/api/account/bilibili/avatar?url=https://evil.invalid/unused")
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert len([call for call in calls if call.url.host.endswith("hdslb.com")]) == 1
    assert client.get("/api/account/bilibili/avatar").status_code == 200
    assert len(calls) == 3
    client.put("/api/account/bilibili", json={"sessdata": "new-account"}).raise_for_status()
    assert client.get("/api/account/bilibili/avatar").status_code == 404
    assert len(calls) == 3


@pytest.mark.parametrize("prefix", ["http:", ""])
def test_avatar_legacy_urls_are_upgraded_to_https_and_gif_is_preserved(account_client, settings, prefix):
    client, _, calls, state = account_client
    configure(client)
    state["face"] = prefix + "//i0.hdslb.com/bfs/face/avatar.gif"
    state["image"] = (settings.root / "assets/whale/cheerful.gif").read_bytes()
    state["mime"] = "image/gif"
    result = client.post("/api/account/bilibili/refresh").json()
    assert result["profile"]["avatar_url"] == "https://i0.hdslb.com/bfs/face/avatar.gif"
    response = client.get("/api/account/bilibili/avatar")
    assert response.status_code == 200 and response.content == state["image"]
    assert calls[-1].url.scheme == "https"
    assert "cookie" not in calls[-1].headers


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.invalid/bfs/x",
        "https://127.0.0.1/bfs/x",
        "https://evil.invalid/bfs/x",
        "https://i0.hdslb.com@evil.invalid/bfs/x",
        "https://i0.hdslb.com/bfs/%2e%2e/x",
        "data:image/png;base64,xxx",
    ],
)
def test_platform_avatar_cannot_be_an_arbitrary_url(account_client, url):
    client, _, calls, state = account_client
    configure(client)
    state["face"] = url
    assert client.post("/api/account/bilibili/refresh").json()["profile"]["avatar_url"] is None
    assert client.get("/api/account/bilibili/avatar").status_code == 404
    assert len(calls) == 2 and avatar_url(url) is None


@pytest.mark.parametrize("failure", ["redirect", "size", "mime"])
def test_avatar_redirect_size_and_mime_fail_without_external_follow(account_client, failure):
    client, _, calls, state = account_client
    configure(client)
    client.post("/api/account/bilibili/refresh").raise_for_status()
    if failure == "redirect":
        state["cdn_status"] = 302
    elif failure == "size":
        state["image"] = PNG + b"x" * (2 * 1024 * 1024)
    else:
        state["mime"] = "text/html"
    assert client.get("/api/account/bilibili/avatar").status_code == 404
    assert len(calls) == 3


async def close_unstarted(app):
    await app.state.browsing.close()
    await app.state.messages.close()
    await app.state.dynamics.close()
    await app.state.comments.close()
    await app.state.runtime.close()
    app.state.store.close()


@pytest.mark.asyncio
async def test_old_profile_and_uid_reads_cannot_bind_to_replaced_credentials(settings, network):
    app = create_app(settings)
    comments, account = app.state.comments, app.state.bilibili_account
    try:
        await account.configure(SECRET)
        old = comments.adapter
        started, release = asyncio.Event(), asyncio.Event()

        async def profile():
            started.set()
            await release.wait()
            return {"uid": "111", "name": "old", "avatar_url": FACE, "coins": 1, "fans": 2}

        old.account_profile = profile
        pending = asyncio.create_task(account.refresh())
        await started.wait()
        await comments.replace_credentials(SECRET | {"sessdata": "new-profile-account"})
        release.set()
        assert (await pending)["profile"] is None
        assert comments.display_account_id() == ""

        old = comments.adapter
        started, release = asyncio.Event(), asyncio.Event()

        async def uid():
            started.set()
            await release.wait()
            return "111"

        old.self_uid = uid
        pending = asyncio.create_task(comments.account_id())
        await started.wait()
        await comments.replace_credentials(SECRET | {"sessdata": "new-uid-account"})
        release.set()
        with pytest.raises(ValueError, match="账号已变更"):
            await pending
        assert comments.display_account_id() == ""
    finally:
        await close_unstarted(app)


@pytest.mark.asyncio
async def test_concurrent_legacy_and_account_edits_reject_stale_snapshot(settings, network, monkeypatch):
    app = create_app(settings)
    comments, account = app.state.comments, app.state.bilibili_account
    try:
        await account.configure(SECRET)
        original = comments.replace_credentials
        captured, ready, release = [], asyncio.Event(), asyncio.Event()

        async def delayed(credential, **options):
            captured.append(options["expected"])
            if len(captured) == 2:
                ready.set()
            await release.wait()
            return await original(credential, **options)

        monkeypatch.setattr(comments, "replace_credentials", delayed)
        tasks = [
            asyncio.create_task(account.configure({"csrf": "account-new-csrf"})),
            asyncio.create_task(comments.set_policy({"sessdata": "legacy-new-session"})),
        ]
        await ready.wait()
        assert captured == [SECRET, SECRET]
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert comments.credentials in (
            SECRET | {"csrf": "account-new-csrf"},
            SECRET | {"sessdata": "legacy-new-session"},
        )
        monkeypatch.setattr(comments, "replace_credentials", original)
        await account.configure({"csrf": "account-new-csrf", "sessdata": "legacy-new-session"})
        assert comments.credentials == {"csrf": "account-new-csrf", "sessdata": "legacy-new-session"}
    finally:
        await close_unstarted(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["sessions", "history", "generation_uid", "generation_wait"])
async def test_private_reads_reject_account_changed_while_awaiting(settings, network, monkeypatch, phase):
    app = create_app(settings)
    comments, messages = app.state.comments, app.state.messages
    started, release = asyncio.Event(), asyncio.Event()
    try:
        await app.state.bilibili_account.configure(SECRET)
        await messages.configure({"enabled": True})
        adapter = await messages._adapter()

        async def blocked(value):
            started.set()
            await release.wait()
            return value

        monkeypatch.setattr(adapter, "sessions", AsyncMock(return_value=("111", ["222"])))
        monkeypatch.setattr(adapter, "history", AsyncMock(return_value=[]))
        if phase == "sessions":
            monkeypatch.setattr(adapter, "sessions", lambda: blocked(("111", ["222"])))
            pending = asyncio.create_task(messages.sync())
        elif phase == "history":
            rows = [
                {
                    "sender_uid": "222",
                    "receiver_id": "111",
                    "msg_type": 1,
                    "msg_key": "333",
                    "timestamp": int(time.time()),
                    "content": {"content": "旧账号私密原文"},
                }
            ]
            monkeypatch.setattr(adapter, "history", lambda uid: blocked(rows))
            pending = asyncio.create_task(messages.sync())
        else:
            inbox, _ = messages.ingest(
                {
                    "message_id": "333",
                    "platform_user_id": "222",
                    "display_name": "用户",
                    "text": "旧账号私密原文",
                    "timestamp": int(time.time()),
                    "account_id": "111",
                }
            )
            draft = messages.enqueue("message", inbox, schedule=False)
            monkeypatch.setattr(adapter, "self_uid", AsyncMock(return_value="111"))
            if phase == "generation_uid":
                monkeypatch.setattr(adapter, "self_uid", lambda: blocked("111"))
            else:
                monkeypatch.setattr(
                    app.state.browsing, "linked_message_evidence", AsyncMock(return_value=None)
                )

                @asynccontextmanager
                async def waiting_model():
                    await blocked(None)
                    yield

                monkeypatch.setattr(app.state.runtime, "offline_generation", waiting_model)
            pending = asyncio.create_task(messages._generate(draft, False))
        await started.wait()
        await comments.replace_credentials(SECRET | {"sessdata": "new-private-account"})
        release.set()
        with pytest.raises(ValueError, match="账号已变更"):
            await pending
        assert messages.display_account_id() == ""
        assert messages.active_credential != comments.credentials
        if phase in {"sessions", "history"}:
            assert app.state.store.one("SELECT COUNT(*) AS n FROM dm_inbox")["n"] == 0
        else:
            row = app.state.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft,))
            assert row["status"] == "failed" and not row["text"]
    finally:
        release.set()
        await close_unstarted(app)
