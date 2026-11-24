"""Fake inference, vision, structured JSON, and browsing model doubles for tests."""

import asyncio
import base64
import json
import re

import httpx
import pytest

from tuber_studio.core.identity import provider_identity_messages
from tuber_studio.core.models import Reply
from tuber_studio.infra.inference.api_providers import APIProvider
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn
from tuber_studio.infra.inference.video_vision import ApiVideoEvidenceProvider
from tuber_studio.persona.context.compaction import approx_token_count


class OfflineChat:
    """Explicit offline diagnostic only; never labelled as real inference."""

    supports_memory_organization = False
    supports_video_research = False
    last_tokens = None
    server_tokens = None
    profile_error = "离线诊断，无真实128K模型能力"
    is_sleeping = None

    async def close(self):
        pass

    async def health(self):
        return {"available": True, "detail": "离线固定台词，不是模型推理"}

    async def count_tokens(self, messages):
        return sum(approx_token_count(m["content"]) + 8 for m in provider_identity_messages(self, messages))

    async def prefill(self, messages):
        return False

    async def summarize(self, messages, *, max_tokens=None):
        raise ValueError("离线诊断Provider没有模型摘要能力，请连接真实LLM")

    async def generate_json(self, messages, schema, *, max_tokens):
        raise ValueError("离线诊断Provider没有模型结构化生成能力，请连接真实LLM")

    async def generate(self, messages, character, event_id, source_text=None):
        return Reply(
            spoken_text="你好呀，先来一碗白饭，再陪你聊聊天。",
            emotion="cheerful",
            animation_id="talk_happy",
            reply_to=[event_id],
        )

    async def generate_operation(self, messages, character, event_id, source_text=None):
        return await self.generate(messages, character, event_id, source_text)


@pytest.fixture
async def providers(settings):
    opened = []

    async def create(
        *,
        count=3,
        actual=3,
        finish="stop",
        content='{"entries":["甲"]}',
        message_extra=None,
        thinking=False,
        context_source="declared",
        stream_response=None,
    ):
        config = settings.data["omni"] | {
            "context_source": context_source,
            "prompt_token_endpoint": False,
            "structured_output": "json_object",
            "base_url": "https://model.test/v1",
            "model": "actual-model",
            "context_tokens": 4096,
            "native_context_tokens": 131072,
            "enable_thinking": thinking,
            "seed": 42,
            "temperature": 0.8,
            "summary_temperature": 0.2,
            "compaction_max_tokens": 32,
        }
        captured = []

        def respond(request):
            captured.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": config["model"], "max_model_len": 32768}]})
            body = json.loads(request.content)
            if stream_response is not None and body.get("stream"):
                return stream_response(body)
            probe = body["max_tokens"] == 1
            message = {"content": None if probe else content, "reasoning_content": "私有推理"}
            if not probe:
                message.update(message_extra or {})
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": count if probe else actual},
                    "choices": [{"finish_reason": "length" if probe else finish, "message": message}],
                },
            )

        provider_client = APIProvider(config)
        await provider_client.client.aclose()
        provider_client.client = httpx.AsyncClient(
            base_url="https://model.test/v1/", transport=httpx.MockTransport(respond)
        )
        opened.append(provider_client)
        return provider_client, captured

    yield create
    for provider_client in opened:
        await provider_client.close()


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
META = {"parts": [{"cid": "34", "duration": 40}]}
FRAMES = [
    {
        "id": "first",
        "cid": "34",
        "actual_time": 10,
        "requested_time": 15,
        "kind": "storyboard",
        "image": PNG,
        "width": 1,
        "height": 1,
        "mime_type": "image/png",
    },
    {
        "id": "second",
        "cid": "34",
        "actual_time": 30,
        "requested_time": 25,
        "kind": "storyboard",
        "image": PNG,
        "width": 1,
        "height": 1,
        "mime_type": "image/png",
    },
]
CONFIG = {
    "model": "actual-vlm",
    "base_url": "http://127.0.0.1:18080/v1",
    "model_revision": "weights-sha",
    "runtime_revision": "runtime-sha",
    "max_frames": 2,
}


def generations(state):
    return [request for request in state["requests"] if request.method == "POST"]


