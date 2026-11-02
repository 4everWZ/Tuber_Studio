"""Read-only remote evidence never turns an inconclusive lookup into a resend."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from tuber_studio.bilibili import BilibiliAdapter
from tuber_studio.bilibili_browsing import BrowsingAdapter
from tuber_studio.bilibili_messages import BilibiliMessagesAdapter

TEXT = "（尾巴晃晃）\n本鱼接住啦～"
TARGET = {"oid": "123", "type": 1, "root": "100", "parent": "200"}


@asynccontextmanager
async def adapter_for(handler, cls=BilibiliAdapter):
    adapter = cls({"sessdata": "test-session", "csrf": "test-csrf"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handler)
    )
    try:
        yield adapter
    finally:
        await adapter.close()


def reply(**changes):
    return {
        "rpid": 300,
        "oid": 123,
        "type": 1,
        "root": 100,
        "parent": 200,
        "member": {"mid": "900"},
        "content": {"message": TEXT},
        "ctime": 100,
        **changes,
    }


async def test_unknown_comment_is_confirmed_by_unique_raw_author_body_target_and_time():
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/x/v2/reply/reply"
        assert dict(request.url.params) == {"oid": "123", "type": "1", "pn": "1", "ps": "20", "root": "100"}
        return httpx.Response(200, json={"code": 0, "data": {"replies": [reply()]}})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_publication(TARGET, author_uid="900", text=TEXT, started_at=100.9)
    assert result["status"] == "visible" and result["receipt"]["rpid"] == "300"
    assert result["receipt"]["parent"] == "200" and len(calls) == 1
    assert TEXT not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "mutation",
    [
        {"root": 999},
        {"parent": 999},
        {"member": {"mid": 901}},
        {"content": {"message": TEXT + "不同"}},
        {"ctime": 99},
        {"oid": 999},
        {"type": True},
        {"root": None},
        {"parent": None},
        {"member": {}},
    ],
)
async def test_comment_near_matches_never_create_a_receipt(mutation):
    async def handle(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"code": 0, "data": {"replies": [reply(**mutation)]}})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_publication(TARGET, author_uid="900", text=TEXT, started_at=100.9)
    assert result["status"] == "not_found" and "receipt" not in result


async def test_duplicate_matching_comments_remain_ambiguous():
    async def handle(request):
        return httpx.Response(200, json={"code": 0, "data": {"replies": [reply(), reply(rpid=301)]}})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_publication(TARGET, author_uid="900", text=TEXT, started_at=100)
    assert result["status"] == "ambiguous" and result["candidate_count"] == 2
    assert "receipt" not in result


async def test_receipt_selects_exact_id_and_reports_hidden_without_resending():
    async def handle(request):
        assert request.method == "GET"
        return httpx.Response(
            200, json={"code": 0, "data": {"replies": [reply(rpid=301), reply(ctime=99, invisible=True)]}}
        )

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_publication(
            TARGET, author_uid="900", text=TEXT, started_at=100, receipt={"rpid": "300"}
        )
    assert result["status"] == "not_visible" and result["receipt"]["rpid"] == "300"


async def test_top_level_dynamic_comment_uses_actual_type17_and_zero_parent():
    target = {"oid": "123", "type": 17, "root": "0", "parent": "0"}

    async def handle(request):
        assert request.method == "GET" and request.url.path == "/x/v2/reply"
        assert request.url.params["type"] == "17" and request.url.params["sort"] == "0"
        return httpx.Response(200, json={"code": 0, "data": {"replies": [reply(type=17, root=0, parent=0)]}})

    async with adapter_for(handle, BrowsingAdapter) as adapter:
        result = await adapter.reconcile_publication(target, author_uid="900", text=TEXT, started_at=100)
    assert result["status"] == "visible" and result["receipt"]["type"] == 17


@pytest.mark.parametrize(
    "returned_bvid,expected", [("BV1d8pU6xEZX", "visible"), ("BV1Yjxw68E9G", "check_failed")]
)
async def test_video_only_binding_reads_and_checks_metadata_before_lookup(returned_bvid, expected):
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {"bvid": returned_bvid, "aid": 123}})
        return httpx.Response(200, json={"code": 0, "data": {"replies": [reply()]}})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_publication(
            {"resource_type": "video", "resource_id": "BV1d8pU6xEZX", "root": "100", "parent": "200"},
            author_uid="900",
            text=TEXT,
            started_at=100,
        )
    assert result["status"] == expected
    assert len(calls) == (2 if expected == "visible" else 1)


def message(**changes):
    return {
        "msg_key": "400",
        "sender_uid": "900",
        "receiver_id": "800",
        "receiver_type": 1,
        "msg_type": 1,
        "msg_status": 0,
        "content": json.dumps({"content": TEXT}),
        "timestamp": 100,
        **changes,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {},
        {"sender_uid": "800"},
        {"receiver_id": "801"},
        {"msg_type": 2},
        {"msg_type": True},
        {"timestamp": 99},
        {"content": "malformed"},
    ],
)
async def test_private_confirmation_checks_owner_recipient_type_body_and_time(mutation):
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={"code": 0, "data": {"mid": "900", "isLogin": True}})
        assert request.url.host == "api.vc.bilibili.com"
        assert request.url.params["talker_id"] == "800" and request.url.params["size"] == "30"
        return httpx.Response(200, json={"code": 0, "data": {"messages": [message(**mutation)]}})

    async with adapter_for(handle, BilibiliMessagesAdapter) as adapter:
        result = await adapter.reconcile_publication(
            {"recipient_uid": "800"}, author_uid="900", text=TEXT, started_at=100
        )
    assert result["status"] == ("visible" if not mutation else "not_found")
    assert ("receipt" in result) == (not mutation)
    assert len(calls) == 2


@pytest.mark.parametrize("owner,logged_in", [("901", True), ("900", False)])
async def test_account_change_stops_before_any_private_history_read(owner, logged_in):
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.url.path == "/x/web-interface/nav"
        return httpx.Response(200, json={"code": 0, "data": {"mid": owner, "isLogin": logged_in}})

    async with adapter_for(handle, BilibiliMessagesAdapter) as adapter:
        result = await adapter.reconcile_publication(
            {"recipient_uid": "800"}, author_uid="900", text=TEXT, started_at=100
        )
    assert result["status"] == "check_failed" and result["reason"] == "account_changed"
    assert len(calls) == 1


async def test_private_image_without_original_receipt_is_not_guessed_from_draft_caption():
    async def handle(request):
        pytest.fail("Unsupported image lookup must not request history")

    async with adapter_for(handle, BilibiliMessagesAdapter) as adapter:
        result = await adapter.reconcile_publication(
            {"recipient_uid": "800", "message_kind": "image"}, author_uid="900", text=TEXT, started_at=100
        )
    assert result["status"] == "unsupported" and "receipt" not in result


@pytest.mark.parametrize("known", [False, True])
async def test_dynamic_confirms_known_detail_or_uniquely_matches_own_recent_feed(known):
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path == "/x/web-interface/nav":
            return wbi_nav()
        assert request.url.path == "/x/polymer/web-dynamic/v1/" + ("detail" if known else "feed/space")
        assert "w_rid" in request.url.params and "features" in request.url.params
        item = dynamic_item()
        if not known:
            assert request.url.params["host_mid"] == "900"
        return httpx.Response(200, json={"code": 0, "data": {"item": item} if known else {"items": [item]}})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_dynamic_publication(
            {}, author_uid="900", text=TEXT, started_at=100, receipt={"dynamic_id": "500"} if known else None
        )
    assert result["status"] == "visible" and result["receipt"]["dynamic_id"] == "500"
    assert len(calls) == 2


def dynamic_item():
    return {
        "id_str": "500",
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"mid": "900", "pub_ts": 100},
            "module_dynamic": {"desc": {"text": TEXT}},
        },
    }


def wbi_nav():
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "mid": 900,
                "isLogin": True,
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                },
            },
        },
    )


@pytest.mark.parametrize("case", ["empty", "old", "other_author", "other_text", "other_type", "ambiguous"])
async def test_unknown_dynamic_requires_unique_recent_matching_text_post(case):
    item = dynamic_item()
    if case == "old":
        item["modules"]["module_author"]["pub_ts"] = 99
    elif case == "other_author":
        item["modules"]["module_author"]["mid"] = "901"
    elif case == "other_text":
        item["modules"]["module_dynamic"]["desc"]["text"] += "不同"
    elif case == "other_type":
        item["type"] = "DYNAMIC_TYPE_DRAW"
    items = [] if case == "empty" else [item]
    if case == "ambiguous":
        another = copy.deepcopy(item)
        another["id_str"] = "501"
        items.append(another)

    async def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return wbi_nav()
        assert request.method == "GET" and request.url.path.endswith("/feed/space")
        return httpx.Response(200, json={"code": 0, "data": {"items": items}})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_dynamic_publication({}, author_uid="900", text=TEXT, started_at=100)
    assert result["status"] == ("ambiguous" if case == "ambiguous" else "not_found")
    assert "receipt" not in result


@pytest.mark.parametrize("code", [-352, -412])
async def test_readback_preserves_sanitized_platform_error_code(code):
    async def handle(request):
        return httpx.Response(200, json={"code": code, "message": "private-session-marker", "data": None})

    async with adapter_for(handle) as adapter:
        result = await adapter.reconcile_publication(TARGET, author_uid="900", text=TEXT, started_at=100)
    assert result["status"] == "check_failed"
    assert str(code) in result["error"] and "private-session-marker" not in json.dumps(result)
    assert "receipt" not in result


@pytest.mark.parametrize("failure", ["timeout", "empty", "invalid", "cancel"])
async def test_inconclusive_reads_and_cancellation_never_supply_receipt(failure):
    async def handle(request):
        assert request.method == "GET"
        if failure == "timeout":
            raise httpx.ReadTimeout("read failed")
        if failure == "cancel":
            raise asyncio.CancelledError
        return httpx.Response(
            200, json={"code": 0, "data": {"replies": None} if failure == "empty" else {"replies": "invalid"}}
        )

    async with adapter_for(handle) as adapter:
        call = adapter.reconcile_publication(
            copy.deepcopy(TARGET), author_uid="900", text=TEXT, started_at=100
        )
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await call
            return
        result = await call
    assert result["status"] == ("not_found" if failure == "empty" else "check_failed")
    assert "receipt" not in result
