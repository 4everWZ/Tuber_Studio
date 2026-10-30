"""Fixed platform HTTP contract with a fake transport; never contacts recipients."""

from urllib.parse import parse_qs

import httpx
import pytest

from tuber_studio.bilibili import PublishUncertain
from tuber_studio.bilibili_messages import BilibiliMessagesAdapter


def nav():
    return {
        "code": 0,
        "data": {
            "mid": 900,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
            },
        },
    }


@pytest.mark.parametrize("outcome", ["ok", "timeout", "no_receipt", "rejected", "disabled"])
async def test_text_send_has_one_post_and_explicit_receipt_semantics(outcome):
    calls = []

    async def handle(request):
        calls.append(request)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=nav())
        assert request.url.host == "api.vc.bilibili.com"
        assert request.url.path == "/web_im/v1/web_im/send_msg"
        body = parse_qs(request.content.decode())
        assert body["msg[receiver_id]"] == ["123"]
        assert body["msg[sender_uid]"] == ["900"] and "w_rid" in request.url.params
        if outcome == "timeout":
            raise httpx.ReadTimeout("post uncertain")
        if outcome == "rejected":
            return httpx.Response(200, json={"code": -352, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {"msg_key": "456"} if outcome == "ok" else {}})

    adapter = BilibiliMessagesAdapter({"sessdata": "test-session", "csrf": "test-csrf"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handle)
    )
    try:
        if outcome == "ok":
            result = await adapter.send("123", "私信测试")
            assert result["msg_key"] == "456"
        elif outcome in {"timeout", "no_receipt"}:
            with pytest.raises(PublishUncertain):
                await adapter.send("123", "私信测试")
        else:
            with pytest.raises(ValueError):
                await adapter.send("123", "私信测试", authorized=lambda: outcome != "disabled")
        assert sum(request.method == "POST" for request in calls) == (0 if outcome == "disabled" else 1)
    finally:
        await adapter.close()
