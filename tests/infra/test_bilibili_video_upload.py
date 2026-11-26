"""Tests for the native pure-Python Bilibili Web video uploader."""

from __future__ import annotations

import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

import httpx
import pytest

from tuber_studio.infra.platforms.bilibili.client import BILIBILI_WEB_USER_AGENT
from tuber_studio.infra.platforms.bilibili.video_upload import upload_bilibili_video_web

SECRETS = {"sessdata": "secret_sess_123", "csrf": "secret_csrf_456", "uid": "998877"}


def _video_file(tmp_path, size: int = 12) -> tuple[object, object]:
    video = tmp_path / "render.mp4"
    video.write_bytes(b"0123456789AB" if size == 12 else b"X" * size)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"C" * 16)
    return video, cover


async def test_web_upload_pipeline_cover_chunks_and_wbi_submit(tmp_path):
    video, cover = _video_file(tmp_path, size=10)
    seen: list[dict] = []
    uploaded_chunks: list[tuple[int, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "url": str(request.url),
                "ua": request.headers.get("user-agent"),
                "origin": request.headers.get("origin"),
                "referer": request.headers.get("referer"),
                "cookie": request.headers.get("cookie"),
                "upos_auth": request.headers.get("x-upos-auth"),
            }
        )
        if request.url.path == "/x/vu/web/cover/up":
            cookie = SimpleCookie(request.headers["cookie"])
            assert cookie["SESSDATA"].value == SECRETS["sessdata"]
            assert cookie["bili_jct"].value == SECRETS["csrf"]
            assert cookie["DedeUserID"].value == SECRETS["uid"]
            form = parse_qs(request.content.decode())
            assert form["cover"][0].startswith("data:image/png;base64,")
            assert form["csrf"] == [SECRETS["csrf"]]
            return httpx.Response(200, json={"code": 0, "data": {"url": "//i0.hdslb.com/bfs/archive/cov1.png"}})

        if request.url.path == "/preupload":
            assert request.url.params["profile"] == "ugcupos/bup"
            return httpx.Response(
                200,
                json={
                    "OK": 1,
                    "upos_uri": "upos://ugcbup/test_video_001.mp4",
                    "endpoint": "//upos-cs-upcdn.bilivideo.com",
                    "auth": "upos-auth-token",
                    "chunk_size": 4,
                    "biz_id": 778899,
                },
            )

        if request.url.host == "upos-cs-upcdn.bilivideo.com":
            assert request.headers.get("cookie") is None, "Session cookies must not be sent to UPOS CDN"
            assert request.headers.get("x-upos-auth") == "upos-auth-token"
            if request.method == "POST" and "uploads" in request.url.params:
                return httpx.Response(200, json={"OK": 1, "upload_id": "up-123"})
            if request.method == "PUT":
                part = int(request.url.params["partNumber"])
                uploaded_chunks.append((part, request.content))
                return httpx.Response(200, json={"OK": 1})
            if request.method == "POST":
                body = json.loads(request.content.decode())
                assert body == {"parts": [{"partNumber": 1, "eTag": "etag"}, {"partNumber": 2, "eTag": "etag"}, {"partNumber": 3, "eTag": "etag"}]}
                return httpx.Response(200, json={"OK": 1})

        if request.url.path == "/x/vupre/web/archive/pre":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "type_mode": 2,
                        "typelist": [
                            {"id": 188, "name": "科技", "children": [{"id": 95, "name": "数码"}, {"id": 231, "name": "计算机技术"}]},
                            {"id": 160, "name": "生活", "children": [{"id": 21, "name": "日常"}]},
                        ],
                    },
                },
            )

        if request.url.path == "/x/vupre/web/archive/human/type2/list":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "type_list": [
                            {"id": 1011, "name": "人工智能"},
                            {"id": 1012, "name": "科技数码"},
                            {"id": 1030, "name": "生活兴趣"},
                        ]
                    },
                },
            )

        if request.url.path == "/x/vu/web/add/v3":
            assert request.url.params["w_rid"] == "signed_md5"
            assert request.url.params["csrf"] == SECRETS["csrf"]
            payload = json.loads(request.content.decode())
            assert payload["title"] == "测试投稿标题"
            assert payload["tid"] == 231
            assert payload["human_type2"] == 1011
            assert payload["desc_format_id"] == 9999
            assert payload["tag"] == "Aqua,Remotion"
            assert payload["cover"] == "//i0.hdslb.com/bfs/archive/cov1.png"
            assert payload["videos"] == [{"filename": "test_video_001", "title": "测试投稿标题", "desc": ""}]
            assert payload["csrf"] == SECRETS["csrf"]
            return httpx.Response(200, json={"code": 0, "data": {"bvid": "BV1webUpload9", "aid": 456789}})

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def fake_wbi(params):
        return {**params, "w_rid": "signed_md5", "wts": 1760000000}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await upload_bilibili_video_web(
            title="测试投稿标题",
            desc="测试摘要",
            tid=188,
            tags=["Aqua", "Remotion"],
            output_path=video,
            cover_path=cover,
            credentials=SECRETS,
            client=client,
            wbi_signer=fake_wbi,
        )

    assert receipt == {"platform": "bilibili", "uploader": "web_api", "bvid": "BV1webUpload9", "aid": 456789}
    assert [(part, len(data)) for part, data in uploaded_chunks] == [(1, 4), (2, 4), (3, 2)]
    assert b"".join(data for _part, data in uploaded_chunks) == b"X" * 10
    assert all(req["ua"] == BILIBILI_WEB_USER_AGENT for req in seen)
    assert all(req["origin"] == "https://member.bilibili.com" for req in seen)


