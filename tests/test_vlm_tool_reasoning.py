"""Native tool reasoning is one-turn transport state, absent from durable reports."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "vlm_tool_protocol", Path(__file__).resolve().parents[1] / "evaluations/vlm_operations.py"
)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)
SENTINEL = "SYNTHETIC_CURRENT_TOOL_THOUGHT_318ae"
FINAL_THOUGHT = "SYNTHETIC_FINAL_THOUGHT_NOT_REPLAYED_526b"


def event(delta, finish):
    payload = [
        {"choices": [{"index": 0, "delta": delta}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        },
    ]
    content = "".join("data: " + json.dumps(row) + "\n\n" for row in payload) + "data: [DONE]\n\n"
    return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})


class Tools:
    def __init__(self):
        self.receipts = []

    async def execute(self, name, arguments, **kwargs):
        receipt = {
            "ref": "tool:1",
            "name": name,
            "arguments": arguments,
            "ok": True,
            "result": {"value": "公开资料"},
        }
        self.receipts.append(receipt)
        return receipt


@pytest.mark.parametrize("second_channel", ["public", "private"])
@pytest.mark.parametrize("policy", ["discard", "ephemeral"])
async def test_only_current_tool_turn_replays_thinking_and_real_user_clears_it(policy, second_channel):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["max_tokens"] == 4096
        assert body["chat_template_kwargs"] == {"enable_thinking": True}
        if len(requests) == 1:
            return event(
                {
                    "reasoning_content": SENTINEL,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": '{"query":"公开资料"}'},
                        }
                    ],
                },
                "tool_calls",
            )
        channel = "public" if len(requests) == 2 else second_channel
        text = json.dumps(
            {
                "text": "鱼宝，收到啦。",
                "channel": channel,
                "citations": [],
                "evidence_refs": ["turn:0" if len(requests) == 2 else "turn:1"],
            },
            ensure_ascii=False,
        )
        return event({"reasoning_content": FINAL_THOUGHT, "content": text}, "stop")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = v.Model(
            "http://model", "controlled", client=client, thinking="on", tool_reasoning_history=policy
        )
        report = await v.run_case(
            {
                "id": "controlled",
                "turns": [
                    {"text": "当前真实问题", "image": "fixture:shapes"},
                    {"text": "下一条真实问题", "input_channel": second_channel},
                ],
            },
            model,
            Tools(),
            character={"core": {}, "style": {}, "rules": []},
            image_url="data:image/png;base64,controlled",
            max_tool_rounds=1,
            image_order="image_first",
        )
        assert model.take_tool_reasoning() is None
    assert len(requests) == 3 and all(t["structural_complete"] for t in report["turns"])
    assert requests[0]["messages"][-1]["content"][0]["type"] == "image_url"
    second, third = requests[1]["messages"], requests[2]["messages"]
    carried = [m for m in second if m.get("reasoning_content")]
    if policy == "ephemeral":
        assert [m["reasoning_content"] for m in carried] == [SENTINEL]
        assert sum(m["role"] == "user" for m in second) == 1
        assert "工具额度已用尽" in json.dumps(second[1], ensure_ascii=False)
        assert report["turns"][0]["tool_reasoning_replayed_characters"] == len(SENTINEL)
    else:
        assert carried == []
        assert sum(m["role"] == "user" for m in second) == 2  # Original baseline boundary retained.
    assert not any(m.get("reasoning_content") or m.get("reasoning") for m in third)
    assert SENTINEL not in json.dumps(third) and FINAL_THOUGHT not in json.dumps(third)
    assert SENTINEL not in json.dumps(report) and FINAL_THOUGHT not in json.dumps(report)


async def test_failed_next_request_cannot_reuse_an_unconsumed_tool_thought():
    counter = 0

    def respond(request):
        nonlocal counter
        counter += 1
        if counter == 1:
            return event(
                {
                    "reasoning_content": SENTINEL,
                    "tool_calls": [
                        {"index": 0, "id": "call-1", "function": {"name": "web_search", "arguments": "{}"}}
                    ],
                },
                "tool_calls",
            )
        raise httpx.ReadError("controlled disconnect", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = v.Model("http://model", "controlled", client=client, tool_reasoning_history="ephemeral")
        first = await model.generate([{"role": "user", "content": "one"}])
        assert first["tool_calls"]
        failed = await model.generate([{"role": "user", "content": "two"}])
        assert failed["error"] and model.take_tool_reasoning() is None
        assert SENTINEL not in json.dumps(failed)


def test_cli_defaults_preserve_old_protocol_and_tagged_replay_is_rejected(tmp_path):
    arguments = [
        "--base-url",
        "http://model",
        "--model",
        "controlled",
        "--quantization",
        "q4",
        "--output",
        str(tmp_path / "report.json"),
    ]
    parsed = v.arguments(arguments)
    assert parsed.tool_reasoning_history == "discard" and parsed.image_order == "text_first"
    with pytest.raises(SystemExit):
        v.arguments(arguments + ["--tools-mode", "tagged", "--tool-reasoning-history", "ephemeral"])
