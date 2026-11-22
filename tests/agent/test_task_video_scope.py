import json
from uuid import uuid4

import httpx
import pytest

from tests.support.agent_helpers import call, gateway
from tests.support.operations_helpers import BV, ControlledPlatform
from tuber_studio.agent.chat import VideoSelection
from tuber_studio.agent.tasks.delegation import visible_steps
from tuber_studio.agent.tasks.delivery import evidence_index
from tuber_studio.agent.tasks.runtime import ToolRequest, ToolWait
from tuber_studio.agent.tasks.tools import Video
from tuber_studio.agent.tasks.video_scope import permits, video_binding
from tuber_studio.app import create_app

OTHER = "BV9876543210"
SELECTION = {"bvid": BV, "ranges": [{"part": 2, "start": 10, "end": 20},
                                   {"part": 2, "start": 25, "end": 30},
                                   {"part": 3, "start": 40, "end": 50}]}


def test_text_mentions_bind_parts_to_individual_videos():
    task = {"goal": f"{BV} 的 P2 和 {OTHER}/?p=3", "plan": {"prior_goals": []}}
    bounds = video_binding(task)
    for video, expected in [(BV, 2), (OTHER, 3)]:
        assert permits(bounds, {"bvid": video, "part": expected})
        assert not permits(bounds, {"bvid": video, "part": 5 - expected})
        assert not permits(bounds, {"bvid": video})


async def test_pending_step_rechecked_before_any_domain_call(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    try:
        task = tasks.create(str(uuid4()), "按所选时段取材", video_selection=VideoSelection(**SELECTION))
        tasks._save(task, steps=[{"id": str(uuid4()), "tool": "inspect_video", "arguments": {"bvid": BV, "part": 2},
                                  "revision": task["revision"]}])
        await tasks._advance(tasks.current(task["id"]))
        assert tasks.current(task["id"])["status"] == "failed"
        assert not app.state.store.one("SELECT 1 FROM browsing_rounds")
    finally:
        app.state.store.close()


@pytest.mark.parametrize(("arguments", "allowed"), [
    ({"bvid": BV, "part": 2, "start": 11, "end": 20}, True),
    ({"bvid": BV, "part": 3, "start": 40, "end": 50}, True),
    ({"bvid": OTHER, "part": 2, "start": 11, "end": 20}, False),
    ({"bvid": BV, "part": 3, "start": 11, "end": 20}, False),
    ({"bvid": BV, "part": 2, "start": 19, "end": 26}, False),
    ({"bvid": BV, "part": 2}, False),
    ({"bvid": BV}, False),
])
async def test_selection_admits_only_subsets_before_scheduling(settings, monkeypatch, arguments, allowed):
    def model(payload):
        assert json.loads(payload["input"][1]["content"])["video_selection"] == SELECTION
        return [call("inspect_video", arguments, "read")]

    gateway(settings, monkeypatch, model)
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    identity = str(uuid4())
    try:
        task = tasks.create(identity, f"对照 {OTHER} 的例子，只看选区", video_selection=VideoSelection(**SELECTION))
        await tasks._advance(task)
        result = tasks.current(identity)
        assert bool(result["steps"]) == allowed
        assert result["status"] == ("queued" if allowed else "failed")
    finally:
        app.state.store.close()


async def test_chat_selection_survives_steer_and_replacement_filters_old_evidence(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    identity = str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        payload = {"request_id": identity, "message": "用这些片段准备开场", "intent": {"action": "agent_task"},
                   "video_selection": SELECTION}
        first = await client.post("/api/chat/intelligent", json=payload)
        assert first.raise_for_status().json()["card"]["data"]["id"] == identity
        changed = await client.post("/api/chat/intelligent", json=payload | {"video_selection": {"bvid": OTHER}})
        assert changed.status_code == 409
        update = {"message": "只留一句", "intent": {"action": "steer_task", "reference": identity}}
        (await client.post("/api/chat/intelligent", json=update | {"request_id": str(uuid4())})).raise_for_status()
        previous = tasks.current(identity)
        assert previous["plan"]["video_selection"] == SELECTION and previous["revision"] == 2
        narrow = {"bvid": BV, "ranges": [{"part": 2, "start": 12, "end": 17}]}
        response = await client.post("/api/chat/intelligent", json=update | {"request_id": str(uuid4()), "video_selection": narrow})
        response.raise_for_status()
        edited = tasks.current(identity)
        assert edited["plan"]["video_selection"] == narrow and edited["revision"] == 3
        findings = [{"part": 2, "seconds": seconds, "text": str(seconds),
                     "url": f"https://www.bilibili.com/video/{BV}/?p=2&t={seconds}"} for seconds in (10, 12, 17)]
        step = {"id": "read", "revision": 1, "tool": "inspect_video",
                "arguments": {"bvid": BV, "part": 2, "start": 10, "end": 20},
                "result": {"findings": findings, "status": "completed", "comments": "outside"}}
        context = visible_steps(tasks, edited | {"steps": [step]})
        assert set(evidence_index(context)) == {"read:2"}
        assert "outside" not in str(context)
        assert visible_steps(tasks, edited | {"steps": [step | {"arguments": {"bvid": OTHER}}]}) == []
    app.state.store.close()


@pytest.mark.parametrize("start,end", [(1, 5), (1, 12), (7.5, 9), (10, 12), (None, None)])
async def test_part_duration_checked_once_and_gap_survives_recovery(settings, monkeypatch, start, end):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    browsing, tasks = app.state.browsing, app.state.runtime.tasks
    browsing.discovery.remember_account("100")
    readings = []

    async def metadata(bvid):
        readings.append(bvid)
        return {"bvid": bvid, "duration": 500, "parts": [{"page": 1, "duration": 65}, {"page": 2, "duration": 7.5}]}

    monkeypatch.setattr(platform, "video_metadata", metadata)
    identity = str(uuid4())
    request = ToolRequest(identity, "读 P2", tasks.scope(), None, None)
    run = tasks.tools["inspect_video"].run
    args = Video(bvid=BV, part=2, start=start, end=end)
    try:
        first = await run(request, args)
        if start is not None and start >= 7.5:
            assert first["error"] and first["part_duration"] == 7.5 and first["requested_range"]["start"] == start
            assert browsing.repo.round(identity) is None and readings == [BV]
            return
        assert isinstance(first, ToolWait)
        assert isinstance(await run(request, args), ToolWait)
        monkeypatch.setattr(browsing, "full_video_status", lambda row: {
            "status": "completed", "selection": row["config"]["operator_selection"],
            "comments": None, "coverage": {}, "error": None})
        receipt = await run(request, args)
        assert readings == [BV]
        assert receipt["selection"]["ranges"] == [{"part": 2, "start": start or 0, "end": min(end or 7.5, 7.5)}]
        if end is not None and end > 7.5:
            assert receipt["status"] == "partial" and receipt["part_duration"] == 7.5
            assert receipt["requested_range"] == {"part": 2, "start": start, "end": end}
            assert receipt["uncovered_range"] == {"part": 2, "start": 7.5, "end": end}
        else:
            assert receipt["status"] == "completed" and "uncovered_range" not in receipt
    finally:
        app.state.store.close()
