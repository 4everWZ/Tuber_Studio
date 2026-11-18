import asyncio
import json
import time
from collections import Counter
from uuid import uuid4

import httpx
import pytest
from test_agent_tasks import until
from test_browsing import BV, ControlledPlatform

from tuber_studio import http_api
from tuber_studio.agent import task_tools
from tuber_studio.agent.task_delivery import (
    CreativeItem,
    PrivateDelivery,
    evidence_index,
    render_private_delivery,
)
from tuber_studio.api_providers import APIProvider
from tuber_studio.app import create_app


def call(name, args, identity):
    return {"type": "function_call", "name": name, "call_id": identity, "arguments": json.dumps(args)}


def gateway(settings, monkeypatch, reply):
    settings.data["responses"] = {"base_url": "https://example.org/llm/v1", "model": "text-worker", "max_output_tokens": 1300}
    settings.provider_keys["responses"] = "mock-responses-key"
    requests = []

    def handle(request):
        assert request.method == "POST" and request.url.path == "/llm/v1/responses"
        assert request.headers["authorization"] == "Bearer mock-responses-key"
        payload = json.loads(request.content)
        assert payload["model"] == "text-worker" and payload["max_output_tokens"] == 1300
        requests.append(payload)
        output = reply(payload)
        if isinstance(output, httpx.Response):
            return output
        return httpx.Response(200, json={"id": f"resp-{len(requests)}", "status": "completed", "output": output})

    monkeypatch.setattr(http_api, "inference_transport", lambda base_url: httpx.MockTransport(handle))
    return requests


@pytest.mark.parametrize("repeat", [True, False])
async def test_bad_tool_arguments_return_a_diagnostic_without_execution(runtime, store, monkeypatch, repeat):
    turns = []

    def model(payload):
        outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
        if outputs:
            receipt = json.loads(outputs[-1]["output"])
            assert not receipt["evidence"] and receipt["receipt"]["issues"][0]["field"] == "web_fetch"
            if not repeat:
                return [call("ask_owner", {"question": "要核对哪个公开页面？"}, "clarify")]
        turns.append(1)
        return [call("web_page", {}, f"bad-{len(turns)}")]

    gateway(runtime.settings, monkeypatch, model)
    tasks = task_tools.create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, "帮我核对并保存页面摘要")
    tasks.start()
    try:
        expected = "failed" if repeat else "needs_input"
        await until(lambda: tasks.current(identity)["status"] == expected)
        task = tasks.current(identity)
        assert len(task["steps"]) == (2 if repeat else 1)
        assert all(step.get("invalid_call") for step in task["steps"])
    finally:
        await tasks.close()


