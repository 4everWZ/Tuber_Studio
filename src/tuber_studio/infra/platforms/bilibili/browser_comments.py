"""Browser UI comment publisher via Playwright persistent Chromium context.

Uses genuine browser navigation, open Shadow DOM interaction inside <bili-comments>,
human-like typing delays, and intercepts the real POST /x/v2/reply/add response
without generating synthetic fingerprints or solving/bypassing CAPTCHAs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import random
import re
import shutil
import tempfile
from pathlib import Path

from ....core.errors import PlatformRejected, PublishUncertain, platform_rejection
from ....core.text import validate_comment_body
from .client import _BV, BILIBILI_WEB_USER_AGENT, BilibiliAccountExpired, PlatformReadError, _positive_id

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - optional runtime guard
    async_playwright = None  # type: ignore[assignment]

CHROMIUM_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
)

LOGIN_WALL_SELECTORS = (
    "#disabled-commentbox",
    ".bili-mini-mask",
    ".login-scan-wp",
)

CAPTCHA_SELECTORS = (
    ".geetest_panel",
    ".geetest_holder",
    "[class*='geetest']",
    ".bili-captcha",
    "[class*='captcha']",
)

TOP_LEVEL_EDITOR_SELECTOR = (
    "bili-comments bili-comments-header-renderer bili-comment-box "
    'bili-comment-rich-textarea .brt-editor[contenteditable="true"]'
)
TOP_LEVEL_PUBLISH_SELECTOR = (
    "bili-comments bili-comments-header-renderer bili-comment-box #footer > #pub > button"
)
INLINE_EDITOR_SELECTOR = (
    "bili-comments bili-comment-thread-renderer #reply-container bili-comment-box "
    'bili-comment-rich-textarea .brt-editor[contenteditable="true"]'
)
INLINE_PUBLISH_SELECTOR = (
    "bili-comments bili-comment-thread-renderer #reply-container bili-comment-box #footer > #pub > button"
)

CAPTCHA_ABORT_MESSAGE = "平台要求人机验证，请人工在 B 站完成验证后重试"
LOGIN_EXPIRED_MESSAGE = "B站登录已失效，请重新登录"

_PROFILE_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}

_LOCATE_REPLY_TARGET_JS = """(targetRpid) => {
    const target = String(targetRpid);
    const rootHost = document.querySelector("bili-comments");
    if (!rootHost) return { found: false, hasExpand: false };

    const visited = new Set();
    const walk = (node, currentThread) => {
        if (!node || visited.has(node)) return null;
        visited.add(node);

        const tag = (node.tagName || "").toLowerCase();
        const threadEl = tag === "bili-comment-thread-renderer" ? node : currentThread;
        if (tag === "bili-comment-renderer" || tag === "bili-comment-reply-renderer") {
            const data = node.data;
            if (data && (String(data.rpid_str || "") === target || String(data.rpid || "") === target)) {
                return { el: node, thread: threadEl };
            }
        }

        if (node.shadowRoot) {
            for (const child of node.shadowRoot.children) {
                const hit = walk(child, threadEl);
                if (hit) return hit;
            }
        }
        for (const child of node.children || []) {
            const hit = walk(child, threadEl);
            if (hit) return hit;
        }
        return null;
    };

    for (const prev of document.querySelectorAll("[data-tuber-reply-target]")) {
        prev.removeAttribute("data-tuber-reply-target");
    }
    const match = walk(rootHost, null);
    if (match) {
        match.el.setAttribute("data-tuber-reply-target", target);
        match.el.scrollIntoView({ behavior: "smooth", block: "center" });
        return { found: true };
    }
    return { found: false };
}"""

_EXPAND_THREAD_REPLIES_JS = """(rootRpid) => {
    const rootId = String(rootRpid || "");
    const rootHost = document.querySelector("bili-comments");
    if (!rootHost || !rootHost.shadowRoot) return false;
    const threads = rootHost.shadowRoot.querySelectorAll("bili-comment-thread-renderer");
    for (const thread of threads) {
        const sr = thread.shadowRoot;
        if (!sr) continue;
        const main = sr.querySelector("bili-comment-renderer");
        const data = main && main.data;
        if (rootId && rootId !== "0" && data) {
            const rid = String(data.rpid_str || data.rpid || "");
            if (rid && rid !== rootId) continue;
        }
        const replies = sr.querySelector("bili-comment-replies-renderer");
        if (!replies || !replies.shadowRoot) continue;
        const btn = replies.shadowRoot.querySelector("#view-more button, bili-text-button button, button");
        if (btn && !btn.disabled) {
            btn.click();
            return true;
        }
    }
    return false;
}"""


def playwright_available() -> bool:
    """Return True if playwright.async_api can be imported."""
    return async_playwright is not None or importlib.util.find_spec("playwright.async_api") is not None


def find_chromium_executable() -> str | None:
    """Locate Chromium/Chrome binary without downloading or mutating the host."""
    env_path = str(os.getenv("TUBER_CHROMIUM_PATH") or "").strip()
    if env_path and Path(env_path).is_file():
        return env_path
    container_default = Path("/usr/bin/chromium")
    if container_default.is_file():
        return str(container_default)
    for binary in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(binary)
        if found:
            return found
    return None


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def resolve_browser_profile_dir(
    uid: str | None = None,
    *,
    data_root: Path | str | None = None,
    settings: object | None = None,
) -> Path:
    """Resolve a writable persistent browser profile directory for the account."""
    raw_uid = str(uid or "").strip()
    uid_part = raw_uid if re.fullmatch(r"[0-9A-Za-z_-]{1,64}", raw_uid) else "anon"

    candidates: list[Path] = []
    if data_root:
        candidates.append(Path(data_root))
    if settings is not None:
        for attr in ("data_root", "runtime"):
            value = getattr(settings, attr, None)
            if value:
                candidates.append(Path(value))
    for env_key in ("TUBER_DATA_ROOT", "TUBER_RUNTIME"):
        env_val = str(os.getenv(env_key) or "").strip()
        if env_val:
            candidates.append(Path(env_val))
    if os.name != "nt" or Path("/data/business").exists():
        candidates.append(Path("/data/business"))
    candidates.append(Path(tempfile.gettempdir()) / "tuber_studio")

    for base in candidates:
        profile_dir = base / "bilibili_browser_profile" / uid_part
        if _is_writable_dir(profile_dir):
            return profile_dir

    fallback = Path(tempfile.gettempdir()) / "tuber_studio" / "bilibili_browser_profile" / uid_part
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


class BilibiliBrowserCommentPublisher:
    """Publish text comments through real Bilibili Web UI in headless Chromium."""

    def __init__(
        self,
        *,
        sessdata: str = "",
        csrf: str = "",
        uid: str = "",
        profile_dir: Path | str | None = None,
        data_root: Path | str | None = None,
        settings: object | None = None,
        executable_path: str | None = None,
        headless: bool = True,
        dwell_range: tuple[float, float] = (5.0, 10.0),
        keystroke_delay_ms: tuple[int, int] = (35, 85),
        post_type_delay_range: tuple[float, float] = (1.0, 2.0),
        response_timeout_ms: int = 20000,
        navigation_timeout_ms: int = 30000,
        playwright_factory=None,
    ):
        self._sessdata = str(sessdata or "")
        self._csrf = str(csrf or "")
        self._uid = str(uid or "")
        self.profile_dir = (
            Path(profile_dir)
            if profile_dir is not None
            else resolve_browser_profile_dir(self._uid, data_root=data_root, settings=settings)
        )
        self._executable_path = executable_path
        self.headless = bool(headless)
        self._dwell_range = (max(0.0, float(dwell_range[0])), max(0.0, float(dwell_range[1])))
        lo_ms, hi_ms = int(keystroke_delay_ms[0]), int(keystroke_delay_ms[1])
        self._keystroke_delay_ms = (max(0, lo_ms), max(0, lo_ms, hi_ms))
        self._post_type_delay_range = (
            max(0.0, float(post_type_delay_range[0])),
            max(0.0, float(post_type_delay_range[1])),
        )
        self._response_timeout_ms = max(1000, int(response_timeout_ms))
        self._navigation_timeout_ms = max(1000, int(navigation_timeout_ms))
        self._playwright_factory = playwright_factory
        self._playwright = None
        self._context = None

    def _account_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = (id(loop), str(self.profile_dir))
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PROFILE_LOCKS[key] = lock
        return lock

    async def _ensure_context(self):
        if self._context is not None:
            return self._context
        factory = self._playwright_factory or async_playwright
        if factory is None:
            raise RuntimeError("Playwright 未安装，无法启动浏览器评论发布器")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await factory().start()
        executable = self._executable_path or find_chromium_executable()
        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "user_agent": BILIBILI_WEB_USER_AGENT,
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
            "timezone_id": "Asia/Taipei",
            "args": list(CHROMIUM_LAUNCH_ARGS),
        }
        if executable:
            launch_kwargs["executable_path"] = executable
        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        return self._context

    async def _sync_cookies(self, context) -> None:
        existing_list = await context.cookies(["https://www.bilibili.com"])
        existing = {
            str(item.get("name")): str(item.get("value") or "")
            for item in (existing_list or [])
            if isinstance(item, dict) and item.get("domain") in {".bilibili.com", "www.bilibili.com", "bilibili.com", None}
        }
        desired = (
            ("SESSDATA", self._sessdata),
            ("bili_jct", self._csrf),
            ("DedeUserID", self._uid),
        )
        to_add = [
            {"name": name, "value": value, "domain": ".bilibili.com", "path": "/"}
            for name, value in desired
            if value and existing.get(name) != value
        ]
        if to_add:
            await context.add_cookies(to_add)

    async def _any_visible(self, page, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() > 0:
                if await locator.first.is_visible():
                    return True
        return False

    async def _assert_no_login_or_captcha(self, page) -> None:
        if await self._any_visible(page, CAPTCHA_SELECTORS):
            raise PlatformRejected(-352, CAPTCHA_ABORT_MESSAGE, display_message=CAPTCHA_ABORT_MESSAGE)
        safety_text = page.locator("text=安全验证")
        if await safety_text.count() > 0 and await safety_text.first.is_visible():
            raise PlatformRejected(-352, CAPTCHA_ABORT_MESSAGE, display_message=CAPTCHA_ABORT_MESSAGE)
        if await self._any_visible(page, LOGIN_WALL_SELECTORS):
            raise BilibiliAccountExpired(LOGIN_EXPIRED_MESSAGE)

    async def _type_human(self, page, editor, text: str) -> None:
        delay = random.randint(self._keystroke_delay_ms[0], self._keystroke_delay_ms[1])
        if hasattr(editor, "press_sequentially"):
            await editor.press_sequentially(text, delay=delay)
        else:
            await page.keyboard.type(text, delay=delay)

    async def _sleep_range(self, bounds: tuple[float, float]) -> None:
        lo, hi = bounds
        if hi > 0:
            duration = random.uniform(lo, max(lo, hi))
            if duration > 0:
                await asyncio.sleep(duration)

    @staticmethod
    def _build_url(target: dict) -> tuple[str, str, str, bool]:
        resource_type = target.get("resource_type") or ("dynamic" if target.get("type") in {11, 17} else "video")
        raw_root = str(target.get("root") or "0")
        raw_parent = str(target.get("parent") or target.get("comment_id") or "0")
        is_top_level = raw_root in {"", "0"} and raw_parent in {"", "0"}
        root = "0" if is_top_level else _positive_id(raw_root)
        parent = "0" if is_top_level else _positive_id(raw_parent)

        if resource_type == "dynamic":
            dynamic_id = str(
                target.get("dynamic_id") or (target.get("oid") if target.get("type") == 17 else "") or ""
            ).strip()
            if not dynamic_id:
                raise ValueError("动态评论浏览器发布需要 dynamic_id")
            url = f"https://t.bilibili.com/{dynamic_id}" if is_top_level else f"https://t.bilibili.com/{dynamic_id}#reply{parent}"
            return url, root, parent, is_top_level

        bvid = str(target.get("bvid") or "").strip()
        if _BV.fullmatch(bvid):
            base = f"https://www.bilibili.com/video/{bvid}/"
        else:
            oid = str(target.get("oid") or "").strip()
            if not oid:
                raise ValueError("视频评论浏览器发布需要 bvid 或 oid")
            base = f"https://www.bilibili.com/video/av{oid}/"
        url = base if is_top_level else f"{base}#reply{parent}"
        return url, root, parent, is_top_level

    async def _locate_reply_button(self, page, root: str, parent: str):
        for attempt in range(3):
            result = await page.evaluate(_LOCATE_REPLY_TARGET_JS, parent)
            if isinstance(result, dict) and result.get("found"):
                return page.locator("bili-comments [data-tuber-reply-target] bili-comment-action-buttons-renderer #reply > button").first
            if attempt == 0 and root != parent:
                expanded = await page.evaluate(_EXPAND_THREAD_REPLIES_JS, root)
                if expanded:
                    await asyncio.sleep(0.6)
                    continue
            if attempt < 2:
                await page.evaluate("() => window.scrollBy(0, 500)")
                await asyncio.sleep(0.4)
        raise PlatformReadError("not_found", "页面未定位到目标评论")

    @staticmethod
    def _is_reply_add_response(response) -> bool:
        try:
            return "/x/v2/reply/add" in response.url and response.request.method == "POST"
        except AttributeError:
            return False

    def _parse_reply_response(self, payload: object, target: dict, root: str, parent: str) -> dict:
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("发布结果未知，请到 B 站核对；不会自动重试")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if data.get("need_captcha") is True or payload["code"] in {-352, 12015}:
            code = payload["code"] if payload["code"] != 0 else 12015
            raise PlatformRejected(code, CAPTCHA_ABORT_MESSAGE, display_message=CAPTCHA_ABORT_MESSAGE)
        if payload["code"] != 0:
            raise platform_rejection(
                payload,
                fallback="平台明确拒绝了本次评论",
                secret_values=(self._sessdata, self._csrf),
            )
        try:
            rpid = _positive_id(data.get("rpid_str") or data.get("rpid"))
        except ValueError:
            raise PublishUncertain("B 站回执缺少评论 ID") from None
        diagnostics = {}
        if type(data.get("success_action")) is int:
            diagnostics["success_action"] = data["success_action"]
        if type(data.get("need_captcha")) is bool:
            diagnostics["need_captcha"] = data["need_captcha"]
        reply_obj = data.get("reply")
        if isinstance(reply_obj, dict) and "invisible" in reply_obj:
            diagnostics["reply_invisible"] = reply_obj.get("invisible") in (True, 1, "1")
        toast = data.get("success_toast")
        if isinstance(toast, str) and toast.strip():
            clean_toast = toast.strip()[:200]
            if not any(secret and secret in clean_toast for secret in (self._sessdata, self._csrf)):
                diagnostics["success_toast"] = clean_toast
        return {
            "platform": "bilibili",
            "code": 0,
            "rpid": rpid,
            "oid": str(target.get("oid") or ""),
            "type": target.get("type", 1),
            "root": root,
            "parent": parent,
            **diagnostics,
            "transport": "browser_ui",
        }

    async def publish_comment(
        self,
        target: dict,
        text: str,
        *,
        before_send=None,
        mentions=(),
    ) -> dict:
        del mentions  # Browser UI submits user-visible text directly through the Bilibili page editor.
        if not self._sessdata or not self._csrf:
            raise ValueError("发布需要当前账号SESSDATA及CSRF")
        text = validate_comment_body(text)
        url, root, parent, is_top_level = self._build_url(target)

        async with self._account_lock():
            context = await self._ensure_context()
            await self._sync_cookies(context)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self._navigation_timeout_ms)
                comments_host = page.locator("bili-comments").first
                await comments_host.wait_for(state="attached", timeout=self._navigation_timeout_ms)
                await comments_host.scroll_into_view_if_needed()
                await self._sleep_range(self._dwell_range)
                await self._assert_no_login_or_captcha(page)

                if is_top_level:
                    editor = page.locator(TOP_LEVEL_EDITOR_SELECTOR).first
                    await editor.wait_for(state="visible", timeout=10000)
                    await editor.click()
                    await self._type_human(page, editor, text)
                    await self._sleep_range(self._post_type_delay_range)
                    pub_btn = page.locator(TOP_LEVEL_PUBLISH_SELECTOR).first
                else:
                    reply_btn = await self._locate_reply_button(page, root, parent)
                    await reply_btn.click()
                    await self._assert_no_login_or_captcha(page)
                    editor = page.locator(INLINE_EDITOR_SELECTOR).first
                    await editor.wait_for(state="visible", timeout=10000)
                    await editor.click()
                    await self._type_human(page, editor, text)
                    await self._sleep_range(self._post_type_delay_range)
                    pub_btn = page.locator(INLINE_PUBLISH_SELECTOR).first

                await pub_btn.wait_for(state="visible", timeout=10000)
                if hasattr(pub_btn, "is_enabled") and not await pub_btn.is_enabled():
                    raise PublishUncertain("发布按钮不可用，未提交评论")
                await self._assert_no_login_or_captcha(page)

                if before_send is not None:
                    before_send()

                try:
                    async with page.expect_response(
                        self._is_reply_add_response, timeout=self._response_timeout_ms
                    ) as resp_info:
                        await pub_btn.click()
                    response = await resp_info.value
                except Exception:
                    await self._assert_no_login_or_captcha(page)
                    raise PublishUncertain("发布结果未知，请到 B 站核对；不会自动重试") from None

                if getattr(response, "status", 200) >= 400:
                    raise PublishUncertain("发布结果未知，请到 B 站核对；不会自动重试")
                try:
                    payload = await response.json()
                except Exception:
                    raise PublishUncertain("发布结果未知，请到 B 站核对；不会自动重试") from None

                return self._parse_reply_response(payload, target, root, parent)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def aclose(self) -> None:
        """Close the persistent browser context while keeping profile data on disk."""
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        try:
            if context is not None:
                await context.close()
        finally:
            if playwright is not None:
                await playwright.stop()
