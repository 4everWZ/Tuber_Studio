import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx

from tuber_studio.agent import public_video
from tuber_studio.agent.task_runtime import ToolRequest
from tuber_studio.agent.task_tools import Video
from tuber_studio.app import create_app

BV = "BV1aa1234567"


@pytest.mark.parametrize("part", [2, 3])
async def test_private_anonymous_video_uses_only_requested_part(settings, monkeypatch, part):
    calls = []

    class PublicReads:
        async def video_metadata(self, bvid):
            assert bvid == BV
            return {"bvid": BV, "parts": [{"page": 2, "cid": "27", "duration": 16}]}

        async def subtitles(self, bvid, cid):
            assert (bvid, cid) == (BV, "27")
            return {"segments": [{"start": 5, "end": 6, "text": "区间内"},
                                 {"start": 15, "end": 16, "text": "区间外"}]}

        async def video_stream_frame(self, meta, *, cid, timestamp):
            assert meta["bvid"] == BV and cid == "27" and 4 <= timestamp < 10
            calls.append(timestamp)
            return {"id": str(timestamp), "cid": cid, "actual_time": timestamp, "presentation_end": timestamp + 0.04}

        async def video_stream_clip(self, meta, *, cid, start, end):
            assert (meta["bvid"], cid, start, end) == (BV, "27", 4, 10)
            return {"cid": cid, "start": start, "end": end, "frames": 150, "audio": "visual_only",
                    "movie": b"fixture clip bytes"}

        async def close(self):
            pass

    async def observe(task, meta, frames, question, *, deadline):
        assert time.time() < deadline <= time.time() + 90
        return {"evidence_ids": [frames[0]["id"]], "observations": "看见了道具。"}

    monkeypatch.setattr(public_video, "BrowsingAdapter", PublicReads)
    app = create_app(settings)
    try:
        tasks = app.state.runtime.tasks
        scope = tasks.scope()
        assert not scope[0]
        app.state.browsing.evidence.visual = SimpleNamespace(analyze_video_evidence=observe)
        request = ToolRequest(str(uuid4()), "只读给定分 P", scope, None, None)
        result = await tasks.tools["inspect_video"].run(request, Video(bvid=BV, part=part, start=4, end=10))
        if part == 3:
            assert not calls and "没有指定" in result["error"]
        else:
            assert len(calls) == 3 and len(result["findings"]) == 4
            assert all(row["cid"] == "27" and "?p=2&t=" in row["url"] for row in result["findings"])
            assert result["findings"][0]["text"] == "区间内" and result["status"] == "partial"
            task = tasks.create(str(uuid4()), "私有素材")
            step = {"id": request.id, "revision": 1, "tool": "inspect_video", "result": result}
            tasks._save(task, steps=[step])
            monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture-owner")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as device:
                path = f"/api/agent/tasks/{task['id']}/clips/{request.id}"
                assert (await device.get(path)).status_code == 401
                device.headers["Authorization"] = "Bearer fixture-owner"
                assert (await device.get(path)).content == b"fixture clip bytes"
                other = tasks.create(str(uuid4()), "另一个目标")
                assert (await device.get(path.replace(task["id"], other["id"]))).status_code == 404
    finally:
        app.state.store.close()
