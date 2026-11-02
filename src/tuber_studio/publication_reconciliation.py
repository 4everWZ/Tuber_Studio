"""Read-only confirmation of an already attempted platform publication.

Only a unique record with the original destination, author and payload returns
a receipt. A missing record in these bounded reads never authorizes resending.
"""

import json
import math
import re
import time

import httpx

# Published bilibili-api-python 17.4.2: user.get_dynamics_new / Dynamic.get_info.
# Signing stays in the adapter; these are ordinary client query parameters.
_DYNAMIC_CLIENT = {"x-bili-device-req-json": '{"platform":"web","device":"pc"}'}
_FEED_FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,decorationCard,commentsNewVersion,onlyfansAssetsV2,ugcDelete,onlyfansQaCard"
_DETAIL_FEATURES = (
    "itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,decorationCard,onlyfansAssetsV2,ugcDelete"
)


def _id(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("核对记录缺少有效标识")
    value = str(value)
    if zero and value == "0":
        return value
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("核对记录缺少有效标识")
    return value


def _raw_id(row, key, *, zero=False):
    value = row.get(key + "_str")
    if value in (None, ""):
        value = row.get(key)
    return _id(value, zero=zero)


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("核对记录缺少有效时间")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("核对记录缺少有效时间")
    return value


def _result(status, coverage, *, receipt=None, **details):
    value = {"status": status, "checked_at": time.time(), "coverage": coverage, **details}
    if receipt is not None:
        value["receipt"] = receipt
    return value


def _failure(error, coverage):
    details = {"error_type": type(error).__name__}
    if isinstance(error, ValueError):
        # The HTTP adapter already maps failures to credential-free descriptions.
        details["error"] = str(error)[:300]
    elif isinstance(error, httpx.HTTPStatusError):
        details["http_status"] = error.response.status_code
    return _result("check_failed", coverage, **details)


def _rows(data, key):
    if not isinstance(data, dict) or key not in data:
        raise ValueError("平台核对列表格式无效")
    rows = data[key]
    if rows is not None and not isinstance(rows, list):
        raise ValueError("平台核对列表格式无效")
    return rows or []


async def reconcile_comment(read, target, *, author_uid, text, started_at, receipt=None):
    """Check the first reply page, using raw root/parent fields, never previews."""
    coverage = {"scope": "first_page_only", "page": 1, "page_size": 20}
    try:
        oid = _id(target.get("oid"))
        kind = target.get("type", 1)
        if type(kind) is not int or kind not in (1, 17):
            raise ValueError("核对评论类型无效")
        root, parent = (_id(target.get(key), zero=True) for key in ("root", "parent"))
        if (root == "0") != (parent == "0"):
            raise ValueError("核对评论楼层不一致")
        author, cutoff = _id(author_uid), math.floor(_timestamp(started_at))
        if not isinstance(text, str) or not text:
            raise ValueError("核对缺少实际发送正文")
        expected = _id(receipt["rpid"]) if receipt and receipt.get("rpid") is not None else None
        params = {"oid": oid, "type": kind, "pn": 1, "ps": 20}
        if root != "0":
            params["root"] = root
            data = await read("/x/v2/reply/reply", params)
        else:
            params["sort"] = 0
            data = await read("/x/v2/reply", params)
        records = _rows(data, "replies")[:20]
        coverage["records"] = len(records)
        matches, known_seen = {}, False
        for row in records:
            try:
                identifier = _raw_id(row, "rpid")
                if expected and identifier != expected:
                    continue
                known_seen = known_seen or identifier == expected
                if (
                    _raw_id(row, "root", zero=True) != root
                    or _raw_id(row, "parent", zero=True) != parent
                    or _id(row["member"]["mid"]) != author
                    or row["content"]["message"] != text
                    or (not expected and _timestamp(row.get("ctime")) < cutoff)
                    or ("oid" in row and _id(row["oid"]) != oid)
                    or ("type" in row and (type(row["type"]) is not int or row["type"] != kind))
                ):
                    continue
                matches[identifier] = row.get("invisible") in (True, 1, "1")
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if len(matches) > 1:
            return _result("ambiguous", coverage, candidate_count=len(matches))
        if not matches:
            return _result("check_failed" if known_seen else "not_found", coverage)
        identifier, hidden = next(iter(matches.items()))
        return _result(
            "not_visible" if hidden else "visible",
            coverage,
            receipt={
                "platform": "bilibili",
                "rpid": identifier,
                "oid": oid,
                "type": kind,
                "root": root,
                "parent": parent,
            },
        )
    except (ValueError, TypeError, KeyError, httpx.HTTPError) as error:
        return _failure(error, coverage)


async def reconcile_message(read, target, *, author_uid, text, started_at, receipt=None):
    """Verify the logged-in sender, then inspect the latest 30 messages read-only."""
    coverage = {"scope": "session_latest_only", "limit": 30}
    try:
        author = _id(author_uid)
        receiver = _id(target.get("recipient_uid", target.get("receiver_id")))
        cutoff = math.floor(_timestamp(started_at))
        kind = target.get("message_kind", "text")
        if kind not in ("text", "image"):
            raise ValueError("核对消息类型无效")
        expected = _id(receipt["msg_key"]) if receipt and receipt.get("msg_key") is not None else None
        if kind == "image" and not expected:
            return _result("unsupported", coverage, reason="image_receipt_required")
        if kind == "text" and (not isinstance(text, str) or not text):
            raise ValueError("核对缺少实际发送正文")
        nav = await read("/x/web-interface/nav", {})
        if not isinstance(nav, dict) or nav.get("isLogin") is not True or _id(nav.get("mid")) != author:
            return _result("check_failed", coverage, reason="account_changed")
        data = await read(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
            {"talker_id": receiver, "session_type": 1, "size": 30, "begin_seqno": 0},
        )
        records = _rows(data, "messages")[:30]
        coverage["records"] = len(records)
        matches, known_seen = {}, False
        for row in records:
            try:
                identifier = _id(row.get("msg_key"))
                if expected and identifier != expected:
                    continue
                known_seen = known_seen or identifier == expected
                if (
                    _id(row.get("sender_uid")) != author
                    or _id(row.get("receiver_id")) != receiver
                    or type(row.get("receiver_type")) is not int
                    or row["receiver_type"] != 1
                    or type(row.get("msg_type")) is not int
                    or row["msg_type"] != (1 if kind == "text" else 2)
                    or (not expected and _timestamp(row.get("timestamp")) < cutoff)
                ):
                    continue
                content = row.get("content")
                content = json.loads(content) if isinstance(content, str) else content
                if not isinstance(content, dict):
                    continue
                if kind == "text" and content.get("content") != text:
                    continue
                if kind == "image" and (not isinstance(content.get("url"), str) or not content["url"]):
                    continue
                matches[identifier] = row.get("msg_status") in (1, "1")
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if len(matches) > 1:
            return _result("ambiguous", coverage, candidate_count=len(matches))
        if not matches:
            return _result("check_failed" if known_seen else "not_found", coverage)
        identifier, hidden = next(iter(matches.items()))
        return _result(
            "not_visible" if hidden else "visible",
            coverage,
            receipt={
                "platform": "bilibili",
                "msg_key": identifier,
                "sender_id": author,
                "receiver_id": receiver,
            },
        )
    except (ValueError, TypeError, KeyError, httpx.HTTPError) as error:
        return _failure(error, coverage)


async def reconcile_dynamic(read, target, *, author_uid, text, started_at, receipt=None):
    """Confirm a known ID or uniquely match the original author's recent feed."""
    known = bool(receipt and receipt.get("dynamic_id"))
    coverage = {"scope": "known_dynamic_detail" if known else "own_feed_first_page", "limit": 20}
    if target.get("message_kind", "text") == "image":
        return _result("unsupported", coverage, reason="image_payload_verification_required")
    try:
        author, cutoff = _id(author_uid), math.floor(_timestamp(started_at))
        if not isinstance(text, str) or not text:
            raise ValueError("核对缺少实际发送正文")
        expected = _id(receipt["dynamic_id"]) if known else None
        if expected:
            data = await read(
                "/x/polymer/web-dynamic/v1/detail",
                {
                    **_DYNAMIC_CLIENT,
                    "id": expected,
                    "timezone_offset": -480,
                    "platform": "web",
                    "gaia_source": "main_web",
                    "features": _DETAIL_FEATURES,
                    "web_location": "333.1368",
                    "x-bili-web-req-json": '{"spm_id":"333.1368"}',
                },
            )
            records = [data["item"]]
        else:
            data = await read(
                "/x/polymer/web-dynamic/v1/feed/space",
                {
                    **_DYNAMIC_CLIENT,
                    "host_mid": author,
                    "offset": "",
                    "timezone_offset": -480,
                    "features": _FEED_FEATURES,
                    "x-bili-web-req-json": '{"spm_id":"333.1387"}',
                },
            )
            records = _rows(data, "items")[:20]
        coverage["records"] = len(records)
        matches = {}
        for item in records:
            try:
                identifier = _id(item.get("id_str"))
                modules = item["modules"]
                poster = modules["module_author"]
                if (
                    (expected and identifier != expected)
                    or _id(poster["mid"]) != author
                    or modules["module_dynamic"]["desc"]["text"] != text
                    or item.get("type") != "DYNAMIC_TYPE_WORD"
                    or (not expected and _timestamp(poster.get("pub_ts")) < cutoff)
                ):
                    continue
                matches[identifier] = item.get("visible") is False
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if len(matches) > 1:
            return _result("ambiguous", coverage, candidate_count=len(matches))
        if not matches:
            return _result("check_failed" if expected else "not_found", coverage)
        identifier, hidden = next(iter(matches.items()))
        return _result(
            "not_visible" if hidden else "visible",
            coverage,
            receipt={
                "platform": "bilibili",
                "code": 0,
                "dynamic_id": identifier,
                "url": f"https://t.bilibili.com/{identifier}",
            },
        )
    except (ValueError, TypeError, KeyError, httpx.HTTPError) as error:
        return _failure(error, coverage)
