"""Run the same scoped public-video task twice in the isolated Studio instance from owner_memory_device."""

import asyncio
from uuid import uuid4

from evaluations.owner_memory_device import AUTO, CONFIRMED, SEARCH, device_call

BV = "BV1Hy4y1J7xH"
GOAL = (f"私下为直播写一份 P2 素材提纲，只读取 {BV} 的 11 到 14 秒，取一个实际画面，给出标题、选用理由和覆盖缺口。"
        "按号主对结论与结构的偏好组织；没有相应偏好时先描述画面，再给建议。先别发。")
SELECTION = {"bvid": BV, "ranges": [{"part": 2, "start": 11, "end": 14}]}


async def compare(client, runtime, port, device, login, confirmed):
    # Reproduce a standing instruction supplied by the owner, with its original scope.
    identity = str(uuid4())
    original = "Global Working Rules\n\n" + AUTO
    (await client.post("/api/chat/intelligent", json={"request_id": identity, "message": original,
                                                     "intent": {"action": "chat"}})).raise_for_status()
    await runtime.memory_organizer.run_once()
    found = (await device_call(device, port, login, "POST", SEARCH, {"query": "turn:" + identity}))["value"]["items"]
    auto, = [row for row in found if row["source_key"] == "turn:" + identity and AUTO in row["quote"]]
    tasks = runtime.tasks
    tasks.start()
    async def edition():
        task_id = str(uuid4())
        result = await client.post("/api/agent/tasks", json={"request_id":task_id, "goal": GOAL, "video_selection": SELECTION})
        result.raise_for_status()
        async with asyncio.timeout(480):
            while True:
                response = (await client.get("/api/agent/tasks/"+task_id)).raise_for_status().json()
                if response["status"] not in {"queued", "scheduled", "running", "waiting"}:
                    break
                await asyncio.sleep(.5)
        assert response["status"] == "completed" and response["artifact"]
        assert any(s["tool"] == "inspect_video" and s["result"].get("findings") for s in response["steps"])
        other = (await device_call(device, port, login, "GET", "/api/agent/tasks/"+task_id))["value"]
        assert other["artifact"]["body"] == response["artifact"]["body"]
        return {"body":other["artifact"]["body"], "citations":other.get("memory_citations", []),
                "items":tasks.current(task_id)["plan"]["selected"]["items"]}
    try:
        before = await edition()
        assert auto["id"] in {row["id"] for row in before["citations"]}
        for row in (auto, confirmed):
            await device_call(device, port, login, "DELETE", f"/api/daily-memory/organized/{row['id']}?expected_revision={row['revision']}")
        after = await edition()
        assert not after["citations"] and before["body"] != after["body"]
        return {"automatic_source":auto["source_key"], "original":AUTO, "before":before, "after":after,
                "semantic_comparison":"inspect the actual order of the recommendation and the observation in both editions"}
    finally:
        await tasks.close()
