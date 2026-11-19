import json
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from evaluations.creative_archive_handoff import export

from tuber_studio.agent import task_tools
from tuber_studio.agent.archived_video import ArchivedVideo
from tuber_studio.agent.public_video import clip_path, read_public_part
from tuber_studio.agent.task_delivery import evidence_index
from tuber_studio.agent.task_runtime import ToolRequest
from tuber_studio.agent.task_tools import Search, Video, video_provenance
from tuber_studio.app import create_app
from tuber_studio.web_fetch import WebFetchRequest
from tuber_studio.platforms.bilibili import PlatformReadError

BV = "BV1aa1234567"
URL = f"https://www.bilibili.com/video/{BV}/?p=2"


@pytest.fixture
def package(tmp_path):
    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        pytest.skip("需要本地 FFmpeg 与 FFprobe 解码归档原片")
    file = tmp_path / "example.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x48:r=25",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-bf",
            "0",
            str(file),
        ],
        check=True,
    )
    source = {
        "bvid": BV,
        "part": 2,
        "cid": "27",
        "part_title": "第二段",
        "part_duration_seconds": 80,
        "upload_title": "《示例作品》合集",
        "uploader": "作者",
        "url": URL,
    }
    page = {
        "url": URL,
        "title": source["upload_title"],
        "archived_public_metadata": "投稿《《示例作品》合集》，UP 主：作者。合集 P2《第二段》，CID 27，时长 80 秒。",
    }
    programme = {
        "url": "https://tv.cctv.com/2020/01/01/VIDA100.shtml",
        "title": "示例作品_节目",
        "archived_programme_credits": "节目名：示例作品；导演：某某",
    }
    clip = {
        "file": file.name,
        "source_start_seconds": 20,
        "source_end_seconds_exclusive": 22,
        "frames": 50,
        "fps": 25,
        "file_first_video_pts_seconds": 0,
        "file_last_video_pts_seconds": 1.96,
        "source_url": URL + "&t=20",
    }
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"source": source, "archived_source_pages": [page, programme], "clips": [clip]})
    )
    return ArchivedVideo(path)


async def test_archive_tools_read_pixels_and_preserve_half_open_source_window(package, tmp_path):
    observed = []

    async def observe(task, metadata, frames, question, *, deadline):
        (frame,) = frames
        assert frame["image"].startswith(b"\xff\xd8") and (frame["width"], frame["height"]) == (64, 48)
        observed.append(frame["actual_time"])
        return {"evidence_ids": [frame["id"]], "observations": "测试图像"}

    request = SimpleNamespace(id=str(uuid4()))
    selection = Video(bvid=BV, part=2, start=20, end=21)
    result = await read_public_part(
        request,
        selection,
        SimpleNamespace(analyze_video_evidence=observe),
        lambda: True,
        tmp_path,
        archive=package,
    )
    assert result["source_kind"] == "archived_video" and result["available_windows"] == [[20, 22]]
    assert result["clip"]["archive_url"] == URL + "&t=20" and result["clip"]["frames"] == 25
    assert [f["seconds"] for f in result["findings"]] == observed and all(20 <= s < 21 for s in observed)
    assert all(f["source_kind"] == "archived_video" for f in result["findings"])
    assert clip_path(tmp_path, request.id).stat().st_size > 0
    primary = result["findings"][0]
    body = (
        f"**开场**\n选材理由：测试画面。\n主帧：{BV} · 单帧采样 · 分 P 2 · {primary['seconds']} 秒 · "
        f"<{primary['url']}>\n\n- 第 1 项 · {BV} P2 · 连续取材入点 20 秒、出点 21 秒（不含），保存 25 个连续原帧。"
    )
    with sqlite3.connect(tmp_path / "studio.sqlite3") as db:
        db.execute("CREATE TABLE creative_editions(task_id,task_revision,edition,body)")
        db.execute("CREATE TABLE agent_tasks(id,steps)")
        db.execute("INSERT INTO creative_editions VALUES ('task',3,1,?)", (body,))
        db.execute(
            "INSERT INTO agent_tasks VALUES ('task',?)",
            (json.dumps([{"id": request.id, "revision": 3, "tool": "inspect_video", "result": result}]),),
        )
    out = export(tmp_path, tmp_path / "manifest.json", tmp_path / "handoff", 3, "task")
    assert out.name == "r3-v1" and (out / "edition.md").read_bytes() == body.encode()
    (binding,) = json.loads((out / "evidence.json").read_text(encoding="utf-8"))["items"][0]["bindings"]
    assert binding["source_file"] == "example.mp4" and binding["source_start"] == 20
    assert (out / binding["delivered_file"]).read_bytes() == clip_path(tmp_path, request.id).read_bytes()
    outside = await read_public_part(
        SimpleNamespace(id=str(uuid4())),
        Video(bvid=BV, part=2, start=22, end=24),
        SimpleNamespace(analyze_video_evidence=observe),
        lambda: True,
        tmp_path,
        archive=package,
    )
    assert not outside["findings"] and "clip" not in outside and "clip_error" in outside
    assert outside["selection"]["ranges"] == []
    with pytest.raises(PlatformReadError, match="归档原件"):
        await package.video_stream_frame({}, cid="27", timestamp=22)
    with pytest.raises(PlatformReadError, match="归档原件"):
        await package.video_stream_clip({}, cid="28", start=20, end=21)


