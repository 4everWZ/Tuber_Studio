"""Verify the machine transcript independently of an owner's editable confirmation."""

import hashlib
import hmac
import json

from fastapi import HTTPException
from pydantic import Field

from ..models import StrictModel
from ..operation_continuations import content_hash


class VoiceOrigin(StrictModel):
    machine_text: str = Field(min_length=1, max_length=2000)
    capture_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    samples: int = Field(strict=True, gt=0, le=240000)
    ticket_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_signature: str = Field(pattern=r"^[a-f0-9]{64}$")


def sign(key, fields):
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.digest(key.encode(), encoded, "sha256").hex()


def ticket_context(key, ticket, scope):
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    return {"ticket_hash": digest, "scope_signature": sign(key, ["studio.mic.v1", digest, content_hash(scope)])}


def verify_origin(origin, key, scope):
    expected = sign(key, ["omni.mic.v1", origin.ticket_hash, origin.capture_id, origin.samples, origin.machine_text])
    bound = sign(key, ["studio.mic.v1", origin.ticket_hash, content_hash(scope)])
    if not hmac.compare_digest(expected, origin.receipt) or not hmac.compare_digest(bound, origin.scope_signature):
        raise HTTPException(409, "无法核实通话录音来源或当前号主、角色已变化")


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
