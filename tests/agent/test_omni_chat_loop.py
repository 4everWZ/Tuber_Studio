import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from evaluations.agent import omni_chat_loop as replay
from tuber_studio.infra.inference import http_api as http_api


async def test_unconfigured_rehearsal_skips_without_opening_credentials(tmp_path):
    assert (await replay.run({"OMNI_CORE_TOKEN_FILE": str(tmp_path / "absent")}))["status"] == "skipped"


@pytest.mark.parametrize("behavior", ["expected", "public_action", "invented_source", "invented_editor_source"])
async def test_isolated_chat_and_responses_wire(tmp_path, monkeypatch, behavior):
    key = uuid4().hex
    token_file = tmp_path / "application-token"
    token_file.write_text(key)
    root = str(httpx.URL(scheme="https", host=uuid4().hex + ".invalid"))
    calls = []
    chat_stages = []
    evidence = {}

    def gateway(request):
        assert request.headers["authorization"] == "Bearer " + key
        path = request.url.path
        calls.append(path)
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/models":
            result = {"data": [{"id": "omni-default", "max_model_len": 32768}]}
        elif path == "/llm/v1/models":
            result = {"data": [{"id": "llm-default", "max_model_len": 262144}]}
        elif path in {"/v1/chat/prompt_tokens", "/llm/v1/chat/prompt_tokens"}:
            result = {"count": 128}
        elif path in {"/v1/chat/completions", "/llm/v1/chat/completions"}:
            content = body["messages"][-1]["content"]
            if path == "/v1/chat/completions" and "structured_outputs" not in body and "response_format" not in body:
                chat_stages.append("front_ack")
                result = {"choices": [{"finish_reason": "stop", "message": {"content": "好的，已经交给后台处理，结果会回到这里。"}}],
                          "usage": {"prompt_tokens": 128}}
            else:
                packet = json.loads(content)
                if "candidate" in packet:
                    assert path == "/llm/v1/chat/completions"
                    assert body["model"] == "llm-default" and body["response_format"]["type"] == "json_schema"
                    assert body["chat_template_kwargs"]["enable_thinking"] is True
                    assert all(isinstance(message["content"], str) for message in body["messages"])
                    chat_stages.append("evidence_editor")
                    assert body["max_tokens"] == 4096
                    assert replay.CHANGE in packet["goal"] and packet["evidence"] == evidence and evidence
                    candidate, = packet["candidate"]["items"]
                    assert candidate["sources"] == list(evidence)
                    decision = {"items": [{"title": "私有交接便笺", "rationale": "根据本轮状态回执选材",
                                           "sources": list(evidence) if behavior != "invented_editor_source"
                                           else ["unverified"]}], "gaps": []}
                    result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}],
                              "usage": {"prompt_tokens": 128}}
                else:
                    pytest.fail("unexpected completion packet")
        elif path == "/llm/v1/responses" and "delegate_task" in {tool["name"] for tool in body["tools"]}:
            assert body["tool_choice"] == "required" and body["parallel_tool_calls"] is False
            assert body["max_output_tokens"] == 640 and body["thinking_token_budget"] == 160
            assert body["temperature"] == 0.0
            assert body["chat_template_kwargs"]["enable_thinking"] is True
            payload = json.loads(body["input"][-1]["content"])
            if payload["message"] == replay.REQUEST:
                chat_stages.append("turn_router")
                if behavior == "public_action":
                    name, args = "direct_action", {"action": "start_live"}
                else:
                    name, args = "delegate_task", {
                        "goal": replay.REQUEST,
                        "deliverable": "写一份私有交接便笺并附操作状态回执",
                    }
            else:
                assert payload["message"] == replay.CHANGE
                chat_stages.append("card_steering")
                card, = payload["recent_task_cards"]
                name, args = "steer_task", {
                    "task_id": card["id"],
                    "task_effect": "revise",
                    "change_quote": "只核对自主运营有没有启动",
                    "brief": {"goal": "只核对自主运营有没有启动", "deliverable": "写一份私有交接便笺并附操作状态回执"},
                }
            result = {"id": uuid4().hex, "status": "completed", "output": [
                {"type": "function_call", "call_id": uuid4().hex, "name": name, "arguments": json.dumps(args)}]}
        elif path == "/llm/v1/responses":
            schemas = {tool["name"]: tool["parameters"] for tool in body["tools"]}
            assert all(tool["strict"] for tool in body["tools"])
            assert schemas["operation_status"]["properties"] == {}
            item = schemas["submit_materials"]["properties"]["items"]["items"]
            assert "title" in item["properties"] and set(item["required"]) == set(item["properties"])
            assert {tool["name"] for tool in body["tools"]} == {
                "live_status", "operation_status", "submit_materials", "ask_owner"}
            assert body["tool_choice"] == "required"
            outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
            if not outputs:
                assert schemas["submit_materials"]["properties"]["items"]["maxItems"] == 0
                name, args = "operation_status", {}
            else:
                output = outputs[0]
                if len(outputs) > 1:
                    assert behavior == "invented_source" and len(outputs) == 2
                    assert outputs[1]["call_id"] == "deliver"
                    assert "error" in json.loads(outputs[1]["output"])["receipt"]
                assert output["call_id"] == "read"
                receipt = json.loads(output["output"])
                assert receipt["receipt"] == {"state": "off"}
                evidence.update(receipt["evidence"])
                assert set(item["properties"]["sources"]["items"]["enum"]) == set(evidence)
                name, args = "submit_materials", {"items": [
                    {"title": "交接便笺", "rationale": "已核实", "sources": list(receipt["evidence"])}], "gaps": []}
                if behavior == "invented_source":
                    args["items"][0]["sources"] = ["unverified"]
            result = {"id": uuid4().hex, "status": "completed", "output": [
                {"type": "function_call", "call_id": ("retry" if len(outputs) > 1 else "deliver") if outputs else "read", "name": name,
                 "arguments": json.dumps(args)}]}
        else:
            pytest.fail("unexpected network request")
        return httpx.Response(200, json=result)

    monkeypatch.setattr(http_api, "inference_transport", lambda base: httpx.MockTransport(gateway))
    config = {"OMNI_GATEWAY_URL": root, "OMNI_CORE_TOKEN_FILE": str(token_file)}
    if behavior != "expected":
        check = {"public_action": "unexpected_chat_action", "invented_source": "responses_delivery",
                 "invented_editor_source": "omni_evidence_edit"}[behavior]
        with pytest.raises(replay.ReplayMismatch, match=check):
            async with asyncio.timeout(5):
                await replay.run(config)
        if behavior == "public_action":
            assert calls.count("/llm/v1/responses") == 1
            assert chat_stages == ["turn_router"]
        else:
            assert calls.count("/llm/v1/responses") == (5 if behavior == "invented_source" else 4)
            expected = ["turn_router", "front_ack", "card_steering"]
            assert chat_stages == (expected + ["evidence_editor"] if behavior == "invented_editor_source" else expected)
        return
    async with asyncio.timeout(5):
        result = await replay.run(config)
    assert result == {"status": "passed", "task_revision": 2, "local_reads": ["operation_status"], "platform_writes": 0}
    assert chat_stages == ["turn_router", "front_ack", "card_steering", "evidence_editor"]
    assert [path for path in calls if path.endswith(("chat/completions", "responses"))] == [
        "/llm/v1/responses", "/v1/chat/completions", "/llm/v1/responses",
        "/llm/v1/responses", "/llm/v1/responses", "/llm/v1/chat/completions",
    ]
