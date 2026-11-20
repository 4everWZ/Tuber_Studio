"""Verify the machine transcript independently of an owner's editable confirmation."""

import hashlib
import hmac
import json

from fastapi import HTTPException
from pydantic import Field

from ..models import StrictModel
from ..operation_continuations import content_hash


class CallOrigin(StrictModel):
    ticket_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_signature: str = Field(pattern=r"^[a-f0-9]{64}$")


class VoiceOrigin(CallOrigin):
    machine_text: str = Field(min_length=1, max_length=2000)
    capture_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    samples: int = Field(strict=True, gt=0, le=240000)
    receipt: str = Field(pattern=r"^[a-f0-9]{64}$")
    transcript_receipt: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def sign(key, fields):
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.digest(key.encode(), encoded, "sha256").hex()


def ticket_context(key, ticket, scope):
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    return {"ticket_hash": digest, "scope_signature": sign(key, ["studio.mic.v1", digest, content_hash(scope)])}


def verify_call(origin, key, scope):
    bound = sign(key, ["studio.mic.v1", origin.ticket_hash, content_hash(scope)])
    if not hmac.compare_digest(bound, origin.scope_signature):
        raise HTTPException(409, "通话授权已变化，请重新开始通话")


def verify_origin(origin, key, scope, *, for_routing=False):
    verify_call(origin, key, scope)
    fields = [origin.ticket_hash, origin.capture_id, origin.samples, origin.machine_text]
    expected = sign(key, ["omni.mic.v1", *fields])
    if not hmac.compare_digest(expected, origin.receipt):
        raise HTTPException(409, "无法核实通话录音来源或当前号主、角色已变化")
    if for_routing and not hmac.compare_digest(sign(key, ["omni.asr.v1", *fields]), origin.transcript_receipt or ""):
        raise HTTPException(409, "这段转写可在通话里确认后交办。")


def install(db):
    db.execute("""CREATE TABLE IF NOT EXISTS confirmed_voice_origins(
        item_id INTEGER NOT NULL, source_key TEXT NOT NULL, machine_text TEXT NOT NULL,
        capture_id TEXT NOT NULL, samples INTEGER NOT NULL, receipt TEXT NOT NULL, ticket_hash TEXT NOT NULL,
        PRIMARY KEY(item_id,source_key), FOREIGN KEY(item_id,source_key)
        REFERENCES memory_sources(item_id,source_key) ON DELETE CASCADE)""")


def save(db, item_id, source_key, origin):
    db.execute("""INSERT OR IGNORE INTO confirmed_voice_origins
        (item_id,source_key,machine_text,capture_id,samples,receipt,ticket_hash) VALUES(?,?,?,?,?,?,?)""",
        (item_id, source_key, origin.machine_text, origin.capture_id, origin.samples, origin.receipt, origin.ticket_hash))


def attach(store, item):
    origin = store.one("""SELECT machine_text,capture_id,samples FROM confirmed_voice_origins
                          WHERE item_id=? AND source_key=?""", (item["id"], item["source_key"]))
    if origin:
        item["voice_origin"] = dict(origin) | {"owner_edited": item["quote"] != origin["machine_text"]}
    return item