async def test_web_upload_timeout_on_submit_raises_timeout_and_rejection_scrubs_secrets(tmp_path):
    video, _cover = _video_file(tmp_path, size=4)
    mode = ["timeout"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/preupload":
            return httpx.Response(
                200,
                json={
                    "OK": 1,
                    "upos_uri": "upos://ugcbup/v2.mp4",
                    "endpoint": "https://upos.bilivideo.com",
                    "auth": "tok",
                    "chunk_size": 100,
                    "biz_id": 1,
                },
            )
        if request.url.host == "upos.bilivideo.com":
            if "uploads" in request.url.params:
                return httpx.Response(200, json={"upload_id": "u1"})
            return httpx.Response(200, json={"OK": 1})
        if request.url.path == "/x/vupre/web/archive/pre":
            return httpx.Response(200, json={"code": 0, "data": {"type_mode": 0, "typelist": [{"id": 188, "children": [{"id": 231}]}]}})
        if request.url.path == "/x/vu/web/add/v3":
            if mode[0] == "timeout":
                raise httpx.ReadTimeout(f"timeout for {SECRETS['sessdata']}", request=request)
            return httpx.Response(
                200,
                json={"code": 21001, "message": f"bad token {SECRETS['sessdata']} / {SECRETS['csrf']}"},
            )
        raise AssertionError(f"Unexpected: {request.url}")

    async def fake_wbi(params):
        return {**params, "w_rid": "sig"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError) as exc_timeout:
            await upload_bilibili_video_web(
                title="超时待核对",
                desc="desc",
                tid=188,
                tags=["Aqua"],
                output_path=video,
                credentials=SECRETS,
                client=client,
                wbi_signer=fake_wbi,
            )
        assert SECRETS["sessdata"] not in str(exc_timeout.value)

        mode[0] = "rejected"
        with pytest.raises(RuntimeError) as exc_rejected:
            await upload_bilibili_video_web(
                title="明确拒绝",
                desc="desc",
                tid=188,
                tags=["Aqua"],
                output_path=video,
                credentials=SECRETS,
                client=client,
                wbi_signer=fake_wbi,
            )
        assert "21001" in str(exc_rejected.value)
        assert SECRETS["sessdata"] not in str(exc_rejected.value)
        assert SECRETS["csrf"] not in str(exc_rejected.value)


@pytest.mark.parametrize(
    ("tid_in", "title_in", "tags_in", "expected_tid", "expected_type2"),
    [
        (174, "我是Aqua，一只住在牢大家里的小鲸鱼——先导版", ["Aqua", "本地AI", "DeepSeek"], 231, 1011),
        (174, "周末白饭制作vlog", ["日常", "生活"], 21, 1030),
        (1011, "直接指定新版人工智能分区", ["闲聊"], 231, 1011),
    ],
)
async def test_web_upload_maps_retired_tid_174_and_direct_human_type2(
    tmp_path, tid_in, title_in, tags_in, expected_tid, expected_type2
):
    video, _cover = _video_file(tmp_path, size=4)
    submitted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/preupload":
            return httpx.Response(
                200,
                json={
                    "OK": 1,
                    "upos_uri": "upos://ugcbup/v3.mp4",
                    "endpoint": "https://upos.bilivideo.com",
                    "auth": "tok",
                    "chunk_size": 100,
                    "biz_id": 1,
                },
            )
        if request.url.host == "upos.bilivideo.com":
            if "uploads" in request.url.params:
                return httpx.Response(200, json={"upload_id": "u3"})
            return httpx.Response(200, json={"OK": 1})
        if request.url.path == "/x/vupre/web/archive/pre":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "type_mode": 2,
                        "typelist": [
                            {"id": 188, "name": "科技", "children": [{"id": 95, "name": "数码"}, {"id": 231, "name": "计算机技术"}]},
                            {"id": 160, "name": "生活", "children": [{"id": 21, "name": "日常"}]},
                        ],
                    },
                },
            )
        if request.url.path == "/x/vupre/web/archive/human/type2/list":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "type_list": [
                            {"id": 1011, "name": "人工智能"},
                            {"id": 1012, "name": "科技数码"},
                            {"id": 1030, "name": "生活兴趣"},
                        ]
                    },
                },
            )
        if request.url.path == "/x/vu/web/add/v3":
            submitted.update(json.loads(request.content.decode()))
            return httpx.Response(200, json={"code": 0, "data": {"bvid": "BV1mapped0001", "aid": 789}})
        raise AssertionError(f"Unexpected: {request.url}")

    async def fake_wbi(params):
        return {**params, "w_rid": "sig"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await upload_bilibili_video_web(
            title=title_in,
            desc="desc",
            tid=tid_in,
            tags=tags_in,
            output_path=video,
            credentials=SECRETS,
            client=client,
            wbi_signer=fake_wbi,
        )

    assert receipt["bvid"] == "BV1mapped0001"
    assert submitted["tid"] == expected_tid
    assert submitted["human_type2"] == expected_type2
    assert submitted["desc_format_id"] == 9999
