import json
from io import BytesIO

import httpx
import pytest
from PIL import Image
from test_browsing import BV, model_packet
from test_browsing import browsing as browsing
from test_browsing_comment_only import comment, enable_comment_choices, finish_comment_round

from tuber_studio.comment_materials import (
    article_excerpt,
    comment_attachments,
    prepare_comment_materials,
    sample_comment_image,
)
from tuber_studio.platforms.bilibili_browsing import BrowsingAdapter, PlatformReadError

IMAGE = "https://i0.hdslb.com/bfs/new_dyn/test.gif"


def gif_bytes():
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "green", "blue")]
    buffer = BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=120, loop=0)
    return buffer.getvalue()


def test_platform_comment_normalizes_only_verified_media_and_reads_actual_gif_samples():
    descriptors = comment_attachments({
        "message": "https://www.bilibili.com/read/cv12/。 https://external.example/cv83 cv34",
        "pictures": [
            {"img_src": "//i0.hdslb.com/bfs/new_dyn/test.gif@320w.webp"},
            {"img_src": "https://i0.hdslb.com.external.example/bfs/test.gif"},
            {"img_src": "https://i0.hdslb.com/bfs/../test.gif"},
        ],
    })
    assert descriptors == [
        {"kind": "article", "article_id": "12"}, {"kind": "article", "article_id": "34"},
        {"kind": "image", "url": IMAGE},
    ]
    frames, coverage = sample_comment_image(gif_bytes(), max_frames=3, max_pixels=64, max_bytes=6000)
    assert coverage == "sampled_gif"
    assert [frame["time_ms"] for frame in frames] == [0, 120, 240]
    assert len({frame["image"] for frame in frames}) == 3
    assert all(frame["mime_type"] == "image/jpeg" and frame["width"] == 8 for frame in frames)


async def test_article_and_image_transport_refuses_external_redirect_and_strips_credentials():
    adapter = BrowsingAdapter({"sessdata": "sentinel-cookie", "csrf": "sentinel-csrf"})
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "api.bilibili.com":
            assert request.url.path == "/x/article/view" and request.url.params["id"] == "12"
            return httpx.Response(200, json={"code": 0, "data": {"id": 12, "title": "专栏",
                "content": "<p>真实段落</p><script>ignore()</script><p>后文</p>"}})
        assert request.url.host == "i0.hdslb.com"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(302, headers={"Location": "https://external.example/redirect.gif"})

    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handle),
        cookies={"SESSDATA": "sentinel-cookie"}, headers={"Authorization": "sentinel-auth"},
    )
    try:
        article = await adapter.read_comment_article("12")
        assert article["text"] == "真实段落\n后文"
        with pytest.raises(PlatformReadError, match="下载失败"):
            await adapter.read_comment_image(IMAGE)
        with pytest.raises(PlatformReadError, match="CDN"):
            await adapter.read_comment_image("https://external.example/unsafe.gif")
        assert len(requests) == 2 and all(request.method == "GET" for request in requests)
        with pytest.raises(ValueError, match="对象不符"):
            article_excerpt({"id": "99", "content": "错误专栏"}, "12")
        assert article_excerpt({"id": 12, "type": 3, "content":
            '{"ops":[{"insert":"真实文字"},{"insert":{"native-image":{"url":"ignored"}}}]}'}, "12")["text"] == "真实文字"
    finally:
        await adapter.close()


