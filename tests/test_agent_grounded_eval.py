"""Independent scripted failures of the Agent's decision model."""

import asyncio
from uuid import uuid4

import pytest

from tuber_studio.agent.task_runtime import TaskDecision, TaskRuntime, Tool
from tuber_studio.models import StrictModel
from tuber_studio.store import Store


async def read_terminal(tasks, identity, wanted=None):
    terminal = {"completed", "failed", "needs_input", "awaiting_approval"}
    async with asyncio.timeout(5):
        while (row := tasks.current(identity))["status"] not in (wanted or terminal):
            await asyncio.sleep(0.01)
    return row


async def run_script(tmp_path, goal, plan, receipts):
    store = Store(tmp_path / "agent-eval.sqlite3", [])
    remaining = iter(plan)

    async def decide(current_goal, steps, tools):
        return next(remaining)

    tasks = TaskRuntime(store, lambda: ["held-out-account", "held-out-role", 1, "eval"], decide)
    calls = []

    def make_tool(name, values):
        values = iter(values)

        async def reply(request, arguments):
            calls.append(name)
            return next(values)

        return Tool(name, StrictModel, reply)

    for name, values in receipts.items():
        tasks.register(name, make_tool(name, values))
    identity = str(uuid4())
    tasks.start()
    try:
        tasks.create(identity, goal)
        return await read_terminal(tasks, identity), calls
    finally:
        await tasks.close()
        store.close()


def call(tool):
    return TaskDecision(action="tool", tool=tool)


SOURCE = "https://example.com/public/material-83"
EXCERPT = "该预告只包含字幕，正式场次尚未公布。"
FALSE_SUMMARY = "全片逐帧核验通过，已经成功推流并送达各账号。"


async def test_multi_step_finish_uses_source_receipts_over_wrong_model_prose(tmp_path):
    video = {"status": "completed", "viewing": "字幕取材", "findings": [
        {"part": 1, "seconds": 17, "text": EXCERPT, "kind": "字幕", "url": SOURCE},
    ], "url": SOURCE}
    live = {"state": "stopped", "muted": True, "session_id": None, "native_program": {"state": "idle"}}
    task, calls = await run_script(
        tmp_path, "请先用 inspect_video 看取材覆盖，再用 live_status 核对节目是否已播，最后按所见报告。",
        [call("inspect_video"), call("live_status"), TaskDecision(action="finish", message=FALSE_SUMMARY)],
        {"inspect_video": [video], "live_status": [live]},
    )
    assert calls == ["inspect_video", "live_status"]
    assert [item["result"] for item in task["steps"]] == [video, live]
    assert task["status"] == "completed"
    assert FALSE_SUMMARY not in task["message"]
    assert SOURCE in task["message"] and "字幕" in task["message"]
    assert any(word in task["message"] for word in ("停止", "未开播", "stopped"))


@pytest.mark.parametrize("useful_receipt", [False, True], ids=["empty", "partly-grounded"])
async def test_finish_cannot_skip_explicit_evidence(tmp_path, useful_receipt):
    plan = [call("operation_status")] if useful_receipt else []
    task, calls = await run_script(
        tmp_path, "通过 operation_status 和 web_page 核对两个来源，再告诉我是否已安排。",
        [*plan, TaskDecision(action="finish", message="两个来源都已确认并完成安排。")],
        {"operation_status": [{"state": "off"}], "web_page": [{"url": SOURCE, "excerpt": EXCERPT}]},
    )
    assert calls == (["operation_status"] if useful_receipt else [])
    assert task["status"] == "failed"
    assert task["message"] and "两个来源都已确认并完成安排" not in task["message"]


@pytest.mark.parametrize("retry", [False, True], ids=["unresolved", "read-recovered"])
async def test_partial_error_requires_resolution_and_keeps_original_receipt(tmp_path, retry):
    unreadable = {"error": "来源读取超时，未取得正文"}
    readable = {"url": SOURCE, "title": "节目说明", "excerpt": EXCERPT, "truncated": False}
    plan = [call("operation_status"), call("web_page"), *([call("web_page")] if retry else [])]
    task, calls = await run_script(
        tmp_path, "先查 operation_status，再查 web_page。清楚写出取得的材料和缺口。",
        [*plan, TaskDecision(action="finish", message="运营正在自动发布，页面已读完。")],
        {"operation_status": [{"state": "off"}], "web_page": [unreadable, readable]},
    )
    assert calls == [item.tool for item in plan]
    assert task["steps"][1]["result"] == unreadable
    assert "运营正在自动发布" not in task["message"]
    if retry:
        assert task["status"] == "completed"
        assert task["steps"][2]["result"] == readable
        assert SOURCE in task["message"] and "未公布" in task["message"]
    else:
        assert task["status"] == "failed"
        assert any(word in task["message"] for word in ("超时", "缺口", "失败", "未完成"))


