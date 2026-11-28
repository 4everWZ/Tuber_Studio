"""Tests for Bilibili browser UI comment publishing, transport routing, and Remotion defaults."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.agent.skills.remotion_video import DEFAULT_VIDEO_PRODUCTION_POLICY, VideoProductionPolicy
from tuber_studio.core.errors import PlatformRejected, PublishUncertain
from tuber_studio.infra.platforms.bilibili import browser_comments as bc
from tuber_studio.infra.platforms.bilibili.browser_comments import (
    CHROMIUM_LAUNCH_ARGS,
    INLINE_EDITOR_SELECTOR,
    INLINE_PUBLISH_SELECTOR,
    TOP_LEVEL_EDITOR_SELECTOR,
    TOP_LEVEL_PUBLISH_SELECTOR,
    BilibiliBrowserCommentPublisher,
    resolve_browser_profile_dir,
)
from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.infra.platforms.bilibili.client import (
    BILIBILI_WEB_USER_AGENT,
    BilibiliAccountExpired,
    BilibiliAdapter,
    PlatformReadError,
)

BV = "BV1wQs16aEF3"
CREDS = {"sessdata": "sess-secret", "csrf": "csrf-secret", "uid": "10086"}


class FakeLocator:
    def __init__(self, page, selector: str):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self.page.counts.get(self.selector, 0)

    async def is_visible(self) -> bool:
        return self.page.visible.get(self.selector, self.page.counts.get(self.selector, 0) > 0)

    async def is_enabled(self) -> bool:
        return self.page.enabled.get(self.selector, True)

    async def wait_for(self, *, state="visible", timeout=None):
        self.page.waits.append((self.selector, state, timeout))
        if state == "attached" and self.selector == "bili-comments" and not self.page.has_bili_comments:
            raise TimeoutError("bili-comments missing")
        if state == "visible" and not await self.is_visible():
            raise TimeoutError(f"{self.selector} not visible")

    async def scroll_into_view_if_needed(self):
        self.page.events.append(("scroll", self.selector))

    async def click(self):
        self.page.events.append(("click", self.selector))
        if self.selector in self.page.on_click:
            self.page.on_click[self.selector]()

    async def press_sequentially(self, text: str, *, delay: int = 0):
        self.page.events.append(("type", self.selector, text, delay))
        self.page.typed.append((self.selector, text, delay))


class FakeExpectResponse:
    def __init__(self, page, predicate, timeout: int):
        self.page = page
        self.predicate = predicate
        self.timeout = timeout
        self.value = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        if self.page.response_error is not None:
            raise self.page.response_error
        resp = self.page.response_to_emit
        if resp is None or not self.predicate(resp):
            raise TimeoutError("no matching response")
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(resp)
        self.value = fut
        return False


class FakeResponse:
    def __init__(self, payload, *, status=200, url="https://api.bilibili.com/x/v2/reply/add", method="POST"):
        self._payload = payload
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method=method)

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakePage:
    def __init__(self):
        self.urls: list[str] = []
        self.events: list[tuple] = []
        self.waits: list[tuple] = []
        self.typed: list[tuple[str, str, int]] = []
        self.closed = False
        self.has_bili_comments = True
        self.counts: dict[str, int] = {
            TOP_LEVEL_EDITOR_SELECTOR: 1,
            TOP_LEVEL_PUBLISH_SELECTOR: 1,
            INLINE_EDITOR_SELECTOR: 1,
            INLINE_PUBLISH_SELECTOR: 1,
            "bili-comments [data-tuber-reply-target] bili-comment-action-buttons-renderer #reply > button": 1,
        }
        self.visible: dict[str, bool] = {}
        self.enabled: dict[str, bool] = {}
        self.on_click: dict[str, object] = {}
        self.evaluate_handler = None
        self.response_to_emit = FakeResponse(
            {
                "code": 0,
                "data": {
                    "rpid": 987654321,
                    "rpid_str": "987654321",
                    "success_action": 0,
                    "need_captcha": False,
                    "success_toast": "发送成功",
                    "reply": {"invisible": False},
                },
            }
        )
        self.response_error = None

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def goto(self, url: str, *, wait_until="domcontentloaded", timeout=None):
        self.urls.append(url)
        self.events.append(("goto", url, wait_until, timeout))

    async def evaluate(self, script: str, arg=None):
        self.events.append(("evaluate", arg))
        if self.evaluate_handler is not None:
            return self.evaluate_handler(script, arg)
        if "data-tuber-reply-target" in script:
            return {"found": True}
        return False

    def expect_response(self, predicate, *, timeout=20000):
        return FakeExpectResponse(self, predicate, timeout)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage | None = None):
        self.pages: list[FakePage] = []
        self.next_page = page
        self.stored_cookies: list[dict] = []
        self.add_cookies_calls: list[list[dict]] = []
        self.closed = False

    async def cookies(self, urls=None):
        return list(self.stored_cookies)

    async def add_cookies(self, cookies: list[dict]):
        self.add_cookies_calls.append(list(cookies))
        for item in cookies:
            self.stored_cookies = [c for c in self.stored_cookies if c.get("name") != item["name"]]
            self.stored_cookies.append(dict(item))

    async def new_page(self) -> FakePage:
        page = self.next_page if self.next_page is not None and not self.pages else FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakePlaywrightRuntime:
    def __init__(self, context: FakeContext):
        self.context = context
        self.launch_calls: list[dict] = []
        self.stopped = False
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    async def _launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self.context

    async def stop(self):
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, runtime: FakePlaywrightRuntime):
        self.runtime = runtime

    async def start(self):
        return self.runtime


def _make_publisher(tmp_path: Path, page: FakePage | None = None, **overrides):
    context = FakeContext(page or FakePage())
    runtime = FakePlaywrightRuntime(context)
    kwargs = {
        "sessdata": CREDS["sessdata"],
        "csrf": CREDS["csrf"],
        "uid": CREDS["uid"],
        "profile_dir": tmp_path / "profile" / CREDS["uid"],
        "executable_path": "/usr/bin/chromium",
        "dwell_range": (0.0, 0.0),
        "post_type_delay_range": (0.0, 0.0),
        "keystroke_delay_ms": (35, 85),
        "playwright_factory": lambda: FakePlaywrightStarter(runtime),
    }
    kwargs.update(overrides)
    publisher = BilibiliBrowserCommentPublisher(**kwargs)
    return publisher, context, runtime


def test_resolve_browser_profile_dir_and_read_only_fallback(tmp_path, monkeypatch):
    resolved = resolve_browser_profile_dir("10086", data_root=tmp_path)
    assert resolved == tmp_path / "bilibili_browser_profile" / "10086"
    assert resolved.is_dir()

    anon = resolve_browser_profile_dir("", data_root=tmp_path)
    assert anon == tmp_path / "bilibili_browser_profile" / "anon"

    fallback_base = tmp_path / "fallback_tmp"
    monkeypatch.setattr(bc.tempfile, "gettempdir", lambda: str(fallback_base))
    monkeypatch.delenv("TUBER_DATA_ROOT", raising=False)
    monkeypatch.delenv("TUBER_RUNTIME", raising=False)

    def fake_writable(path: Path) -> bool:
        return str(path).startswith(str(fallback_base))

    monkeypatch.setattr(bc, "_is_writable_dir", fake_writable)
    fallback = resolve_browser_profile_dir("42", data_root=tmp_path / "ro", settings=SimpleNamespace(data_root=tmp_path / "ro_cfg"))
    assert fallback == fallback_base / "tuber_studio" / "bilibili_browser_profile" / "42"


async def test_launch_flags_cookie_sync_and_context_lifecycle(tmp_path):
    publisher, context, runtime = _make_publisher(tmp_path)
    target = {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"}

    receipt1 = await publisher.publish_comment(target, "第一条真实浏览器评论")
    assert receipt1["rpid"] == "987654321"
    assert receipt1["transport"] == "browser_ui"
    assert len(runtime.launch_calls) == 1
    launch = runtime.launch_calls[0]
    assert launch["user_data_dir"] == str(tmp_path / "profile" / CREDS["uid"])
    assert launch["executable_path"] == "/usr/bin/chromium"
    assert launch["headless"] is True
    assert launch["user_agent"] == BILIBILI_WEB_USER_AGENT
    assert launch["viewport"] == {"width": 1440, "height": 900}
    assert launch["locale"] == "zh-CN"
    assert launch["timezone_id"] == "Asia/Taipei"
    assert launch["args"] == list(CHROMIUM_LAUNCH_ARGS)

    assert len(context.add_cookies_calls) == 1
    assert {c["name"]: c["value"] for c in context.add_cookies_calls[0]} == {
        "SESSDATA": CREDS["sessdata"],
        "bili_jct": CREDS["csrf"],
        "DedeUserID": CREDS["uid"],
    }
    assert all(c["domain"] == ".bilibili.com" and c["path"] == "/" for c in context.add_cookies_calls[0])
    assert context.pages[0].closed is True
    assert context.closed is False

    # Second publish reuses persistent context and skips redundant cookie sync.
    context.next_page = FakePage()
    receipt2 = await publisher.publish_comment(target, "第二条")
    assert receipt2["rpid"] == "987654321"
    assert len(runtime.launch_calls) == 1
    assert len(context.add_cookies_calls) == 1

    await publisher.aclose()
    assert context.closed is True
    assert runtime.stopped is True


async def test_top_level_video_and_dynamic_publish_order_and_receipt(tmp_path):
    page = FakePage()
    publisher, _, _ = _make_publisher(tmp_path, page=page)
    order: list[str] = []

    def record_pub_click():
        order.append("publish_click")

    page.on_click[TOP_LEVEL_PUBLISH_SELECTOR] = record_pub_click

    def before_send():
        assert len(page.typed) == 1
        order.append("before_send")

    receipt = await publisher.publish_comment(
        {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"},
        "这条视频留白真舒服",
        before_send=before_send,
    )
    assert page.urls == [f"https://www.bilibili.com/video/{BV}/"]
    assert order == ["before_send", "publish_click"]
    assert receipt == {
        "platform": "bilibili",
        "code": 0,
        "rpid": "987654321",
        "oid": "123",
        "type": 1,
        "root": "0",
        "parent": "0",
        "success_action": 0,
        "need_captcha": False,
        "reply_invisible": False,
        "success_toast": "发送成功",
        "transport": "browser_ui",
    }

    dyn_page = FakePage()
    pub2, _, _ = _make_publisher(tmp_path / "dyn", page=dyn_page)
    dyn_receipt = await pub2.publish_comment(
        {"resource_type": "dynamic", "type": 11, "oid": "999", "dynamic_id": "1275455259411480581", "root": "0", "parent": "0"},
        "动态来打个招呼",
    )
    assert dyn_page.urls == ["https://t.bilibili.com/1275455259411480581"]
    assert dyn_receipt["transport"] == "browser_ui"
    assert dyn_receipt["type"] == 11


async def test_reply_flow_matches_lit_data_expands_nested_and_handles_not_found(tmp_path):
    page = FakePage()
    calls = {"locate": 0, "expand": 0}

    def eval_handler(script: str, arg=None):
        if "data-tuber-reply-target" in script:
            calls["locate"] += 1
            assert arg == "321413045233"
            return {"found": calls["locate"] >= 2}
        if "bili-comment-replies-renderer" in script:
            calls["expand"] += 1
            assert arg == "321413045000"
            return True
        return False

    page.evaluate_handler = eval_handler
    publisher, _, _ = _make_publisher(tmp_path, page=page)
    receipt = await publisher.publish_comment(
        {
            "resource_type": "video",
            "type": 1,
            "oid": "123",
            "bvid": BV,
            "root": "321413045000",
            "parent": "321413045233",
        },
        "楼内补充一句",
    )
    assert page.urls == [f"https://www.bilibili.com/video/{BV}/#reply321413045233"]
    assert calls == {"locate": 2, "expand": 1}
    assert page.typed[0][0] == INLINE_EDITOR_SELECTOR
    assert receipt["root"] == "321413045000"
    assert receipt["parent"] == "321413045233"

    missing_page = FakePage()
    missing_page.evaluate_handler = lambda script, arg=None: {"found": False} if "data-tuber-reply-target" in script else False
    pub_missing, _, _ = _make_publisher(tmp_path / "missing", page=missing_page)
    with pytest.raises(PlatformReadError, match="页面未定位到目标评论") as exc:
        await pub_missing.publish_comment(
            {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "11", "parent": "12"},
            "找不到评论",
        )
    assert exc.value.kind == "not_found"
    assert missing_page.closed is True


@pytest.mark.parametrize("selector", [".geetest_panel", ".geetest_holder", "text=安全验证"])
async def test_captcha_overlay_aborts_immediately_without_publish(tmp_path, selector):
    page = FakePage()
    page.counts[selector] = 1
    page.visible[selector] = True
    publisher, _, _ = _make_publisher(tmp_path, page=page)
    called = []

    with pytest.raises(PlatformRejected, match="平台要求人机验证"):
        await publisher.publish_comment(
            {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"},
            "触发验证",
            before_send=lambda: called.append("before_send"),
        )
    assert called == []
    assert not any(ev[0] == "click" and ev[1] == TOP_LEVEL_PUBLISH_SELECTOR for ev in page.events)
    assert page.closed is True


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "data": {"need_captcha": True}},
        {"code": -352, "message": "风控拦截"},
        {"code": 12015, "message": "需要验证码"},
    ],
)
async def test_captcha_response_aborts_immediately_without_retry(tmp_path, payload):
    page = FakePage()
    page.response_to_emit = FakeResponse(payload)
    publisher, _, _ = _make_publisher(tmp_path, page=page)
    with pytest.raises(PlatformRejected, match="平台要求人机验证"):
        await publisher.publish_comment(
            {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"},
            "验证码响应",
        )
    assert len(page.urls) == 1
    assert page.closed is True


@pytest.mark.parametrize("selector", ["#disabled-commentbox", ".bili-mini-mask", ".login-scan-wp"])
async def test_login_wall_raises_account_expired(tmp_path, selector):
    page = FakePage()
    page.counts[selector] = 1
    page.visible[selector] = True
    publisher, _, _ = _make_publisher(tmp_path, page=page)
    with pytest.raises(BilibiliAccountExpired, match="B站登录已失效"):
        await publisher.publish_comment(
            {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"},
            "掉登录",
        )
    assert page.closed is True


@pytest.mark.parametrize(
    "setup",
    ["timeout", "http_500", "invalid_json", "missing_rpid"],
)
async def test_missing_or_invalid_response_raises_publish_uncertain(tmp_path, setup):
    page = FakePage()
    if setup == "timeout":
        page.response_error = TimeoutError("timed out")
    elif setup == "http_500":
        page.response_to_emit = FakeResponse({"code": 0, "data": {"rpid": 1}}, status=500)
    elif setup == "invalid_json":
        page.response_to_emit = FakeResponse(ValueError("bad json"))
    else:
        page.response_to_emit = FakeResponse({"code": 0, "data": {}})

    publisher, _, _ = _make_publisher(tmp_path, page=page)
    with pytest.raises(PublishUncertain):
        await publisher.publish_comment(
            {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"},
            "未知结果",
        )
    assert page.closed is True


async def test_per_account_lock_serializes_concurrent_publishes(tmp_path):
    events: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowPublisher(BilibiliBrowserCommentPublisher):
        async def _ensure_context(self):
            events.append("enter")
            if len(events) == 1:
                entered.set()
                await release.wait()
            return await super()._ensure_context()

    page1, page2 = FakePage(), FakePage()
    ctx = FakeContext(page1)
    runtime = FakePlaywrightRuntime(ctx)
    pub = SlowPublisher(
        sessdata=CREDS["sessdata"],
        csrf=CREDS["csrf"],
        uid=CREDS["uid"],
        profile_dir=tmp_path / "profile" / CREDS["uid"],
        dwell_range=(0.0, 0.0),
        post_type_delay_range=(0.0, 0.0),
        playwright_factory=lambda: FakePlaywrightStarter(runtime),
    )
    target = {"resource_type": "video", "type": 1, "oid": "123", "bvid": BV, "root": "0", "parent": "0"}
    t1 = asyncio.create_task(pub.publish_comment(target, "第一"))
    await entered.wait()
    t2 = asyncio.create_task(pub.publish_comment(target, "第二"))
    await asyncio.sleep(0.02)
    assert events == ["enter"]
    release.set()
    ctx.next_page = page2
    await asyncio.gather(t1, t2)
    assert events == ["enter", "enter"]


async def test_transport_routing_mock_vs_real_vs_image_vs_env(monkeypatch):
    monkeypatch.setattr(bc, "playwright_available", lambda: True)
    monkeypatch.setattr(bc, "find_chromium_executable", lambda: "/usr/bin/chromium")
    monkeypatch.delenv("TUBER_BILIBILI_COMMENT_TRANSPORT", raising=False)

    # Real adapter defaults to browser_ui when Playwright & Chromium exist.
    real_adapter = BrowsingAdapter(CREDS)
    try:
        assert real_adapter.resolve_comment_transport() == "browser_ui"
        assert real_adapter.resolve_comment_transport(has_image=True) == "http_api"
        monkeypatch.setenv("TUBER_BILIBILI_COMMENT_TRANSPORT", "http_api")
        assert real_adapter.resolve_comment_transport() == "http_api"
    finally:
        await real_adapter.close()

    # MockTransport ALWAYS routes to http_api even when env requests browser_ui.
    monkeypatch.setenv("TUBER_BILIBILI_COMMENT_TRANSPORT", "browser_ui")
    mock_adapter = BrowsingAdapter(CREDS)
    orig = mock_adapter.client
    mock_adapter.client = httpx.AsyncClient(
        base_url=orig.base_url,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"code": 0})),
    )
    await orig.aclose()
    try:
        assert mock_adapter.resolve_comment_transport() == "http_api"
    finally:
        await mock_adapter.close()

    # Auto mode falls back to http_api when Chromium is absent.
    monkeypatch.delenv("TUBER_BILIBILI_COMMENT_TRANSPORT", raising=False)
    monkeypatch.setattr(bc, "find_chromium_executable", lambda: None)
    no_chrome = BrowsingAdapter(CREDS)
    try:
        assert no_chrome.resolve_comment_transport() == "http_api"
    finally:
        await no_chrome.close()


async def test_browsing_and_client_adapter_wire_to_browser_publisher_and_close():
    calls = []
    closed = []

    class StubPublisher:
        async def publish_comment(self, target, text, *, before_send=None, mentions=()):
            calls.append((dict(target), text, before_send, tuple(mentions)))
            if before_send:
                before_send()
            return {
                "platform": "bilibili",
                "code": 0,
                "rpid": "445566",
                "oid": target["oid"],
                "type": target["type"],
                "root": target["root"],
                "parent": target["parent"],
                "transport": "browser_ui",
            }

        async def aclose(self):
            closed.append(True)

    browsing = BrowsingAdapter(CREDS, comment_transport="browser_ui", browser_publisher=StubPublisher())
    browsing._record_video_read({"aid": 123, "bvid": BV, "cid": 456, "duration": 60})
    r1 = await browsing.publish_top_level({"resource_type": "video", "oid": "123"}, "自动补全 bvid")
    assert r1["transport"] == "browser_ui" and r1["rpid"] == "445566"
    assert calls[-1][0]["bvid"] == BV

    r2 = await browsing.publish_resource_reply(
        {"resource_type": "dynamic", "type": 11, "oid": "777", "dynamic_id": "888", "root": "10", "parent": "11"},
        "动态回复保留 dynamic_id",
    )
    assert r2["rpid"] == "445566"
    assert calls[-1][0]["dynamic_id"] == "888"
    await browsing.close()
    assert closed == [True]

    client = BilibiliAdapter(CREDS, comment_transport="browser_ui", browser_publisher=StubPublisher())
    client._record_video_read({"aid": 123, "bvid": BV, "cid": 456})
    r3 = await client.publish(BV, "500", "通过 client.publish 发送", thread_id="400")
    assert r3["video_id"] == BV and r3["aid"] == 123 and r3["root"] == "400" and r3["parent"] == "500"
    assert r3["transport"] == "browser_ui"
    await client.close()
    assert closed == [True, True]


def test_remotion_policy_defaults_raised():
    assert DEFAULT_VIDEO_PRODUCTION_POLICY["max_render_concurrency"] == 4
    assert DEFAULT_VIDEO_PRODUCTION_POLICY["render_timeout_seconds"] == 1800.0
    policy = VideoProductionPolicy()
    assert policy.max_render_concurrency == 4
    assert policy.render_timeout_seconds == 1800.0