def test_archived_pages_retain_original_byline_credits_and_provenance(package):
    links = package.search("示例作品 原出处")["links"]
    (other,) = links
    assert package.page(other)["programme_credits"] == "节目名：示例作品；导演：某某"
    archived = package.page(URL)
    assert archived["source_kind"] == "bilibili_public_metadata" and archived["retrieval"] == "archive"
    assert package.search(BV)["links"] == [URL, other]
    assert package.search(BV, "央视节目库")["links"] == [other]
    steps = [
        {"id": key, "tool": "web_page", "arguments": {"web_fetch": url}, "result": package.page(url)}
        for key, url in (("page", URL), ("origin", other))
    ]
    index = evidence_index(steps)
    assert "UP 主：作者" in index.part_bylines[BV, 2, "27"]
    assert video_provenance(steps, index)[2:] == (["page"], ["origin"])
    assert not package.search("完全无关")["links"]
    assert "error" in package.page("https://example.org/unlisted")


@pytest.mark.parametrize("broken", ["missing", "invalid"])
async def test_source_mount_failure_does_not_block_history_or_other_research(
    settings, monkeypatch, tmp_path, broken
):
    manifest = tmp_path / "manifest.json"
    if broken == "invalid":
        manifest.write_text("{")
    monkeypatch.setenv("TUBER_PRIVATE_SOURCE_MANIFEST", str(manifest))
    monkeypatch.setenv("TUBER_PRIVATE_SOURCE_URL", URL)
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture")
    app = create_app(settings)
    try:
        tasks = app.state.runtime.tasks
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            assert (await client.get("/api/chat/intelligent")).json()["turns"] == []
        task = tasks.create(str(uuid4()), "看看 " + URL)
        step = {
            "tool": "inspect_video",
            "id": str(uuid4()),
            "revision": 1,
            "arguments": {"bvid": BV, "part": 2},
        }
        receipt, _, _ = await tasks._invoke(task, step, (), task["goal"])
        assert "归档来源包不可读取" in receipt["result"]["error"]
        other_url = "https://example.org/other"

        async def fetch(address):
            assert str(address) == other_url
            return {"excerpt": "另一个合法网页"}

        monkeypatch.setattr(task_tools, "fetch_excerpt", fetch)
        for grant in ({BV: {2: None}}, {}, {BV: {1: None}}):
            request = ToolRequest(str(uuid4()), "查 " + other_url, tasks.scope(), None, None, (), grant)
            data = await tasks.tools["web_page"].run(request, WebFetchRequest(web_fetch=other_url))
            assert data == {"excerpt": "另一个合法网页"}
    finally:
        app.state.store.close()


async def test_archive_respects_granted_goal_and_works_across_account_bindings(
    package, settings, monkeypatch, tmp_path
):
    monkeypatch.setenv("TUBER_PRIVATE_SOURCE_MANIFEST", str(tmp_path / "manifest.json"))
    monkeypatch.setenv("TUBER_PRIVATE_SOURCE_URL", URL)
    app = create_app(settings)
    try:
        tasks = app.state.runtime.tasks

        async def ordinary(query):
            return ["https://example.org/ordinary"]

        monkeypatch.setattr(task_tools, "search_public_web", ordinary)
        for account in ("", "123"):
            scope = [account, *tasks.scope()[1:]]
            request = ToolRequest(str(uuid4()), "看 " + URL, scope, None, None, (), {BV: {2: None}})
            assert (await tasks.tools["web_search"].run(request, Search(query=BV)))[
                "source_kind"
            ] == "archived_page_index"
            assert (await tasks.tools["web_page"].run(request, WebFetchRequest(web_fetch=URL)))[
                "retrieval"
            ] == "archive"
            assert (await tasks.tools["web_search"].run(request, Search(query="无关天气")))["links"] == [
                "https://example.org/ordinary"
            ]

            async def read(request, selection, vision, granted, runtime_dir, *, archive):
                return {"bound": archive.source["cid"]}

            monkeypatch.setattr(task_tools, "read_public_part", read)
            assert (await tasks.tools["inspect_video"].run(request, Video(bvid=BV, part=2)))["bound"] == "27"
        for grant in ({BV: {1: None}}, {}, {BV: None}):
            request = ToolRequest(str(uuid4()), "查无关来源", tasks.scope(), None, None, (), grant)
            assert (await tasks.tools["web_search"].run(request, Search(query=BV)))["links"] == [
                "https://example.org/ordinary"
            ]
    finally:
        app.state.store.close()
