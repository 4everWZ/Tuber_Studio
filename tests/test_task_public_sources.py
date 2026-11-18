import httpx
import pytest

from tuber_studio.agent import public_sources
from tuber_studio.agent.task_delivery import evidence_index
from tuber_studio.web_fetch import read_excerpt


async def test_source_is_bound_to_requested_part(monkeypatch):
    class Submission:
        async def video_metadata(self, bvid):
            return {"bvid": bvid, "title": "合集", "owner": "作者", "description": "投稿简介",
                    "parts": [{"page": 2, "title": "投稿第二段", "duration": 40, "cid": "200"}]}

        async def close(self):
            pass

    monkeypatch.setattr(public_sources, "BrowsingAdapter", Submission)
    url = "https://www.bilibili.com/video/BV1Hy4y1J7xH/"
    page = await public_sources.fetch_task_source(url + "?p=2", None)
    assert "P2《投稿第二段》，CID 200" in page["excerpt"]
    assert page["url"] == url + "?p=2"
    with pytest.raises(ValueError):
        await public_sources.fetch_task_source(url + "?p=3", None)
    with pytest.raises(ValueError):
        await public_sources.fetch_task_source(url + "?p=2&p=1", None)


async def test_programme_credits_are_literal_fields_on_cctv_only():
    body = """<title>节目</title><script>
    var video_album_title = '节目名';
    var play_director = '导演甲';
    var unrelated = '其他内容';
    </script><p>选集</p>"""
    async def excerpt(host):
        response = httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())
        return await read_excerpt(httpx.URL(f"https://{host}/v/album.html"), response)

    programme = await excerpt("tv.cctv.com")
    assert programme["excerpt"] == "节目名：节目名 导演：导演甲 选集"
    assert programme["programme_credits"] == "节目名：节目名；导演：导演甲"
    sources = evidence_index([{"id": "read", "tool": "web_page", "result": programme}])
    assert sources.display_source("read") == "节目名：节目名；导演：导演甲 <https://tv.cctv.com/v/album.html>"
    other_page = await excerpt("example.org")
    assert other_page["excerpt"] == "选集"
    assert "programme_credits" not in other_page
