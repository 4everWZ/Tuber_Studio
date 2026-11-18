"""Run the same scoped public-video task twice in the isolated Studio instance from owner_memory_device."""

import asyncio
from uuid import uuid4

from evaluations.owner_memory_device import AUTO, SEARCH, device_call

BV = "BV1Hy4y1J7xH"
GOAL = (f"私下为直播写一份 P2 素材提纲，只读取 {BV} 的 11 到 14 秒，取一个实际画面，给出标题、选用理由和覆盖缺口。"
        "按号主已有的标题偏好组织。没有相应偏好时，使用不带末尾标点的简短名词标题。先别发。")
SELECTION = {"bvid": BV, "ranges": [{"part": 2, "start": 11, "end": 14}]}


async def forget_current(client, port, device, login, sources):
    latest = {}
    for source in sources:
        row = latest.get(source["id"])
        if row is None or source["revision"] > row["revision"]:
            latest[source["id"]] = source
    for identity, expected in latest.items():
        current = (await client.post("/api/agent/memory.get", json={"id":identity})).raise_for_status().json()
        if current["revision"] != expected["revision"]:
            raise RuntimeError("偏好在本次已核实的修订之外发生变化；未执行撤回")
        await device_call(device, port, login, "DELETE",
                          f"/api/daily-memory/organized/{identity}?expected_revision={current['revision']}")
    return {identity:row["revision"] for identity,row in latest.items()}


async def compare(client, runtime, port, device, login, confirmed, record, correct_voice):
    # Reproduce a standing instruction supplied by the owner, with its original scope.
    identity = str(uuid4())
    original = AUTO
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
        if response["status"] != "completed" or not response["artifact"]:
            current = tasks.current(task_id)
            record(phase + "_diagnostic", {"status":response["status"], "message":response.get("message",current["message"]),
                   "steps":[{"tool":s["tool"], "arguments":s.get("arguments"),
                             "result":{k:v for k,v in (s.get("result") or {}).items()
                                       if k in {"status","error","gaps","viewing","selection","findings","clip_error"}}}
                            for s in current["steps"]]})
            raise RuntimeError(f"Private video {phase} ended in {response['status']}; see task diagnostic")
        assert any(s["tool"] == "inspect_video" and s["result"].get("findings") for s in response["steps"])
        other = (await device_call(device, port, login, "GET", "/api/agent/tasks/"+task_id))["value"]
        assert other["artifact"]["body"] == response["artifact"]["body"]
        return {"body":other["artifact"]["body"], "citations":other.get("memory_citations", []),
                "items":tasks.current(task_id)["plan"]["selected"]["items"]}
    try:
        before = await edition("before")
        record("before", before)
        assert auto["id"] in {row["id"] for row in before["citations"]}
        updated = await correct_voice(auto)
        if updated:
            corrected = await edition("voice_corrected")
            record("voice_corrected", corrected)
            record("voice_style_observed", all(i["title"].endswith(("。",".")) for i in corrected["items"])
                   and any(c["id"] == updated["id"] and c["revision"] == updated["revision"] for c in corrected["citations"]))
        removed = await forget_current(client, port, device, login, [auto, confirmed] + ([updated] if updated else []))
        record("forgotten_revisions", removed)
        after = await edition("after")
        record("after", after)
        assert not after["citations"]
        before_titles = [i["title"] for i in before["items"]]
        after_titles = [i["title"] for i in after["items"]]
        question_titles = all(t.endswith(("?", "？")) for t in before_titles)
        default_titles = all(not t.endswith(tuple("？?。.!！")) for t in after_titles)
        return {"input":"owner-authorized isolated QA preference", "original":AUTO, "before_titles":before_titles,
                "after_titles":after_titles, "semantic_status":"observed" if question_titles and default_titles else "unverified"}
    finally:
        await tasks.close()
