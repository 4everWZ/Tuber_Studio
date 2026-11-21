import asyncio
import base64
import io
import json
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from evaluations import creative_video_loop as replay
from tuber_studio import http_api


def test_decode_original_fixture_and_fail_closed_transport():
    archive = replay.Archive()
    duration, frames = replay.decode_clip(archive.clips["/media/p2.gif"])
    assert duration == 12 and [seconds for seconds, _ in frames] == [0, 4, 9]
    assert replay.decode_clip(archive.clips["/media/p1.gif"])[0] == 2
    for method, url in [("POST", replay.SOURCE), ("GET", "https://other.invalid/media/p2.gif")]:
        with pytest.raises(replay.ReplayMismatch):
            archive.serve(httpx.Request(method, url))


async def test_no_configuration_opens_no_clients(monkeypatch):
    monkeypatch.setattr(http_api, "inference_transport", lambda *_: pytest.fail("unexpected client"))
    assert (await replay.run({}))["status"] == "skipped"


def function(name, args):
    return {"type": "function_call", "call_id": str(uuid4()), "name": name, "arguments": json.dumps(args)}


@pytest.mark.parametrize("interval", [{}, {"start": 0, "end": 300}])
async def test_two_real_client_paths_and_decoded_media_over_mock_transport(monkeypatch, interval):
    seen = []
    edits = []

    def gateway(request):
        assert request.url.host == "workstation.invalid" and request.method in {"GET", "POST"}
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/v1/models":
            result = {"data": [{"id": "omni-default", "max_model_len": 32768}]}
        elif path == "/llm/v1/models":
            result = {"data": [{"id": "llm-default", "max_model_len": 262144}]}
        elif path in {"/v1/chat/prompt_tokens", "/llm/v1/chat/prompt_tokens"}:
            result = {"count": 128}
        elif path == "/llm/v1/responses" and "text" in body:
            fmt = body["text"]["format"]
            assert fmt["type"] == "json_schema" and fmt["strict"]
            assert set(fmt["schema"]["required"]) == {"color", "shape"}
            encoded = next(part["image_url"] for item in body["input"] if isinstance(item["content"], list)
                           for part in item["content"] if part["type"] == "input_image")
            assert encoded.startswith("data:image/png;base64,")
            with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as frame:
                pixel = frame.convert("RGB").getpixel((160, 100))
            color, shape = {(255, 255, 0): ("yellow", "triangle"), (0, 0, 255): ("blue", "square"),
                            (255, 0, 0): ("red", "circle")}[pixel]
            seen.append(shape)
            result = {"status": "completed", "id": str(uuid4()), "usage": {"input_tokens": 128, "output_tokens": 12},
                      "output": [{"type": "message", "content": [
                          {"type": "output_text", "text": json.dumps({"color": color, "shape": shape})},
                      ]}]}
        elif path == "/llm/v1/responses":
            brief = json.loads(body["input"][1]["content"])
            evidence = brief["saved_evidence"].copy()
            for item in body["input"]:
                if item.get("type") == "function_call_output":
                    evidence.update(json.loads(item["output"])["evidence"])
            tools = {tool["name"]: tool for tool in body["tools"]}
            allowed = {"inspect_video", "submit_materials", "ask_owner"}
            if any(replay.SOURCE in value for value in evidence.values()):
                assert set(tools) == allowed
            else:
                assert set(tools) == allowed | {"web_page"}
                assert tools["web_page"]["parameters"]["properties"]["web_fetch"]["enum"] == [replay.SOURCE]
            if not evidence:
                calls = [function("inspect_video", {"bvid": replay.BV, "part": 2, **interval}),
                         function("web_page", {"web_fetch": replay.SOURCE})]
            else:
                page = next(key for key, value in evidence.items() if replay.SOURCE in value)
                names = ["triangle"] if brief["prior_goals"] else ["triangle", "square"]
                items = [{"title": shape, "rationale": "适合引入主题", "sources": [
                    next(key for key, value in evidence.items() if shape in value), page]} for shape in names]
                for item in items:
                    item["primary_frame"] = item["sources"][0]
                calls = [function("submit_materials", {"items": items, "gaps": ["只有一个合格镜头"] if len(items) == 1 else []})]
            result = {"status": "completed", "id": str(uuid4()), "output": calls}
        elif path in {"/v1/chat/completions", "/llm/v1/chat/completions"}:
            content = body["messages"][-1]["content"]
            assert isinstance(content, str)
            if path == "/v1/chat/completions" and content in (replay.REQUEST, replay.CHANGE):
                schema = json.loads(body["structured_outputs"]["structural_tag"])["format"]["json_schema"]
                if schema["title"] == "ReplyDelivery":
                    assert "reply_markdown" not in schema["properties"]
                    decision = {"action": "work", "clock_fields": []}
                elif schema["title"] == "WorkLifecycle":
                    assert "reply_markdown" not in schema["properties"]
                    decision = {"action": "agent_task" if content == replay.REQUEST else "task_turn"}
                else:
                    assert schema["title"] == "CardTurnIntent" and content == replay.CHANGE and edits == [2]
                    card, = json.loads(body["messages"][1]["content"])["recent_task_cards"]
                    decision = {"intent": "实际修改同一任务或成果的部分或全部", "patch": {"field": "goal", "subject": "提纲",
                                "requirement": "改做三角形主题的开场"}, "reference": card["id"]}
            elif "person_subject" in body["messages"][0]["content"]:
                decision = {"person_subject": "地点、构图或物件"}
            elif content == replay.REQUEST:
                assert "requested_count" in body["messages"][0]["content"]
                decision = {"requested_count": 2, "usage": "列出形状、理由和时码", "playable": False,
                            "trace_origin": True, "clip_seconds": None}
            else:
                packet = json.loads(content)
                if "candidate" in packet:
                    assert path == "/llm/v1/chat/completions"
                    decision = packet["candidate"]
                    for item in decision["items"]:
                        frame = packet["evidence"][item["primary_frame"]]
                        item["title"] = next(shape for shape in ("triangle", "square") if shape in frame)
                        item["rationale"] = "我会用这个图形引入主题。"
                    if len(decision["items"]) == 1:
                        decision["gaps"] = ["本轮符合改口要求的只有一个镜头。"]
                    edits.append(len(decision["items"]))
                elif "上一版" in packet:
                    decision = {"ordered_actions": ["其它编辑"] * len(packet["上一版"])}
                elif "items_to_verify" in packet:
                    assert all(len(item["evidence"]) == 2 for item in packet["items_to_verify"])
                    decision = {"supported": True}
                else:
                    pytest.fail("unexpected completion packet")
            result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}],
                      "usage": {"prompt_tokens": 128}}
        else:
            pytest.fail(f"unexpected route: {path}")
        return httpx.Response(200, json=result)

    monkeypatch.setattr(http_api, "inference_transport", lambda base: httpx.MockTransport(gateway))
    async with asyncio.timeout(15):
        report = await replay.run({"OMNI_GATEWAY_URL": "https://workstation.invalid", "OMNI_STUDIO_TOKEN": uuid4().hex})
    assert report["status"] == "passed" and report["platform_writes"] == 0
    assert [edition["selected_seconds"] for edition in report["editions"]] == [[0, 4], [0]]
    assert seen == ["triangle", "square", "circle"] and edits == [2, 1]


