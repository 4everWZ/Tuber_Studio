import json

import httpx
import pytest

from tuber_studio.bilibili import BilibiliAdapter, PublishUncertain
from tuber_studio.bilibili_dynamics import PublishCancelled, publish_text


@pytest.fixture
async def platform():
    adapter = BilibiliAdapter({"sessdata": "private-session", "csrf": "private-csrf"})
    await adapter.client.aclose()
    requests = []
    outcome = {"code": 0, "data": {"dyn_id_str": "987654321"}}

    def respond(request):
        requests.append(request)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                        }
                    },
                },
            )
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200, json=outcome)

    adapter.client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://api.bilibili.com",
        cookies={"SESSDATA": "private-session", "bili_jct": "private-csrf"},
    )
    yield adapter, requests, outcome
    await adapter.close()


async def test_signed_exact_payload_single_write_and_receipt(platform):
    adapter, requests, _ = platform
    receipt = await publish_text(adapter, "虚构海底小剧场开饭啦！")
    writes = [r for r in requests if r.method == "POST"]
    assert len(writes) == 1
    request = writes[0]
    assert request.url.host == "api.bilibili.com" and request.url.path == "/x/dynamic/feed/create/dyn"
    assert request.url.params["csrf"] == "private-csrf"
    assert len(request.url.params["w_rid"]) == 32 and int(request.url.params["wts"]) > 0
    assert json.loads(request.content) == {
        "dyn_req": {
            "content": {"contents": [{"raw_text": "虚构海底小剧场开饭啦！", "biz_id": "", "type": 1}]},
            "scene": 1,
            "attach_card": None,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
        }
    }
    assert receipt == {
        "platform": "bilibili",
        "code": 0,
        "dynamic_id": "987654321",
        "url": "https://t.bilibili.com/987654321",
    }
    assert "private-" not in str(receipt)


@pytest.mark.parametrize("content", ["海底小剧场（memory:1）", "今天开饭啦[reply:24]", "fictional=1"])
async def test_internal_memory_metadata_never_reaches_platform(platform, content):
    adapter, requests, _ = platform
    with pytest.raises(ValueError, match="内部来源"):
        await publish_text(adapter, content)
    assert requests == []


@pytest.mark.parametrize(
    "payload", [{"code": 0, "data": {}}, {"data": {}}, {"code": 0, "data": {"dyn_id_str": "javascript:bad"}}]
)
async def test_missing_receipt_uncertain_no_retry(platform, payload):
    adapter, requests, outcome = platform
    outcome.clear()
    outcome.update(payload)
    with pytest.raises(PublishUncertain):
        await publish_text(adapter, "公开动态")
    assert sum(r.method == "POST" for r in requests) == 1


async def test_explicit_rejection_safe_and_no_retry(platform):
    adapter, requests, outcome = platform
    outcome.clear()
    outcome.update(code=-352, message="private-session", data=None)
    with pytest.raises(ValueError, match="风控") as caught:
        await publish_text(adapter, "公开动态")
    assert not isinstance(caught.value, PublishUncertain) and "private-session" not in str(caught.value)
    assert sum(r.method == "POST" for r in requests) == 1


async def test_network_loss_after_write_is_uncertain(platform):
    adapter, requests, _ = platform

    def disconnect(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-session")

    async def signed(params):
        return params | {"w_rid": "abc", "wts": "123"}

    adapter.wbi_params = signed
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(disconnect))
    with pytest.raises(PublishUncertain) as caught:
        await publish_text(adapter, "公开动态")
    assert "private-session" not in str(caught.value) and len(requests) == 1


@pytest.mark.parametrize("text", ["", "x" * 501, "bad\x00text", None])
async def test_invalid_body_never_network(platform, text):
    adapter, requests, _ = platform
    with pytest.raises(ValueError):
        await publish_text(adapter, text)
    assert requests == []


async def test_missing_credentials_and_signing_failure_never_write(platform):
    adapter, requests, _ = platform
    adapter._csrf = ""
    with pytest.raises(ValueError):
        await publish_text(adapter, "公开动态")
    assert requests == []
    adapter._csrf = "secret"

    async def failure(params):
        raise ValueError("登录失效")

    adapter.wbi_params = failure
    with pytest.raises(ValueError, match="登录失效"):
        await publish_text(adapter, "公开动态")
    assert requests == []


async def test_guard_after_readonly_signing_prevents_post_with_definite_cancellation(platform):
    adapter, requests, _ = platform

    def stopped():
        assert requests and requests[-1].method == "GET"
        raise PublishCancelled("已停止")

    with pytest.raises(PublishCancelled) as caught:
        await publish_text(adapter, "公开动态", before_send=stopped)
    assert not isinstance(caught.value, PublishUncertain)
    assert not any(r.method == "POST" for r in requests)
