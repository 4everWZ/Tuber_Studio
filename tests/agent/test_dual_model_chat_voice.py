"""Regression tests for #55 dual-model role separation, strict chat preemption and voice-text task sharing."""

import asyncio
import json
from uuid import uuid4

import httpx

from tests.support.agent_helpers import wait_status
from tuber_studio.agent.chat.context import ChatContext
from tuber_studio.agent.chat.front_dialogue import acknowledge_delegation, paraphrase_delivery
from tuber_studio.agent.chat.models import DeliveryPacket
from tuber_studio.agent.tasks.history import build_shared_task_summary, select_reason_lines
from tuber_studio.agent.voice import origin as voice_origin
from tuber_studio.agent.voice import work as private_call_work
from tuber_studio.app import create_app
from tuber_studio.core.config import Settings, chat_profile
from tuber_studio.infra.inference import http_api as http_api
from tuber_studio.infra.inference.api_providers import APIProvider
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn
from tuber_studio.operations.kernel.turns import OperationTurns


def test_chat_profile_derives_text_llm_with_thinking_disabled():
    omni = {
        "base_url": "https://gateway.example/v1",
        "model": "omni-default",
        "responses_model": "llm-default",
        "max_tokens": 2048,
        "temperature": 0.4,
    }
    derived = chat_profile(omni)
    assert derived["base_url"] == "https://gateway.example/llm/v1"
    assert derived["model"] == "llm-default"
    assert derived["enable_thinking"] is False
    assert derived["max_tokens"] == 2048
    assert derived["temperature"] == 0.4


async def test_text_chat_uses_chat_llm_only_and_never_calls_omni(tmp_path, monkeypatch):
    seen_paths = []
    seen_chat_bodies = []

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "omni-default", "max_model_len": 8192}]})
        if request.url.path == "/llm/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llm-default", "max_model_len": 16384}]})
        if request.url.path == "/llm/v1/chat/prompt_tokens":
            return httpx.Response(200, json={"count": 24})
        if request.url.path == "/v1/chat/completions":
            raise AssertionError("Foreground text chat must NEVER call omni-default (/v1/chat/completions)")

        body = json.loads(request.content)
        if request.url.path == "/llm/v1/responses":
            return httpx.Response(
                200,
                json={
                    "id": "resp-chat",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "chat",
                            "call_id": "call-chat",
                            "arguments": "{}",
                        }
                    ],
                },
            )
        if request.url.path == "/llm/v1/chat/completions":
            seen_chat_bodies.append(body)
            if body.get("response_format") or "JSON Schema" in json.dumps(
                body["messages"], ensure_ascii=False
            ):
                content = json.dumps({"edition": 1, "evidence": "recorded", "lines": [1]})
            elif body.get("stream"):
                payload = json.dumps(
                    {"choices": [{"delta": {"content": "海风很轻。"}, "finish_reason": "stop"}]}
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=f"data: {payload}\n\ndata: [DONE]\n\n".encode(),
                )
            else:
                content = "海风很轻。"
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                    "usage": {"prompt_tokens": 32, "completion_tokens": 8},
                },
            )
        raise AssertionError(f"Unexpected path {request.url.path}")

    monkeypatch.setattr(http_api, "inference_transport", lambda _: httpx.MockTransport(gateway_handler))
    cfg = Settings(runtime=tmp_path)
    cfg.data["omni"].update(
        base_url="https://gateway.example/v1",
        model="omni-default",
        responses_model="llm-default",
    )
    cfg.provider_keys = {"omni": "test-omni-key"}

    app = create_app(cfg)
    runtime = app.state.runtime
    assert isinstance(runtime.llm, APIProvider)
    assert isinstance(runtime.chat_llm, APIProvider)
    assert runtime.llm.config["model"] == "omni-default"
    assert runtime.chat_llm.config["model"] == "llm-default"
    assert runtime.chat_llm.config.get("enable_thinking") is False
    assert runtime.foreground_chat_llm() is runtime.chat_llm

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {cfg.admin_token}"},
        ) as client,
    ):
        reply = (
            (
                await client.post(
                    "/api/chat/intelligent", json={"request_id": str(uuid4()), "message": "海风怎么样？"}
                )
            )
            .raise_for_status()
            .json()
        )
        assert reply["reply_text"] == "海风很轻。"

        chat_ctx = ChatContext(runtime, runtime.store, runtime.browsing, app.state.dynamics)
        ack = await acknowledge_delegation(
            chat_ctx,
            runtime.character,
            str(uuid4()),
            "帮我看看新闻",
            DeliveryPacket(kind="task_accepted", summary="已交给后台。"),
        )
        assert ack

        paraphrased = await paraphrase_delivery(
            chat_ctx,
            runtime.character,
            chat_ctx.account(),
            str(uuid4()),
            type("Req", (), {"message": "有什么发现"})(),
            {"reply_text": "已查完。"},
            DeliveryPacket(kind="task_completed", summary="已查到新记录"),
            None,
        )
        assert "海风很轻。" in paraphrased["reply_text"]

        selected, lines = await select_reason_lines(
            runtime.foreground_chat_llm(),
            "为什么选第一句",
            [{"edition": 1, "author": "大肥鱼", "body": "选材理由：海湾风景开阔。"}],
            None,
        )
        assert selected["edition"] == 1 and lines == [(1, "选材理由：海湾风景开阔。")]

    assert "/v1/chat/completions" not in seen_paths
    foreground_bodies = [
        body for body in seen_chat_bodies if "读这位说话人自己的话" not in body["messages"][0]["content"]
    ]
    assert len(foreground_bodies) >= 4
    for body in foreground_bodies:
        assert body["model"] == "llm-default"
        assert body["chat_template_kwargs"]["enable_thinking"] is False


