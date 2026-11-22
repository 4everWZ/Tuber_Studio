"""Mint an ordinary Omni browser ticket for the active connection."""

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from ...core.config import omni_base
from ...core.identity import operating_account_facts, private_participant_instructions
from ...core.models import content_hash
from ...core.store import dump
from ...core.validation import endpoint
from ...persona.characters.front_persona import persona_instructions
from ...persona.characters.performance import performance
from ...persona.memory.daily import contains_memory_credential
from ...persona.memory.sources import original_turn
from ..memory_organizer import is_chat_turn
from ..memory_organizer.owner_memory import citation, correctable_preference, private_rows, read_stamp
from ..tasks.history import build_shared_task_summary, history_preference
from .origin import ticket_context


class CallOptions(BaseModel):
    temporary: bool = False
    topic: str = Field(default="", max_length=500)
    topic_turn: UUID | None = None


def shared_call_facts(runtime, operator_chat, turn_id):
    if operator_chat is None:
        return []
    character = runtime.character
    account = str(runtime.tasks.scope()[0] or "")
    context = getattr(operator_chat, "context", operator_chat)
    return build_shared_task_summary(runtime, context, character, account, turn_id)


def inherited_topic(runtime, turn_id):
    memory = runtime.daily_memory
    scope = runtime.memory_organizer.scope()
    account, character, version = scope
    owner = memory.private_owner
    if not owner or not history_preference(runtime)["history_enabled"] or not memory.preference(scope)["enabled"]:
        return ""
    # The chat reader supplies the visible turn ID. Verify its kind and age before reading its words.
    now = time.time()
    row = runtime.store.one(
        "SELECT result FROM operator_chat_turns WHERE request_id=? AND character_id=? AND created_at BETWEEN ? AND ? "
        "AND json_extract(result,'$.owner_source')=? AND ((character_version=? AND json_extract(result,'$.account_id')=?) "
        "OR json_extract(result,'$.private_owner')=?)",
        (str(turn_id), character, now - 30 * 60, now, owner, version, account, owner),
    )
    if not row:
        return ""
    saved = json.loads(row["result"])
    if not saved.get("private") or saved.get("temporary") or saved.get("task_reference") or not is_chat_turn(saved):
        return ""
    key = f"turn:{turn_id}"
    source = original_turn(runtime.store.db, character, key)
    if not source:
        return ""
    if not memory.history_turn_available((source["account"], character, source["character_version"]), key, owner):
        # A corrected turn can still identify the current preference without
        # supplying the superseded utterance as the call's topic.
        return key if memory.lookup_source(scope, "operator", key) else ""
    words = source["message"].strip()
    return words if not contains_memory_credential(words) else ""


def browser_socket_origin(service_origin):
    public = os.environ.get("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN")
    deployed = os.environ.get("OMNI_GATEWAY_URL")
    if public and deployed and omni_base(deployed) == service_origin + "/v1":
        public = endpoint(public)
        if urlsplit(public).scheme != "https" or urlsplit(public).path:
            raise ValueError("私聊浏览器入口须为 HTTPS 源")
        return public.replace("https", "wss", 1)
    parsed = urlsplit(service_origin)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("私聊浏览器入口须为 HTTPS 或回环源")
    return service_origin.replace("http", "ws", 1)


def private_service(settings):
    origin = settings.data["omni"]["base_url"].removesuffix("/v1")
    token = settings.provider_key("omni")
    if not token:
        return None
    try:
        browser_socket_origin(origin)
    except ValueError:
        return None
    return origin, token


async def mint_ticket(origin, token):
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
        response = await client.post(
            origin + "/v1/browser-tickets", headers={"Authorization": "Bearer " + token},
            json={"path": "/v1/realtime"},
        )
        response.raise_for_status()
        data = response.json()
    ticket = data.get("ticket") if isinstance(data, dict) else None
    if not isinstance(ticket, str) or ticket == token or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", ticket):
        raise ValueError("invalid ticket")
    return ticket


def call_avatar(settings, character):
    idle = None
    if character.get("avatar_manifest"):
        try:
            entries = settings.assets(character)
        except (TypeError, AttributeError):
            entries = []
        for entry in entries:
            path = entry.get("path", "")
            if entry.get("tag") != "idle":
                continue
            if not path.startswith("assets/") or Path(path).suffix.lower() not in {".png", ".gif", ".webp", ".jpg", ".jpeg"}:
                continue
            try:
                file = settings.asset_path(path)
            except (OSError, ValueError):
                continue
            idle = "/" + file.relative_to(settings.root).as_posix()
            break
    return {"still": idle, "speaking": None, "fallback": idle}


RECORDING_ROUTE = "/api/v1/agent/private-call/recordings"


