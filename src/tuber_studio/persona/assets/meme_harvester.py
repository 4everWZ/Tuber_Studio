"""Multi-channel meme harvesting, two-stage screening, inbox staging, and subscription sync."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import time
from contextlib import nullcontext
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urldefrag, urlsplit
from uuid import uuid4

import httpx
from PIL import Image, ImageOps
from pydantic import Field

from ...core.models import Emotion, StrictModel
from ...core.turns import operation_ticket
from ...core.validation import linked
from ...infra.platforms.bilibili.account import space_uid
from ...infra.platforms.bilibili.comment_materials import (
    MAX_IMAGE_BYTES,
    CommentMaterialError,
    comment_image_url,
    sample_comment_image,
    without_article_links,
)
from ...infra.web_client.fetch import MAX_URL, public_https_address, public_ip
from .meme_dhash import compute_dhash, dhash_matches
from .memes import MAX_BYTES, MAX_PIXELS, MemeCatalog, MemeLabels, MemeLicense, MemeSource

DISCARD_TTL_SECONDS = 7 * 86400
MAX_QUEUE_ROWS = 128
MAX_FEED_JSON_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
SYNC_TICK_SECONDS = 15 * 60
VISION_PASS_CONFIDENCE = 0.55

INBOX_BLOB_RE = re.compile(r"^[a-f0-9]{64}\.(gif|png|jpg|webp)$", re.IGNORECASE)
PENDING_BLOB_RE = re.compile(r"^\.pending-[a-z0-9_-]{1,64}$", re.IGNORECASE)
GITHUB_TREE_RE = re.compile(
    r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/tree/([^/]+)(?:/(.*))?$"
)
CV_RE = re.compile(r"^(?:https?://(?:www\.|m\.)?bilibili\.com/read/)?cv([1-9][0-9]{0,19})/?$", re.I)
RL_RE = re.compile(r"^(?:https?://(?:www\.|m\.)?bilibili\.com/read/readlist/)?(?:rl)?([1-9][0-9]{0,19})/?$", re.I)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_AD_QR_RE = re.compile(
    r"(?:加[微vV]|微信[号：:\s]?[a-zA-Z0-9_-]{3,}|[vV][xX][:：\s]?[a-zA-Z0-9_-]{3,}|"
    r"二维码|扫码|扫一扫|兼职|代刷|代充|刷单|返利|领红包|优惠券|私[聊信]领|免费领|"
    r"进群|扣扣群|QQ群|\bQR\b|https?://)",
    re.I,
)
_WHALE_MEME_CUE_RE = re.compile(
    r"(?:whale|aqua|大肥鱼|小鲸鱼|鲸鱼|表情包|表情|梗图|斗图|配图|收图|存图|偷图|好图|可爱|萌|贴贴|图)",
    re.I,
)
_VALID_EMOTIONS = frozenset(Emotion.__args__)

HARVESTER_CONFIG_KEY = "meme_harvester_config"
DEFAULT_HARVESTER_CONFIG = {
    "comment_harvest_enabled": True,
    "default_sync_interval_hours": 12,
}


class HarvesterConfigUpdate(StrictModel):
    comment_harvest_enabled: bool | None = None
    default_sync_interval_hours: int | None = Field(default=None, ge=1, le=168)
    meme_inbox_limit_mib: int | None = Field(default=None, ge=0, le=8192)
    meme_inbox_max_items: int | None = Field(default=None, ge=0, le=10000)


class _ArticleImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        for key, value in attrs:
            if key.lower() in {"src", "data-src"} and isinstance(value, str) and value.strip():
                self.urls.append(value.strip())


def parse_public_https_url(raw: str) -> httpx.URL:
    """Validate URL syntax synchronously and reject non-HTTPS, credentials, custom ports, localhost, or private IPs."""
    clean = str(raw or "").strip()
    if not 1 <= len(clean) <= MAX_URL or any(ch.isspace() or ch in "\\\x00" for ch in clean):
        raise ValueError("订阅链接格式无效")
    try:
        url = httpx.URL(urldefrag(clean).url)
    except (ValueError, httpx.InvalidURL):
        raise ValueError("订阅链接格式无效") from None
    host = (url.host or "").rstrip(".").lower()
    if (
        url.scheme != "https"
        or not host
        or url.userinfo
        or url.port is not None
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal", ".ts.net"))
        or ("." not in host and ":" not in host)
    ):
        raise ValueError("仅支持公开 HTTPS 链接（不支持 HTTP、凭据、自定义端口或内网/本机地址）")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not public_ip(str(addr)):
        raise ValueError("订阅链接不能指向内网、回环或链路本地 IP")
    return url


def passes_comment_coarse_filter(text: str | None, attachments: list | tuple | None) -> bool:
    """Fast rule filter for passive comment attachment harvesting (no network or LLM)."""
    items = attachments or ()
    has_image = any(
        (isinstance(item, dict) and item.get("kind") == "image")
        or (isinstance(item, str) and bool(comment_image_url(item)))
        for item in items
    )
    if not has_image:
        return False
    raw = str(text or "").strip()
    dict_items = [item for item in items if isinstance(item, dict)]
    cleaned = without_article_links(raw, dict_items) if dict_items else raw
    if _AD_QR_RE.search(cleaned):
        return False
    if len(raw) > 400 and not _WHALE_MEME_CUE_RE.search(raw):
        return False
    return True


def _clean_title(raw: str, fallback: str = "未命名表情") -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(raw or "")).strip()
    if not cleaned:
        return fallback
    return cleaned[:120]


def _filename_title(source_hint: str) -> str:
    stem = unquote(Path(urlsplit(str(source_hint or "")).path).stem)
    stem = re.sub(r"[-_]+", " ", stem).strip()
    return _clean_title(stem, fallback="未命名表情")


def _normalize_emotions(raw) -> list[str]:
    if not isinstance(raw, list):
        return ["idle"]
    picked: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in _VALID_EMOTIONS and item not in picked:
            picked.append(item)
        if len(picked) >= 3:
            break
    return picked or ["idle"]


def _normalize_labels(tags, description: str) -> MemeLabels:
    clean_tags: list[str] = []
    for tag in tags if isinstance(tags, list) else ():
        if isinstance(tag, str):
            val = re.sub(r"[\x00-\x1f\x7f]+", "", tag).strip()[:40]
            if val:
                clean_tags.append(val)
        if len(clean_tags) >= 8:
            break
    clean_desc = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", str(description or "")).strip()[:400]
    return MemeLabels(tags=clean_tags, description=clean_desc)


def _sample_frames_bounded(content: bytes, *, max_frames: int = 3, max_pixels: int = 1024 * 1024, max_bytes: int = 1024 * 1024) -> list[dict]:
    """Sample 1..max_frames JPEG frames bounded by pixels and bytes."""
    max_frames = max(1, min(int(max_frames), 3))
    max_pixels = max(65536, int(max_pixels))
    max_bytes = max(32768, int(max_bytes))
    if len(content) <= MAX_IMAGE_BYTES:
        try:
            frames, _ = sample_comment_image(
                content, max_frames=max_frames, max_pixels=max_pixels, max_bytes=max_bytes
            )
            return frames
        except CommentMaterialError as err:
            if err.kind != "too_large":
                raise ValueError(str(err)) from None

    edge = min(1024, math.isqrt(max_pixels))

    def encode_frame(img: Image.Image, byte_budget: int) -> dict:
        rgb = img.convert("RGB")
        rgb.thumbnail((edge, edge))
        for quality in (83, 65, 45):
            out = BytesIO()
            rgb.save(out, "JPEG", quality=quality)
            if out.tell() <= byte_budget:
                return {"image": out.getvalue(), "width": rgb.width, "height": rgb.height, "mime_type": "image/jpeg"}
            rgb.thumbnail((max(1, rgb.width * 3 // 4), max(1, rgb.height * 3 // 4)))
        raise ValueError("表情采样帧超出视觉字节上限")

    try:
        with Image.open(BytesIO(content), formats=("JPEG", "PNG", "GIF", "WEBP")) as img:
            if img.width * img.height > MAX_PIXELS:
                raise ValueError("表情图片像素超出上限")
            if img.format not in {"GIF", "WEBP"} or not getattr(img, "is_animated", False):
                return [encode_frame(ImageOps.exif_transpose(img), max_bytes)]
            frames_meta: list[tuple[int, int]] = []
            timestamp = 0
            for idx in range(24):
                try:
                    img.seek(idx)
                except EOFError:
                    break
                frames_meta.append((idx, timestamp))
                dur = img.info.get("duration", 100)
                timestamp += max(10, dur if type(dur) is int else 100)
            count = min(max_frames, len(frames_meta))
            indices = [0] if count == 1 else [i * (len(frames_meta) - 1) // (count - 1) for i in range(count)]
            result = []
            for pos in indices:
                frame_idx, ts = frames_meta[pos]
                img.seek(frame_idx)
                result.append(encode_frame(img, max_bytes // count) | {"time_ms": ts})
            return result
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("表情图片无法安全解码") from None


def _github_contents_api_url(url: str) -> str | None:
    parsed = parse_public_https_url(url)
    host = (parsed.host or "").lower()
    if host == "api.github.com" and parsed.path.startswith("/repos/"):
        return str(parsed)
    if host != "github.com":
        return None
    match = GITHUB_TREE_RE.fullmatch(parsed.path)
    if not match:
        return None
    owner, repo, ref, subpath = match.groups()
    repo = repo.removesuffix(".git")
    clean_path = (subpath or "").strip("/")
    base = f"https://api.github.com/repos/{owner}/{repo}/contents"
    if clean_path:
        base += f"/{clean_path}"
    return f"{base}?ref={ref}"


def _extract_article_images(data: dict) -> list[str]:
    urls: list[str] = []
    for key in ("banner_url",):
        if isinstance(data.get(key), str):
            urls.append(data[key])
    for key in ("image_urls", "origin_image_urls"):
        vals = data.get(key)
        if isinstance(vals, list):
            urls.extend(v for v in vals if isinstance(v, str))
    content = data.get("content")
    if isinstance(content, str) and content:
        if data.get("type") == 3:
            try:
                ops = json.loads(content).get("ops", [])
                for op in ops if isinstance(ops, list) else ():
                    ins = op.get("insert") if isinstance(op, dict) else None
                    if isinstance(ins, dict) and isinstance(ins.get("image"), str):
                        urls.append(ins["image"])
            except (ValueError, TypeError, AttributeError):
                pass
        else:
            parser = _ArticleImageParser()
            try:
                parser.feed(content)
                parser.close()
                urls.extend(parser.urls)
            except Exception:
                pass
    normalized: list[str] = []
    for raw in urls:
        clean = comment_image_url(raw)
        if clean and clean not in normalized:
            normalized.append(clean)
    return normalized


def _extract_dynamic_images(item: dict) -> list[str]:
    urls: list[str] = []
    modules = item.get("modules") or {}
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}
    draw_items = (major.get("draw") or {}).get("items") or []
    for pic in draw_items if isinstance(draw_items, list) else ():
        if isinstance(pic, dict) and isinstance(pic.get("src"), str):
            urls.append(pic["src"])
    opus_pics = (major.get("opus") or {}).get("pics") or []
    for pic in opus_pics if isinstance(opus_pics, list) else ():
        if isinstance(pic, dict) and isinstance(pic.get("url"), str):
            urls.append(pic["url"])
    normalized: list[str] = []
    for raw in urls:
        clean = comment_image_url(raw)
        if clean and clean not in normalized:
            normalized.append(clean)
    return normalized


class MemeHarvester:
    """Automated multi-channel meme harvester, two-stage screener, and inbox manager."""

    def __init__(
        self,
        settings,
        store,
        *,
        catalog: MemeCatalog,
        cache=None,
        runtime=None,
        visual_provider=None,
        browsing=None,
        http_client_factory=None,
    ):
        self.settings = settings
        self.store = store
        self.catalog = catalog
        self.cache = cache
        self.runtime = runtime
        self._visual_provider = visual_provider
        self._browsing = browsing
        self._http_client_factory = http_client_factory
        self.inbox_dir = settings.runtime / "meme_inbox"
        self._safe_inbox_dir()
        self._inbox_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._closed = False
        self._init_tables()

    def _init_tables(self) -> None:
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meme_inbox(
                item_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                dhash TEXT NOT NULL,
                blob_name TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                image_info TEXT NOT NULL,
                title TEXT NOT NULL,
                emotions TEXT NOT NULL,
                tags TEXT NOT NULL,
                description TEXT NOT NULL,
                usage_scope TEXT NOT NULL DEFAULT 'all_channels',
                confidence REAL NOT NULL DEFAULT 0.0,
                ai_tagged INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL,
                license TEXT NOT NULL,
                subscription_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS meme_inbox_status_idx ON meme_inbox(status, created_at);
            CREATE INDEX IF NOT EXISTS meme_inbox_sha256_idx ON meme_inbox(sha256);

            CREATE TABLE IF NOT EXISTS meme_discard_cache(
                sha256 TEXT PRIMARY KEY,
                dhash TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meme_subscriptions(
                source_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_approve INTEGER NOT NULL DEFAULT 0,
                sync_interval_hours INTEGER NOT NULL DEFAULT 12,
                license_name TEXT NOT NULL DEFAULT '',
                license_url TEXT NOT NULL DEFAULT '',
                license_note TEXT NOT NULL DEFAULT '',
                last_etag TEXT NOT NULL DEFAULT '',
                last_cursor TEXT NOT NULL DEFAULT '',
                last_synced_at REAL,
                last_status TEXT NOT NULL DEFAULT 'idle',
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meme_harvest_queue(
                queue_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                image_url TEXT NOT NULL,
                cached_blob TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                license TEXT NOT NULL,
                subscription_id TEXT NOT NULL DEFAULT '',
                auto_approve INTEGER NOT NULL DEFAULT 0,
                context_hint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )

    def _safe_inbox_dir(self) -> None:
        if linked(self.inbox_dir):
            raise ValueError("表情收件箱目录不能使用链接")
        self.inbox_dir.mkdir(exist_ok=True)
        if not self.inbox_dir.resolve().is_relative_to(self.settings.runtime.resolve()):
            raise ValueError("表情收件箱目录路径越界")

    @property
    def visual_provider(self):
        if self._visual_provider is not None:
            return self._visual_provider
        if self.runtime is not None:
            vp = getattr(self.runtime, "visual_provider", None)
            if vp is not None:
                return vp
            browsing = self.browsing
            if browsing is not None:
                return getattr(browsing, "visual_provider", None)
        return None

    @property
    def browsing(self):
        if self._browsing is not None:
            return self._browsing
        if self.runtime is not None:
            return getattr(self.runtime, "browsing", None)
        return None

    def get_config(self) -> dict:
        stored = self.store.get(HARVESTER_CONFIG_KEY)
        cfg = dict(DEFAULT_HARVESTER_CONFIG)
        if isinstance(stored, dict):
            cfg["comment_harvest_enabled"] = bool(stored.get("comment_harvest_enabled", True))
            hours = stored.get("default_sync_interval_hours")
            if type(hours) is int and 1 <= hours <= 168:
                cfg["default_sync_interval_hours"] = hours
        return cfg

    def full_config(self) -> dict:
        cfg = self.get_config()
        if self.cache is not None:
            policy = self.cache.policy()
            cfg["meme_inbox_limit_mib"] = policy.get("meme_inbox_limit_mib", 512)
            cfg["meme_inbox_max_items"] = policy.get("meme_inbox_max_items", 200)
        return cfg

    async def update_config(self, patch: dict) -> dict:
        data = HarvesterConfigUpdate.model_validate(patch)
        cfg = self.get_config()
        if data.comment_harvest_enabled is not None:
            cfg["comment_harvest_enabled"] = data.comment_harvest_enabled
        if data.default_sync_interval_hours is not None:
            cfg["default_sync_interval_hours"] = data.default_sync_interval_hours
        self.store.set(HARVESTER_CONFIG_KEY, cfg)
        cache_patch = {}
        if data.meme_inbox_limit_mib is not None:
            cache_patch["meme_inbox_limit_mib"] = data.meme_inbox_limit_mib
        if data.meme_inbox_max_items is not None:
            cache_patch["meme_inbox_max_items"] = data.meme_inbox_max_items
        if cache_patch and self.cache is not None:
            await self.cache.configure(cache_patch)
        return self.full_config()

    def _is_known_duplicate_or_discarded(self, sha256: str, dhash: str = "") -> str | None:
        if self.catalog.find_duplicate(sha256, dhash):
            return "duplicate_catalog"
        if self.catalog.is_rejected(sha256, dhash):
            return "rejected_blacklist"
        for row in self.store.all("SELECT sha256, dhash FROM meme_inbox WHERE status='pending'"):
            if row["sha256"] == sha256 or (dhash and row["dhash"] and dhash_matches(dhash, row["dhash"])):
                return "duplicate_inbox"
        now = time.time()
        self.store.execute("DELETE FROM meme_discard_cache WHERE expires_at<=?", (now,))
        for row in self.store.all("SELECT sha256, dhash FROM meme_discard_cache WHERE expires_at>?", (now,)):
            if row["sha256"] == sha256 or (dhash and row["dhash"] and dhash_matches(dhash, row["dhash"])):
                return "discard_cache"
        return None

    async def screen_stage1(self, content: bytes, *, mime_type: str | None = None) -> dict:
        """Stage 1 screening: magic bytes, native decode, bounds, dHash, and dual dedup."""
        try:
            info = await self.catalog.inspector.inspect(content, mime=mime_type)
        except ValueError as err:
            return {"accepted": False, "reason": "invalid_image", "detail": str(err)}
        w, h = info["width"], info["height"]
        if min(w, h) < 64 or max(w, h) < 96 or max(w / h, h / w) > 3.2:
            return {
                "accepted": False,
                "reason": "dimension_filtered",
                "detail": f"图片尺寸或比例不符合表情要求 ({w}x{h})",
            }
        sha256 = hashlib.sha256(content).hexdigest()
        dhash = compute_dhash(content)
        dup_reason = self._is_known_duplicate_or_discarded(sha256, dhash)
        if dup_reason is not None:
            return {"accepted": False, "reason": dup_reason, "sha256": sha256, "dhash": dhash}
        ext = info.pop("extension")
        return {
            "accepted": True,
            "sha256": sha256,
            "dhash": dhash,
            "extension": ext,
            "image_info": info,
            "bytes": len(content),
        }

    def _write_inbox_blob(self, sha256: str, ext: str, content: bytes) -> str:
        self._safe_inbox_dir()
        blob_name = f"{sha256}.{ext}"
        if not INBOX_BLOB_RE.fullmatch(blob_name):
            raise ValueError("收件箱文件名无效")
        target = self.inbox_dir / blob_name
        if linked(target):
            raise ValueError("收件箱图片不能使用链接")
        if target.exists():
            existing = target.read_bytes()
            if hashlib.sha256(existing).hexdigest() != sha256:
                raise ValueError("已有同名收件箱图片完整性异常")
        else:
            tmp = self.inbox_dir / (".tmp-" + uuid4().hex)
            try:
                with tmp.open("xb") as handle:
                    handle.write(content)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
        if self.cache is not None:
            self.cache.touch(target)
        return blob_name

    def _write_pending_blob(self, queue_id: str, content: bytes) -> str:
        self._safe_inbox_dir()
        name = f".pending-{queue_id}"
        if not PENDING_BLOB_RE.fullmatch(name):
            raise ValueError("待处理缓存文件名无效")
        if len(content) > MAX_BYTES:
            raise ValueError("待处理图片超过大小上限")
        target = self.inbox_dir / name
        if linked(target):
            raise ValueError("待处理缓存不能使用链接")
        tmp = self.inbox_dir / (".tmp-" + uuid4().hex)
        try:
            with tmp.open("xb") as handle:
                handle.write(content)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        return name

    def _remove_pending_blob(self, name: str) -> None:
        if not name or not PENDING_BLOB_RE.fullmatch(name):
            return
        path = self.inbox_dir / name
        if not linked(path):
            path.unlink(missing_ok=True)

    def _insert_inbox_item(
        self,
        *,
        sha256: str,
        dhash: str,
        blob_name: str,
        size: int,
        image_info: dict,
        title: str,
        emotions: list[str],
        tags: list[str],
        description: str,
        usage_scope: str,
        confidence: float,
        ai_tagged: int,
        source: dict,
        license: dict,
        subscription_id: str,
    ) -> dict:
        item_id = "inbox_" + uuid4().hex[:16]
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO meme_inbox("
                "item_id, sha256, dhash, blob_name, bytes, image_info, title, emotions, tags, "
                "description, usage_scope, confidence, ai_tagged, source, license, subscription_id, status, created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    sha256,
                    dhash,
                    blob_name,
                    size,
                    json.dumps(image_info, ensure_ascii=False),
                    title,
                    json.dumps(emotions, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    description,
                    usage_scope,
                    float(confidence),
                    int(ai_tagged),
                    json.dumps(source, ensure_ascii=False),
                    json.dumps(license, ensure_ascii=False),
                    subscription_id,
                    "pending",
                    now,
                ),
            )
        if self.cache is not None:
            try:
                self.cache.enforce("meme_inbox")
            except ValueError:
                pass
        row = self.store.one("SELECT * FROM meme_inbox WHERE item_id=?", (item_id,))
        return self._format_inbox_row(row) if row else {"item_id": item_id, "evicted": True}

    def _upsert_discard_cache(self, sha256: str, dhash: str, reason: str) -> None:
        expires_at = time.time() + DISCARD_TTL_SECONDS
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO meme_discard_cache(sha256, dhash, reason, expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(sha256) DO UPDATE SET dhash=excluded.dhash, reason=excluded.reason, expires_at=excluded.expires_at",
                (sha256, dhash, str(reason)[:200], expires_at),
            )

    def _queue_pending_vision(
        self,
        *,
        queue_id: str | None,
        channel: str,
        image_url: str,
        content: bytes,
        source: dict,
        license: dict,
        subscription_id: str,
        auto_approve: bool,
        context_hint: str,
        error: str,
    ) -> str:
        now = time.time()
        qid = queue_id or ("hq_" + uuid4().hex[:16])
        blob_name = self._write_pending_blob(qid, content)
        existing = self.store.one("SELECT queue_id, attempts FROM meme_harvest_queue WHERE queue_id=?", (qid,))
        if existing:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE meme_harvest_queue SET cached_blob=?, status='pending_vision', attempts=?, "
                    "last_error=?, updated_at=? WHERE queue_id=?",
                    (blob_name, int(existing["attempts"]) + 1, str(error)[:400], now, qid),
                )
        else:
            self._insert_queue_row(
                queue_id=qid,
                channel=channel,
                image_url=image_url,
                cached_blob=blob_name,
                source=source,
                license=license,
                subscription_id=subscription_id,
                auto_approve=auto_approve,
                context_hint=context_hint,
                status="pending_vision",
                attempts=1,
                last_error=str(error)[:400],
            )
        return qid

    async def screen_stage2(
        self,
        content: bytes,
        stage1: dict,
        *,
        channel: str,
        source: dict,
        license: dict,
        subscription_id: str = "",
        auto_approve: bool = False,
        context_hint: str = "",
        usage_scope: Literal["all_channels", "interaction_only"] = "all_channels",
        filename_hint: str = "",
        manifest_entry: dict | None = None,
        queue_id: str | None = None,
        image_url: str = "",
        character_id: str = "whale",
    ) -> dict:
        """Stage 2 screening: optional manifest fast-path or vision classification + inbox/catalog routing."""
        source_valid = MemeSource.model_validate(source).model_dump()
        license_valid = MemeLicense.model_validate(license).model_dump()

        if channel == "remote_feed" and auto_approve and isinstance(manifest_entry, dict):
            raw_title = manifest_entry.get("title")
            raw_emotions = manifest_entry.get("emotions")
            if (
                isinstance(raw_title, str)
                and raw_title.strip()
                and isinstance(raw_emotions, list)
                and raw_emotions
                and all(e in _VALID_EMOTIONS for e in raw_emotions)
            ):
                labels = _normalize_labels(manifest_entry.get("tags", []), manifest_entry.get("description", ""))
                asset = await self.catalog.register(
                    character_id,
                    content,
                    title=_clean_title(raw_title),
                    emotions=list(dict.fromkeys(raw_emotions)),
                    source=source_valid,
                    license=license_valid,
                    tags=labels.tags,
                    description=labels.description,
                    usage_scope=usage_scope,
                    dhash=stage1["dhash"],
                )
                return {"outcome": "registered", "asset": asset}

        provider = self.visual_provider
        vision_result = None
        vision_error = None
        if provider is None or not callable(getattr(provider, "classify_meme_image", None)):
            vision_error = "视觉识别服务未就绪"
        else:
            caps = provider.capabilities() if callable(getattr(provider, "capabilities", None)) else {}
            try:
                frames = await asyncio.to_thread(
                    _sample_frames_bounded,
                    content,
                    max_frames=int((caps or {}).get("max_frames") or 3),
                    max_pixels=int((caps or {}).get("max_pixels") or 1024 * 1024),
                    max_bytes=int((caps or {}).get("max_bytes") or 1024 * 1024),
                )
            except Exception as err:
                frames = None
                vision_error = str(err)
            if frames is not None:
                try:
                    has_ticket = operation_ticket.get() is not None
                    slot = (
                        self.runtime.offline_generation("background")
                        if (
                            not has_ticket
                            and self.runtime is not None
                            and callable(getattr(self.runtime, "offline_generation", None))
                        )
                        else nullcontext()
                    )
                    async with slot:
                        vision_result = await provider.classify_meme_image(frames, context_hint=context_hint)
                except Exception as err:
                    vision_error = str(err)

        if vision_result is None:
            if channel == "remote_feed" and auto_approve:
                title = _filename_title(filename_hint or image_url)
                async with self._inbox_lock:
                    blob_name = self._write_inbox_blob(stage1["sha256"], stage1["extension"], content)
                    item = self._insert_inbox_item(
                        sha256=stage1["sha256"],
                        dhash=stage1["dhash"],
                        blob_name=blob_name,
                        size=stage1["bytes"],
                        image_info=stage1["image_info"],
                        title=title,
                        emotions=["idle"],
                        tags=[],
                        description="",
                        usage_scope=usage_scope,
                        confidence=0.0,
                        ai_tagged=0,
                        source=source_valid,
                        license=license_valid,
                        subscription_id=subscription_id,
                    )
                return {"outcome": "inbox_fallback", "item": item, "error": vision_error}

            qid = self._queue_pending_vision(
                queue_id=queue_id,
                channel=channel,
                image_url=image_url,
                content=content,
                source=source_valid,
                license=license_valid,
                subscription_id=subscription_id,
                auto_approve=auto_approve,
                context_hint=context_hint,
                error=vision_error or "视觉分类失败",
            )
            return {"outcome": "pending_vision", "queue_id": qid, "error": vision_error}

        is_whale = bool(vision_result.get("is_whale_aqua"))
        clean_ok = bool(vision_result.get("clean_ok"))
        try:
            confidence = float(vision_result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        if not (is_whale and clean_ok and confidence >= VISION_PASS_CONFIDENCE):
            reason = f"vision_rejected:whale={is_whale},clean={clean_ok},conf={confidence:.2f}"
            self._upsert_discard_cache(stage1["sha256"], stage1["dhash"], reason)
            return {"outcome": "discarded", "reason": reason}

        title = _clean_title(vision_result.get("title") or _filename_title(filename_hint or image_url))
        emotions = _normalize_emotions(vision_result.get("emotions"))
        labels = _normalize_labels(vision_result.get("tags", []), vision_result.get("description", ""))

        if channel == "remote_feed" and auto_approve:
            asset = await self.catalog.register(
                character_id,
                content,
                title=title,
                emotions=emotions,
                source=source_valid,
                license=license_valid,
                tags=labels.tags,
                description=labels.description,
                usage_scope=usage_scope,
                dhash=stage1["dhash"],
            )
            return {"outcome": "registered", "asset": asset}

        async with self._inbox_lock:
            blob_name = self._write_inbox_blob(stage1["sha256"], stage1["extension"], content)
            item = self._insert_inbox_item(
                sha256=stage1["sha256"],
                dhash=stage1["dhash"],
                blob_name=blob_name,
                size=stage1["bytes"],
                image_info=stage1["image_info"],
                title=title,
                emotions=emotions,
                tags=labels.tags,
                description=labels.description,
                usage_scope=usage_scope,
                confidence=confidence,
                ai_tagged=1,
                source=source_valid,
                license=license_valid,
                subscription_id=subscription_id,
            )
        return {"outcome": "inbox", "item": item}

    async def process_candidate_bytes(
        self,
        content: bytes,
        *,
        channel: str,
        source: dict,
        license: dict,
        subscription_id: str = "",
        auto_approve: bool = False,
        context_hint: str = "",
        usage_scope: Literal["all_channels", "interaction_only"] = "all_channels",
        filename_hint: str = "",
        manifest_entry: dict | None = None,
        queue_id: str | None = None,
        image_url: str = "",
    ) -> dict:
        stage1 = await self.screen_stage1(content)
        if not stage1["accepted"]:
            return {"outcome": "filtered", **stage1}
        return await self.screen_stage2(
            content,
            stage1,
            channel=channel,
            source=source,
            license=license,
            subscription_id=subscription_id,
            auto_approve=auto_approve,
            context_hint=context_hint,
            usage_scope=usage_scope,
            filename_hint=filename_hint,
            manifest_entry=manifest_entry,
            queue_id=queue_id,
            image_url=image_url,
        )

    async def _safe_fetch_bytes(
        self,
        raw_url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_BYTES,
    ) -> dict:
        current = raw_url
        extra_headers = dict(headers or {})
        client_cm = (
            self._http_client_factory()
            if self._http_client_factory is not None
            else httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=10),
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        )
        async with client_cm as client:
            for _ in range(MAX_REDIRECTS + 1):
                parsed = parse_public_https_url(current)
                host, address = await public_https_address(parsed)
                target = parsed.copy_with(host=address)
                req_headers = {
                    "Host": f"[{host}]" if ":" in host else host,
                    "User-Agent": "TuberStudio-MemeHarvester/1.0",
                    **extra_headers,
                }
                async with client.stream(
                    "GET",
                    target,
                    headers=req_headers,
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code == 304:
                        return {
                            "status_code": 304,
                            "etag": response.headers.get("etag", ""),
                            "content": b"",
                            "url": str(parsed),
                        }
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("重定向缺少目标地址")
                        current = str(parsed.join(location))
                        extra_headers.pop("If-None-Match", None)
                        continue
                    response.raise_for_status()
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(buf) + len(chunk) > max_bytes:
                            raise ValueError("远程文件超出大小上限")
                        buf.extend(chunk)
                    return {
                        "status_code": response.status_code,
                        "etag": response.headers.get("etag", ""),
                        "content": bytes(buf),
                        "url": str(parsed),
                    }
        raise ValueError("远程重定向次数超过限制")

    async def sync_remote_feed(self, sub: dict) -> dict:
        """Channel A: sync a remote manifest.json or GitHub directory with ETag/SHA incremental skips."""
        url = sub["url"]
        target_kind = sub["target_kind"]
        auto_approve = bool(sub["auto_approve"])
        sub_id = sub["source_id"]

        if target_kind == "github_dir":
            fetch_url = _github_contents_api_url(url)
            if fetch_url is None:
                raise ValueError("GitHub 目录链接须为 github.com/{owner}/{repo}/tree/{ref}/{path} 或 Contents API")
            accept = "application/vnd.github+json"
        else:
            fetch_url = url
            accept = "application/json"

        headers = {"Accept": accept}
        if sub.get("last_etag"):
            headers["If-None-Match"] = sub["last_etag"]

        res = await self._safe_fetch_bytes(fetch_url, headers=headers, max_bytes=MAX_FEED_JSON_BYTES)
        now = time.time()
        if res["status_code"] == 304:
            self._update_sync_state(
                sub_id, last_synced_at=now, last_status="not_modified", last_error="", last_etag=res["etag"] or sub.get("last_etag", "")
            )
            return {"source_id": sub_id, "status": "not_modified", "processed": 0, "accepted": 0, "skipped": 0}

        try:
            payload = json.loads(res["content"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("远程订阅源返回的 JSON 无效") from None

        etag = res["etag"] or sub.get("last_etag", "")
        base_url = httpx.URL(res["url"])
        items_to_fetch: list[dict] = []
        new_cursor = sub.get("last_cursor", "")

        if target_kind == "github_dir":
            if not isinstance(payload, list):
                raise ValueError("GitHub 目录 API 应返回文件列表")
            previous_shas = {}
            if sub.get("last_cursor"):
                try:
                    previous_shas = json.loads(sub["last_cursor"])
                except ValueError:
                    previous_shas = {}
            current_shas = {}
            for entry in payload:
                if not isinstance(entry, dict) or entry.get("type") != "file":
                    continue
                name = str(entry.get("name") or "")
                if Path(name).suffix.lower() not in IMAGE_EXTS:
                    continue
                size = entry.get("size")
                if type(size) is int and size > MAX_BYTES:
                    continue
                download_url = entry.get("download_url")
                if not isinstance(download_url, str) or not download_url:
                    continue
                blob_sha = str(entry.get("sha") or "")
                current_shas[name] = blob_sha
                if blob_sha and previous_shas.get(name) == blob_sha:
                    continue
                items_to_fetch.append(
                    {
                        "image_url": download_url,
                        "filename": name,
                        "source": {
                            "kind": "remote_feed",
                            "author": sub["name"],
                            "url": str(entry.get("html_url") or url),
                            "revision": blob_sha[:40],
                            "note": f"GitHub 目录同步：{name}",
                        },
                        "license": {
                            "name": sub.get("license_name") or f"{sub['name']} (远程订阅)",
                            "url": sub.get("license_url") or url,
                            "note": sub.get("license_note") or "",
                        },
                        "manifest_entry": None,
                        "context_hint": name,
                    }
                )
            new_cursor = json.dumps(current_shas, sort_keys=True)
            if not items_to_fetch and current_shas == previous_shas and previous_shas:
                self._update_sync_state(
                    sub_id,
                    last_synced_at=now,
                    last_status="not_modified",
                    last_error="",
                    last_etag=etag,
                    last_cursor=new_cursor,
                )
                return {"source_id": sub_id, "status": "not_modified", "processed": 0, "accepted": 0, "skipped": 0}
        else:
            manifest_digest = hashlib.sha256(res["content"]).hexdigest()
            if sub.get("last_cursor") == manifest_digest:
                self._update_sync_state(
                    sub_id, last_synced_at=now, last_status="not_modified", last_error="", last_etag=etag
                )
                return {"source_id": sub_id, "status": "not_modified", "processed": 0, "accepted": 0, "skipped": 0}
            if isinstance(payload, list):
                entries, credits = payload, {}
            elif isinstance(payload, dict) and isinstance(payload.get("entries"), list):
                entries, credits = payload["entries"], payload.get("sources") or {}
            else:
                raise ValueError("manifest.json 格式须为条目数组或包含 entries 的对象")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw_img = entry.get("url") or entry.get("download_url") or entry.get("path")
                if not isinstance(raw_img, str) or not raw_img.strip():
                    continue
                resolved_url = str(base_url.join(raw_img.strip()))
                declared_sha = str(entry.get("sha256") or "")
                if declared_sha and self._is_known_duplicate_or_discarded(declared_sha):
                    continue
                credit = credits.get(entry.get("credit"), {}) if isinstance(credits, dict) else {}
                author = str(credit.get("author") or entry.get("author") or sub["name"] or "remote_feed")
                lic = credit.get("license") if isinstance(credit.get("license"), dict) else entry.get("license")
                if not isinstance(lic, dict) or not lic.get("name"):
                    lic = {
                        "name": sub.get("license_name") or f"{sub['name']} (远程订阅)",
                        "url": sub.get("license_url") or url,
                        "note": sub.get("license_note") or "",
                    }
                src_url = str(entry.get("source") or resolved_url)
                if not src_url.startswith(("http://", "https://")):
                    src_url = resolved_url
                items_to_fetch.append(
                    {
                        "image_url": resolved_url,
                        "filename": str(entry.get("path") or Path(urlsplit(resolved_url).path).name),
                        "source": {
                            "kind": "remote_feed",
                            "author": author[:300],
                            "url": src_url[:2048],
                            "revision": str(credit.get("revision") or entry.get("revision") or "")[:200],
                            "note": str(entry.get("note") or "")[:3000],
                        },
                        "license": {
                            "name": str(lic["name"])[:300],
                            "url": str(lic.get("url") or "")[:2048],
                            "note": str(lic.get("note") or "")[:5000],
                        },
                        "manifest_entry": entry,
                        "context_hint": str(entry.get("title") or entry.get("description") or entry.get("path") or ""),
                    }
                )
            new_cursor = manifest_digest

        processed = accepted = skipped = 0
        for item in items_to_fetch:
            processed += 1
            try:
                img_res = await self._safe_fetch_bytes(item["image_url"], max_bytes=MAX_BYTES)
                outcome = await self.process_candidate_bytes(
                    img_res["content"],
                    channel="remote_feed",
                    source=item["source"],
                    license=item["license"],
                    subscription_id=sub_id,
                    auto_approve=auto_approve,
                    context_hint=item["context_hint"],
                    usage_scope="all_channels",
                    filename_hint=item["filename"],
                    manifest_entry=item["manifest_entry"],
                    image_url=item["image_url"],
                )
                if outcome["outcome"] in {"registered", "inbox", "inbox_fallback"}:
                    accepted += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1

        self._update_sync_state(
            sub_id,
            last_synced_at=time.time(),
            last_status="ok",
            last_error="",
            last_etag=etag,
            last_cursor=new_cursor,
        )
        return {
            "source_id": sub_id,
            "status": "ok",
            "processed": processed,
            "accepted": accepted,
            "skipped": skipped,
        }

    def enqueue_comment_attachments(
        self,
        items: list[dict],
        *,
        bvid: str | None = None,
        dynamic_id: str | None = None,
        image_bytes_by_url: dict[str, bytes] | None = None,
    ) -> int:
        """Channel B: passively enqueue comment image attachments (zero extra comment paging)."""
        if not self.get_config()["comment_harvest_enabled"]:
            return 0
        if not isinstance(items, list) or not items:
            return 0

        queued = 0
        bytes_map = image_bytes_by_url or {}
        for item in items:
            if not isinstance(item, dict):
                continue
            attachments = item.get("attachments") or []
            if not isinstance(attachments, list) or not attachments:
                continue
            text = str(item.get("content") or item.get("text") or "")
            if not passes_comment_coarse_filter(text, attachments):
                continue
            comment_id = str(item.get("comment_id") or "")
            author = str(item.get("display_name") or item.get("author_name") or item.get("username") or "").strip()
            author_id = str(item.get("platform_user_id") or item.get("author_uid") or item.get("user_id") or "").strip()
            if bvid:
                src_url = f"https://www.bilibili.com/video/{bvid}" + (f"#reply{comment_id}" if comment_id else "")
            elif dynamic_id:
                src_url = f"https://t.bilibili.com/{dynamic_id}"
            else:
                src_url = "https://www.bilibili.com"

            source = {
                "kind": "bilibili_comment",
                "author": (author or (f"UID {author_id}" if author_id else "B站评论用户"))[:300],
                "author_id": author_id[:120],
                "url": src_url[:2048],
                "note": f"B站评论 {comment_id}" + (f" ({bvid})" if bvid else ""),
            }
            license_info = {
                "name": "B站评论区公开配图（仅限互动使用，待号主确认授权）",
                "url": src_url[:2048],
            }
            for att in attachments:
                if not isinstance(att, dict) or att.get("kind") != "image":
                    continue
                img_url = comment_image_url(att.get("url"))
                if not img_url:
                    continue
                raw_bytes = bytes_map.get(img_url) or bytes_map.get(str(att.get("url") or ""))
                if raw_bytes is not None:
                    if len(raw_bytes) > MAX_BYTES:
                        continue
                    sha = hashlib.sha256(raw_bytes).hexdigest()
                    if self._is_known_duplicate_or_discarded(sha):
                        continue
                existing = self.store.one(
                    "SELECT queue_id, cached_blob FROM meme_harvest_queue WHERE image_url=? AND status IN ('queued','pending_vision')",
                    (img_url,),
                )
                if existing:
                    if raw_bytes is not None and not existing["cached_blob"]:
                        try:
                            blob = self._write_pending_blob(existing["queue_id"], raw_bytes)
                            with self.store.db:
                                self.store.db.execute(
                                    "UPDATE meme_harvest_queue SET cached_blob=?, updated_at=? WHERE queue_id=?",
                                    (blob, time.time(), existing["queue_id"]),
                                )
                        except ValueError:
                            pass
                    continue
                qid = "hq_" + uuid4().hex[:16]
                cached_blob = ""
                if raw_bytes is not None:
                    try:
                        cached_blob = self._write_pending_blob(qid, raw_bytes)
                    except ValueError:
                        continue
                self._insert_queue_row(
                    queue_id=qid,
                    channel="bilibili_comment",
                    image_url=img_url,
                    cached_blob=cached_blob,
                    source=source,
                    license=license_info,
                    subscription_id="",
                    auto_approve=False,
                    context_hint=f"{bvid or ''} {text[:120]}".strip(),
                    status="queued",
                )
                queued += 1
        return queued

    def _insert_queue_row(
        self,
        *,
        queue_id: str,
        channel: str,
        image_url: str,
        cached_blob: str,
        source: dict,
        license: dict,
        subscription_id: str,
        auto_approve: bool,
        context_hint: str,
        status: str,
        attempts: int = 0,
        last_error: str = "",
    ) -> None:
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO meme_harvest_queue("
                "queue_id, channel, image_url, cached_blob, source, license, subscription_id, "
                "auto_approve, context_hint, status, attempts, last_error, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    queue_id,
                    channel,
                    image_url,
                    cached_blob,
                    json.dumps(source, ensure_ascii=False),
                    json.dumps(license, ensure_ascii=False),
                    subscription_id,
                    int(bool(auto_approve)),
                    context_hint[:400],
                    status,
                    attempts,
                    last_error[:400],
                    now,
                    now,
                ),
            )
            count = self.store.one("SELECT COUNT(*) AS n FROM meme_harvest_queue")["n"]
            if count > MAX_QUEUE_ROWS:
                excess = count - MAX_QUEUE_ROWS
                oldest = self.store.all(
                    "SELECT queue_id, cached_blob FROM meme_harvest_queue ORDER BY created_at ASC, rowid ASC LIMIT ?",
                    (excess,),
                )
                for row in oldest:
                    self._remove_pending_blob(row["cached_blob"])
                    self.store.db.execute("DELETE FROM meme_harvest_queue WHERE queue_id=?", (row["queue_id"],))

    async def _fetch_bilibili_image(self, img_url: str) -> bytes:
        clean = comment_image_url(img_url)
        if clean is None:
            raise ValueError("评论/二创图片来源不属于支持的 B 站 CDN")
        browsing = self.browsing
        adapter = getattr(browsing, "adapter", None) if browsing is not None else None
        if browsing is not None and adapter is not None and callable(getattr(adapter, "read_comment_image", None)):
            return await browsing._read(adapter.read_comment_image, clean)
        res = await self._safe_fetch_bytes(clean, headers={"Referer": "https://www.bilibili.com/"}, max_bytes=MAX_BYTES)
        return res["content"]

    async def sync_bilibili_creator(self, sub: dict) -> dict:
        """Channel C: sync Bilibili UP space dynamics, articles, or readlists via browsing._read + read_gate."""
        browsing = self.browsing
        if browsing is None or getattr(browsing, "adapter", None) is None:
            raise ValueError("B站读取服务未就绪")
        adapter = browsing.adapter
        target_kind = sub["target_kind"]
        raw_url = sub["url"]
        sub_id = sub["source_id"]
        cursor = sub.get("last_cursor") or ""
        candidates: list[dict] = []
        new_cursor = cursor

        async def _bili_get(path: str, params: dict, *, wbi: bool = False):
            signed = params
            if wbi and callable(getattr(adapter, "wbi_params", None)):
                signed = await browsing._read(adapter.wbi_params, params)
            return await browsing._read(adapter._get, path, signed)

        if target_kind == "space_dynamic":
            uid = space_uid(raw_url)
            data = await _bili_get(
                "/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid, "timezone_offset": -480},
                wbi=True,
            )
            items = data.get("items") or []
            if items and isinstance(items[0], dict) and items[0].get("id_str"):
                new_cursor = str(items[0]["id_str"])
            for item in items:
                if not isinstance(item, dict):
                    continue
                dyn_id = str(item.get("id_str") or "")
                if not dyn_id:
                    continue
                if cursor and dyn_id == cursor:
                    break
                modules = item.get("modules") or {}
                author_mod = modules.get("module_author") or {}
                author = str(author_mod.get("name") or sub["name"] or f"UID {uid}")
                author_id = str(author_mod.get("mid") or uid)
                desc_text = str(((modules.get("module_dynamic") or {}).get("desc") or {}).get("text") or "")
                src_url = f"https://t.bilibili.com/{dyn_id}"
                for img_url in _extract_dynamic_images(item):
                    candidates.append(
                        {
                            "image_url": img_url,
                            "source": {
                                "kind": "bilibili_creator",
                                "author": author[:300],
                                "author_id": author_id[:120],
                                "url": src_url,
                                "note": f"B站动态 {dyn_id}",
                            },
                            "license": {
                                "name": sub.get("license_name") or "B站创作者公开动态（待号主确认授权）",
                                "url": sub.get("license_url") or src_url,
                                "note": sub.get("license_note") or "",
                            },
                            "context_hint": desc_text[:200] or sub["name"],
                        }
                    )
        elif target_kind == "article":
            cv_match = CV_RE.fullmatch(raw_url.strip())
            if cv_match:
                cv_id = cv_match.group(1)
                if cursor != f"cv:{cv_id}":
                    article = await _bili_get("/x/article/view", {"id": cv_id})
                    author_info = article.get("author") or {}
                    author = str(author_info.get("name") or sub["name"] or "B站专栏作者")
                    author_id = str(author_info.get("mid") or "")
                    src_url = f"https://www.bilibili.com/read/cv{cv_id}"
                    title = str(article.get("title") or sub["name"])
                    for img_url in _extract_article_images(article):
                        candidates.append(
                            {
                                "image_url": img_url,
                                "source": {
                                    "kind": "bilibili_creator",
                                    "author": author[:300],
                                    "author_id": author_id[:120],
                                    "url": src_url,
                                    "note": f"B站专栏 cv{cv_id} · {title[:80]}",
                                },
                                "license": {
                                    "name": sub.get("license_name") or "B站创作者公开专栏（待号主确认授权）",
                                    "url": sub.get("license_url") or src_url,
                                    "note": sub.get("license_note") or "",
                                },
                                "context_hint": title[:200],
                            }
                        )
                    new_cursor = f"cv:{cv_id}"
            else:
                uid = space_uid(raw_url)
                data = await _bili_get("/x/space/article", {"mid": uid, "pn": 1, "ps": 10, "sort": "publish_time"}, wbi=True)
                articles = data.get("articles") or []
                if articles and isinstance(articles[0], dict) and articles[0].get("id"):
                    new_cursor = str(articles[0]["id"])
                for art in articles:
                    if not isinstance(art, dict) or not art.get("id"):
                        continue
                    art_id = str(art["id"])
                    if cursor and art_id == cursor:
                        break
                    detail = await _bili_get("/x/article/view", {"id": art_id})
                    author_info = detail.get("author") or art.get("author") or {}
                    author = str(author_info.get("name") or sub["name"] or f"UID {uid}")
                    author_id = str(author_info.get("mid") or uid)
                    src_url = f"https://www.bilibili.com/read/cv{art_id}"
                    title = str(detail.get("title") or art.get("title") or sub["name"])
                    imgs = _extract_article_images(detail) or [
                        u for u in (comment_image_url(x) for x in (art.get("image_urls") or ())) if u
                    ]
                    for img_url in imgs:
                        candidates.append(
                            {
                                "image_url": img_url,
                                "source": {
                                    "kind": "bilibili_creator",
                                    "author": author[:300],
                                    "author_id": author_id[:120],
                                    "url": src_url,
                                    "note": f"B站专栏 cv{art_id} · {title[:80]}",
                                },
                                "license": {
                                    "name": sub.get("license_name") or "B站创作者公开专栏（待号主确认授权）",
                                    "url": sub.get("license_url") or src_url,
                                    "note": sub.get("license_note") or "",
                                },
                                "context_hint": title[:200],
                            }
                        )
        elif target_kind == "readlist":
            rl_match = RL_RE.fullmatch(raw_url.strip())
            if not rl_match:
                raise ValueError("文集链接须为 rl{id} 或 bilibili.com/read/readlist/rl{id}")
            rl_id = rl_match.group(1)
            data = await _bili_get("/x/article/list/web/articles", {"id": rl_id})
            articles = data.get("articles") or []
            if articles and isinstance(articles[0], dict) and articles[0].get("id"):
                new_cursor = str(articles[0]["id"])
            for art in articles:
                if not isinstance(art, dict) or not art.get("id"):
                    continue
                art_id = str(art["id"])
                if cursor and art_id == cursor:
                    break
                detail = await _bili_get("/x/article/view", {"id": art_id})
                author_info = detail.get("author") or art.get("author") or {}
                author = str(author_info.get("name") or sub["name"] or "B站文集作者")
                author_id = str(author_info.get("mid") or "")
                src_url = f"https://www.bilibili.com/read/cv{art_id}"
                title = str(detail.get("title") or art.get("title") or sub["name"])
                for img_url in _extract_article_images(detail):
                    candidates.append(
                        {
                            "image_url": img_url,
                            "source": {
                                "kind": "bilibili_creator",
                                "author": author[:300],
                                "author_id": author_id[:120],
                                "url": src_url,
                                "note": f"B站文集 rl{rl_id} · cv{art_id}",
                            },
                            "license": {
                                "name": sub.get("license_name") or "B站创作者公开文集（待号主确认授权）",
                                "url": sub.get("license_url") or src_url,
                                "note": sub.get("license_note") or "",
                            },
                            "context_hint": title[:200],
                        }
                    )
        else:
            raise ValueError(f"不支持的 B 站二创同步类型：{target_kind}")

        processed = accepted = skipped = 0
        for cand in candidates:
            processed += 1
            try:
                content = await self._fetch_bilibili_image(cand["image_url"])
                outcome = await self.process_candidate_bytes(
                    content,
                    channel="bilibili_creator",
                    source=cand["source"],
                    license=cand["license"],
                    subscription_id=sub_id,
                    auto_approve=False,
                    context_hint=cand["context_hint"],
                    usage_scope="all_channels",
                    image_url=cand["image_url"],
                )
                if outcome["outcome"] in {"inbox", "inbox_fallback", "registered"}:
                    accepted += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1

        self._update_sync_state(
            sub_id,
            last_synced_at=time.time(),
            last_status="ok" if candidates or not cursor else "not_modified",
            last_error="",
            last_cursor=new_cursor,
        )
        return {
            "source_id": sub_id,
            "status": "ok",
            "processed": processed,
            "accepted": accepted,
            "skipped": skipped,
        }

    async def drain_queue(self, *, limit: int = 20) -> dict:
        """Process queued and pending_vision items from meme_harvest_queue."""
        rows = self.store.all(
            "SELECT * FROM meme_harvest_queue WHERE status IN ('queued','pending_vision') "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (limit,),
        )
        processed = accepted = discarded = deferred = 0
        for row in rows:
            qid = row["queue_id"]
            processed += 1
            cached_blob = row["cached_blob"]
            content = None
            if cached_blob and PENDING_BLOB_RE.fullmatch(cached_blob):
                blob_path = self.inbox_dir / cached_blob
                if blob_path.is_file() and not linked(blob_path):
                    content = blob_path.read_bytes()
            if content is None:
                try:
                    if row["channel"] in {"bilibili_comment", "bilibili_creator"}:
                        content = await self._fetch_bilibili_image(row["image_url"])
                    else:
                        content = (await self._safe_fetch_bytes(row["image_url"], max_bytes=MAX_BYTES))["content"]
                except Exception as err:
                    attempts = int(row["attempts"]) + 1
                    if attempts >= 5:
                        self._remove_pending_blob(cached_blob)
                        with self.store.db:
                            self.store.db.execute("DELETE FROM meme_harvest_queue WHERE queue_id=?", (qid,))
                        discarded += 1
                    else:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE meme_harvest_queue SET attempts=?, last_error=?, updated_at=? WHERE queue_id=?",
                                (attempts, str(err)[:400], time.time(), qid),
                            )
                        deferred += 1
                    continue

            usage_scope: Literal["all_channels", "interaction_only"] = (
                "interaction_only" if row["channel"] == "bilibili_comment" else "all_channels"
            )
            outcome = await self.process_candidate_bytes(
                content,
                channel=row["channel"],
                source=json.loads(row["source"]),
                license=json.loads(row["license"]),
                subscription_id=row["subscription_id"],
                auto_approve=bool(row["auto_approve"]),
                context_hint=row["context_hint"],
                usage_scope=usage_scope,
                queue_id=qid,
                image_url=row["image_url"],
            )
            if outcome["outcome"] == "pending_vision":
                deferred += 1
                continue
            self._remove_pending_blob(cached_blob)
            with self.store.db:
                self.store.db.execute("DELETE FROM meme_harvest_queue WHERE queue_id=?", (qid,))
            if outcome["outcome"] in {"registered", "inbox", "inbox_fallback"}:
                accepted += 1
            else:
                discarded += 1
        return {
            "processed": processed,
            "accepted": accepted,
            "discarded": discarded,
            "deferred": deferred,
        }

    def _update_sync_state(
        self,
        source_id: str,
        *,
        last_synced_at: float,
        last_status: str,
        last_error: str,
        last_etag: str | None = None,
        last_cursor: str | None = None,
    ) -> None:
        sets = ["last_synced_at=?", "last_status=?", "last_error=?", "updated_at=?"]
        vals: list = [last_synced_at, last_status, last_error[:500], time.time()]
        if last_etag is not None:
            sets.append("last_etag=?")
            vals.append(last_etag)
        if last_cursor is not None:
            sets.append("last_cursor=?")
            vals.append(last_cursor)
        vals.append(source_id)
        with self.store.db:
            self.store.db.execute(f"UPDATE meme_subscriptions SET {','.join(sets)} WHERE source_id=?", tuple(vals))

    @staticmethod
    def _format_source_row(row: dict) -> dict:
        return {
            "source_id": row["source_id"],
            "kind": row["kind"],
            "target_kind": row["target_kind"],
            "name": row["name"],
            "url": row["url"],
            "enabled": bool(row["enabled"]),
            "auto_approve": bool(row["auto_approve"]),
            "sync_interval_hours": int(row["sync_interval_hours"]),
            "license_name": row["license_name"],
            "license_url": row["license_url"],
            "license_note": row["license_note"],
            "last_etag": row["last_etag"],
            "last_cursor": row["last_cursor"],
            "last_synced_at": row["last_synced_at"],
            "last_status": row["last_status"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sources(self) -> dict:
        rows = self.store.all("SELECT * FROM meme_subscriptions ORDER BY created_at DESC, source_id ASC")
        return {"items": [self._format_source_row(row) for row in rows], "config": self.full_config()}

    def add_source(self, data: dict) -> dict:
        kind = data.get("kind")
        if kind not in {"remote_feed", "bilibili_creator"}:
            raise ValueError("订阅类型须为 remote_feed 或 bilibili_creator")
        name = _clean_title(data.get("name") or "", fallback="")
        if not name:
            raise ValueError("请填写订阅名称")
        raw_url = str(data.get("url") or "").strip()
        if not raw_url:
            raise ValueError("请填写订阅地址或标识")
        default_hours = self.get_config()["default_sync_interval_hours"]
        sync_hours = data.get("sync_interval_hours")
        if sync_hours is None:
            sync_hours = default_hours
        if type(sync_hours) is not int or not 1 <= sync_hours <= 168:
            raise ValueError("同步周期须为 1 到 168 小时")
        if kind == "remote_feed":
            parse_public_https_url(raw_url)
            target_kind = data.get("target_kind") or (
                "github_dir" if _github_contents_api_url(raw_url) else "manifest"
            )
            if target_kind not in {"manifest", "github_dir"}:
                raise ValueError("远程订阅目标类型须为 manifest 或 github_dir")
            if target_kind == "github_dir" and _github_contents_api_url(raw_url) is None:
                raise ValueError("GitHub 目录链接须为 github.com/{owner}/{repo}/tree/{ref}/{path}")
            auto_approve = bool(data.get("auto_approve", False))
        else:
            target_kind = data.get("target_kind") or "space_dynamic"
            if target_kind not in {"space_dynamic", "article", "readlist"}:
                raise ValueError("B站订阅类型须为 space_dynamic、article 或 readlist")
            if target_kind == "space_dynamic":
                space_uid(raw_url)
            elif target_kind == "article":
                if not CV_RE.fullmatch(raw_url):
                    space_uid(raw_url)
            elif target_kind == "readlist":
                if not RL_RE.fullmatch(raw_url):
                    raise ValueError("文集链接须为 rl{id} 或 bilibili.com/read/readlist/rl{id}")
            auto_approve = False

        license_url = str(data.get("license_url") or "").strip()
        if license_url:
            MemeLicense.provenance_url(license_url)
        source_id = "sub_" + uuid4().hex[:16]
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO meme_subscriptions("
                "source_id, kind, target_kind, name, url, enabled, auto_approve, sync_interval_hours, "
                "license_name, license_url, license_note, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    kind,
                    target_kind,
                    name,
                    raw_url,
                    int(bool(data.get("enabled", True))),
                    int(auto_approve),
                    sync_hours,
                    str(data.get("license_name") or "")[:300],
                    license_url[:2048],
                    str(data.get("license_note") or "")[:3000],
                    now,
                    now,
                ),
            )
        row = self.store.one("SELECT * FROM meme_subscriptions WHERE source_id=?", (source_id,))
        return self._format_source_row(row)

    def update_source(self, source_id: str, patch: dict) -> dict:
        row = self.store.one("SELECT * FROM meme_subscriptions WHERE source_id=?", (source_id,))
        if row is None:
            raise LookupError("未找到该订阅源")
        updates: dict = {"updated_at": time.time()}
        if "enabled" in patch and patch["enabled"] is not None:
            updates["enabled"] = int(bool(patch["enabled"]))
        if "name" in patch and patch["name"] is not None:
            name = _clean_title(patch["name"], fallback="")
            if not name:
                raise ValueError("订阅名称不能为空")
            updates["name"] = name
        if "auto_approve" in patch and patch["auto_approve"] is not None:
            if row["kind"] != "remote_feed" and patch["auto_approve"]:
                raise ValueError("仅远程订阅源支持自动入库")
            updates["auto_approve"] = int(bool(patch["auto_approve"])) if row["kind"] == "remote_feed" else 0
        if "sync_interval_hours" in patch and patch["sync_interval_hours"] is not None:
            hours = patch["sync_interval_hours"]
            if type(hours) is not int or not 1 <= hours <= 168:
                raise ValueError("同步周期须为 1 到 168 小时")
            updates["sync_interval_hours"] = hours
        for field in ("license_name", "license_url", "license_note"):
            if field in patch and patch[field] is not None:
                val = str(patch[field]).strip()
                if field == "license_url" and val:
                    MemeLicense.provenance_url(val)
                updates[field] = val
        cols = ", ".join(f"{k}=?" for k in updates)
        with self.store.db:
            self.store.db.execute(
                f"UPDATE meme_subscriptions SET {cols} WHERE source_id=?",
                (*updates.values(), source_id),
            )
        updated = self.store.one("SELECT * FROM meme_subscriptions WHERE source_id=?", (source_id,))
        return self._format_source_row(updated)

    def delete_source(self, source_id: str) -> dict:
        row = self.store.one("SELECT source_id FROM meme_subscriptions WHERE source_id=?", (source_id,))
        if row is None:
            raise LookupError("未找到该订阅源")
        queued = self.store.all("SELECT queue_id, cached_blob FROM meme_harvest_queue WHERE subscription_id=?", (source_id,))
        for q in queued:
            self._remove_pending_blob(q["cached_blob"])
        with self.store.db:
            self.store.db.execute("DELETE FROM meme_harvest_queue WHERE subscription_id=?", (source_id,))
            self.store.db.execute("DELETE FROM meme_subscriptions WHERE source_id=?", (source_id,))
        return {"source_id": source_id, "deleted": True}

    async def sync_source(self, source_id: str) -> dict:
        row = self.store.one("SELECT * FROM meme_subscriptions WHERE source_id=?", (source_id,))
        if row is None:
            raise LookupError("未找到该订阅源")
        sub = self._format_source_row(row)
        async with self._sync_lock:
            try:
                if sub["kind"] == "remote_feed":
                    return await self.sync_remote_feed(sub)
                return await self.sync_bilibili_creator(sub)
            except Exception as err:
                self._update_sync_state(
                    source_id,
                    last_synced_at=time.time(),
                    last_status="error",
                    last_error=str(err),
                )
                return {"source_id": source_id, "status": "error", "error": str(err)}

    async def sync_all(self, *, only_due: bool = False) -> dict:
        now = time.time()
        rows = self.store.all(
            "SELECT * FROM meme_subscriptions" + (" WHERE enabled=1" if only_due else "")
            + " ORDER BY created_at ASC"
        )
        results = []
        for row in rows:
            sub = self._format_source_row(row)
            if only_due:
                last = sub["last_synced_at"]
                if last is not None and now - last < sub["sync_interval_hours"] * 3600:
                    continue
            results.append(await self.sync_source(sub["source_id"]))
        queue_result = await self.drain_queue()
        return {"sources": results, "queue": queue_result}

    @staticmethod
    def _format_inbox_row(row: dict) -> dict:
        return {
            "item_id": row["item_id"],
            "sha256": row["sha256"],
            "dhash": row["dhash"],
            "blob_name": row["blob_name"],
            "bytes": row["bytes"],
            **json.loads(row["image_info"]),
            "title": row["title"],
            "emotions": json.loads(row["emotions"]),
            "tags": json.loads(row["tags"]),
            "description": row["description"],
            "usage_scope": row["usage_scope"],
            "confidence": float(row["confidence"]),
            "ai_tagged": bool(row["ai_tagged"]),
            "source": json.loads(row["source"]),
            "license": json.loads(row["license"]),
            "subscription_id": row["subscription_id"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def list_inbox(self, *, status: str = "pending") -> dict:
        if status == "all":
            rows = self.store.all("SELECT * FROM meme_inbox ORDER BY created_at DESC, item_id ASC")
        else:
            rows = self.store.all(
                "SELECT * FROM meme_inbox WHERE status=? ORDER BY created_at DESC, item_id ASC",
                (status,),
            )
        items = [self._format_inbox_row(row) for row in rows]
        pending_count = sum(1 for item in items if item["status"] == "pending")
        pending_vision_row = self.store.one(
            "SELECT COUNT(*) AS n FROM meme_harvest_queue WHERE status='pending_vision'"
        )
        pending_vision_count = int(pending_vision_row["n"]) if pending_vision_row else 0
        cache_usage = self.cache.usage("meme_inbox") if self.cache is not None else None
        return {
            "items": items,
            "total": len(items),
            "pending_count": pending_count,
            "pending_vision_count": pending_vision_count,
            "cache": cache_usage,
        }

    def _read_inbox_blob(self, row: dict) -> tuple[Path, bytes]:
        self._safe_inbox_dir()
        blob_name = row["blob_name"]
        if not INBOX_BLOB_RE.fullmatch(blob_name):
            raise ValueError("收件箱文件名无效")
        path = self.inbox_dir / blob_name
        if linked(path) or not path.resolve().is_relative_to(self.inbox_dir.resolve()):
            raise ValueError("收件箱文件路径无效或使用了链接")
        if not path.is_file():
            raise LookupError("收件箱图片文件不存在或已被缓存回收")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("收件箱图片文件完整性校验失败")
        return path, content

    def preview_inbox_item(self, item_id: str) -> dict:
        row = self.store.one("SELECT * FROM meme_inbox WHERE item_id=?", (item_id,))
        if row is None:
            raise LookupError("未找到该待审表情")
        path, content = self._read_inbox_blob(row)
        if self.cache is not None:
            self.cache.touch(path)
        item = self._format_inbox_row(row)
        return {"item": item, "content": content, "mime_type": item["mime_type"]}

    def _unlink_inbox_blob(self, blob_name: str) -> None:
        if not blob_name or not INBOX_BLOB_RE.fullmatch(blob_name):
            return
        path = self.inbox_dir / blob_name
        if not linked(path):
            path.unlink(missing_ok=True)
        if self.cache is not None:
            try:
                rel = str(path.relative_to(self.settings.runtime))
                self.store.execute("DELETE FROM media_cache_access WHERE path=?", (rel,))
            except ValueError:
                pass

    async def approve_inbox_item(
        self,
        item_id: str,
        *,
        title: str | None = None,
        emotions: list[str] | None = None,
        tags: list[str] | None = None,
        description: str | None = None,
        usage_scope: Literal["all_channels", "interaction_only"] | None = None,
        character_id: str = "whale",
    ) -> dict:
        async with self._inbox_lock:
            row = self.store.one("SELECT * FROM meme_inbox WHERE item_id=? AND status='pending'", (item_id,))
            if row is None:
                raise LookupError("未找到待审表情或该条目已处理")
            _, content = self._read_inbox_blob(row)
            item = self._format_inbox_row(row)
            final_title = _clean_title(title if title is not None else item["title"])
            final_emotions = emotions if emotions is not None else item["emotions"]
            final_tags = tags if tags is not None else item["tags"]
            final_desc = description if description is not None else item["description"]
            final_scope = usage_scope if usage_scope is not None else item["usage_scope"]
            asset = await self.catalog.register(
                character_id,
                content,
                title=final_title,
                emotions=final_emotions,
                source=item["source"],
                license=item["license"],
                tags=final_tags,
                description=final_desc,
                usage_scope=final_scope,
                dhash=item["dhash"],
            )
            with self.store.db:
                self.store.db.execute("DELETE FROM meme_inbox WHERE item_id=?", (item_id,))
            if not self.store.one("SELECT 1 FROM meme_inbox WHERE blob_name=? AND status='pending'", (row["blob_name"],)):
                self._unlink_inbox_blob(row["blob_name"])
            return {"item_id": item_id, "approved": True, "asset": asset}

    def reject_inbox_item(self, item_id: str) -> dict:
        row = self.store.one("SELECT * FROM meme_inbox WHERE item_id=? AND status='pending'", (item_id,))
        if row is None:
            raise LookupError("未找到待审表情或该条目已处理")
        source = json.loads(row["source"])
        self.catalog.record_rejected(
            row["sha256"],
            row["dhash"],
            source_kind=source.get("kind", ""),
            source_url=source.get("url", ""),
        )
        with self.store.db:
            self.store.db.execute("DELETE FROM meme_inbox WHERE item_id=?", (item_id,))
        if not self.store.one("SELECT 1 FROM meme_inbox WHERE blob_name=? AND status='pending'", (row["blob_name"],)):
            self._unlink_inbox_blob(row["blob_name"])
        return {"item_id": item_id, "rejected": True, "sha256": row["sha256"]}

    async def batch_inbox_action(
        self,
        action: Literal["approve", "reject"],
        item_ids: list[str],
        *,
        usage_scope: Literal["all_channels", "interaction_only"] | None = None,
        character_id: str = "whale",
    ) -> dict:
        if action not in {"approve", "reject"}:
            raise ValueError("批量操作须为 approve 或 reject")
        ids = list(dict.fromkeys(str(i) for i in item_ids if str(i).strip()))[:100]
        succeeded: list[str] = []
        failed: list[dict] = []
        for item_id in ids:
            try:
                if action == "approve":
                    await self.approve_inbox_item(item_id, usage_scope=usage_scope, character_id=character_id)
                else:
                    self.reject_inbox_item(item_id)
                succeeded.append(item_id)
            except (LookupError, ValueError) as err:
                failed.append({"item_id": item_id, "error": str(err)})
        return {"action": action, "succeeded": succeeded, "failed": failed}

    def on_cache_evicted(self, removed_paths: list[Path]) -> None:
        """Delete matching meme_inbox DB rows when MediaCache evicts files from runtime/meme_inbox/."""
        blob_names = [Path(p).name for p in removed_paths if INBOX_BLOB_RE.fullmatch(Path(p).name)]
        if not blob_names:
            return
        placeholders = ",".join("?" for _ in blob_names)
        with self.store.db:
            self.store.db.execute(
                f"DELETE FROM meme_inbox WHERE blob_name IN ({placeholders})",
                tuple(blob_names),
            )

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._closed = False
            self._worker = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self._closed = True
        self._wakeup.set()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def _loop(self) -> None:
        while not self._closed:
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=SYNC_TICK_SECONDS)
            except TimeoutError:
                pass
            if self._closed:
                break
            try:
                await self.sync_all(only_due=True)
            except Exception:
                pass
