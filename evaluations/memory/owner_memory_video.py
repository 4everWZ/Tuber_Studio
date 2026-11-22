"""Read back and independently grade three private editions of the same video task."""

import asyncio
import re
from uuid import uuid4

from evaluations.memory.owner_memory_device import AUTO, SEARCH, device_call
from evaluations.memory.owner_memory_editor_trace import editor_requests
from tuber_studio.agent.tasks.title_form import frame_observations, meets_form, read_title, title_words
from tuber_studio.core.meter import budgeted_call
from tuber_studio.infra.inference.http_api import InferenceBudgetExceeded, InferenceQueueBusy

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
            "reviewed_draft":task["plan"].get("editor_draft"), "field_issues":task["plan"].get("editor_issues"),
            "attribution_rejections":task["plan"].get("attribution_rejections", []),
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
        current = (await client.post("/api/v1/agent/memory/get", json={"id":identity})).raise_for_status().json()
        if current["revision"] != expected["revision"]:
            raise RuntimeError("偏好在本次已核实的修订之外发生变化；未执行撤回")
        await device_call(device, port, login, "DELETE",
                          f"/api/v1/persona/daily-memory/organized/{identity}?expected_revision={current['revision']}")
    return {identity:row["revision"] for identity,row in latest.items()}


async def await_admission(operation, call, deadline, receipts, record):
    loop = asyncio.get_running_loop()
    while True:
        if loop.time() >= deadline:
            record("deadline_diagnostic", {"operation": operation, "reason": "run deadline expired"})
            raise TimeoutError("等待网关准入已到总期限")
        try:
            async with asyncio.timeout_at(deadline):
                return await call()
        except InferenceQueueBusy as busy:
            remaining = max(0, deadline - loop.time())
            wait = {"operation":operation, "retry_after":busy.retry_after, "remaining_seconds":remaining}
            receipts.append(wait)
            record("queue_waits", receipts)
            if busy.retry_after >= remaining:
                record("deadline_diagnostic", wait | {"reason":"Retry-After exceeds remaining run time"})
                raise TimeoutError("总期限内无法按网关 Retry-After 再次准入") from busy
            await asyncio.sleep(busy.retry_after)
        except InferenceBudgetExceeded as error:
            record("budget_diagnostic", {"operation":operation, "message":str(error)})
            raise


