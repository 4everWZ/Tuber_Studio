import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from front_effects import direct_request

from evaluations import omni_chat_loop as replay
from tuber_studio import http_api


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
            if content == replay.REQUEST:
                schema = json.loads(body["structured_outputs"]["structural_tag"])["format"]["json_schema"]
                chat_stages.append(schema["title"])
                assert behavior == "public_action" and "start_live" in schema["properties"]["action"]["enum"]
                decision = {"action": "start_live"}
            else:
                packet = (json.loads(body["messages"][1]["content"]) | {"message": content}
                          if content == replay.CHANGE else json.loads(content))
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
                else:
                    assert path == "/v1/chat/completions"
                    chat_stages.append("card_steering")
                    assert packet["message"] == replay.CHANGE
                    card, = packet["recent_task_cards"]
                    decision = {"intent": "实际修改同一任务或成果的部分或全部", "patch": {"field": "goal", "subject": "便笺", "requirement": "只核对自主运营有没有启动"}, "reference": card["id"]}
            result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}],
                      "usage": {"prompt_tokens": 128}}
        elif path == "/llm/v1/responses" and "direct_tool" in {tool["name"] for tool in body["tools"]}:
            chat_stages.append("turn_plan")
            assert body["tool_choice"] == "required" and body["store"] is False
            current = body["input"][-1]["content"]
            assert current in (replay.REQUEST, replay.CHANGE)
            decision = (direct_request(current, mode="change", target="演播间") if behavior == "public_action"
                        else {"action": "agent_task" if current == replay.REQUEST else "task_turn"})
            action = decision.pop("action")
            assert action in {tool["name"] for tool in body["tools"]}
            result = {"id": uuid4().hex, "status": "completed", "output": [
                {"type": "function_call", "call_id": uuid4().hex, "name": action, "arguments": json.dumps(decision)}]}
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
            assert chat_stages == ["turn_plan", "AssistantDecision"]
        else:
            assert calls.count("/llm/v1/responses") == (5 if behavior == "invented_source" else 4)
            expected = ["turn_plan", "turn_plan", "card_steering"]
            assert chat_stages == (expected + ["evidence_editor"] if behavior == "invented_editor_source" else expected)
        return
    async with asyncio.timeout(5):
        result = await replay.run(config)
    assert result == {"status": "passed", "task_revision": 2, "local_reads": ["operation_status"], "platform_writes": 0}
    assert chat_stages == ["turn_plan", "turn_plan", "card_steering", "evidence_editor"]
    assert [path for path in calls if path.endswith(("chat/completions", "responses"))] == [
        "/llm/v1/responses", "/llm/v1/responses", "/v1/chat/completions", "/llm/v1/responses", "/llm/v1/responses",
        "/llm/v1/chat/completions"]
