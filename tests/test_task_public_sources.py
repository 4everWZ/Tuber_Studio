import httpx
import pytest

from tuber_studio.agent import public_sources
from tuber_studio.agent.task_delivery import evidence_index
from tuber_studio.infra.web_client.fetch import programme_credit_text, read_excerpt


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


@pytest.mark.parametrize("host,album,expected", [
    ("tv.cctv.com", "VIDA100", "节目名：春江；节目类型：电视剧；导演：导演甲"),
    ("tv.cctv.com", "VIDE100", None),
    ("example.org", "VIDA100", None),
])
async def test_archive_programme_cites_album_fields_only(host, album, expected):
    body = """<title>春江_CCTV节目官网-电视剧_央视网(cctv.com)</title>
    <div class="text_mod"><h3>春江</h3><p><span>导演：</span>导演甲</p><p>剧情简介</p></div>
    <div class="text_mod"><h3>另一节目</h3><p><span>导演：</span>导演乙</p></div>
    <p><span>导演：</span>访客留言</p>"""
    response = httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())
    url = httpx.URL(f"https://{host}/2012/12/03/{album}.shtml")
    page = await read_excerpt(url, response)
    assert page.get("programme_credits") == expected
    if expected:
        sources = evidence_index([{"id": "page", "tool": "web_page", "result": page}])
        assert sources.display_source("page") == f"{expected} <{url}>"
        historical = {key: value for key, value in page.items() if key != "programme_credits"}
        assert programme_credit_text(historical) == "节目名：春江；节目类型：电视剧"
        assert not programme_credit_text(historical | {"excerpt": ""})
