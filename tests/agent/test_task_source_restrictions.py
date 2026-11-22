import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from evaluations.memory.creative_memory_receipt import WRITE_TABLES, counts
from tests.support.agent_helpers import frame, page, reading, selection
from tuber_studio.agent.tasks import public_sources, public_video
from tuber_studio.agent.tasks.delivery import AdditionalVideoRead, DelegationError, evidence_index
from tuber_studio.agent.tasks.tools import Video, create_task_runtime
from tuber_studio.app import create_app
from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.infra.platforms.bilibili.client import PlatformReadError

BV = "BV1aa1234567"
URL = f"https://www.bilibili.com/video/{BV}/?p=2"


@pytest.mark.parametrize("tool", ["inspect_video", "web_page"])
async def test_412_survives_tool_runtime_and_owner_task_detail(settings, monkeypatch, tool):
    calls = []

    def limited(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(412, text="fixture private response body")

    class Read(BrowsingAdapter):
        def __init__(self):
            super().__init__()
            self.client = httpx.AsyncClient(
                base_url="https://example.org", transport=httpx.MockTransport(limited)
            )

    monkeypatch.setattr(public_sources, "BrowsingAdapter", Read)
    monkeypatch.setattr(public_video, "BrowsingAdapter", Read)
    app = create_app(settings)
    try:
        tasks = app.state.runtime.tasks
        task = tasks.create(str(uuid4()), f"只读 {URL}")
        previous = {
            "id": str(uuid4()),
            "revision": 1,
            "tool": "web_page",
            "arguments": {"web_fetch": "https://example.org/source"},
            "result": {"url": "https://example.org/source", "title": "来源", "excerpt": "已读正文"},
        }
        step = {
            "id": str(uuid4()),
            "revision": 1,
            "tool": tool,
            "arguments": {"bvid": BV, "part": 2} if tool == "inspect_video" else {"web_fetch": URL},
        }
        tasks._save(task, steps=[previous, step], plan={"backend": "responses", "prior_goals": []})
        for _ in range(2):
            await tasks._advance(tasks.current(task["id"]))
        monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer fixture"},
        ) as owner:
            response = await owner.get("/api/v1/agent/tasks/" + task["id"])
        assert response.is_success
        view = response.json()
        assert view["steps"][0] == previous and previous["id"] in evidence_index(view["steps"])
        receipt = view["steps"][1]["result"]
        assert view["status"] == "needs_input" and view["message"] == receipt["error"] == receipt["message"]
        assert receipt["read_restriction"]["http_status"] == 412
        assert all(
            text in view["message"] for text in ("平台限制读取（HTTP 412）", BV, "P2", "请补充", "本任务继续")
        )
        assert not any(text in view["message"] for text in ("参数", "fixture"))
        assert calls == [("GET", "/x/web-interface/view")]
        assert not any(counts(app.state.store.db, WRITE_TABLES).values())
    finally:
        app.state.store.close()


async def test_restricted_frame_preserves_subtitle_and_prior_frame(monkeypatch, tmp_path):
    calls = []

    class Partial:
        async def video_metadata(self, bvid):
            return {"bvid": bvid, "parts": [{"page": 2, "cid": "27", "duration": 16}]}

        async def subtitles(self, bvid, cid):
            return {"segments": [{"start": 5, "end": 6, "text": "已读字幕"}]}

        async def video_stream_frame(self, metadata, *, cid, timestamp):
            calls.append(timestamp)
            if len(calls) == 2:
                raise PlatformReadError.restricted(412)
            return {"id": "first", "cid": cid, "actual_time": timestamp, "presentation_end": timestamp + 0.04}

        async def video_stream_clip(self, *args, **kwargs):
            pytest.fail("读取被限后再次请求了素材")

        async def close(self):
            pass

    async def observe(*args, **kwargs):
        return {"evidence_ids": ["first"], "observations": "已见的画面"}

    monkeypatch.setattr(public_video, "BrowsingAdapter", Partial)
    receipt = await public_video.read_public_part(
        SimpleNamespace(id=str(uuid4())),
        Video(bvid=BV, part=2, start=4, end=10),
        SimpleNamespace(analyze_video_evidence=observe),
        lambda: True,
        tmp_path,
    )
    assert len(calls) == 2 and receipt["error"] is None
    assert [row["text"] for row in receipt["findings"]] == ["已读字幕", "已见的画面"]
    assert receipt["message"] in receipt["gaps"] and receipt["read_restriction"]["http_status"] == 412
    assert not public_sources.video_read_restriction(ValueError("412"), BV, 2, "画面")
    api_error = public_sources.video_read_restriction(
        PlatformReadError("restricted", "API -412"), BV, 2, "画面"
    )
    assert "HTTP" not in api_error["message"]