@pytest.mark.parametrize("fault", ["duplicate", "spoiler", "missing_origin", "no_gap", "wrong_pts", "wrong_cid",
                                   "invented_time", "invented_origin", "spoiler_in_prose"])
def test_evidence_oracle_rejects_invalid_second_edition(fault):
    seconds = 9 if fault == "spoiler" else 0
    link = f"https://www.bilibili.com/video/{replay.BV}/?p=2&t={seconds}"
    frame = {"seconds": seconds, "part": 2, "cid": "local-p2", "text": "yellow triangle",
             "url": link, "kind": "实际解码画面"}
    if fault == "wrong_pts":
        frame["url"] = link + "7"
    if fault == "wrong_cid":
        frame["cid"] = "local-p1"
    video = {"id": "v", "call_id": "read", "tool": "inspect_video", "result": {"status": "completed", "findings": [frame]}}
    page = {"id": "w", "call_id": "page", "tool": "web_page", "result": {"url": replay.SOURCE, "excerpt": replay.NOTE}}
    item = {"title": "triangle", "rationale": "开场", "sources": ["v:1"] if fault == "missing_origin" else ["v:1", "w"]}
    item["rationale"] = {"invented_time": "看 00:17 的画面", "invented_origin": "原作见 https://other.invalid/",
                         "spoiler_in_prose": "后来会出现红色圆形"}.get(fault, item["rationale"])
    product = {"items": [item, item] if fault == "duplicate" else [item], "gaps": [] if fault == "no_gap" else ["不足两段"]}
    task = {"status": "completed", "revision": 2, "steps": [video, page], "plan": {"candidate": product, "selected": product},
            "message": "\n".join(replay.evidence_index([video, page]).values())}
    with pytest.raises(replay.ReplayMismatch):
        replay.validate_delivery(task, {"task_revision": 2, "body": task["message"]}, {seconds: {"color": "yellow", "shape": "triangle"}})
