"""Platform boundaries: fixed destinations, stable identity, and one-shot writes."""

import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio

from tuber_studio.bilibili import BilibiliAdapter, PublishUncertain

BV = "BV1d8pU6xEZX"
AID = 117441451525192
SECRETS = {"sessdata": "private-session-value", "csrf": "private-csrf-value"}


@pytest_asyncio.fixture
async def adapter_factory():
    adapters = []

    async def create(handler, credentials=None):
        adapter = BilibiliAdapter(credentials)
        original = adapter.client
        adapter.client = httpx.AsyncClient(
            base_url=original.base_url,
            headers=original.headers,
            cookies=original.cookies,
            follow_redirects=False,
            transport=httpx.MockTransport(handler),
        )
        await original.aclose()
        adapters.append(adapter)
        return adapter

    yield create
    for adapter in adapters:
        await adapter.close()


def video_response():
    return httpx.Response(200, json={"code": 0, "data": {"bvid": BV, "aid": AID, "title": "视频"}})


def comment(rpid, uid, name="同名观众"):
    return {
        "rpid": rpid,
        "member": {"mid": uid, "uname": name},
        "content": {"message": f"评论{rpid}"},
        "like": 2,
        "ctime": 1793180000,
    }


def assert_no_secrets(value):
    rendered = str(value)
    assert all(secret not in rendered for secret in SECRETS.values())


@pytest.mark.parametrize(
    ("value", "parameter", "identifier"),
    [
        (BV, "bvid", BV),
        ("av170001", "aid", "170001"),
        (f"https://www.bilibili.com/video/{BV}/?p=1", "bvid", BV),
        ("https://m.bilibili.com/video/av170001", "aid", "170001"),
    ],
)
async def test_video_input_is_parsed_into_fixed_api_requests(adapter_factory, value, parameter, identifier):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "api.bilibili.com"
        if request.url.path == "/x/web-interface/view":
            assert dict(request.url.params) == {parameter: identifier}
            return video_response()
        assert request.url.path == "/x/v2/reply"
        assert dict(request.url.params) == {"oid": str(AID), "type": "1", "pn": "1", "ps": "20", "sort": "2"}
        return httpx.Response(200, json={"code": 0, "data": {"replies": None, "page": {"count": 0}}})

    adapter = await adapter_factory(handle)
    result = await adapter.fetch(value)
    assert result == {"video_id": BV, "aid": AID, "title": "视频", "items": []}
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/video/" + BV,
        "https://www.bilibili.com.evil.example/video/" + BV,
        "https://www.bilibili.com@127.0.0.1/video/" + BV,
        "https://user:password@www.bilibili.com/video/" + BV,
        "https://www.bilibili.com:1234/video/" + BV,
        "https://b23.tv/short-link",
        "file:///etc/passwd",
        "https://www.bilibili.com/video/" + BV + "/extra",
        "BV1invalid",
    ],
)
async def test_untrusted_video_destinations_are_rejected_before_network(adapter_factory, value):
    requests = []

    def handle(request):
        requests.append(request)
        raise AssertionError("An invalid video must never reach HTTP")

    adapter = await adapter_factory(handle)
    with pytest.raises(ValueError):
        await adapter.fetch(value)
    assert requests == []


@pytest.mark.parametrize("pages", [0, -1, 11, True, 1.5, "2"])
async def test_invalid_page_limits_are_rejected_before_network(adapter_factory, pages):
    requests = []

    def handle(request):
        requests.append(request)
        raise AssertionError("An invalid limit must never reach HTTP")

    adapter = await adapter_factory(handle)
    with pytest.raises(ValueError):
        await adapter.fetch(BV, pages)
    assert requests == []