@pytest.fixture
async def provider(runtime, request):
    value = ApiVideoEvidenceProvider(runtime, CONFIG | getattr(request, "param", {}))
    await value.api.client.aclose()
    state = {
        "requests": [],
        "result": {
            "sufficient": True,
            "summary": "两帧有不同图形",
            "used_frames": [1, 2],
            "limitations": ["仅抽样画面"],
        },
        "captions": {"first": "红色圆形", "second": "蓝色方形"},
        "caption_requests": 0,
        "window": 131072,
        "vision": True,
        "aliases": ["actual-vlm"],
        "block": False,
        "started": asyncio.Event(),
        "cancelled": asyncio.Event(),
    }

    async def handle(http_request):
        state["requests"].append(http_request)
        if http_request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": alias, "max_model_len": state["window"]} for alias in state["aliases"]]},
            )
        if http_request.url.path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "checkpoint": value.config.model_revision,
                    "engine_version": state.get("build_info", value.config.runtime_revision),
                },
            )
        assert http_request.url.path == "/v1/chat/completions"
        payload = json.loads(http_request.content)
        assert "tools" not in payload and not payload["stream"]
        caption = "caption" in payload.get("response_format", {}).get("json_schema", {}).get("schema", {}).get(
            "properties", {}
        )
        probe = payload["max_tokens"] == 1
        if (
            state["block"]
            or not probe
            and (
                state.get("block_assessment")
                and not caption
                or state.get("block_at_caption") == state["caption_requests"] + 1
                and caption
            )
        ):
            state["started"].set()
            try:
                await asyncio.Event().wait()
            finally:
                state["cancelled"].set()
        if probe:
            content = None
        elif caption:
            index = state["caption_requests"] % len(state["captions"])
            state["caption_requests"] += 1
            content = json.dumps({"caption": list(state["captions"].values())[index]})
        else:
            content = json.dumps(state["result"])
        message = state.get("message") or {"role": "assistant", "content": content}
        if not probe and state.get("after_generation"):
            state["after_generation"](payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length" if probe else state.get("finish", "stop"), "message": message}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1 if probe else 20},
            },
        )

    value.api.client = httpx.AsyncClient(
        base_url="http://127.0.0.1:18080/v1/", transport=httpx.MockTransport(handle)
    )
    yield value, state
    await value.close()


def model_packet(source):
    body = source.split("：", 1)[1] if "：" in source else source
    if not body.startswith("{"):
        header, content = body.split("\n内容:\n", 1)
        value = json.loads(header.rsplit("\n", 1)[-1])
        value["metadata"] = {"title": header.split("\n", 1)[0].removeprefix("标题:")}
        value["content"] = content
        value["segments"] = [{"id": reference} for reference in re.findall(r"(?m)^\[([^\|\]]+)\|", content)]
        return value
    if "\n\n内容：\n" in body and body.startswith("{"):
        header, _, content = body.partition("\n\n内容：\n")
        value = json.loads(header)
        value["content"] = content
        refs = re.findall(r"(?m)^\[([^\|\]]+)\|", content)
        if not refs:
            refs = [f"b{i}" for i, _ in enumerate(re.findall(r"(?m)^\[\d+:\d+", content), 1)]
        value["segments"] = [{"id": reference} for reference in refs]
        return value
    if "\n\n" in body and body.startswith("{"):
        header, content = body.split("\n\n", 1)
        try:
            value = json.loads(header)
            value["evidence"] = value.get("evidence") or []
            value["content"] = content
            return value
        except ValueError:
            pass
    header, separator, content = body.partition("\n\n内容：\n")
    value = json.loads(header)
    if separator:
        value["content"] = content
        refs = re.findall(r"(?m)^\[([^\|\]]+)\|", content)
        if not refs:
            refs = [f"b{i}" for i, _ in enumerate(re.findall(r"(?m)^\[\d+:\d+", content), 1)]
        value["segments"] = [{"id": reference} for reference in refs]
    return value


def batch_plan_wire(choices, *, workers=1, explanation=""):
    assert len({item["bvid"] for item in choices}) == len(choices)
    return {
        "workers": workers,
        "explanation": explanation,
        "choices": {
            item["bvid"]: {key: value for key, value in item.items() if key != "bvid"}
            | {"priority": len(choices) - index}
            for index, item in enumerate(choices)
        },
    }


