"""Route verified microphone turns to Chats, and read back the same task card."""

import asyncio
import json
import logging
import re
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from ..http_api import InferenceQueueBusy
from ..models import StrictModel
from ..omni_speech import sentences
from ..operation_continuations import content_hash
from ..store import dump
from .operator_chat import ChatIntent, ChatTurn
from .task_delegation import function
from .voice_origin import CallOrigin, VoiceOrigin, verify_call, verify_origin


class Utterance(StrictModel):
    origin: VoiceOrigin
    confirmation: str | None = Field(default=None, min_length=1, max_length=2000)


class Readback(StrictModel):
    call: CallOrigin
    revision: int = Field(strict=True, ge=1)


class VoicePlan(StrictModel):
    action: Literal["conversation", "delegate", "revise", "cancel"]
    quote: str = Field(default="", max_length=2000)
    task_id: UUID | None = None


VOICE_ROUTING = (
    "判断当前这句号主来话是否明确交给工坊处理一项工作。只作分流。"
    "原话里的「我」是号主，「你」是工坊。由号主指定工坊去做时才交办。"
    "号主说自己要去做、把结果发给听者、或者复述他人的吩咐，属于 conversation。"
    "普通接话、事实问答、讨论、转述、否定和假设归 conversation。"
    "要求接下来查找并交付资料、制作成果或稍后执行具体工作，可以交办。"
    "新交办选 delegate，目标和交付动作都须明确。改口选 revise，停止选 cancel，这两类都须指向本次通话里的唯一任务。"
    "quote 原样摘录能确定受托方和工作目标的连续原话。任务列表仅用于识别改口和取消。"
)
log = logging.getLogger("uvicorn.error").getChild("private_call")


async def classify(chat, words, tasks):
    packet = {"utterance": words, "call_tasks": tasks}
    client = chat._planning_client(chat.runtime.llm)
    if client is None:
        return await chat._model(VOICE_ROUTING, packet, VoicePlan, max_tokens=192)
    quote = {"type": "string", "minLength": 2, "maxLength": 2000}
    def route(name, description, properties):
        schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
        return function(name, description, schema)

    menu = [route("conversation", "继续当前语音对话。", {}),
            route("delegate", "号主明确交办新工作并说明目标。", {"quote": quote})]
    if tasks:
        target = {"quote": quote, "task_id": {"type": "string", "enum": [item["id"] for item in tasks]}}
        menu.extend([route("revise", "号主明确修改指定交办的要求。", target),
                     route("cancel", "号主明确停止指定交办。", target)])
    async with chat._generation_slot():
        reply = await client(
            input=[{"role": "system", "content": VOICE_ROUTING}, {"role": "user", "content": dump(packet)}],
            tools=menu, tool_choice="required", max_output_tokens=512, store=False, thinking=False,
        )
    if len(reply.calls) != 1:
        raise ValueError("expected one voice route")
    call, = reply.calls
    if call.name not in {item["name"] for item in menu}:
        raise ValueError("unknown voice route")
    return VoicePlan.model_validate(call.arguments | {"action": call.name})