@pytest.mark.parametrize("repair_supported", [False, True])
async def test_gap_repair_receives_read_scope_and_is_reviewed_again(
    runtime, store, monkeypatch, repair_supported
):
    read = reading(frame(11))
    limitation = "本轮未听审音轨。"
    read["result"]["gaps"] = [limitation]
    read["provided_page_urls"] = ["https://example.org/missing"]
    evidence = evidence_index([read, page("credits")])
    draft = selection("v:1", "credits").model_dump()
    draft["gaps"] = ["原片无声。"]
    observed = []

    async def no_people(fields, *_):
        return dict.fromkeys(fields, [])

    monkeypatch.setattr("tuber_studio.agent.tasks.visual_identity.extract_people", no_people)

    async def editor(messages, schema, *, max_tokens):
        packet = json.loads(messages[-1]["content"])
        if "issues" in packet:
            assert packet["video_read_coverage"]["v"]["read_gaps"] == [limitation]
            assert "credits" in packet["gap_evidence"]
            assert packet["pages_without_body_receipts"] == ["https://example.org/missing"]
            observed.append("repair")
            return {"gaps.0": limitation if repair_supported else "原片无声。"}
        if packet["gaps"]:
            observed.append("review")
            if not repair_supported:
                return {
                    "supported": False,
                    "issues": [{"field": "gaps.0", "quote": "无声", "reason": "画面不能判断实际声音"}],
                }
        return {"supported": True, "issues": []}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)

    monkeypatch.setattr(runtime.operation_llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    task = {
        "goal": "整理画面",
        "plan": {
            "candidate": draft,
            "prior_goals": [],
            "editor_draft": draft,
            "editor_issues": {"gaps.0": "画面不能判断实际声音"},
        },
    }
    if repair_supported:
        result = await tasks.edit_delivery(task, evidence)
        assert result.gaps == [limitation] and result.items[0].model_dump() == draft["items"][0]
    else:
        with pytest.raises(DelegationError, match="gaps.0"):
            await tasks.edit_delivery(task, evidence)
    assert observed == ["repair", "review"]


async def test_continue_before_first_edition_can_request_missing_clips(runtime, store, monkeypatch):
    read = reading(frame(5))
    read["result"]["part_duration"] = 300
    candidate = selection("v:1", primary="v:1").model_dump()
    task = {
        "id": str(uuid4()),
        "goal": "请继续",
        "steps": [read],
        "revision": 2,
        "plan": {"prior_goals": ["准备一支可播放的开场短片"], "candidate": candidate},
    }

    async def brief(messages, schema, **kwargs):
        assert "requested_count" in schema["properties"]
        assert "可播放的开场短片" in messages[-1]["content"]
        return {"requested_count": 1, "usage": "开场", "playable": True, "trace_origin": False}

    monkeypatch.setattr(runtime.llm, "generate_json", brief)

    monkeypatch.setattr(runtime.operation_llm, "generate_json", brief)
    tasks = create_task_runtime(runtime, store, None)
    with pytest.raises(AdditionalVideoRead) as pending:
        await tasks.edit_delivery(task, evidence_index([read]))
    assert pending.value.windows and pending.value.windows[0]["start"] <= 5 < pending.value.windows[0]["end"]
