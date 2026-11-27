"""Native Bilibili Web Creator Center video uploader using scoped session cookies."""

from __future__ import annotations

import base64
import hashlib
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .browsing import BrowsingAdapter
from .client import BILIBILI_WEB_USER_AGENT, BilibiliAdapter, PlatformReadError, bilibili_web_headers
from .urls import COMMENT_ID, VIDEO_ID

MEMBER_UPLOAD_REFERER = "https://member.bilibili.com/platform/upload/video/frame"
MEMBER_ORIGIN = "https://member.bilibili.com"
_PREUPLOAD_URL = "https://member.bilibili.com/preupload"
_COVER_UP_URL = "https://member.bilibili.com/x/vu/web/cover/up"
_ADD_V3_URL = "https://member.bilibili.com/x/vu/web/add/v3"
_PUBLIC_VIDEO_PATH = "/x/web-interface/wbi/view"
_ARCHIVE_PRE_URL = "https://member.bilibili.com/x/vupre/web/archive/pre"
_HUMAN_TYPE2_URL = "https://member.bilibili.com/x/vupre/web/archive/human/type2/list"
_DEFAULT_CHUNK_BYTES = 10 * 1024 * 1024


class VideoSubmissionRejected(RuntimeError):
    """The creator endpoint returned a definite rejection."""


def video_receipt_ids(receipt) -> dict[str, str | int]:
    """Read only verified identifiers, without coercing falsey or virtual values."""
    if not isinstance(receipt, dict):
        return {}
    bvid, aid = receipt.get("bvid"), receipt.get("aid")
    result = {"bvid": bvid} if isinstance(bvid, str) and VIDEO_ID.fullmatch(bvid) else {}
    if COMMENT_ID.fullmatch(str(aid)):
        result["aid"] = int(aid)
    return result


async def verify_bilibili_video_publication(receipt, account_id: str, *, client: httpx.AsyncClient | None = None):
    """Read the exact creator-center receipt through the anonymous video page API."""
    ids = video_receipt_ids(receipt)
    if not ids:
        return {"state": "unconfirmed", "reason": "缺少可核对的投稿编号"}
    key = "bvid" if "bvid" in ids else "aid"

    try:
        if client is None:
            adapter = BrowsingAdapter()
            try:
                params = await adapter.wbi_params({key: ids[key]}, anonymous=True)
                payload = await adapter._get_payload(_PUBLIC_VIDEO_PATH, params, anonymous=True)
            finally:
                await adapter.close()
        else:
            response = await client.get("https://api.bilibili.com" + _PUBLIC_VIDEO_PATH, params={key: ids[key]},
                                        headers=bilibili_web_headers())
            response.raise_for_status()
            payload = response.json()
    except PlatformReadError as error:
        return {"state": "check_failed", "code": error.code or error.http_status,
                "reason": "暂时无法读回 B 站视频页面"}
    except (httpx.HTTPError, ValueError):
        return {"state": "check_failed", "reason": "暂时无法读回 B 站视频页面"}
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        return {"state": "check_failed", "reason": "平台视频详情响应格式无效"}
    if payload["code"] != 0:
        return {"state": "unconfirmed", "code": payload["code"], "reason": "公开页面暂未返回此投稿"}
    row = payload.get("data")
    if not isinstance(row, dict) or not isinstance(row.get("owner"), dict):
        return {"state": "check_failed", "reason": "平台视频详情缺少作者资料"}
    found = video_receipt_ids(row)
    if (any(found.get(k) != v for k, v in ids.items())
            or str(row["owner"].get("mid", "")) != str(account_id)):
        return {"state": "mismatch", "reason": "页面投稿编号或作者与提交回执不一致"}
    url = (f"https://www.bilibili.com/video/{found['bvid']}" if "bvid" in found else
           f"https://www.bilibili.com/video/av{found['aid']}")
    return {"state": "public_visible", "url": url}