def register_call_work(app, runtime, require_admin, chat):
    path = "/api/chat/private-call"
    dependencies = [Depends(require_admin)]

    def scope(call):
        key = runtime.settings.provider_key("omni")
        if not key:
            raise HTTPException(409, "通话授权已变化，请重新开始通话")
        grant = runtime.tasks.scope()
        verify_call(call, key, grant)
        return key, grant

    def work(call, grant):
        rows = runtime.store.all(
            "SELECT request_id FROM operator_chat_turns WHERE character_id=? AND character_version=? "
            "AND json_extract(result,'$.owner_source')=? AND json_extract(result,'$.call_origin.id')=? "
            "AND json_extract(result,'$.execution_scope')=json(?) AND json_extract(result,'$.card.type')='agent_task' "
            "ORDER BY created_at,request_id",
            (grant[1], grant[2], chat.private_owner, call.ticket_hash, dump(grant)),
        )
        return [task for row in rows if (task := runtime.tasks.executable(row["request_id"]))]

    def receipt(request_id, origin, grant):
        row = chat._turn(str(request_id))
        if not row:
            return None
        saved = json.loads(row["result"])
        if (saved.get("call_origin") != origin or saved.get("execution_scope") != grant
                or saved.get("owner_source") != chat.private_owner):
            raise HTTPException(409, "这句话已绑定其他通话")
        card = saved.get("card") or {}
        return {
            "turn_id": request_id,
            "task_id": card.get("data", {}).get("id") or saved.get("task_reference"),
            "message": saved["reply_text"],
        }

    @app.post(path + "/utterances", dependencies=dependencies)
    async def utterance(data: Utterance):
        origin = data.origin
        key, grant = scope(origin)
        verify_origin(origin, key, grant, for_routing=data.confirmation is None)
        request_id = uuid5(NAMESPACE_URL, f"studio.private-call/{origin.ticket_hash}/{origin.capture_id}")
        source = {"id": origin.ticket_hash, "utterance": origin.capture_id}
        if saved := receipt(request_id, source, grant):
            return saved
        words = origin.machine_text
        task = None
        if data.confirmation is None:
            candidates = work(origin, grant)[-4:]
            packet = [{key: item[key] for key in ("id", "goal", "status")} for item in candidates]
            try:
                async with asyncio.timeout(35):
                    plan = await classify(chat, words, packet)
            except (ValueError, TimeoutError, httpx.HTTPError, HTTPException, InferenceQueueBusy) as error:
                log.info("Voice routing unavailable: %s", type(error).__name__)
                raise HTTPException(503, "暂时无法识别语音交办，可在通话转写中交办。") from error
            if scope(origin) != (key, grant):
                raise HTTPException(409, "通话授权已变化，请重新开始通话")
            if plan.action == "conversation" or not plan.quote.strip() or plan.quote not in words:
                return {"turn_id": None}
            if plan.action != "delegate":
                task = next((item for item in candidates if item["id"] == str(plan.task_id)), None)
                if task is None:
                    return {"turn_id": None}
            action = {"delegate": "agent_task", "revise": "steer_task", "cancel": "cancel_task"}[plan.action]
        else:
            words = data.confirmation.strip()
            if not words:
                raise HTTPException(422, "请填写要交办的原话。")
            action = "agent_task"
        if saved := receipt(request_id, source, grant):
            return saved
        await chat.send(ChatTurn(request_id=request_id, message=words, call_scope=content_hash(grant),
                                 intent=ChatIntent(action=action, reference=task["id"] if task else None)),
                        call_origin=source, call_task=task)
        return receipt(request_id, source, grant)

    @app.post(path + "/work/status", dependencies=dependencies)
    async def work_status(call: CallOrigin):
        _, grant = scope(call)
        fields = ("id", "goal", "revision", "status", "message", "updated")
        return {"tasks": [{key: view[key] for key in fields} for task in work(call, grant)
                          if (view := runtime.tasks.view(task))]}

    @app.post(path + "/work/{task_id}/read", dependencies=dependencies)
    async def read_result(task_id: UUID, data: Readback):
        def current():
            _, grant = scope(data.call)
            task = next((item for item in work(data.call, grant) if item["id"] == str(task_id)), None)
            if task is None or task["revision"] != data.revision or task["status"] != "completed":
                raise HTTPException(409, "交办结果已更新")
            return runtime.tasks.view(task)

        view = current()
        if view["status"] != "completed" or not view["message"]:
            raise HTTPException(409, "当前没有可朗读的结果")
        # Read the first actionable paragraph while the full delivery stays in Chats.
        block = view["message"].partition("\n\n")[0]
        body = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)|https?://\S+", lambda match: match[1] or "", block)
        bullet = next((line[2:].strip() for line in body.splitlines() if line.startswith("- ")), "")
        plain = re.sub(r"(?m)^\s*(?:#{1,6}|[-*])\s+|[\\`*]", "", bullet or body).strip()
        parts = [part.strip() for part in sentences(plain)]
        text = next((part for part in parts if part.endswith(("。", "！", "？", ".", "!", "?"))), "")
        text = text or (parts[0] if parts else "交办已完成，结果已经发到对话里。")
        if len(text) > 120:
            text = text[:120].rstrip() + "。"
        try:
            audio = await runtime.voices.synthesize_live(text, runtime.character["voice_profile"])
        except (OSError, ValueError, TimeoutError, httpx.HTTPError, InferenceQueueBusy) as error:
            log.info("Call readback unavailable: %s", type(error).__name__)
            raise HTTPException(503, "结果暂时无法朗读") from error
        latest = current()
        if latest["status"] != view["status"] or latest["message"] != view["message"]:
            raise HTTPException(409, "交办结果已更新")
        return FileResponse(audio.path, media_type="audio/wav", headers={"Cache-Control": "no-store"})