async def test_operation_turns_strict_chat_preemption_over_background():
    gate = asyncio.Lock()
    turns = OperationTurns(gate, lambda: False)
    order = []

    holder = await turns.acquire("background", ready_at=1.0)

    async def worker(name, lane, ready_at):
        ticket = await turns.acquire(lane, ready_at=ready_at)
        try:
            turns.started(ticket)
            order.append(name)
        finally:
            turns.release(ticket)

    bg1 = asyncio.create_task(worker("bg-1", "background", 2.0))
    direct1 = asyncio.create_task(worker("direct-1", "direct", 2.1))
    prio1 = asyncio.create_task(worker("prio-1", "priority", 2.2))
    direct2 = asyncio.create_task(worker("direct-2", "direct", 2.3))
    direct3 = asyncio.create_task(worker("direct-3", "direct", 2.4))
    chat1 = asyncio.create_task(worker("chat-1", "chat", 5.0))
    chat2 = asyncio.create_task(worker("chat-2", "chat", 5.1))
    chat3 = asyncio.create_task(worker("chat-3", "chat", 5.2))
    await asyncio.sleep(0.03)

    turns.release(holder)
    await asyncio.gather(bg1, direct1, prio1, direct2, direct3, chat1, chat2, chat3)

    # All queued chat turns execute consecutively before any background lane; then 2:1 direct/background fairness holds.
    assert order[:3] == ["chat-1", "chat-2", "chat-3"]
    assert order[3:] == ["direct-1", "direct-2", "prio-1", "direct-3", "bg-1"]