async def compare(client, runtime, port, device, login, confirmed, record, correct_voice, deadline):
    queue_receipts = []
    # Reproduce a standing instruction supplied by the owner, with its original scope.
    identity = str(uuid4())
    original = AUTO
    (await client.post("/api/v1/agent/chat", json={"request_id": identity, "message": original,
                                                     "intent": {"action": "chat"}})).raise_for_status()
    record("saved_owner_turn", {"source_key":"turn:"+identity, "quote":original})
    record("stage", "organizing_owner_chat_turn")
    await await_admission("organize turn:"+identity, runtime.memory_organizer.run_once, deadline, queue_receipts, record)
    found = (await device_call(device, port, login, "POST", SEARCH, {"query": "turn:" + identity}))["value"]["items"]
    record("automatic_lookup", {"source_key":"turn:"+identity, "items":found, "state":runtime.memory_organizer.state()})
    auto, = [row for row in found if row["source_key"] == "turn:" + identity and AUTO in row["quote"]]
    preserved = (await client.post("/api/v1/agent/memory/get", json={"id":confirmed["id"]})).raise_for_status().json()
    record("independent_preferences", {"manual": preserved, "automatic": auto})
    assert auto["id"] != confirmed["id"] and preserved == confirmed
    tasks = runtime.tasks
    edit = tasks.edit_delivery
    phase_for_task = {}
    editor_calls = {}
    async def audited_edit(task, evidence):
        phase = phase_for_task[task["id"]]
        def publish(receipt):
            editor_calls.setdefault(phase, []).append(receipt)
            record(phase + "_editor_requests", editor_calls[phase])
        try:
            with editor_requests(runtime.llm, task["goal"], runtime.character["name"], publish):
                return await edit(task, evidence)
        except Exception as error:
            if audit := task["plan"].get("editor_memory", {}).get("review"):
                record(phase_for_task[task["id"]] + "_rejected_writing",
                       {"task_id":task["id"], "message":str(error), "review":audit})
            raise
    tasks.edit_delivery = audited_edit
    tasks.start()
    record("video_request", {"goal":GOAL, "selection":SELECTION})
    async def grader(messages, schema):
        async def read():
            async with runtime.operator_generation("background"):
                limit = min(runtime.settings.data["omni"]["max_tokens"], 1024)
                return await budgeted_call(runtime.llm, lambda: runtime.llm.generate_json(messages, schema, max_tokens=limit),
                                           messages, max_tokens=limit)
        return await await_admission("independent_title_reading", read, deadline, queue_receipts, record)
    async def edition(phase):
        task_id = str(uuid4())
        phase_for_task[task_id] = phase
        result = await client.post("/api/v1/agent/tasks", json={"request_id":task_id, "goal": GOAL, "video_selection": SELECTION})
        result.raise_for_status()
        record("stage", "generating_" + phase)
        try:
            async with asyncio.timeout(480):
                while True:
                    response = (await client.get("/api/v1/agent/tasks/"+task_id)).raise_for_status().json()
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
            return {"status":response["status"], "body":None, "items":[], "citations":[],
                    "measurement":{"passed":False, "items":[]}, "message":response["message"]}
        assert any(s["tool"] == "inspect_video" and s["result"].get("findings") for s in response["steps"])
        other = (await device_call(device, port, login, "GET", "/api/v1/agent/tasks/"+task_id))["value"]
        assert other["artifact"]["body"] == response["artifact"]["body"]
        plan = tasks.current(task_id)["plan"]
        body = other["artifact"]["body"]
        headings = re.findall(r"^\*\*(.+?)\*\*\r?\n选材理由：", body, re.MULTILINE)
        visible_items = [item | {"title":heading} for item,heading in zip(plan["selected"]["items"], headings, strict=True)]
        points = {f"{step['id']}:{index}":{key:f[key] for key in ("part","cid","seconds")}
                  for step in response["steps"] if step["tool"] == "inspect_video"
                  for index,f in enumerate((step.get("result") or {}).get("findings", []), 1)
                  if f.get("kind") == "实际视频帧" and f.get("cid") and f.get("part") == 2 and 11 <= f["seconds"] < 14}
        primary = [points.get(item["primary_frame"]) for item in visible_items]
        result = {"status":"completed", "body":body, "citations":other.get("memory_citations", []),
                  "editor_review":plan.get("editor_memory", {}).get("review"), "items":visible_items,
                  "primary_points":primary, "independent_device_body_matched":True}
        if plan.get("attribution_rejections"):
            result["attribution_rejections"] = plan["attribution_rejections"]
        record(phase, result)
        result["measurement"] = await measure_edition(result["items"], plan.get("frame_reviews", {}), phase, grader)
        nearest = min(points.values(), key=lambda p:abs(p["seconds"] - 13.5), default=None)
        result["measurement"]["requested_frame"] = len(primary) == 1 and nearest is not None and primary[0] == nearest
        result["measurement"]["passed"] &= result["measurement"]["requested_frame"]
        return result
    try:
        before = await edition("before")
        record("before", before)
        if before["status"] == "completed":
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
        same_frame = bool(updated and before.get("primary_points") and
                          before["primary_points"] == corrected.get("primary_points") == after.get("primary_points"))
        observed = before["measurement"]["passed"] and voice_observed and after["measurement"]["passed"] and same_frame
        return {"input":"owner-authorized isolated QA preference", "original":AUTO, "before_titles":before_titles,
                "after_titles":after_titles, "same_frame":same_frame, "semantic_status":"observed" if observed else "unverified"}
    finally:
        await tasks.close()
        tasks.edit_delivery = edit
