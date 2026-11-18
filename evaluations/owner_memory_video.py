"""Read back and independently grade three private editions of the same video task."""

import asyncio
from uuid import uuid4

from evaluations.owner_memory_device import AUTO, SEARCH, device_call
from tuber_studio.agent.title_form import frame_observations, meets_form, read_title, title_words
from tuber_studio.generation_meter import budgeted_call

BV = "BV1Hy4y1J7xH"
GOAL = (f"请只读取 {BV} P2 的 11 到 14 秒，用其中最接近 13.5 秒的已核实画面给我写一项私有直播文字提纲。"
        "交付标题、选用理由和覆盖缺口。有画面即可成稿，缺少字幕或音轨就标明。"
        "标题沿用已约定的写法，未约定时用末尾无标点的简短名词标题。先别发。")
SELECTION = {"bvid": BV, "ranges": [{"part": 2, "start": 11, "end": 14}]}


async def measure_edition(items, frames, phase, call):
    expected = {"before":"问句", "voice_corrected":"陈述句", "after":"名词短语"}[phase]
    measured = []
    for item in items:
        title = item["title"].strip()
        observations = frame_observations(frames.get(item["primary_frame"], {}))
        reading = await read_title(title, observations, call)
        ending = title[len(title_words(title)):]
        punctuation = {"before":ending in {"?", "？"}, "voice_corrected":ending in {".", "。"},
                       "after":ending == ""}[phase]
        measured.append({"title":title, "ending":ending, "punctuation_matches":punctuation,
                         "expected_form":expected, "reading":reading,
                         "form_matches":meets_form(reading, expected, require_grounding=False),
                         "frame_observations":observations})
    return {"items":measured, "passed":bool(measured) and all(
        row["punctuation_matches"] and row["form_matches"] and row["reading"]["grounded"] for row in measured)}


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
    edit = tasks.edit_delivery
    async def audited_edit(task, evidence):
        try:
            return await edit(task, evidence)
        except Exception as error:
            if audit := task["plan"].get("editor_memory", {}).get("review"):
                record("rejected_writing", {"task_id":task["id"], "message":str(error), "review":audit})
            raise
    tasks.edit_delivery = audited_edit
    tasks.start()
    record("video_request", {"goal":GOAL, "selection":SELECTION})
    async def grader(messages, schema):
        async with runtime.operator_generation("background"):
            limit = min(runtime.settings.data["llm"]["max_tokens"], 1024)
            return await budgeted_call(runtime.llm, lambda: runtime.llm.generate_json(messages, schema, max_tokens=limit),
                                       messages, max_tokens=limit)
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
        result = {"body":other["artifact"]["body"], "citations":other.get("memory_citations", []),
                  "editor_review":plan.get("editor_memory", {}).get("review"), "items":plan["selected"]["items"],
                  "independent_device_body_matched":True}
        record(phase, result)
        result["measurement"] = await measure_edition(result["items"], plan.get("frame_reviews", {}), phase, grader)
        return result
    try:
        before = await edition("before")
        record("before", before)
        assert any(c["id"] == auto["id"] and c["revision"] == auto["revision"] for c in before["citations"])
        updated = await correct_voice(auto)
        voice_observed = False
        if updated:
            corrected = await edition("voice_corrected")
            record("voice_corrected", corrected)
            voice_observed = corrected["measurement"]["passed"] and any(
                c["id"] == updated["id"] and c["revision"] == updated["revision"] for c in corrected["citations"])
            record("voice_style_observed", voice_observed)
        removed = await forget_current(client, port, device, login, [auto, confirmed] + ([updated] if updated else []))
        record("forgotten_revisions", removed)
        after = await edition("after")
        record("after", after)
        assert not after["citations"]
        before_titles = [i["title"] for i in before["items"]]
        after_titles = [i["title"] for i in after["items"]]
        observed = before["measurement"]["passed"] and voice_observed and after["measurement"]["passed"]
        return {"input":"owner-authorized isolated QA preference", "original":AUTO, "before_titles":before_titles,
                "after_titles":after_titles, "semantic_status":"observed" if observed else "unverified"}
    finally:
        await tasks.close()
        tasks.edit_delivery = edit
