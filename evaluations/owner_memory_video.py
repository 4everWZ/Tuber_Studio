"""Run the same scoped public-video task twice in the isolated Studio instance from owner_memory_device."""

import asyncio
from uuid import uuid4

from evaluations.owner_memory_device import AUTO, SEARCH, device_call
from tuber_studio.generation_meter import budgeted_call
from tuber_studio.models import StrictModel

BV = "BV1Hy4y1J7xH"
GOAL = (f"私下为直播写一份 P2 素材提纲，只读取 {BV} 的 11 到 14 秒，取一个实际画面，给出标题、选用理由和覆盖缺口。"
        "按号主对结论与结构的偏好组织；没有相应偏好时先描述画面，再给建议。先别发。")
SELECTION = {"bvid": BV, "ranges": [{"part": 2, "start": 11, "end": 14}]}


class OrderEvidence(StrictModel):
    recommendation: str
    observation: str


async def first_content(runtime, item):
    """Ask for literal, independently checkable spans without telling the reader which edition it is."""
    text = item["title"] + "\n" + item["rationale"]
    messages = [{"role":"system","content":"逐字摘取第一处具体的选用建议或可执行结论，以及第一处具体画面观察。"
                 "两个字段各取一个最短、完整、互不重叠的原文片段。没有则留空，不改写。"},
                {"role":"user","content":text}]
    provider = runtime.llm
    async with runtime.operator_generation("background"):
        output = await budgeted_call(provider, lambda: provider.generate_json(
            messages, OrderEvidence.model_json_schema(), max_tokens=512), messages, max_tokens=512)
    quotes = OrderEvidence.model_validate(output)
    positions = [text.find(term) if term and text.count(term) == 1 else -1
                 for term in (quotes.recommendation, quotes.observation)]
    r, o = positions
    order = "unverified"
    if min(positions) >= 0:
        if r + len(quotes.recommendation) <= o:
            order = "recommendation_first"
        elif o + len(quotes.observation) <= r:
            order = "observation_first"
    return {"order":order, **quotes.model_dump()}


async def compare(client, runtime, port, device, login, confirmed, record):
    # Reproduce a standing instruction supplied by the owner, with its original scope.
    identity = str(uuid4())
    original = "Global Working Rules\n\n" + AUTO
    (await client.post("/api/chat/intelligent", json={"request_id": identity, "message": original,
                                                     "intent": {"action": "chat"}})).raise_for_status()
    record("stage", "organizing_owner_chat_turn")
    await runtime.memory_organizer.run_once()
    found = (await device_call(device, port, login, "POST", SEARCH, {"query": "turn:" + identity}))["value"]["items"]
    record("automatic_lookup", {"source_key":"turn:"+identity, "items":found, "state":runtime.memory_organizer.state()})
    auto, = [row for row in found if row["source_key"] == "turn:" + identity and AUTO in row["quote"]]
    tasks = runtime.tasks
    tasks.start()
    async def edition(phase):
        task_id = str(uuid4())
        result = await client.post("/api/agent/tasks", json={"request_id":task_id, "goal": GOAL, "video_selection": SELECTION})
        result.raise_for_status()
        record("stage", "generating_" + phase)
        async with asyncio.timeout(480):
            while True:
                response = (await client.get("/api/agent/tasks/"+task_id)).raise_for_status().json()
                if response["status"] not in {"queued", "scheduled", "running", "waiting"}:
                    break
                await asyncio.sleep(.5)
        record(phase + "_transport", {"status":response["status"], "tools":[s["tool"] for s in response["steps"]],
               "video_findings":[len(s.get("result",{}).get("findings",[])) for s in response["steps"] if s["tool"]=="inspect_video"]})
        assert response["status"] == "completed" and response["artifact"]
        assert any(s["tool"] == "inspect_video" and s["result"].get("findings") for s in response["steps"])
        other = (await device_call(device, port, login, "GET", "/api/agent/tasks/"+task_id))["value"]
        assert other["artifact"]["body"] == response["artifact"]["body"]
        return {"body":other["artifact"]["body"], "citations":other.get("memory_citations", []),
                "items":tasks.current(task_id)["plan"]["selected"]["items"]}
    try:
        before = await edition("before")
        record("before", before)
        assert auto["id"] in {row["id"] for row in before["citations"]}
        for row in (auto, confirmed):
            await device_call(device, port, login, "DELETE", f"/api/daily-memory/organized/{row['id']}?expected_revision={row['revision']}")
        after = await edition("after")
        record("after", after)
        assert not after["citations"] and before["body"] != after["body"]
        first = await first_content(runtime, before["items"][0])
        second = await first_content(runtime, after["items"][0])
        return {"automatic_source":auto["source_key"], "original":AUTO, "before":before, "after":after,
                "order_evidence":{"before":first, "after":second}, "semantic_status":
                "observed" if (first["order"],second["order"]) == ("recommendation_first","observation_first") else "unverified"}
    finally:
        await tasks.close()