async def test_responses_parallel_checkpoint_child_isolation_and_steering(runtime, settings, store, monkeypatch):
    urls = ["https://example.com/opening", "https://example.com/ending"]
    fetched = Counter()
    both = asyncio.Event()
    restarting = True
    chosen = {}

    async def fetch(address):
        fetched[address] += 1
        if all(fetched[url] for url in urls):
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        if address == urls[1] and restarting:
            await asyncio.Future()
        return {"url": address, "title": "原出处", "excerpt": address.rsplit("/", 1)[1]}

    def model(payload):
        if not payload["tools"]:
            assert "private-marker" not in payload["input"] and "ending" not in payload["input"]
            assert json.loads(payload["input"])["objective"] == "写一句开场"
            return [{"type": "message", "content": [{"type": "output_text", "text": "先从开场的小问题说起。"}]}]
        assert payload["tool_choice"] == "required"
        conversation = payload["input"]
        brief = json.loads(conversation[1]["content"])
        outputs = {item["call_id"]: json.loads(item["output"]) for item in conversation if item.get("type") == "function_call_output"}
        if brief["prior_goals"]:
            assert "private-marker" in brief["prior_goals"][0] and "大肥鱼的选材" in brief["previous_delivery"]
            assert chosen["opening"] in brief["saved_evidence"] and not outputs
            reason = "新版只保留开场。"
        elif not outputs:
            return [call("web_page", {"web_fetch": url}, url.rsplit("/", 1)[1]) for url in urls]
        elif "draft" not in outputs:
            assert list(outputs) == ["opening", "ending"]
            assert [item["type"] for item in conversation[2:]] == ["function_call"] * 2 + ["function_call_output"] * 2
            chosen["opening"] = next(iter(outputs["opening"]["evidence"]))
            return [call("delegate_text", {"objective": "写一句开场", "sources": [chosen["opening"]]}, "draft")]
        else:
            assert not outputs["draft"]["evidence"]
            reason = outputs["draft"]["receipt"]["draft"]
        return [call("submit_materials", {"items": [{"title": "提纲", "rationale": reason, "sources": [chosen["opening"]]}],
                                         "gaps": ["未找到第二段合格材料。"]}, "delivery")]

    requests = gateway(settings, monkeypatch, model)
    editor_calls = []

    async def edit(messages, schema, *, max_tokens):
        brief = json.loads(messages[1]["content"])
        editor_calls.append(brief)
        assert "大肥鱼" in messages[0]["content"] and chosen["opening"] in brief["evidence"]
        assert chosen["opening"] in schema["$defs"]["CreativeItem"]["properties"]["sources"]["items"]["enum"]
        product = brief["candidate"]
        product["items"][0]["rationale"] = "大肥鱼的选材：" + product["items"][0]["rationale"]
        return product

    monkeypatch.setattr(runtime.llm, "generate_json", edit)
    monkeypatch.setattr(task_tools, "fetch_excerpt", fetch)
    tasks = task_tools.create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, f"private-marker：给我做个提纲，先别发。{urls[0]} 和 {urls[1]}")
    tasks.start()
    try:
        def checkpoint():
            steps = tasks.current(identity)["steps"]
            return len(steps) == 2 and "result" in steps[0] and "result" not in steps[1]

        await until(checkpoint)
    finally:
        await tasks.close()
    restarting = False
    recovered = task_tools.create_task_runtime(runtime, store, None)
    recovered.start()
    try:
        await until(lambda: recovered.current(identity)["status"] in {"completed", "failed"})
        first = recovered.view(recovered.current(identity))
        assert first["status"] == "completed" and "大肥鱼的选材：先从开场" in first["message"] and urls[0] in first["message"]
        assert "未找到第二段" in first["message"] and fetched == {urls[0]: 1, urls[1]: 2}
        assert not any("previous_response_id" in payload for payload in requests)
        await recovered.control(identity, 1, "steer", "换成适合开场的一段")
        await until(lambda: recovered.current(identity)["status"] in {"completed", "failed"})
        edited = recovered.view(recovered.current(identity))
        assert edited["revision"] == 2 and "新版" in edited["message"] and urls[0] in edited["message"]
        assert fetched == {urls[0]: 1, urls[1]: 2} and len(requests) == 5 and len(editor_calls) == 2
    finally:
        await recovered.close()


@pytest.mark.parametrize(("requested", "accepted"), [({"part": 2}, True), ({"part": 1}, False), ({}, False),
                                                       ({"part": 2, "start": 10, "end": 20}, True)])
async def test_bound_part_reaches_domain_and_only_selected_evidence_returns(settings, monkeypatch, requested, accepted):
    def model(payload):
        outputs = [json.loads(item["output"]) for item in payload["input"] if item.get("type") == "function_call_output"]
        if not outputs:
            return [call("inspect_video", {"bvid": BV, **requested}, "read")]
        receipt = outputs[0]["receipt"]
        assert receipt["selection"]["ranges"] == [{"part": 2, "start": requested.get("start", 0), "end": requested.get("end", 42)}]
        assert [item["cid"] for item in receipt["findings"]] == ["p2"]
        return [call("submit_materials", {"items": [{"title": "开场", "rationale": "适合引入", "sources": list(outputs[0]["evidence"])}],
                                         "gaps": ["只有字幕"]}, "finish")]

    gateway(settings, monkeypatch, model)
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    async def edit(messages, schema, *, max_tokens):
        return json.loads(messages[1]["content"])["candidate"]

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", edit)
    browsing = app.state.browsing
    tasks = app.state.runtime.tasks
    browsing.discovery.remember_account("100")
    queued = []

    async def metadata(bvid):
        return {"bvid": bvid, "parts": [{"page": 1, "duration": 8}, {"page": 2, "duration": 42}]}

    def status(row):
        selection = row["config"]["operator_selection"]
        queued.append(selection)
        def part(number, second):
            return {"part": number, "cid": f"p{number}", "ranges": [{"highlights": [{"start": second, "kind": "字幕",
                    "text": f"材料P{number}", "source_url": f"https://www.bilibili.com/video/{BV}/?p={number}&t={second}"}]}]}
        return {"status": "completed", "selection": selection, "coverage": {"viewing": "subtitles_only",
                "parts": [part(1, 3), part(2, 12)]}, "comments": {}, "error": None}

    monkeypatch.setattr(platform, "video_metadata", metadata)
    monkeypatch.setattr(browsing, "full_video_status", status)
    identity = str(uuid4())
    tasks.create(identity, f"只取 {BV} 的 P2，做个开场")
    for _ in range(5):
        task = tasks.current(identity)
        if task["status"] in {"completed", "failed"}:
            break
        await tasks._advance(task)
    final = tasks.current(identity)
    assert final["status"] == ("completed" if accepted else "failed"), final
    assert bool(queued) == accepted and not platform.sent
    if accepted:
        assert "材料P1" not in final["message"] and "分 P 2 · 12 秒" in final["message"]
    app.state.store.close()


