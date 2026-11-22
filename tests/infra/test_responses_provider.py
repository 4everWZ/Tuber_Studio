import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest

from tuber_studio.core.config import Settings
from tuber_studio.core.errors import ContextWindowExceeded
from tuber_studio.core.validation import endpoint
from tuber_studio.infra.inference.http_api import (
    InferenceBudgetExceeded,
    InferenceQueueBusy,
    response_json,
)
from tuber_studio.infra.inference.providers import read_chat_stream
from tuber_studio.infra.inference.responses_provider import ResponsesClient

TOOL = {"type": "function", "name": "lookup", "description": "Read the status of a ticket.",
        "parameters": {"type": "object", "properties": {"ticket": {"type": "string"}}, "required": ["ticket"],
                       "additionalProperties": False}}


def call_item(call_id, ticket):
    return {"type": "function_call", "id": "item_" + call_id, "call_id": call_id,
            "name": "lookup", "arguments": json.dumps({"ticket": ticket})}


def completed(output):
    return {"id": "resp_first", "status": "completed", "output": output}


async def test_real_http_roundtrip_uses_scoped_key_budget_and_all_call_ids(settings):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), payload))
            output = ([call_item("call_a", "A"), call_item("call_b", "B")] if len(requests) == 1 else
                      [{"type": "message", "content": [{"type": "output_text", "text": "核对完毕"}]}])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(completed(output)).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            settings.data["omni"].update(base_url=f"http://127.0.0.1:{server.server_port}/service/v1",
                                         responses_model="local-worker",
                                         responses_options={"max_output_tokens": 768})
            settings.save_public(settings.data)
            settings.save_keys({"omni": "worker-key"})
            async with ResponsesClient.from_settings(Settings(runtime=settings.runtime)) as client:
                first = await client.create("Read both tickets", tools=[TOOL])
                assert [(call.call_id, call.name, call.arguments) for call in first.calls] == [
                    ("call_a", "lookup", {"ticket": "A"}), ("call_b", "lookup", {"ticket": "B"})]
                for invalid in ({"call_a": "ok"}, {"item_call_a": "ok", "call_b": "ok"}):
                    with pytest.raises(ValueError, match="call_id"):
                        await client.submit_results(first, invalid)
                result = await client.submit_results(first, {"call_b": "blocked", "call_a": {"ok": True}},
                                                     tools=[TOOL], instructions="总结", max_output_tokens=128)
                assert not result.calls and result.text == "核对完毕"
        finally:
            server.shutdown()
            worker.join()
    initial, follow = [payload for _, _, payload in requests]
    assert all((path, auth) == ("/service/llm/v1/responses", "Bearer worker-key") for path, auth, _ in requests)
    assert initial == {"input": "Read both tickets", "tools": [TOOL], "model": "local-worker",
                       "max_output_tokens": 768, "store": True, "thinking_token_budget": 192,
                       "chat_template_kwargs": {"enable_thinking": True}}
    assert follow == initial | {"input": [{"type": "function_call_output", "call_id": "call_a", "output": '{"ok": true}'},
                                         {"type": "function_call_output", "call_id": "call_b", "output": "blocked"}],
                                "instructions": "总结", "previous_response_id": "resp_first", "max_output_tokens": 128,
                                "thinking_token_budget": 32}


@pytest.mark.parametrize("response", [
    completed([call_item("same", "A"), call_item("same", "B")]),
    completed([call_item("id", "A") | {"name": "unlisted"}]),
    completed([call_item("id", "A") | {"arguments": "[]"}]),
    completed([call_item("id", "A")]) | {"status": "incomplete"},
])
async def test_invalid_turn_exposes_no_executable_call(response, monkeypatch):
    async def transport(*args, **kwargs):
        return response

    monkeypatch.setattr("tuber_studio.infra.inference.responses_provider.response_json", transport)
    async with ResponsesClient({"base_url": "https://api.example/llm/v1", "model": "worker"}) as client:
        with pytest.raises(ValueError):
            await client.create("read", tools=[TOOL])