class ControlledModel:
    def __init__(self):
        self.calls = []
        self.active = self.peak = 0
        self.interest = "like"
        self.worth = True
        self.response_action = "reply_public"
        self.bad_ref = False

    async def health(self):
        return {"ok": True}

    async def close(self):
        pass

    async def generate(self, messages, character, event_id, source_text=None):
        return Reply(
            spoken_text="鱼鱼在这里陪你聊，我们慢慢说。",
            emotion="cheerful",
            animation_id="talk",
            reply_to=[event_id],
        )

    async def create(
        self,
        input,
        *,
        tools=(),
        tool_choice=None,
        previous_response_id=None,
        instructions=None,
        max_output_tokens=None,
        store=False,
        thinking=True,
        **kwargs,
    ):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.001)
            text_parts = [
                str(item.get("content") or "")
                for item in input
                if isinstance(item, dict) and item.get("role") == "user"
            ]
            prompt_text = "\n".join(text_parts)
            prior_calls = [
                str(item.get("name") or "")
                for item in input
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if not prior_calls:
                self.calls.append(("ResponseDecision", input))
            if (
                "like_video" in prompt_text
                and self.interest in {"like", "not_interested"}
                and "like_video" not in prior_calls
                and any(t.get("name") == "like_video" for t in tools)
            ):
                return ResponseTurn(
                    id="controlled-like",
                    calls=(
                        FunctionCall(
                            call_id="call_like_1",
                            name="like_video",
                            arguments={
                                "like": self.interest == "like",
                                "favorite": self.interest == "like",
                                "reason": "盐水密度改变后鸡蛋浮起来的过程很有趣",
                            },
                        ),
                    ),
                    text="",
                    raw={},
                )
            if (
                "已折叠为目录" in prompt_text
                and "read_video_segment" not in prior_calls
                and any(t.get("name") == "read_video_segment" for t in tools)
            ):
                return ResponseTurn(
                    id="controlled-read-seg",
                    calls=(FunctionCall(call_id="call_seg_1", name="read_video_segment", arguments={"part": 1}),),
                    text="",
                    raw={},
                )
            action = self.response_action
            if "不可公开发言" in prompt_text or "只允许观察" in prompt_text or "不可发表公开留言" in prompt_text:
                action = "finish"
            if action in {"skip", "finish"}:
                return ResponseTurn(
                    id="controlled-skip",
                    calls=(
                        FunctionCall(
                            call_id="call_skip_1",
                            name="skip_reply",
                            arguments={"reason": action, "explanation": "看明白鸡蛋浮起来的过程了"},
                        ),
                    ),
                    text="",
                    raw={},
                )
            if action == "read_comments":
                return ResponseTurn(
                    id="controlled-comments",
                    calls=(
                        FunctionCall(
                            call_id="call_skip_rc",
                            name="skip_reply",
                            arguments={"reason": "read_comments", "explanation": "先看看评论里大家用了多少盐"},
                        ),
                    ),
                    text="",
                    raw={},
                )
            if action == "handoff_private":
                return ResponseTurn(
                    id="controlled-handoff",
                    calls=(
                        FunctionCall(
                            call_id="call_handoff_1",
                            name="queue_dm_handoff",
                            arguments={"reason": "confirmed_invitation", "summary": "讨论盐水鸡蛋实验"},
                        ),
                    ),
                    text="",
                    raw={},
                )
            return ResponseTurn(
                id="controlled-text",
                calls=(),
                text="盐水把鸡蛋托起来这段好有意思，鱼鱼也想试试！",
                raw={},
            )
        finally:
            self.active -= 1

    async def count_tokens(self, messages):
        return sum(len(m["content"]) for m in messages) // 2 + 1

    async def generate_json(self, messages, schema, *, max_tokens):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.001)
            if schema["title"] == "OutreachAssessment":
                data = json.loads(messages[-1]["content"])
                self.calls.append((schema["title"], data))
                return {
                    "action": "send",
                    "reason": "对方邀请我方就具体实验私下继续",
                    "purpose": data["purpose"] or "讨论盐水鸡蛋实验",
                    "invitation_kind": "invitation",
                    "invitation_comment_id": data["public_thread"]["source"]["comment_id"],
                    "invitation_quote": data["public_thread"]["source"]["text"],
                    "invited_actor_uid": data["self_account_uid"],
                    "body_addresses_purpose": True,
                }
            data = model_packet(messages[-1]["content"])
            self.calls.append((schema["title"], data))
            if schema["title"] == "BrowsePlan":
                return {"action": "recommend", "keywords": [], "reason": "explore", "delay_seconds": 300}
            if schema["title"] == "CandidateBatchPlan":
                return batch_plan_wire(
                    [
                        {
                            "bvid": item["bvid"],
                            "action": "watch" if index < data["max_videos"] else "skip",
                            "explanation": f"想看看{item['title']}的具体过程",
                        }
                        for index, item in enumerate(data["candidates"])
                    ],
                    workers=data["max_workers"],
                    explanation="看看这批视频里各自的实验结果",
                )
            if schema["title"] == "ContentDecision":
                segments = data.get("segments") or []
                return {
                    "interest": self.interest,
                    "worth_commenting": self.worth,
                    "reason": "specific_detail" if self.worth else "nothing_to_add",
                    "explanation": "加盐后鸡蛋浮起来这个细节想聊聊"
                    if self.worth
                    else "盐水实验看明白了，暂时没有补充",
                    "evidence_ids": ["missing"] if self.bad_ref else ([segments[0]["id"]] if segments else []),
                    "coverage_required": "provided",
                }
            if schema["title"] == "VideoEngagementDecision":
                return {
                    "should_like": True,
                    "like_reason": "视频很有趣味，值得点赞",
                    "should_favorite": True,
                    "favorite_reason": "优质干货，收藏备查",
                }
            action = self.response_action
            if data.get("video_actions") and not data.get("allow_public", True):
                action = "finish"
            evidence = data.get("evidence") or []
            return {
                "action": action,
                "text": "盐水把鸡蛋托起来这段好有意思，鱼鱼也想试试！" if action != "finish" else "",
                "reason": "specific_detail" if action in {"reply_public", "finish"} else "user_requested",
                "explanation": "想接着聊盐水如何把鸡蛋托起来",
                "evidence_ids": [evidence[0]["id"]] if evidence else [],
                **(
                    {"contact_purpose": "讨论盐水鸡蛋实验", "contact_trigger": "invitation"}
                    if action == "handoff_private"
                    else {}
                ),
                **(
                    {
                        "video_preference": self.interest,
                        "preference_reason": "盐水密度改变后鸡蛋浮起来的过程很有趣",
                    }
                    if data.get("video_actions")
                    else {}
                ),
            }
        finally:
            self.active -= 1