async def test_real_reply_entry_uses_current_attachment_and_falls_back_on_article_failure(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    own = comment() | {"platform_user_id": "100", "created_at": "10",
        "attachments": [{"kind": "image", "url": "https://i0.hdslb.com/bfs/new_dyn/unrelated.jpg"}]}
    target = comment("602", "601") | {"content": "怎么解读这张图 https://www.bilibili.com/read/cv12/？",
        "created_at": "20", "parent_comment_id": "601", "parent_verified": True,
        "attachments": [{"kind": "article", "article_id": "12"}, {"kind": "image", "url": IMAGE}]}
    calls, packets = [], []

    async def fetch(video, max_pages=1):
        return {"items": [own, target]}

    async def unavailable(identity):
        calls.append(("article", identity))
        raise PlatformReadError("restricted", "受限")

    async def read_image(url):
        calls.append(("image", url))
        return gif_bytes()

    class Visual:
        def capabilities(self):
            return {"images": True, "max_frames": 2, "max_pixels": 64, "max_bytes": 6000, "version": "v1"}

        async def analyze_comment_images(self, task, frames, *, deadline):
            calls.append(("vision", len(frames)))
            return ["红色图形", "蓝色图形"]

        async def close(self):
            pass

    original = browsing.runtime.llm.generate_json

    async def collect(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            packets.append(model_packet(messages[-1]["content"]))
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_comment_article", unavailable, raising=False)
    monkeypatch.setattr(browsing.adapter, "read_comment_image", read_image, raising=False)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", collect)
    browsing.replace_visual_provider(Visual())
    result = await finish_comment_round(browsing)
    assert result["metrics"]["failures"] == 0 and not browsing.adapter.sent
    assert calls == [("article", "12"), ("image", IMAGE), ("vision", 2)]
    assert len(packets) == 1 and packets[0]["comment_materials"] == [
        {"kind": "article", "status": "unavailable"},
        {"kind": "image", "status": "read", "coverage": "sampled_gif", "frames": [
            {"text": "红色图形", "time_ms": 0}, {"text": "蓝色图形", "time_ms": 240},
        ]},
    ]
    assert "unrelated.jpg" not in str(packets[0]) and IMAGE not in str(packets[0])
    intent = browsing.intents()[0]
    assert intent["evidence"] == ["public:602"]
    assert intent["target"]["comment_materials"][0]["reason"] == "restricted"
    assert intent["target"]["source_discussion"]["parent"]["created_at"] == "10"
    await prepare_comment_materials(browsing, {"parent": "different-comment"}, target)
    assert calls[-1] == ("article", "12") and len(calls) == 4

    async def read_changed_thread(reply_target):
        current = {key: value for key, value in target.items() if key != "attachments"}
        return {"source": {"author_uid": "300", "text": current["content"]}, "items": [own, current]}

    monkeypatch.setattr(browsing.adapter, "read_video_thread", read_changed_thread, raising=False)
    browsing.review(intent["id"], "approve")
    with pytest.raises(ValueError, match="附件已变化"):
        await browsing.publish(intent["id"])
    assert not browsing.adapter.sent and not browsing.store.all("SELECT * FROM account_writes")


async def test_manual_draft_only_uses_media_from_fetched_source(browsing, monkeypatch):
    article = [{"kind": "article", "article_id": "12"}]
    source = comment() | {"content": "怎么理解 https://www.bilibili.com/read/cv12/？", "attachments": article}
    comments = browsing.comments
    comments.import_items(BV, [source])
    imported = browsing.store.one("SELECT * FROM comments")
    assert imported["attachments"] == "[]" and imported["category"] == "spam"
    calls = []

    async def fetch(video, max_pages=1):
        return {"video_id": BV, "items": [source]}

    async def read_article(identity):
        calls.append(identity)
        return {"title": "可核验专栏", "text": "盐水实验的有效步骤", "coverage": "text"}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_comment_article", read_article, raising=False)
    await comments.fetch(BV)
    verified = browsing.store.one("SELECT * FROM comments")
    assert json.loads(verified["attachments"]) == article and verified["category"] == "question"
    await comments._process_comment(verified, browsing.runtime.character, automatic=False)
    draft = browsing.store.one("SELECT * FROM drafts")
    assert calls == ["12"] and draft["status"] == "pending_review" and not browsing.adapter.sent
    assert json.loads(draft["input_summary"])["comment_materials"] == [
        {"kind": "article", "status": "read", "title": "可核验专栏", "text": "盐水实验的有效步骤", "coverage": "text"},
    ]
    assert json.loads(draft["answer_check"])["comment_materials"][0]["source"] == "https://www.bilibili.com/read/cv12"