async def test_pagination_deduplicates_comments_without_merging_same_nickname(adapter_factory):
    pages = []

    def handle(request):
        if request.url.path == "/x/web-interface/view":
            return video_response()
        page = int(request.url.params["pn"])
        pages.append(page)
        replies = (
            [comment(11, 1001), comment(12, 1002)] if page == 1 else [comment(11, 1001), comment(13, 1001)]
        )
        return httpx.Response(200, json={"code": 0, "data": {"replies": replies, "page": {"count": 100}}})

    adapter = await adapter_factory(handle)
    result = await adapter.fetch(BV, max_pages=2)
    assert pages == [1, 2]
    assert [item["comment_id"] for item in result["items"]] == ["11", "12", "13"]
    assert [item["platform_user_id"] for item in result["items"]] == ["1001", "1002", "1001"]
    assert {item["display_name"] for item in result["items"]} == {"同名观众"}


async def test_reply_previews_keep_thread_root_parent_and_deduplicate_across_pages(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/view":
            return video_response()
        root = comment(11, 1001)
        root.update(root=0, parent=0)
        if request.url.params["pn"] == "1":
            direct = comment(12, 1002) | {"root": 11, "parent": 11}
            nested = comment(13, 1003) | {"root": 11, "parent": 12}
            root["replies"] = [direct, nested]
        else:
            # A repeated root can expose a new preview. An absent child root
            # inherits the explicit containing thread, not a whole-video scope.
            root["replies"] = [comment(12, 1002), comment(14, 1004)]
        return httpx.Response(200, json={"code": 0, "data": {"replies": [root], "page": {"count": 100}}})

    adapter = await adapter_factory(handle)
    items = (await adapter.fetch(BV, max_pages=2))["items"]
    assert [item["comment_id"] for item in items] == ["11", "12", "13", "14"]
    assert {item["thread_id"] for item in items} == {"11"}
    assert [item["parent_comment_id"] for item in items] == [None, "11", "12", "11"]


async def test_flat_root_metadata_and_missing_top_level_root_stay_separate(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/view":
            return video_response()
        replies = [comment(21, 1001), comment(22, 1002) | {"root": 11, "parent": 12}]
        return httpx.Response(200, json={"code": 0, "data": {"replies": replies}})

    adapter = await adapter_factory(handle)
    items = (await adapter.fetch(BV))["items"]
    assert [(item["thread_id"], item["parent_comment_id"]) for item in items] == [("21", None), ("11", "12")]


async def test_inconsistent_nested_thread_metadata_is_rejected(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/view":
            return video_response()
        root = comment(11, 1001) | {"replies": [comment(12, 1002) | {"root": 99, "parent": 11}]}
        return httpx.Response(200, json={"code": 0, "data": {"replies": [root]}})

    adapter = await adapter_factory(handle)
    with pytest.raises(ValueError, match="评论数据格式无效"):
        await adapter.fetch(BV)


async def test_popular_uses_read_only_fixed_endpoint(adapter_factory):
    def handle(request):
        assert request.method == "GET"
        assert request.url.host == "api.bilibili.com"
        assert request.url.path == "/x/web-interface/popular"
        assert dict(request.url.params) == {"pn": "1", "ps": "20"}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {"bvid": BV, "aid": AID, "title": "视频", "owner": {"name": "作者"}},
                    ]
                },
            },
        )

    adapter = await adapter_factory(handle)
    assert await adapter.popular() == [{"bvid": BV, "aid": AID, "title": "视频", "owner": "作者"}]


@pytest.mark.parametrize("case", ["platform", "http", "timeout", "json", "redirect"])
async def test_read_failures_expose_safe_errors_without_credentials(adapter_factory, case):
    def handle(request):
        if case == "timeout":
            raise httpx.ReadTimeout(str(SECRETS), request=request)
        if case == "http":
            return httpx.Response(403, text=str(SECRETS))
        if case == "json":
            return httpx.Response(200, text=str(SECRETS))
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, json={"code": -352, "message": str(SECRETS)})

    adapter = await adapter_factory(handle, SECRETS)
    with pytest.raises(ValueError) as caught:
        await adapter.popular()
    assert not isinstance(caught.value, PublishUncertain)
    assert_no_secrets(caught.value)


