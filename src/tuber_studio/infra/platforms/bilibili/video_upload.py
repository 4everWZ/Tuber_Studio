"""Native Bilibili Web Creator Center video uploader using scoped session cookies."""

from __future__ import annotations

import base64
import math
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .client import BILIBILI_WEB_USER_AGENT, BilibiliAdapter, bilibili_web_headers

MEMBER_UPLOAD_REFERER = "https://member.bilibili.com/platform/upload/video/frame"
MEMBER_ORIGIN = "https://member.bilibili.com"
_PREUPLOAD_URL = "https://member.bilibili.com/preupload"
_COVER_UP_URL = "https://member.bilibili.com/x/vu/web/cover/up"
_ADD_V3_URL = "https://member.bilibili.com/x/vu/web/add/v3"
_DEFAULT_CHUNK_BYTES = 10 * 1024 * 1024


def _scrub(text: str, credentials: dict[str, Any]) -> str:
    cleaned = text
    for key in ("sessdata", "csrf", "refresh_token"):
        secret = str(credentials.get(key) or "").strip()
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def _cookie_header(credentials: dict[str, Any]) -> str:
    sessdata = str(credentials.get("sessdata") or "").strip()
    csrf = str(credentials.get("csrf") or "").strip()
    uid = str(credentials.get("uid") or "").strip()
    parts = [f"SESSDATA={sessdata}", f"bili_jct={csrf}"]
    if uid:
        parts.append(f"DedeUserID={uid}")
    return "; ".join(parts)


def _detect_cover_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _normalize_upos_url(endpoint: str, upos_uri: str) -> tuple[str, str]:
    raw_ep = endpoint.strip()
    if raw_ep.startswith("//"):
        raw_ep = "https:" + raw_ep
    parsed = urlsplit(raw_ep)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"无效的 B 站上传节点地址: {raw_ep}")
    rel = upos_uri.strip().removeprefix("upos://").lstrip("/")
    if not rel:
        raise RuntimeError("B 站 preupload 返回的 upos_uri 无效")
    remote_filename = Path(rel).stem
    return f"{raw_ep.rstrip('/')}/{rel}", remote_filename


async def _upload_cover(
    client: httpx.AsyncClient,
    *,
    cover_path: Path,
    csrf: str,
    member_headers: dict[str, str],
    credentials: dict[str, Any],
) -> str:
    raw = cover_path.read_bytes()
    if not raw:
        return ""
    mime = _detect_cover_mime(raw)
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    try:
        response = await client.post(
            _COVER_UP_URL,
            headers=member_headers,
            data={"cover": data_uri, "csrf": csrf},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"封面上传失败: {_scrub(str(exc), credentials)}") from None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        msg = payload.get("message") if isinstance(payload, dict) else "响应格式无效"
        raise RuntimeError(f"封面上传被拒绝: {_scrub(str(msg or payload), credentials)}")
    url = str(((payload.get("data") or {}).get("url")) or "").strip()
    if not url:
        raise RuntimeError("封面上传成功但未返回有效 URL")
    return url


async def _upos_chunked_upload(
    client: httpx.AsyncClient,
    *,
    output_path: Path,
    file_size: int,
    upos_url: str,
    auth: str,
    chunk_size: int,
    biz_id: Any,
    credentials: dict[str, Any],
) -> None:
    upos_headers = {
        "User-Agent": BILIBILI_WEB_USER_AGENT,
        "Origin": MEMBER_ORIGIN,
        "Referer": MEMBER_UPLOAD_REFERER,
        "X-Upos-Auth": auth,
    }
    try:
        init_resp = await client.post(
            upos_url,
            params={"uploads": "", "output": "json"},
            headers=upos_headers,
        )
        init_resp.raise_for_status()
        init_data = init_resp.json()
        upload_id = str(init_data.get("upload_id") or "").strip()
        if not upload_id:
            raise RuntimeError("B 站分片初始化未返回 upload_id")

        total_chunks = max(1, math.ceil(file_size / chunk_size))
        parts: list[dict[str, Any]] = []
        with output_path.open("rb") as fh:
            for idx in range(total_chunks):
                start = idx * chunk_size
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                end = start + len(chunk)
                put_resp = await client.put(
                    upos_url,
                    params={
                        "partNumber": idx + 1,
                        "uploadId": upload_id,
                        "chunk": idx,
                        "chunks": total_chunks,
                        "size": len(chunk),
                        "start": start,
                        "end": end,
                        "total": file_size,
                    },
                    headers={**upos_headers, "Content-Type": "application/octet-stream"},
                    content=chunk,
                )
                put_resp.raise_for_status()
                parts.append({"partNumber": idx + 1, "eTag": "etag"})

        fin_resp = await client.post(
            upos_url,
            params={
                "output": "json",
                "name": output_path.name,
                "profile": "ugcupos/bup",
                "uploadId": upload_id,
                "biz_id": biz_id,
            },
            headers=upos_headers,
            json={"parts": parts},
        )
        fin_resp.raise_for_status()
        fin_data = fin_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"视频分片上传失败: {_scrub(str(exc), credentials)}") from None

    if not isinstance(fin_data, dict) or fin_data.get("OK") != 1:
        raise RuntimeError(f"视频合片未成功: {_scrub(str(fin_data), credentials)}")