def call_recordings(runtime, character):
    """Return available private-call preview recordings keyed by profile."""
    if character != runtime.character["id"] or not re.fullmatch("[a-zA-Z0-9_-]+", character):
        return {}
    base = runtime.settings.runtime / "private-call-previews" / character
    if base.is_symlink() or base.parent.is_symlink():
        return {}
    files = {}
    for profile in ("mature", "q"):
        path = base / (profile + ".mp4")
        if path.is_file() and not path.is_symlink():
            files[profile] = path
    return files


def call_availability(runtime):
    """Return private-call readiness and available preview recording URLs."""
    character = runtime.character["id"]
    result = {
        "configured": private_service(runtime.settings) is not None,
        "character": runtime.store.display_name(character),
    }
    if profiles := call_recordings(runtime, character):
        result["recordings"] = {p: f"{RECORDING_ROUTE}/{character}/{p}.mp4" for p in profiles}
    return result


async def read_call_voice(runtime, character):
    """Load the active character's reference WAV bytes for realtime voice setup."""
    try:
        voice = runtime.settings.voice(character["voice_profile"])
        reference = await asyncio.to_thread(runtime.voices._read_reference, voice)
    except (OSError, ValueError):
        raise HTTPException(409, "当前音色缺少实时参考录音，请在角色卡中选择可用音色。") from None
    return voice, reference


async def create_call_ticket(request_origin: str, root_path: str, runtime, operator_chat, options: CallOptions):
    """Mint a realtime Omni ticket and assemble scoped voice call instructions."""
    service = private_service(runtime.settings)
    if service is None:
        raise HTTPException(503, "尚未配置可用的 Omni 私聊连接")
    origin, token = service
    grant = runtime.tasks.scope()
    character = runtime.character
    voice, reference = await read_call_voice(runtime, character)
    try:
        ticket = await mint_ticket(origin, token)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 429:
            raise HTTPException(429, "语音服务繁忙，请稍后重试") from None
        raise HTTPException(502, "语音服务未签发私聊票据，请核对应用凭据与服务地址") from None
    except (httpx.RequestError, ValueError):
        raise HTTPException(502, "私聊语音服务不可用") from None
    if runtime.tasks.scope() != grant or private_service(runtime.settings) != service:
        raise HTTPException(409, "角色、授权或 Omni 连接已变化，请重新开始通话")
    topic = "" if options.temporary else options.topic.strip()
    inherited = not options.temporary and not topic and options.topic_turn is not None
    if inherited:
        topic = inherited_topic(runtime, options.topic_turn)
    shared = shared_call_facts(runtime, operator_chat, options.topic_turn) if inherited else []
    rows = private_rows(runtime, topic) if topic else []
    topic_source = None
    if inherited and (rows or shared):
        topic_source = {
            "turn_id": options.topic_turn,
            "history_revision": history_preference(runtime)["history_revision"],
            "topic_stamp": content_hash(topic) if rows else None,
            **({"shared_stamp": content_hash(shared)} if shared else {}),
        }
    # A retained headline or source quote may contain the superseded preference.
    notes = [row["anecdote"] for row in rows]
    instructions = [
        persona_instructions(character),
        private_participant_instructions(
            character, operating_account=operating_account_facts(runtime.account.status())
        ),
    ]
    if notes:
        instructions.append("与本次私聊话题有关的记忆资料：" + dump(notes))
    if shared:
        instructions.append("接通时的工作台状态：" + dump(shared))
    instruction_text = "\n".join(instructions)
    # The deployed gateway supports an 8 MiB realtime message. Account for
    # the encoded reference, role instructions and session.update envelope.
    encoded_bytes = 4 * ((len(reference) + 2) // 3)
    if encoded_bytes + len(dump({"instructions": instruction_text}).encode()) + 512 > 8 * 1024 * 1024:
        raise HTTPException(409, "参考录音超过实时网关的单条消息大小，请在角色卡中更换音色。")
    show = performance(runtime.store)
    socket_origin = browser_socket_origin(origin)
    socket_prefix = root_path.rstrip("/") if socket_origin == request_origin else ""
    return {
        "socket": socket_origin + socket_prefix + "/v1/realtime",
        "ticket": ticket,
        "voice_context": ticket_context(token, ticket, grant) if not options.temporary else None,
        "call_scope": content_hash(grant),
        "character": runtime.store.display_name(character["id"]),
        "voice": {
            "id": voice["id"],
            "name": voice["name"],
            "reference_sha256": hashlib.sha256(reference).hexdigest(),
        },
        "performance": show,
        "avatar": call_avatar(runtime.settings, character) if show["pets_enabled"] else {
            "still": None, "speaking": None, "fallback": None,
        },
        "memory_citations": [citation(row) for row in rows],
        "correction_targets": [
            row["id"] for row in rows if correctable_preference(row, runtime.daily_memory.private_owner)
        ],
        "memory_use": {"ids": [row["id"] for row in rows], "stamp": read_stamp(runtime, rows)}
        if rows or topic_source else None,
        **({"topic_source": topic_source} if topic_source else {}),
        "instructions": instruction_text,
    }
