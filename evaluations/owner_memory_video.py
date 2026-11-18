"""Run the same scoped public-video task twice in the isolated Studio instance from owner_memory_device."""

import asyncio
from uuid import uuid4

from evaluations.owner_memory_device import AUTO, SEARCH, device_call

BV = "BV1Hy4y1J7xH"
GOAL = (f"请只读取 {BV} P2 的 11 到 14 秒，用其中最接近 13.5 秒的已核实画面给我写一项私有直播文字提纲。"
        "交付标题、选用理由和覆盖缺口。有画面即可成稿，缺少字幕或音轨就标明。"
        "标题沿用已约定的写法，未约定时用末尾无标点的简短名词标题。先别发。")
SELECTION = {"bvid": BV, "ranges": [{"part": 2, "start": 11, "end": 14}]}


def task_diagnostic(task):
    return {"status":task["status"], "message":task["message"], "revision":task["revision"],
            "steps":[{"tool":step["tool"], "arguments":step.get("arguments"),
                      "result":{k:v for k,v in (step.get("result") or {}).items()
                                if k in {"status","error","gaps","viewing","selection","findings","clip_error"}}}
                     for step in task["steps"]]}


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
    record("video_request", {"goal":GOAL, "selection":SELECTION})
    async def edition(phase):
        task_id = str(uuid4())
        result = await client.post("/api/agent/tasks", json={"request_id":task_id, "goal": GOAL, "video_selection": SELECTION})
        result.raise_for_status()
        record("stage", "generating_" + phase)
        try:
            async with asyncio.timeout(480):
                while True:
                    response = (await client.get("/api/agent/tasks/"+task_id)).raise_for_status().json()
                    if response["status"] not in {"queued", "scheduled", "running", "waiting"}:
                        break
                    await asyncio.sleep(.5)
        except TimeoutError:
            record(phase + "_diagnostic", task_diagnostic(tasks.current(task_id)))
            raise
        reads = [s["result"] for s in response["steps"] if s["tool"]=="inspect_video" and s.get("result")]
        record(phase + "_transport", {"status":response["status"], "tools":[s["tool"] for s in response["steps"]],
               "video_findings":[len(r.get("findings",[])) for r in reads],
               "frames":[{k:f.get(k) for k in ("part","cid","seconds","kind")} for r in reads
                         for f in r.get("findings",[]) if f.get("kind") == "实际视频帧"]})
        if response["status"] != "completed" or not response["artifact"]:
            record(phase + "_diagnostic", task_diagnostic(tasks.current(task_id)))
            raise RuntimeError(f"Private video {phase} ended in {response['status']}; see task diagnostic")
        assert any(s["tool"] == "inspect_video" and s["result"].get("findings") for s in response["steps"])
        other = (await device_call(device, port, login, "GET", "/api/agent/tasks/"+task_id))["value"]
        assert other["artifact"]["body"] == response["artifact"]["body"]
        plan = tasks.current(task_id)["plan"]
        return {"body":other["artifact"]["body"], "citations":other.get("memory_citations", []),
                "editor_review":plan.get("editor_memory", {}).get("review"), "items":plan["selected"]["items"]}
    try:
        before = await edition("before")
        record("before", before)
        assert auto["id"] in {row["id"] for row in before["citations"]}
        updated = await correct_voice(auto)
        if updated:
            corrected = await edition("voice_corrected")
            record("voice_corrected", corrected)
            record("voice_style_observed", bool(corrected["items"]) and all(i["title"].endswith(("。",".")) for i in corrected["items"])
                   and any(c["id"] == updated["id"] and c["revision"] == updated["revision"] for c in corrected["citations"]))
        removed = await forget_current(client, port, device, login, [auto, confirmed] + ([updated] if updated else []))
        record("forgotten_revisions", removed)
        after = await edition("after")
        record("after", after)
        assert not after["citations"]
        before_titles = [i["title"] for i in before["items"]]
        after_titles = [i["title"] for i in after["items"]]
        question_titles = bool(before_titles) and all(t.endswith(("?", "？")) for t in before_titles)
        default_titles = bool(after_titles) and all(not t.endswith(tuple("？?。.!！")) for t in after_titles)
        return {"input":"owner-authorized isolated QA preference", "original":AUTO, "before_titles":before_titles,
                "after_titles":after_titles, "semantic_status":"observed" if question_titles and default_titles else "unverified"}
    finally:
        await tasks.close()