async def upload_bilibili_video_web(
    *,
    title: str,
    desc: str,
    tid: int,
    tags: list[str],
    output_path: Path,
    cover_path: Path | None = None,
    credentials: dict[str, Any],
    timeout: float = 600.0,
    client: httpx.AsyncClient | None = None,
    wbi_signer: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Upload MP4 and optional cover via Bilibili Web Creator Center UPOS + add/v3."""
    sessdata = str(credentials.get("sessdata") or "").strip()
    csrf = str(credentials.get("csrf") or "").strip()
    if not (sessdata and csrf):
        raise ValueError("缺少有效的 B 站登录 Cookie 凭证，无法执行投稿")
    if not output_path.is_file() or (file_size := output_path.stat().st_size) <= 0:
        raise ValueError("视频成片文件不存在或为空，无法执行投稿")

    member_headers = bilibili_web_headers(referer=MEMBER_UPLOAD_REFERER, origin=MEMBER_ORIGIN) | {
        "Cookie": _cookie_header(credentials),
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=False,
    )
    try:
        cover_url = ""
        if cover_path is not None and cover_path.is_file():
            cover_url = await _upload_cover(
                http,
                cover_path=cover_path,
                csrf=csrf,
                member_headers=member_headers,
                credentials=credentials,
            )

        try:
            pre_resp = await http.get(
                _PREUPLOAD_URL,
                params={
                    "name": output_path.name,
                    "size": file_size,
                    "r": "upos",
                    "profile": "ugcupos/bup",
                    "ssl": "0",
                    "version": "2.14.0.0",
                    "build": "2140000",
                    "os": "upos",
                    "probe_version": "20221109",
                },
                headers=member_headers,
            )
            pre_resp.raise_for_status()
            pre = pre_resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"预上传申请失败: {_scrub(str(exc), credentials)}") from None

        if not isinstance(pre, dict) or pre.get("OK") != 1:
            raise RuntimeError(f"B 站 preupload 返回异常: {_scrub(str(pre), credentials)}")

        upos_url, remote_filename = _normalize_upos_url(
            str(pre.get("endpoint") or ""),
            str(pre.get("upos_uri") or ""),
        )
        auth = str(pre.get("auth") or "").strip()
        if not auth:
            raise RuntimeError("B 站 preupload 缺少上传凭证 auth")
        chunk_size = max(1, int(pre.get("chunk_size") or _DEFAULT_CHUNK_BYTES))

        await _upos_chunked_upload(
            http,
            output_path=output_path,
            file_size=file_size,
            upos_url=upos_url,
            auth=auth,
            chunk_size=chunk_size,
            biz_id=pre.get("biz_id"),
            credentials=credentials,
        )

        sign_base = {"t": int(time.time() * 1000), "csrf": csrf}
        if wbi_signer is not None:
            signed_params = await wbi_signer(sign_base)
        else:
            adapter = BilibiliAdapter(credentials)
            try:
                signed_params = await adapter.wbi_params(sign_base)
            finally:
                await adapter.close()

        clean_title = (title or "").strip()[:80]
        clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()][:10]
        body = {
            "copyright": 1,
            "videos": [{"filename": remote_filename, "title": clean_title, "desc": ""}],
            "source": "",
            "tid": int(tid),
            "cover": cover_url,
            "title": clean_title,
            "tag": ",".join(clean_tags) or "日常",
            "desc_format_id": 0,
            "desc": (desc or "").strip()[:250],
            "dynamic": "",
            "subtitle": {"open": 0, "lan": ""},
            "open_elec": 0,
            "no_reprint": 1,
            "recreate": -1,
            "web_os": 1,
            "csrf": csrf,
        }

        try:
            submit_resp = await http.post(
                _ADD_V3_URL,
                params=signed_params,
                headers=member_headers,
                json=body,
            )
            payload = submit_resp.json()
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            raise TimeoutError(
                f"B 站投稿提交结果待核对: {_scrub(str(exc), credentials)}"
            ) from None

        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            if submit_resp.status_code >= 400:
                raise TimeoutError(f"B 站投稿 HTTP {submit_resp.status_code}，结果待核对")
            raise TimeoutError("B 站投稿响应格式无效，结果待核对")

        code = payload["code"]
        if code != 0:
            msg = str(payload.get("message") or payload.get("msg") or "投稿被拒绝")
            raise RuntimeError(f"B 站投稿被拒绝 (code={code}): {_scrub(msg, credentials)}")

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        bvid = str(data.get("bvid") or "").strip()
        aid_raw = data.get("aid")
        aid = int(aid_raw) if isinstance(aid_raw, int) or (isinstance(aid_raw, str) and aid_raw.isdigit()) else None
        if not (bvid or aid):
            raise TimeoutError("B 站投稿返回成功但缺少 bvid/aid，转为待核对状态")

        receipt: dict[str, Any] = {"platform": "bilibili", "uploader": "web_api"}
        if bvid:
            receipt["bvid"] = bvid
        if aid is not None:
            receipt["aid"] = aid
        return receipt
    finally:
        if owns_client:
            await http.aclose()
