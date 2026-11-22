"""Submission revisions retain the original intent and every previous attempt."""

import hashlib
import json
import re
import time
from uuid import uuid4


def binding_value(binding):
    if not isinstance(binding, dict) or not binding:
        raise ValueError("恢复发送需要原目标与来源绑定")
    value = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(value.encode()) > 32768:
        raise ValueError("发送绑定超出资源边界")
    return value, hashlib.sha256(value.encode()).hexdigest()


def validate_content_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("修订正文需要内容指纹")


class OperationRevisions:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS operation_write_bindings(
                root_token TEXT PRIMARY KEY,binding_json TEXT NOT NULL,binding_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_write_retries(
                token TEXT PRIMARY KEY,account_id TEXT NOT NULL,character_id TEXT NOT NULL,
                character_version INTEGER NOT NULL,intent TEXT NOT NULL,channel TEXT NOT NULL,
                recipient TEXT NOT NULL,automatic INTEGER NOT NULL,epoch INTEGER NOT NULL,
                status TEXT NOT NULL,started_at REAL NOT NULL,receipt TEXT,error TEXT,
                root_token TEXT NOT NULL,previous_token TEXT NOT NULL UNIQUE);
            CREATE INDEX IF NOT EXISTS operation_write_retries_quota
                ON operation_write_retries(account_id,started_at);
            CREATE TABLE IF NOT EXISTS operation_revision_tickets(
                ticket TEXT PRIMARY KEY,previous_token TEXT NOT NULL,binding_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,automatic INTEGER NOT NULL,epoch INTEGER NOT NULL,
                state TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_write_resolutions(
                token TEXT PRIMARY KEY,receipt TEXT NOT NULL,verified_at REAL NOT NULL);
            CREATE VIEW IF NOT EXISTS operation_write_attempts AS
                SELECT w.*,w.token AS root_token,NULL AS previous_token,
                    CASE WHEN r.token IS NULL THEN w.status ELSE 'published' END AS resolved_status
                FROM account_writes w LEFT JOIN operation_write_resolutions r ON r.token=w.token
                UNION ALL
                SELECT w.*,CASE WHEN r.token IS NULL THEN w.status ELSE 'published' END AS resolved_status
                FROM operation_write_retries w LEFT JOIN operation_write_resolutions r ON r.token=w.token;
        """)

    def get(self, token):
        row = self.store.db.execute(
            "SELECT w.*,b.binding_json AS original_binding_json FROM operation_write_attempts w "
            "LEFT JOIN operation_write_bindings b ON b.root_token=w.root_token WHERE w.token=?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        original_binding = value.pop("original_binding_json")
        value["binding"] = json.loads(original_binding) if original_binding else None
        value["receipt"] = json.loads(value["receipt"]) if value["receipt"] else None
        resolution = self.store.db.execute(
            "SELECT receipt FROM operation_write_resolutions WHERE token=?", (token,)
        ).fetchone()
        value["resolution_receipt"] = json.loads(resolution["receipt"]) if resolution else None
        return value

    def bind_initial(self, token, binding):
        if binding is not None:
            value, digest = binding_value(binding)
            self.store.db.execute(
                "INSERT INTO operation_write_bindings VALUES(?,?,?)", (token, value, digest)
            )

    def rejected_head(self, token):
        previous = self.get(token)
        if previous is None or previous["resolved_status"] != "rejected":
            raise ValueError("只有明确拒绝的发送可修订重投")
        if self.store.db.execute(
            "SELECT 1 FROM operation_write_retries WHERE previous_token=?", (token,)
        ).fetchone():
            raise ValueError("该发送已有后续尝试")
        return previous

    def check_binding(self, previous, binding):
        _, digest = binding_value(binding)
        saved = self.store.db.execute(
            "SELECT binding_hash FROM operation_write_bindings WHERE root_token=?", (previous["root_token"],)
        ).fetchone()
        if saved is None or saved["binding_hash"] != digest:
            raise ValueError("恢复发送的目标或来源已变化，需要重新准备")
        return digest

    def issue(self, previous, *, binding, content_hash, automatic, epoch):
        validate_content_hash(content_hash)
        digest = self.check_binding(previous, binding)
        self.store.db.execute(
            "UPDATE operation_revision_tickets SET state='superseded' WHERE previous_token=? AND state='ready'",
            (previous["token"],),
        )
        ticket = str(uuid4())
        self.store.db.execute(
            "INSERT INTO operation_revision_tickets VALUES(?,?,?,?,?,?,'ready',?)",
            (ticket, previous["token"], digest, content_hash, int(automatic), epoch, time.time()),
        )
        return ticket

    def validate_ticket(self, ticket, *, binding, content_hash, automatic, epoch, identity):
        validate_content_hash(content_hash)
        row = self.store.db.execute(
            "SELECT * FROM operation_revision_tickets WHERE ticket=?", (ticket,)
        ).fetchone()
        if row is None or row["state"] != "ready":
            raise ValueError("修订授权不存在或已使用")
        previous = self.rejected_head(row["previous_token"])
        digest = self.check_binding(previous, binding)
        if (
            digest != row["binding_hash"]
            or content_hash != row["content_hash"]
            or int(automatic) != row["automatic"]
            or epoch != row["epoch"]
            or any(previous[key] != value for key, value in identity.items())
        ):
            raise ValueError("修订授权的身份、正文或权限版本已变化")
        return previous

    def insert_retry(self, ticket, previous, values):
        self.store.db.execute(
            "INSERT INTO operation_write_retries VALUES(?,?,?,?,?,?,?,?,?,'sending',?,?,NULL,?,?)",
            (*values, previous["root_token"], previous["token"]),
        )
        self.store.db.execute("UPDATE operation_revision_tickets SET state='used' WHERE ticket=?", (ticket,))

    def resolve(self, token, *, account_id, binding, receipt):
        previous = self.get(token)
        if previous is None or previous["status"] != "uncertain" or previous["account_id"] != str(account_id):
            raise ValueError("核对的账号或发送状态不符")
        self.check_binding(previous, binding)
        if not isinstance(receipt, dict) or not receipt:
            raise ValueError("核对成功需要平台回执")
        value = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
        if len(value.encode()) > 32768:
            raise ValueError("平台回执超出资源边界")
        old = self.store.db.execute(
            "SELECT receipt FROM operation_write_resolutions WHERE token=?", (token,)
        ).fetchone()
        if old is not None:
            if json.loads(old["receipt"]) != receipt:
                raise ValueError("已核对的回执不能改写")
            return previous
        self.store.db.execute(
            "INSERT INTO operation_write_resolutions VALUES(?,?,?)", (token, value, time.time())
        )
        return previous