async def test_incomplete_budget_is_reported_even_with_a_partial_function_call(monkeypatch):
    async def transport(*args, **kwargs):
        return completed([call_item("partial", "A")]) | {
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}

    monkeypatch.setattr("tuber_studio.infra.inference.responses_provider.response_json", transport)
    async with ResponsesClient({"base_url": "https://api.example/llm/v1", "model": "worker"}) as client:
        with pytest.raises(InferenceBudgetExceeded, match="317 token"):
            await client.create("read", tools=[TOOL], max_output_tokens=317)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status,code,kind", [
    (429, "gateway_busy", InferenceQueueBusy), (429, "quota_exhausted", httpx.HTTPStatusError),
    (400, "request_budget_exceeded", InferenceBudgetExceeded), (400, "context_length_exceeded", ContextWindowExceeded),
    (401, "bad_credentials", httpx.HTTPStatusError), (404, "response_not_found", httpx.HTTPStatusError),
    (504, "response_upstream_unavailable", httpx.HTTPStatusError),
])
async def test_http_and_stream_share_gateway_errors(stream, status, code, kind):
    def upstream(request):
        return httpx.Response(status, json={"error": {"code": code}}, headers={"Retry-After": "45"})

    async with httpx.AsyncClient(base_url="https://alternate.example/v1", transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(kind) as caught:
            if stream:
                async with client.stream("POST", "chat/completions") as response:
                    await read_chat_stream(response, None)
            else:
                await response_json(client, "POST", "responses")
    if kind is InferenceQueueBusy:
        assert caught.value.retry_after == 45
    if kind is httpx.HTTPStatusError:
        assert caught.value.response.status_code == status


def test_explicit_internal_service_name_does_not_allow_unrelated_http(monkeypatch):
    monkeypatch.setenv("OMNI_GATEWAY_URL", "http://my-workstation:8000")
    assert endpoint("http://my-workstation:8000/v1") == "http://my-workstation:8000/v1"
    for url in ("http://my-workstation:9000/v1", "http://my-workstation.example:8000/v1"):
        with pytest.raises(ValueError, match="HTTPS"):
            endpoint(url)


async def test_responses_client_forwards_explicit_temperature_only(monkeypatch):
    payloads = []

    async def transport(client, method, path, *, payload=None):
        payloads.append(payload)
        return completed([call_item("t1", "A")])

    monkeypatch.setattr("tuber_studio.infra.inference.responses_provider.response_json", transport)
    async with ResponsesClient({"base_url": "https://api.example/llm/v1", "model": "worker"}) as client:
        await client.create("default temp", tools=[TOOL])
        await client.create("zero temp", tools=[TOOL], temperature=0.0)
    assert "temperature" not in payloads[0]
    assert payloads[1]["temperature"] == 0.0


@pytest.mark.skipif(os.getenv("TUBER_TEST_LIVE_RESPONSES") != "1", reason="explicit workstation test only")
async def test_workstation_function_roundtrip():
    config = {"base_url": os.environ["OMNI_GATEWAY_URL"].rstrip("/") + "/llm/v1",
              "model": os.getenv("OMNI_LLM_MODEL_ALIAS", "llm-default")}
    key = os.getenv("OMNI_STUDIO_TOKEN") or Path(os.environ["OMNI_GATEWAY_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    async with ResponsesClient(config, key) as client:
        first = await client.create("Look up ticket T-57.", tools=[TOOL], tool_choice={"type": "function", "name": "lookup"})
        assert len(first.calls) == 1 and first.calls[0].arguments == {"ticket": "T-57"}
        result = await client.submit_results(first, {first.calls[0].call_id: "READY-5738"}, tools=[TOOL],
                                             tool_choice="none", instructions="Repeat the returned status code exactly.")
        assert not result.calls and "READY-5738" in result.text