def test_candidate_links_and_model_drafts_are_not_citable_evidence():
    steps = [{"id": "candidate", "tool": "web_search", "result": {"links": ["https://example.com"]}},
             {"id": "draft", "tool": "delegate_text", "result": {"draft": "我确认过"}}]
    product = PrivateDelivery(items=[CreativeItem(title="开场", rationale="合适", sources=["candidate"])], gaps=[])
    with pytest.raises(ValueError, match="没有回执"):
        render_private_delivery(product, evidence_index(steps))


@pytest.mark.parametrize("failure", ["invalid_source", "offline", "busy_then_recover"])
async def test_only_front_editor_can_complete_saved_candidate(runtime, settings, store, monkeypatch, failure):
    def model(payload):
        outputs = [json.loads(item["output"]) for item in payload["input"] if item.get("type") == "function_call_output"]
        if not outputs:
            return [call("live_status", {}, "state")]
        return [call("submit_materials", {"items": [{"title": "候选", "rationale": "worker-text",
                         "sources": list(outputs[0]["evidence"])}], "gaps": []}, "candidate")]

    requests = gateway(settings, monkeypatch, model)
    edits = []

    async def edit(messages, schema, *, max_tokens):
        edits.append(True)
        candidate = json.loads(messages[1]["content"])["candidate"]
        if failure == "offline":
            raise httpx.ConnectError("front offline")
        if failure == "invalid_source":
            candidate["items"][0]["sources"] = ["invented-source"]
        if failure == "busy_then_recover" and len(edits) == 1:
            raise http_api.InferenceQueueBusy(3)
        candidate["items"][0]["rationale"] = "Omni选定"
        return candidate

    monkeypatch.setattr(runtime.llm, "generate_json", edit)
    tasks = task_tools.create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, "核对直播状态")
    for _ in range(4):
        await tasks._advance(tasks.current(identity))
    pending = tasks.current(identity)
    assert pending["status"] == "queued" and "candidate" in pending["plan"] and not edits
    recovered = task_tools.create_task_runtime(runtime, store, None)
    await recovered._advance(recovered.current(identity))
    final = recovered.current(identity)
    assert recovered.workspace.latest(identity) is None and len(requests) == 2
    if failure == "busy_then_recover":
        assert final["status"] == "waiting" and final["due"] > time.time() + 2
        await recovered._advance(final)
        assert recovered.current(identity)["status"] == "completed"
        body = recovered.workspace.latest(identity)["body"]
        assert "Omni选定" in body and "worker-text" not in body and len(requests) == 2
    else:
        assert final["status"] == "failed" and "worker-text" not in final["message"]


@pytest.mark.parametrize("mode", ["many", "mixed", "busy", "budget"])
async def test_native_admission_is_not_an_eight_step_plan(runtime, settings, store, monkeypatch, mode):
    def model(payload):
        if mode == "busy":
            return httpx.Response(429, headers={"Retry-After": "4"}, json={"error": {"code": "gateway_busy"}})
        if mode == "budget":
            return httpx.Response(400, json={"error": {"code": "request_budget_exceeded"}})
        calls = [call("web_search", {"query": f"主题{number}"}, str(number)) for number in range(9)]
        if mode == "mixed":
            calls.append(call("live_handoff", {"text": "不要播"}, "write"))
        return calls

    gateway(settings, monkeypatch, model)
    tasks = task_tools.create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, "独立查证这些主题")
    await tasks._advance(tasks.current(identity))
    await tasks._advance(tasks.current(identity))
    task = tasks.current(identity)
    if mode == "many":
        assert task["status"] == "queued" and len(task["steps"]) == 9
    else:
        assert task["steps"] == []
        if mode == "busy":
            assert task["status"] == "waiting" and task["due"] > time.time() + 3
        else:
            assert task["status"] == "failed"
            if mode == "budget":
                assert "预算" in task["message"]