_PARENT_TO_HUMAN_TYPE2: dict[int, int] = {
    181: 1001, 11: 1001, 23: 1001, 5: 1002, 3: 1003, 129: 1004,
    1: 1005, 167: 1005, 13: 1005, 119: 1007, 4: 1008, 36: 1010, 177: 1010,
    188: 1012, 223: 1013, 155: 1014, 234: 1018, 211: 1020, 217: 1024, 160: 1030,
}
_HUMAN_TYPE2_TO_LEGACY_TID: dict[int, int] = {
    1001: 182, 1002: 241, 1003: 130, 1004: 154, 1005: 27, 1006: 162,
    1007: 22, 1008: 17, 1010: 201, 1011: 231, 1012: 95, 1013: 176,
    1014: 159, 1017: 164, 1018: 238, 1019: 161, 1020: 76, 1024: 75,
    1025: 254, 1029: 21, 1030: 21, 1031: 21,
}
_AI_TECH_MARKERS = (
    "ai", "人工智能", "本地ai", "deepseek", "llm", "大模型",
    "remotion", "python", "代码", "开源", "编程", "算法", "技术",
)


def _is_ai_tech_topic(title: str, tags: list[str]) -> bool:
    haystack = f"{title} {' '.join(tags)}".casefold()
    return any(marker in haystack for marker in _AI_TECH_MARKERS)