class DynamicModel:
    def __init__(self):
        self.calls = []
        self.schemas = []
        self.count_calls = []
        self.output = None
        self.summary_output = None
        self.summary_hook = None
        self.hook = None
        self.illustration = None

    async def create(
        self,
        input,
        *,
        tools=(),
        tool_choice=None,
        previous_response_id=None,
        instructions=None,
        max_output_tokens=None,
        store=False,
        thinking=True,
        **kwargs,
    ):
        self.calls.append(input)
        if self.hook:
            await self.hook()
        user_text = "\n".join(str(m.get("content") or "") for m in input if isinstance(m, dict) and m.get("role") == "user")
        if self.output is not None:
            try:
                parsed = json.loads(self.output)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                if parsed.get("action") == "wait":
                    return ResponseTurn(
                        id="dyn-skip",
                        calls=(
                            FunctionCall(
                                call_id="call_dyn_skip",
                                name="skip_reply",
                                arguments={"reason": "wait", "explanation": parsed.get("reason") or "这次先不发"},
                            ),
                        ),
                        text="",
                        raw={},
                    )
                return ResponseTurn(id="dyn-post", calls=(), text=str(parsed.get("content") or ""), raw={})
            return ResponseTurn(id="dyn-post", calls=(), text=str(self.output), raw={})
        if "最近没有强制要提的公开互动记录" in user_text and "本次指定分享主题" not in user_text:
            return ResponseTurn(
                id="dyn-wait",
                calls=(FunctionCall(call_id="call_dyn_wait", name="skip_reply", arguments={"reason": "wait", "explanation": "这次先不发"}),),
                text="",
                raw={},
            )
        if "[公开经历 ·" in user_text:
            return ResponseTurn(id="dyn-incident", calls=(), text="刚才回评论接错楼了，你消息是不是回错了那条赶紧记录一下。", raw={})
        if "[评论区互动" in user_text:
            match = re.search(r"说：「([^」]+)」", user_text)
            frag = (match.group(1)[:8] if match else "评论区")
            return ResponseTurn(id="dyn-reply", calls=(), text=f"海底小剧场：今天又多盛了一碗饭，刚和人聊到「{frag}」。", raw={})
        return ResponseTurn(id="dyn-story", calls=(), text="海底小剧场：今天又多盛了一碗饭。", raw={})

    async def generate_json(self, messages, schema, *, max_tokens):
        self.calls.append(messages)
        self.schemas.append(schema)
        payload = json.loads(messages[1]["content"])
        if payload.get("task") == "operations_memory_summary":
            if self.summary_hook:
                await self.summary_hook()
            return (
                json.loads(self.summary_output)
                if self.summary_output is not None
                else {
                    "entries": [
                        {"source_id": source["id"], "quote": source["text"]}
                        for source in payload["public_sources"]
                    ]
                }
            )
        if self.hook:
            await self.hook()
        if self.output is None and not payload["public_sources"]:
            return {
                "action": "wait",
                "reason": "这次先不发",
                "content": "",
                "source_ids": [],
                "next_evaluation_seconds": 1800,
            }
        source = next(iter(payload["public_sources"]), {})
        discussion = source.get("type") in {"public_incident", "published_reply"}
        return (
            json.loads(self.output)
            if self.output is not None
            else {
                "action": "post",
                "reason": "和大家分享公开的虚构故事",
                "content": "海底小剧场：今天又多盛了一碗饭。",
                "source_ids": [source["id"]],
                "link_source_ids": [source["id"]] if discussion else [],
                "illustration": self.illustration or ("comment_card" if discussion else "none"),
                "next_evaluation_seconds": 1800,
            }
        )

    async def count_tokens(self, messages):
        self.count_calls.append(messages)
        return len(messages[0]["content"]) // 2 + 1