async def test_runtime_background_yields_between_steps_for_foreground_chat(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    events = []
    step1_entered = asyncio.Event()
    release_step1 = asyncio.Event()

    async def background_job():
        async with runtime.offline_generation("background"):
            events.append("bg-step-1-start")
            step1_entered.set()
            await release_step1.wait()
            events.append("bg-step-1-end")
        async with runtime.offline_generation("background"):
            events.append("bg-step-2")

    async def foreground_chat(tag):
        async with runtime.chat_text_generation():
            events.append(tag)

    bg = asyncio.create_task(background_job())
    await step1_entered.wait()
    c1 = asyncio.create_task(foreground_chat("chat-a"))
    c2 = asyncio.create_task(foreground_chat("chat-b"))
    await asyncio.sleep(0.02)
    release_step1.set()
    await asyncio.gather(bg, c1, c2)

    assert events.index("chat-a") < events.index("bg-step-2")
    assert events.index("chat-b") < events.index("bg-step-2")
    await runtime.close()
    app.state.store.close()


async def test_delegate_persists_task_before_ack_and_isolates_raw_tool_logs(settings, monkeypatch):
    app = create_app(settings)
    runtime = app.state.runtime
    raw_tool_secret = "RAW_TOOL_LOG_INTERMEDIATE_SECRET_9912"
    ack_observed_tasks = []
    release_step = asyncio.Event()

    async def fake_router(**kwargs):
        return ResponseTurn(
            "resp-delegate",
            (
                FunctionCall(
                    "call-del",
                    "delegate_task",
                    {"goal": "整理今天的码头公开记录", "deliverable": "码头公开记录摘要"},
                ),
            ),
            "",
            {},
        )

    async def fake_chat_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        # Verify raw intermediate tool logs from completed background steps never pollute foreground messages.
        serialized = json.dumps(messages, ensure_ascii=False)
        assert raw_tool_secret not in serialized
        # Verify the newly delegated task row is already persisted with a real task_id before ack runs!
        active = runtime.tasks.activity()
        assert active
        if not ack_observed_tasks:
            assert runtime.tasks.current(active[0]["id"])["status"] in {"queued", "running"}
        ack_observed_tasks.append(active[0]["id"])
        text = "已经记下交给后台查了。"
        if on_content:
            on_content(text)
        return text

    async def fake_step(task):
        await release_step.wait()
        steps = [
            {
                "id": "step-raw",
                "tool": "web_page",
                "arguments": {"url": "https://example.org/dock"},
                "result": {"excerpt": raw_tool_secret},
            }
        ]
        runtime.tasks._save(
            task,
            status="completed",
            steps=steps,
            message="码头公开记录已核对完成。",
            plan={"backend": "responses", "prior_goals": []},
        )
        return runtime.tasks.current(task["id"])

    monkeypatch.setattr(runtime.chat_llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.chat_llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.chat_llm, "generate_text_stream", fake_chat_stream, raising=False)
    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(
        runtime.llm,
        "generate_text_stream",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Omni must not run text chat ack")),
        raising=False,
    )
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)
    monkeypatch.setattr(runtime.tasks, "_step", fake_step)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {settings.admin_token}"},
        ) as client,
    ):
        streamed = (
            await client.post(
                "/api/chat/intelligent/stream",
                json={"request_id": str(uuid4()), "message": "帮我把今天的码头公开记录交给后台查一下"},
            )
        ).raise_for_status()
        done_payload = next(
            json.loads(part.split("\ndata: ")[1])
            for part in streamed.text.split("\n\n")
            if part.startswith("event: done")
        )
        task_id = done_payload["card"]["data"]["id"]
        assert ack_observed_tasks == [task_id]
        release_step.set()
        completed = await wait_status(runtime.tasks, task_id, "completed")
        assert completed["message"] == "码头公开记录已核对完成。"

        # Follow-up normal chat must NOT contain raw tool result logs in foreground chat messages.
        async def second_router(**kwargs):
            return ResponseTurn("resp-chat-2", (FunctionCall("call-c2", "chat", {}),), "", {})

        monkeypatch.setattr(runtime.tasks, "responses_client", lambda: second_router)
        follow = (
            (
                await client.post(
                    "/api/chat/intelligent", json={"request_id": str(uuid4()), "message": "继续聊聊今天"}
                )
            )
            .raise_for_status()
            .json()
        )
        assert follow["reply_text"] == "已经记下交给后台查了。"