@pytest.mark.parametrize(
    "credentials",
    [None, {"sessdata": SECRETS["sessdata"]}, {"csrf": SECRETS["csrf"]}],
)
async def test_publish_requires_both_credentials_before_network(adapter_factory, credentials):
    requests = []

    def handle(request):
        requests.append(request)
        raise AssertionError("An unauthenticated write must never reach HTTP")

    adapter = await adapter_factory(handle, credentials)
    with pytest.raises(ValueError) as caught:
        await adapter.publish(BV, "11", "你好")
    assert not isinstance(caught.value, PublishUncertain)
    assert_no_secrets(caught.value)
    assert requests == []


@pytest.mark.parametrize(
    ("case", "uncertain"),
    [
        ("timeout", True),
        ("http", True),
        ("json", True),
        ("missing_code", True),
        ("missing_receipt", True),
        ("invalid_receipt", True),
        ("rejected", False),
        ("success", False),
    ],
)
async def test_publish_sends_once_and_distinguishes_rejection_uncertainty_and_receipt(
    adapter_factory,
    case,
    uncertain,
):
    posts = []

    def handle(request):
        assert request.url.host == "api.bilibili.com"
        if request.method == "GET":
            return video_response()
        posts.append(request)
        assert request.url.path == "/x/v2/reply/add"
        cookie = SimpleCookie(request.headers["cookie"])
        assert cookie["SESSDATA"].value == SECRETS["sessdata"]
        assert cookie["bili_jct"].value == SECRETS["csrf"]
        form = parse_qs(request.content.decode())
        assert form["root"] == form["parent"] == ["11"]
        assert form["oid"] == [str(AID)]
        assert form["message"] == ["你好"]
        assert form["csrf"] == form["csrf_token"] == [SECRETS["csrf"]]
        if case == "timeout":
            raise httpx.ReadTimeout(str(SECRETS), request=request)
        if case == "http":
            return httpx.Response(503, text=str(SECRETS))
        if case == "json":
            return httpx.Response(200, text=str(SECRETS))
        if case == "missing_code":
            return httpx.Response(200, json={"data": {"rpid": 123}})
        if case == "missing_receipt":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if case == "invalid_receipt":
            return httpx.Response(200, json={"code": 0, "data": {"rpid": 0}})
        if case == "rejected":
            return httpx.Response(200, json={"code": 12015, "message": str(SECRETS)})
        return httpx.Response(200, json={"code": 0, "data": {"rpid_str": "1234567890123456789"}})

    adapter = await adapter_factory(handle, SECRETS)
    if case == "success":
        result = await adapter.publish(BV, "11", "你好")
        assert result == {
            "platform": "bilibili",
            "code": 0,
            "rpid": "1234567890123456789",
            "video_id": BV,
            "aid": AID,
            "root": "11",
            "parent": "11",
        }
        assert_no_secrets(json.dumps(result))
    else:
        with pytest.raises(ValueError) as caught:
            await adapter.publish(BV, "11", "你好")
        assert isinstance(caught.value, PublishUncertain) is uncertain
        assert_no_secrets(caught.value)
    assert len(posts) == 1


async def test_publish_reply_inside_thread_uses_root_and_immediate_target(adapter_factory):
    posts = []

    def handle(request):
        if request.method == "GET":
            return video_response()
        posts.append(request)
        form = parse_qs(request.content.decode())
        assert form["root"] == ["11"] and form["parent"] == ["13"]
        return httpx.Response(200, json={"code": 0, "data": {"rpid": 99}})

    adapter = await adapter_factory(handle, SECRETS)
    receipt = await adapter.publish(BV, "13", "收到你的楼内补充啦", thread_id="11")
    assert receipt["root"] == "11" and receipt["parent"] == "13"
    assert len(posts) == 1