async def test_steered_read_cannot_reuse_an_old_approval_or_receipt(tmp_path):
    store = Store(tmp_path / "agent-eval.sqlite3", [])
    plans = iter([
        call("operation_status"), call("game_action"),
        TaskDecision(action="finish", message="旧的运营回执可证明新要求已核对。"),
    ])

    async def decide(goal, steps, tools):
        return next(plans)

    task = TaskRuntime(store, lambda: ["fixture-owner"], decide)
    performed = []

    async def read(request, args):
        return {"state": "off"}

    async def perform(request, args):
        performed.append(request.id)
        return {"success": True}

    task.register("operation_status", Tool("读运营模式", StrictModel, read))
    task.register("game_action", Tool("拟议动作", StrictModel, perform, review=lambda args: {"target": "模拟地块"}))
    identity = str(uuid4())
    task.start()
    try:
        task.create(identity, "先查 operation_status，再拟一份游戏操作。")
        waiting = await read_terminal(task, identity, {"awaiting_approval"})
        old_step = waiting["steps"][-1]["id"]
        assert performed == []
        await task.control(identity, 1, "steer", "改成只读。请重新查询 operation_status。")
        with pytest.raises(ValueError, match="待确认步骤已变化"):
            await task.control(identity, 2, "approve", step_id=old_step)
        result = await read_terminal(task, identity)
        assert performed == []
        assert result["revision"] == 2
        assert result["steps"][0]["revision"] == 1
        assert result["status"] == "failed"
        assert "旧的运营回执可证明新要求已核对" not in result["message"]
    finally:
        await task.close()
        store.close()


async def test_search_candidate_plus_explicit_page_read_can_complete(tmp_path):
    task, calls = await run_script(
        tmp_path, "先运行 web_search 找一条公告，再用 web_page 核对实际内容，分别列出发现和已读来源。",
        [call("web_search"), call("web_page"), TaskDecision(action="finish", message="搜索结果都已验证。")],
        {"web_search": [{"links": [SOURCE]}], "web_page": [{"url": SOURCE, "excerpt": EXCERPT}]},
    )
    assert calls == ["web_search", "web_page"]
    assert task["status"] == "completed"
    assert "候选" in task["message"] and SOURCE in task["message"]
    assert "搜索结果都已验证" not in task["message"]


async def test_approved_live_handoff_with_no_playback_receipt_is_not_delivered(tmp_path):
    store = Store(tmp_path / "agent-eval.sqlite3", [])
    offered = iter([call("live_handoff"), TaskDecision(action="finish", message="玩家听到了，已完整播完。")])
    actions = []

    async def decide(goal, steps, tools):
        return next(offered)

    async def handoff(request, arguments):
        actions.append(request.id)
        return {"status": "cancelled", "spoken": ""}

    tasks = TaskRuntime(store, lambda: ["fixture-owner"], decide)
    tasks.register("live_handoff", Tool("向节目交接", StrictModel, handoff, review=lambda args: {"target": "模拟节目"}))
    task_id = str(uuid4())
    tasks.start()
    try:
        tasks.create(task_id, "把公开材料交给节目，收到播完回执再确认。")
        waiting = await read_terminal(tasks, task_id, {"awaiting_approval"})
        assert not actions
        step_id = waiting["steps"][-1]["id"]
        await tasks.control(task_id, 1, "approve", step_id=step_id)
        outcome = await read_terminal(tasks, task_id)
        assert actions == [step_id]
        assert outcome["steps"][-1]["result"] == {"status": "cancelled", "spoken": ""}
        assert outcome["status"] == "failed"
        assert "未收到播放器的播完确认" in outcome["message"]
        assert "玩家听到了" not in outcome["message"]
    finally:
        await tasks.close()
        store.close()