async def test_steer_reuses_original_video_operation_even_if_last_sibling_has_finished(settings, monkeypatch):
    reads = []
    metadata_reads = []

    async def search(query):
        reads.append(query)
        return ["https://example.com/about"]

    def model(payload):
        revised = json.loads(payload["input"][1]["content"])["prior_goals"]
        video = call("inspect_video", {"bvid": BV, "part": 2, "start": 10, "end": 20}, "continue" if revised else "view")
        return [video] if revised else [video, call("web_search", {"query": "原出处"}, "origin")]

    gateway(settings, monkeypatch, model)
    monkeypatch.setattr(task_tools, "search_public_web", search)
    platform = ControlledPlatform()
    async def metadata(bvid):
        metadata_reads.append(bvid)
        return {"bvid": bvid, "parts": [{"page": 1, "duration": 30}, {"page": 2, "duration": 18}]}

    monkeypatch.setattr(platform, "video_metadata", metadata)
    app = create_app(settings, adapter=platform)
    tasks, browsing = app.state.runtime.tasks, app.state.browsing
    browsing.discovery.remember_account("100")
    identity = str(uuid4())
    tasks.create(identity, f"看 {BV} 的P2，顺便查出处。")
    try:
        for _ in range(3):
            await tasks._advance(tasks.current(identity))
        before = tasks.current(identity)
        resource = before["steps"][0]["id"]
        assert before["status"] == "waiting" and before["steps"][-1]["result"]["links"]
        revised = await tasks.control(identity, 1, "steer", "第二段准备给开场用")
        assert revised["status"] == "queued" and browsing.repo.round(resource)["status"] == "cancelled"
        await tasks._advance(revised)
        await tasks._advance(tasks.current(identity))
        resumed = tasks.current(identity)
        assert resumed["status"] == "waiting" and resumed["steps"][-1]["resource_id"] == resource
        assert metadata_reads == [BV]
        assert browsing.repo.round(resource)["config"]["operator_selection"]["ranges"] == [
            {"part": 2, "start": 10, "end": 18}]
        assert browsing.repo.round(resource)["status"] == "waiting" and reads == ["原出处"]
        assert app.state.store.one("SELECT COUNT(*) AS n FROM browsing_rounds")["n"] == 1
        await tasks.control(identity, 2, "cancel")
        assert browsing.repo.round(resource)["status"] == "cancelled" and not platform.sent
    finally:
        app.state.store.close()


async def test_front_chat_and_background_responses_use_distinct_http_contracts(runtime, settings, store, monkeypatch):
    def worker(payload):
        receipts = [json.loads(item["output"]) for item in payload["input"] if item.get("type") == "function_call_output"]
        if not receipts:
            return [call("live_status", {}, "lookup")]
        return [call("submit_materials", {"items": [{"title": "后台建议", "rationale": "候选理由",
                        "sources": list(receipts[0]["evidence"])}], "gaps": []}, "materials")]

    requests = gateway(settings, monkeypatch, worker)
    worker_transport = http_api.inference_transport
    front_requests = []

    def front(request):
        front_requests.append(request.url.path)
        body = json.loads(request.content)
        assert body["model"] == "omni-front" and "input" not in body and "tools" not in body
        if request.url.path == "/v1/chat/prompt_tokens":
            return httpx.Response(200, json={"count": 64})
        assert request.url.path == "/v1/chat/completions" and "大肥鱼" in body["messages"][0]["content"]
        brief = json.loads(next(item["content"] for item in body["messages"] if item["role"] == "user"))
        evidence_id = next(iter(brief["evidence"]))
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["$defs"]["CreativeItem"]["properties"]["sources"]["items"]["enum"] == [evidence_id]
        final = {"items": [{"title": "大肥鱼的开场准备", "rationale": "先在后台排练", "sources": [evidence_id]}], "gaps": []}
        return httpx.Response(200, json={"usage": {"prompt_tokens": 64}, "choices": [{"finish_reason": "stop",
                                        "message": {"content": json.dumps(final, ensure_ascii=False)}}]})

    monkeypatch.setattr(http_api, "inference_transport", lambda url: httpx.MockTransport(front)
                        if url == "https://example.org/v1" else worker_transport(url))
    provider = APIProvider({"base_url": "https://example.org/v1", "model": "omni-front", "context_tokens": 32768,
                            "native_context_tokens": 32768, "structured_output": "json_schema", "prompt_token_endpoint": True})
    monkeypatch.setattr(runtime, "llm", provider)
    tasks = task_tools.create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, "请核对开播状态并给个建议")
    try:
        for _ in range(5):
            await tasks._advance(tasks.current(identity))
        saved = tasks.current(identity)
        assert saved["status"] == "completed" and "大肥鱼的开场准备" in saved["message"]
        assert "后台建议" not in saved["message"] and len(requests) == 2
        assert front_requests == ["/v1/chat/prompt_tokens", "/v1/chat/completions"]
    finally:
        await provider.close()