async def test_voice_delegate_via_omni_survives_hangup_and_shares_task_without_raw_transcript(
    settings, monkeypatch
):
    gateway_key = "voice-gateway-secret"
    configured_key = settings.provider_key
    monkeypatch.setattr(
        settings, "provider_key", lambda name: gateway_key if name == "omni" else configured_key(name)
    )
    app = create_app(settings)
    runtime = app.state.runtime

    raw_voice_casual_secret = "【私有通话原始闲聊_DO_NOT_LEAK】码头喝了热可可"
    voice_delegate_transcript = "去帮我把灯塔检修记录查出来"
    task_done_text = "灯塔检修记录已查到三条。"
    chat_seen_prompts = []

    async def route_voice(chat, text, tasks):
        if text == voice_delegate_transcript:
            return private_call_work.VoicePlan(
                action="delegate", quote=voice_delegate_transcript, task_id=None
            )
        return private_call_work.VoicePlan(action="conversation", quote=text, task_id=None)

    async def fake_step(task):
        # Simulate task still running even after the voice call ends.
        await asyncio.sleep(0.04)
        runtime.tasks._save(
            task,
            status="completed",
            steps=[],
            message=task_done_text,
            plan={"backend": "responses", "prior_goals": []},
        )
        return runtime.tasks.current(task["id"])

    async def fake_router(**kwargs):
        return ResponseTurn("resp-text-chat", (FunctionCall("call-t", "chat", {}),), "", {})

    async def fake_chat_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        chat_seen_prompts.append(messages)
        serialized = json.dumps(messages, ensure_ascii=False)
        assert raw_voice_casual_secret not in serialized
        assert voice_delegate_transcript in serialized
        reply = "后台那边已经查完同一项工作啦。"
        if on_content:
            on_content(reply)
        return reply

    monkeypatch.setattr(private_call_work, "classify", route_voice)
    monkeypatch.setattr(runtime.tasks, "_step", fake_step)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)
    monkeypatch.setattr(runtime.chat_llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.chat_llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.chat_llm, "generate_text_stream", fake_chat_stream, raising=False)
    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(
        runtime.llm,
        "generate_text_stream",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("Text chat after voice hangup must use chat_llm")
        ),
        raising=False,
    )

    ticket = voice_origin.ticket_context(gateway_key, "voice-call-1", runtime.tasks.scope())

    def signed_origin(capture_id, transcript):
        fields = ["omni.mic.v1", ticket["ticket_hash"], capture_id, 48000, transcript]
        return {
            "origin": {
                **ticket,
                "machine_text": transcript,
                "capture_id": capture_id,
                "samples": 48000,
                "receipt": voice_origin.sign(gateway_key, fields),
                "transcript_receipt": voice_origin.sign(gateway_key, ["omni.asr.v1", *fields[1:]]),
            }
        }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {settings.admin_token}"},
        ) as client,
    ):
        # Send a casual voice turn and ensure a call_origin record is never exposed to text assistant_history().
        casual = (
            (
                await client.post(
                    "/api/chat/private-call/utterances",
                    json=signed_origin("utt-casual", raw_voice_casual_secret),
                )
            )
            .raise_for_status()
            .json()
        )
        assert casual == {"turn_id": None}
        chat_ctx = ChatContext(runtime, runtime.store, runtime.browsing, app.state.dynamics)
        runtime.store.execute(
            "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
            (
                str(uuid4()),
                runtime.character["id"],
                runtime.character["version"],
                raw_voice_casual_secret,
                json.dumps(
                    {
                        "private": True,
                        "assistant_context": True,
                        "assistant_read_scope": chat_ctx.binding(runtime.character),
                        "account_id": chat_ctx.account(),
                        "reply_text": raw_voice_casual_secret + "的语音回声",
                        "call_origin": {"id": ticket["ticket_hash"], "utterance": "utt-casual"},
                    },
                    ensure_ascii=False,
                ),
                1.0,
            ),
        )

        delegated = (
            (
                await client.post(
                    "/api/chat/private-call/utterances",
                    json=signed_origin("utt-del", voice_delegate_transcript),
                )
            )
            .raise_for_status()
            .json()
        )
        task_id = delegated["turn_id"]
        assert task_id and runtime.tasks.current(task_id)["goal"] == voice_delegate_transcript

        # Hangup / leave voice call (background task in runtime.tasks keeps executing to completion).
        completed = await wait_status(runtime.tasks, task_id, "completed")
        assert completed["message"] == task_done_text

        assert chat_ctx.assistant_history(runtime.character, chat_ctx.account()) == []

        shared = build_shared_task_summary(
            runtime, chat_ctx, runtime.character, chat_ctx.account(), include_ids=True
        )
        assert any(item["id"] == task_id and item["status"] == "completed" for item in shared)

        # Text Chats shares the task reference via task_context while raw voice utterance remains isolated from chat_llm.
        task_ctx = chat_ctx.task_context(
            runtime.character,
            chat_ctx.account(),
            type(
                "D",
                (),
                {
                    "intent": None,
                    "creative_goal_id": None,
                    "message": "查一下刚才的灯塔任务",
                    "opened_artifact": None,
                },
            )(),
        )
        assert any(card["id"] == task_id for card in task_ctx.get("recent_task_cards", []))

        reply = (
            (
                await client.post(
                    "/api/chat/intelligent", json={"request_id": str(uuid4()), "message": "现在聊点日常"}
                )
            )
            .raise_for_status()
            .json()
        )
        assert reply["reply_text"] == "后台那边已经查完同一项工作啦。"
        assert chat_seen_prompts
