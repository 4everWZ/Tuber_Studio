"""Evidence, account scope and one-shot platform boundaries, without real writes."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio

from tuber_studio.bilibili import PublishUncertain
from tuber_studio.bilibili_browsing import BrowsingAdapter, PlatformReadError

BV = "BV1d8pU6xEZX"


def result(data, code=0):
    return httpx.Response(200, json={"code": code, "data": data})


def nav():
    return result(
        {
            "mid": 9,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            },
        }
    )


def metadata():
    return {
        "bvid": BV,
        "aid": 123,
        "title": "真实完整标题",
        "desc": "完整视频简介，不是搜索摘要",
        "owner": {"mid": 77, "name": "作者"},
        "duration": 120,
        "pages": [
            {"cid": 456, "part": "第一P", "duration": 60},
            {"cid": 457, "part": "第二P", "duration": 60},
        ],
    }


def video():
    return {
        "bvid": BV,
        "aid": 123,
        "title": "<em class=keyword>水下</em>故事",
        "duration": 120,
        "owner": {"mid": 77, "name": "作者"},
    }


def comment(identity=100, author=9, parent=0, text="鱼鱼自己的评论"):
    return {
        "rpid": identity,
        "root": 0 if identity == 100 else 100,
        "parent": parent,
        "member": {"mid": author, "uname": "同名用户"},
        "content": {"message": text},
        "ctime": 1,
    }


def event(kind="reply", resource="video"):
    return {
        "event_id": "1001",
        "kind": kind,
        "account_id": "9",
        "sender_uid": "7",
        "sender_name": "同名用户",
        "resource_type": resource,
        "oid": "123",
        "root": "100",
        "parent": "101",
        "comment_id": "101",
        "timestamp": 200,
        "source_text": "你好鱼鱼",
    }


@pytest_asyncio.fixture
async def adapter_factory():
    adapters = []

    async def create(handler, credentials=True):
        adapter = BrowsingAdapter(
            {"sessdata": "secret-session", "csrf": "secret-csrf"} if credentials else {}
        )
        original = adapter.client
        adapter.client = httpx.AsyncClient(
            base_url=original.base_url,
            headers=original.headers,
            cookies=original.cookies,
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        await original.aclose()
        adapter._read_spacing = 0
        adapters.append(adapter)
        return adapter

    yield create
    for adapter in adapters:
        await adapter.close()


async def test_empty_search_never_falls_back_and_anonymous_recommend_never_claims_login(adapter_factory):
    requests = []
    adapter = await adapter_factory(lambda request: requests.append(request), credentials=False)
    with pytest.raises(ValueError, match="非空关键词"):
        await adapter.discover("search")
    with pytest.raises(PlatformReadError) as denied:
        await adapter.discover("recommend")
    assert denied.value.kind == "permission"
    assert requests == []


@pytest.mark.parametrize(
    "source,path,key",
    [
        ("recommend", "/x/web-interface/wbi/index/top/feed/rcmd", "item"),
        ("search", "/x/web-interface/wbi/search/type", "result"),
        ("popular", "/x/web-interface/popular", "list"),
    ],
)
async def test_discovery_preserves_source_and_real_page(adapter_factory, source, path, key):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/x/web-interface/nav":
            return nav()
        assert request.url.path == path
        assert (
            request.url.params.get(
                "fresh_idx" if source == "recommend" else "page" if source == "search" else "pn"
            )
            == "2"
        )
        return result({key: [video()]})

    adapter = await adapter_factory(handle)
    output = await adapter.discover(source, "水下" if source == "search" else "", page=2)
    assert output["items"][0]["title"] == "水下故事"
    assert output["items"][0]["owner_uid"] == "77"
    assert output["source"] == source and output["page"] == 2
    assert not any(request.method != "GET" for request in requests)


@pytest.mark.parametrize("card_type", ["ketang", "video"])
async def test_search_ignores_course_cards_but_rejects_invalid_video_ids(adapter_factory, card_type):
    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        assert request.url.path == "/x/web-interface/wbi/search/type"
        return result(
            {
                "result": [
                    {"type": card_type, "bvid": "", "aid": 438856364},
                    video() | {"type": "video"},
                ]
            }
        )

    adapter = await adapter_factory(handle)
    if card_type == "ketang":
        output = await adapter.discover("search", "大肥鱼", page=3)
        assert [item["bvid"] for item in output["items"]] == [BV]
        assert output["has_more"] and output["page"] == 3
    else:
        with pytest.raises(PlatformReadError, match="视频标识无效"):
            await adapter.discover("search", "大肥鱼", page=3)


async def test_metadata_reads_full_description_real_tags_and_all_part_identifiers(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/view":
            return result(metadata())
        assert request.url.path == "/x/web-interface/view/detail/tag"
        return result([{"tag_name": "科普"}])

    adapter = await adapter_factory(handle)
    output = await adapter.video_metadata(BV)
    assert output["description_complete"] and output["description"] == metadata()["desc"]
    assert output["tags"] == ["科普"] and not output["tags_missing"]
    assert [row["cid"] for row in output["parts"]] == ["456", "457"]


def subtitle_handler(request):
    if request.url.path == "/x/web-interface/nav":
        return nav()
    if request.url.path == "/x/web-interface/view":
        return result(metadata())
    if request.url.path == "/x/player/wbi/v2":
        return result(
            {
                "cid": 456,
                "subtitle": {
                    "subtitles": [
                        {
                            "id": 1,
                            "lan": "zh",
                            "type": 1,
                            "subtitle_url": "https://aisubtitle.hdslb.com/ai.json",
                        },
                        {"id": 2, "lan": "zh", "type": 0, "subtitle_url": "https://i0.hdslb.com/manual.json"},
                    ]
                },
            }
        )
    assert request.url == "https://i0.hdslb.com/manual.json"
    assert "cookie" not in request.headers and "authorization" not in request.headers
    return httpx.Response(200, json={"body": [{"from": 1.2, "to": 2.7, "content": "章鱼有三个心脏"}]})


async def test_subtitle_prefers_manual_same_language_has_times_and_strips_api_cookies(adapter_factory):
    adapter = await adapter_factory(subtitle_handler)
    calls = []

    async def gate():
        calls.append(1)

    adapter.request_gate = gate
    output = await adapter.subtitles(BV, "456", ["zh"])
    assert not output["is_ai"] and output["track_id"] == "2"
    assert output["segments"] == [{"id": "456:2:0", "start": 1.2, "end": 2.7, "text": "章鱼有三个心脏"}]
    assert output["coverage"]["complete"] and len(calls) == 4


async def test_wrong_cid_stops_before_requesting_player_or_cdn(adapter_factory):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/x/web-interface/view"
        return result(metadata())

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        await adapter.subtitles(BV, "999")
    assert caught.value.kind == "invalid" and len(requests) == 1


@pytest.mark.parametrize(
    "player,kind",
    [
        ({"need_login_subtitle": True, "subtitle": {"subtitles": []}}, "permission"),
        ({"subtitle": {"subtitles": []}}, "no_subtitles"),
        ({"subtitle": "bad"}, "invalid"),
        ({"cid": 999, "subtitle": {"subtitles": []}}, "invalid"),
    ],
)
async def test_absent_subtitle_and_login_requirement_are_not_conflated(adapter_factory, player, kind):
    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        if request.url.path == "/x/web-interface/view":
            return result(metadata())
        return result(player)

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        await adapter.subtitles(BV, "456")
    assert caught.value.kind == kind


@pytest.mark.parametrize(
    "body,kind",
    [
        ([], "empty"),
        ([{"from": 0, "to": 1, "content": "字幕by小鱼"}], "empty"),
        ([{"from": 0, "to": 1, "content": "   "}], "empty"),
        ([{"from": 5, "to": 1, "content": "正文"}], "invalid"),
    ],
)
async def test_invalid_and_credit_only_subtitles_do_not_become_content(adapter_factory, body, kind):
    adapter = await adapter_factory(lambda request: httpx.Response(200, json={"body": body}))
    track = {
        "url": "https://i0.hdslb.com/sub.json",
        "bvid": BV,
        "cid": "456",
        "track_id": "1",
        "language": "zh",
        "is_ai": False,
    }
    with pytest.raises(PlatformReadError) as caught:
        await adapter.fetch_subtitle(track)
    assert caught.value.kind == kind


@pytest.mark.parametrize(
    "url",
    [
        "http://i0.hdslb.com/a",
        "https://127.0.0.1/a",
        "https://i0.hdslb.com.evil.invalid/a",
        "https://user@i0.hdslb.com/a",
        "https://i0.hdslb.com:444/a",
    ],
)
async def test_subtitle_url_validation_before_any_network(adapter_factory, url):
    adapter = await adapter_factory(lambda request: pytest.fail("untrusted URL requested"))
    with pytest.raises(PlatformReadError):
        await adapter.fetch_subtitle({"url": url})


@pytest.mark.parametrize(
    "failure,kind",
    [
        ("redirect", "invalid"),
        ("too_large", "too_large"),
        ("forbidden", "permission"),
        ("timeout", "network"),
    ],
)
async def test_subtitle_download_failures_are_not_no_subtitle(adapter_factory, failure, kind):
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})
        if failure == "too_large":
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        if failure == "forbidden":
            return httpx.Response(403)
        raise httpx.ReadTimeout("private URL and secret-session must not leak")

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        await adapter.fetch_subtitle({"url": "https://i0.hdslb.com/sub.json"})
    assert caught.value.kind == kind and len(requests) == 1
    assert "secret-session" not in str(caught.value)


@pytest.mark.parametrize("stage", ["api", "subtitle"])
@pytest.mark.parametrize(
    "status,kind",
    [
        (302, "network"),
        (401, "permission"),
        (403, "permission"),
        (412, "restricted"),
        (429, "restricted"),
        (503, "network"),
    ],
)
async def test_read_http_status_is_visible_without_request_secrets(adapter_factory, stage, status, kind):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://private.invalid/?secret-query", "set-cookie": "secret-cookie"},
            text="secret-response-body",
        )

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        if stage == "api":
            await adapter._get("/x/web-interface/view", {"bvid": BV, "token": "secret-query"})
        else:
            await adapter.fetch_subtitle({"url": "https://i0.hdslb.com/sub.json?token=secret-query"})
    expected = "invalid" if stage == "subtitle" and status == 302 else kind
    assert caught.value.kind == expected and f"HTTP {status}" in str(caught.value)
    assert len(seen) == 1
    assert all(word not in str(caught.value) for word in ("secret", "https://", "hdslb.com", "bilibili.com"))


@pytest.mark.parametrize("stage", ["api", "subtitle"])
@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadError]
)
async def test_read_transport_type_is_visible_without_exception_details(adapter_factory, stage, error_type):
    seen = []

    def handle(request):
        seen.append(request)
        raise error_type(
            "https://private.invalid/?secret-query Cookie=secret-session Authorization=secret-header",
            request=request,
        )

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        if stage == "api":
            await adapter._get("/x/web-interface/view", {"bvid": BV, "token": "secret-query"})
        else:
            await adapter.fetch_subtitle({"url": "https://i0.hdslb.com/sub.json?token=secret-query"})
    assert caught.value.kind == "network" and error_type.__name__ in str(caught.value)
    assert len(seen) == 1 and "secret" not in str(caught.value) and "https://" not in str(caught.value)


@pytest.mark.parametrize("code,kind", [(-101, "permission"), (-412, "restricted"), (-404, "not_found")])
async def test_platform_read_errors_remain_distinct(adapter_factory, code, kind):
    adapter = await adapter_factory(lambda request: result({}, code))
    with pytest.raises(PlatformReadError) as caught:
        await adapter.discover("popular")
    assert caught.value.kind == kind


async def test_signer_login_expiry_preserves_permission_category(adapter_factory):
    adapter = await adapter_factory(lambda request: result({}, -101))
    with pytest.raises(PlatformReadError) as caught:
        await adapter.discover("search", "真实关键词")
    assert caught.value.kind == "permission"


async def test_notification_pages_account_scope_ids_and_same_second_events(adapter_factory):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/x/web-interface/nav":
            return nav()
        assert request.url.path == "/x/msgfeed/at"
        assert request.url.params["id"] == "1000" and request.url.params["at_time"] == "200"
        return result(
            {
                "cursor": {"is_end": True, "id": 1001, "time": 200},
                "items": [
                    {
                        "id": 1001,
                        "user": {"mid": 7, "nickname": "用户"},
                        "at_time": 200,
                        "item": {
                            "business_id": 1,
                            "subject_id": 123,
                            "root_id": 100,
                            "source_id": 101,
                            "target_id": 100,
                            "source_content": "你好鱼鱼",
                        },
                    }
                ],
            }
        )

    adapter = await adapter_factory(handle)
    page = await adapter.notifications("mention", {"id": "1000", "time": 200})
    assert page["account_id"] == "9" and page["items"][0]["comment_id"] == "101"
    assert page["cursor"] == {"id": "1001", "time": 200, "is_end": True}
    assert not page["has_more"] and not any("read" in path for path in paths)
    with pytest.raises(ValueError):
        await adapter.notifications("like")


async def test_real_reply_source_must_match_actor_and_own_original_comment(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        if request.url.path == "/x/v2/reply/reply":
            return result({"root": comment(), "replies": [comment(101, 7, 100, "你好鱼鱼")]})
        assert request.url.path == "/x/web-interface/view"
        return result(metadata())

    adapter = await adapter_factory(handle)
    thread = await adapter.read_public_thread(event())
    assert thread["target"]["parent"] == "101" and thread["target"]["bvid"] == BV
    assert thread["source"]["author_uid"] == "7" and not thread["coverage"]["complete"]
    with pytest.raises(PlatformReadError):
        await adapter.read_public_thread(event() | {"account_id": "8"})
    with pytest.raises(PlatformReadError):
        await adapter.read_public_thread(event() | {"sender_uid": "8"})


async def test_reply_to_someone_else_is_not_treated_as_reply_to_bot(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        return result({"root": comment(author=44), "replies": [comment(101, 7, 100, "你好鱼鱼")]})

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        await adapter.read_public_thread(event())
    assert caught.value.kind == "permission"


async def test_reply_to_unread_descendant_does_not_fall_back_to_own_root(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        return result({"root": comment(), "replies": [comment(101, 7, 999, "你好鱼鱼")]})

    adapter = await adapter_factory(handle)
    with pytest.raises(PlatformReadError) as caught:
        await adapter.read_public_thread(event())
    assert caught.value.kind == "permission"


async def test_text_dynamic_uses_real_original_not_video_fallback(adapter_factory):
    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        if request.url.path == "/x/v2/reply/reply":
            assert request.url.params["type"] == "17"
            return result({"root": comment(), "replies": [comment(101, 7, 100, "你好鱼鱼")]})
        assert request.url.path == "/x/polymer/web-dynamic/v1/detail"
        return result(
            {
                "item": {
                    "id_str": "123",
                    "type": "DYNAMIC_TYPE_WORD",
                    "modules": {"module_dynamic": {"desc": {"text": "公开原动态正文"}}},
                }
            }
        )

    adapter = await adapter_factory(handle)
    output = await adapter.read_public_thread(event(resource="dynamic"))
    assert output["public_text"] == "公开原动态正文" and output["target"]["bvid"] is None


@pytest.mark.parametrize("top_level", [True, False])
async def test_write_top_level_and_reply_have_different_roots_and_valid_receipt(adapter_factory, top_level):
    sent = []
    checkpoints = []

    def handle(request):
        if request.url.path == "/x/web-interface/nav":
            return nav()
        assert checkpoints == ["checked"]
        sent.append(parse_qs(request.content.decode()))
        return result({"rpid": 555})

    adapter = await adapter_factory(handle)
    target = {"resource_type": "video", "oid": "123"}
    if not top_level:
        target |= {"root": "100", "parent": "101"}
    method = adapter.publish_top_level if top_level else adapter.publish_resource_reply
    output = await method(target, "具体内容留言", before_send=lambda: checkpoints.append("checked"))
    assert output["rpid"] == "555" and len(sent) == 1
    assert sent[0]["root"] == ["0" if top_level else "100"]
    assert sent[0]["parent"] == ["0" if top_level else "101"]


async def test_final_authorization_prevents_post_after_preparing_signature(adapter_factory):
    seen = []

    def handle(request):
        seen.append(request.method)
        assert request.method == "GET"
        return nav()

    def stopped():
        raise ValueError("紧急停止")

    adapter = await adapter_factory(handle)
    with pytest.raises(ValueError, match="紧急停止"):
        await adapter.publish_top_level({"resource_type": "video", "oid": "123"}, "留言", stopped)
    assert seen == ["GET"]


async def test_missing_write_receipt_is_uncertain_never_a_read_retry(adapter_factory):
    writes = []

    def handle(request):
        if request.method == "GET":
            return nav()
        writes.append(request)
        return result({})

    adapter = await adapter_factory(handle)
    with pytest.raises(PublishUncertain):
        await adapter.publish_top_level({"resource_type": "video", "oid": "123"}, "留言")
    assert len(writes) == 1


@pytest.mark.parametrize(
    "rows,status",
    [([], "not_found"), ([{"rpid": 555}], "visible"), ([{"rpid": 555, "invisible": 1}], "not_visible")],
)
async def test_publication_acceptance_is_independent_from_first_page_visibility(
    adapter_factory, rows, status
):
    adapter = await adapter_factory(lambda request: result({"replies": rows}))
    output = await adapter.verify_publication({"resource_type": "video", "oid": "123"}, {"rpid": "555"})
    assert output["status"] == status
    assert "secret" not in json.dumps(output)