async def _resolve_submit_categories(
    client: httpx.AsyncClient,
    *,
    requested_tid: int,
    title: str,
    tags: list[str],
    member_headers: dict[str, str],
) -> tuple[int, int | None]:
    """Align submission partition IDs with Bilibili's active typelist and type_mode=2."""
    try:
        pre_resp = await client.get(_ARCHIVE_PRE_URL, params={"lang": "cn"}, headers=member_headers)
        pre_resp.raise_for_status()
        pre_json = pre_resp.json()
    except (httpx.HTTPError, ValueError):
        return requested_tid, None
    if not isinstance(pre_json, dict) or pre_json.get("code") != 0:
        return requested_tid, None

    data = pre_json.get("data") if isinstance(pre_json.get("data"), dict) else {}
    type_mode = int(data.get("type_mode") or 0)
    typelist = data.get("typelist") or []
    valid_children: dict[int, int] = {}
    parent_first_child: dict[int, int] = {}
    for parent in typelist:
        if not isinstance(parent, dict) or not isinstance(parent.get("id"), int):
            continue
        pid = parent["id"]
        for child in parent.get("children") or []:
            if isinstance(child, dict) and isinstance(child.get("id"), int):
                cid = child["id"]
                valid_children[cid] = pid
                parent_first_child.setdefault(pid, cid)

    valid_type2: set[int] = set()
    type2_order: list[int] = []
    if type_mode == 2:
        try:
            t2_resp = await client.get(_HUMAN_TYPE2_URL, headers=member_headers)
            t2_resp.raise_for_status()
            t2_json = t2_resp.json()
            if isinstance(t2_json, dict) and t2_json.get("code") == 0:
                for item in (t2_json.get("data") or {}).get("type_list") or []:
                    if isinstance(item, dict) and isinstance(item.get("id"), int):
                        valid_type2.add(item["id"])
                        type2_order.append(item["id"])
        except (httpx.HTTPError, ValueError):
            pass

    ai_tech = _is_ai_tech_topic(title, tags)
    human_type2: int | None = None
    if requested_tid in valid_type2:
        human_type2 = requested_tid
        mapped_tid = _HUMAN_TYPE2_TO_LEGACY_TID.get(requested_tid)
        if mapped_tid in valid_children:
            resolved_tid = mapped_tid
        elif 231 in valid_children:
            resolved_tid = 231
        else:
            resolved_tid = next(iter(valid_children), requested_tid)
        return resolved_tid, human_type2

    if requested_tid in valid_children:
        resolved_tid = requested_tid
    elif requested_tid in parent_first_child:
        resolved_tid = 231 if requested_tid == 188 and 231 in valid_children else parent_first_child[requested_tid]
    elif ai_tech and 231 in valid_children:
        resolved_tid = 231
    elif 21 in valid_children:
        resolved_tid = 21
    else:
        resolved_tid = next(iter(valid_children), requested_tid)

    if type_mode == 2 and valid_type2:
        if ai_tech and (requested_tid not in valid_children or resolved_tid in {231, 230, 95}) and 1011 in valid_type2:
            human_type2 = 1011
        elif resolved_tid == 161 and 1019 in valid_type2:
            human_type2 = 1019
        elif resolved_tid == 162 and 1006 in valid_type2:
            human_type2 = 1006
        elif (parent_id := valid_children.get(resolved_tid)) in _PARENT_TO_HUMAN_TYPE2 and _PARENT_TO_HUMAN_TYPE2[parent_id] in valid_type2:
            human_type2 = _PARENT_TO_HUMAN_TYPE2[parent_id]
        elif 1030 in valid_type2:
            human_type2 = 1030
        else:
            human_type2 = type2_order[0]
    return resolved_tid, human_type2


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
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or not host.endswith((".bilivideo.com", ".bilibili.com"))
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment):
        raise RuntimeError("B 站返回了无效的上传节点地址")
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
        raise RuntimeError(f"封面上传被拒绝: {_scrub(str(msg or '平台未返回说明'), credentials)}")
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
    expected_output_digest: str | None,
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
        digest = hashlib.sha256()
        uploaded = 0
        with output_path.open("rb") as fh:
            for idx in range(total_chunks):
                start = idx * chunk_size
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                end = start + len(chunk)
                digest.update(chunk)
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
                uploaded += len(chunk)
            if fh.read(1) or uploaded != file_size or expected_output_digest and digest.hexdigest() != expected_output_digest:
                raise RuntimeError("上传的成片与批准时的文件不同，尚未提交投稿")

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
        raise RuntimeError("B 站未确认视频合片成功")


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
    submission_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    expected_output_digest: str | None = None,
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
            raise RuntimeError("B 站未确认预上传申请")

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
            expected_output_digest=expected_output_digest,
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
        resolved_tid, human_type2 = await _resolve_submit_categories(
            http,
            requested_tid=int(tid),
            title=clean_title,
            tags=clean_tags,
            member_headers=member_headers,
        )
        body: dict[str, Any] = {
            "copyright": 1,
            "videos": [{"filename": remote_filename, "title": clean_title, "desc": ""}],
            "source": "",
            "tid": resolved_tid,
            **({"human_type2": human_type2} if human_type2 is not None else {}),
            "cover": cover_url,
            "title": clean_title,
            "tag": ",".join(clean_tags) or "日常",
            "desc_format_id": 9999,
            "desc": (desc or "").strip()[:250],
            "dynamic": "",
            "subtitle": {"open": 0, "lan": ""},
            "dolby": 0,
            "lossless_music": 0,
            "interactive": 0,
            "up_selection_reply": False,
            "up_close_reply": False,
            "up_close_danmu": False,
            "is_only_self": 0,
            "open_elec": 0,
            "no_reprint": 1,
            "recreate": -1,
            "web_os": 1,
            "csrf": csrf,
        }

        async with submission_guard() if submission_guard is not None else nullcontext():
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

        if not submit_resp.is_success:
            raise TimeoutError(f"B 站投稿 HTTP {submit_resp.status_code}，结果待核对")
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise TimeoutError("B 站投稿响应格式无效，结果待核对")

        code = payload["code"]
        if code != 0:
            msg = str(payload.get("message") or payload.get("msg") or "投稿被拒绝")
            raise VideoSubmissionRejected(f"B 站投稿被拒绝 (code={code}): {_scrub(msg, credentials)}")

        if not (ids := video_receipt_ids(payload.get("data"))):
            raise TimeoutError("B 站投稿返回成功但缺少 bvid/aid，转为待核对状态")

        return {"platform": "bilibili", "uploader": "web_api", **ids}
    finally:
        if owns_client:
            await http.aclose()
