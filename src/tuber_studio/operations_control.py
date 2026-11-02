"""Account-scoped write admission, including authorized rejected-write revisions."""

import json
import time
from uuid import uuid4

from pydantic import Field

from .live_media import StrictModel
from .operations_revisions import OperationRevisions


class OperationsPolicy(StrictModel):
    enabled: bool = False
    emergency: bool = False
    max_per_hour: int = Field(default=5, ge=1, le=30)
    max_per_day: int = Field(default=20, ge=1, le=200)
    recipient_cooldown_seconds: int = Field(default=120, ge=0, le=86400)
    max_recoveries: int = Field(default=8, ge=0, le=64)


OVERLAYS = {
    "comments": {"auto_browse": True, "auto_reply": True},
    "messages": {"enabled": True, "auto_poll": True, "auto_send": True, "allow_comment_handoff": True},
    "browsing": {
        "discovery_enabled": True,
        "top_level_enabled": True,
        "reply_enabled": True,
        "mentions_enabled": True,
        "mention_reply_enabled": True,
        "replies_enabled": True,
        "message_poll_enabled": True,
        "message_reply_enabled": True,
        "comment_handoff_enabled": True,
    },
}


def canonical_intent(platform, resource, source, character_id):
    """Stable across OC versions and public/private delivery of the same reply."""
    return json.dumps([str(platform), str(resource), str(source), str(character_id)], separators=(",", ":"))


class OperationsControl:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS account_writes(
                token TEXT PRIMARY KEY,account_id TEXT NOT NULL,character_id TEXT NOT NULL,
                character_version INTEGER NOT NULL,intent TEXT NOT NULL,channel TEXT NOT NULL,
                recipient TEXT NOT NULL,automatic INTEGER NOT NULL,epoch INTEGER NOT NULL,
                status TEXT NOT NULL,started_at REAL NOT NULL,receipt TEXT,error TEXT,
                UNIQUE(account_id,character_id,intent));
            CREATE INDEX IF NOT EXISTS account_writes_quota ON account_writes(account_id,started_at);
            CREATE TABLE IF NOT EXISTS operation_channel_blocks(
                account_id TEXT NOT NULL,channel TEXT NOT NULL,reason TEXT NOT NULL,
                PRIMARY KEY(account_id,channel));
        """)
        self.revisions = OperationRevisions(store)
        # No HTTP write is resumed after process loss, regardless of worker lease.
        with store.db:
            store.db.execute(
                "INSERT OR IGNORE INTO operation_channel_blocks SELECT account_id,channel,"
                "'发送中重启，等待核对平台状态' FROM operation_write_attempts WHERE status='sending'"
            )
            store.db.execute(
                "UPDATE account_writes SET status='uncertain',error='发送中重启，等待核对状态' WHERE status='sending'"
            )
            store.db.execute(
                "UPDATE operation_write_retries SET status='uncertain',error='发送中重启，等待核对状态' WHERE status='sending'"
            )

    def policy(self):
        value = OperationsPolicy.model_validate(self.store.get("operations_policy", {})).model_dump()
        return value | self.store.get(
            "operations_binding",
            {
                "epoch": 0,
                "enabled_at": None,
                "account_id": None,
                "character_id": None,
                "character_version": None,
            },
        )

    def configure(self, patch, *, account_id=None, character=None):
        if not isinstance(patch, dict) or set(patch) - set(OperationsPolicy.model_fields):
            raise ValueError("未知自动化控制字段")
        old = self.policy()
        base = {key: old[key] for key in OperationsPolicy.model_fields}
        value = OperationsPolicy.model_validate(base | patch).model_dump()
        if value["emergency"] and patch.get("enabled") is True:
            raise ValueError("紧急停止仍有效，请先显式解除后再启用")
        if value["emergency"]:
            value["enabled"] = False
        binding = {
            key: old[key]
            for key in ("epoch", "enabled_at", "account_id", "character_id", "character_version")
        }
        if value["enabled"] and (not old["enabled"] or account_id is not None or character is not None):
            if not account_id or not character:
                raise ValueError("启用自动化前须核验账号与角色")
            binding.update(
                account_id=str(account_id),
                character_id=character["id"],
                character_version=character["version"],
                enabled_at=time.time(),
            )
        if value != base or (value["enabled"] and binding != {key: old[key] for key in binding}):
            binding["epoch"] += 1
        if value["emergency"]:
            value["enabled"] = False
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO kv VALUES('operations_policy',?)", (json.dumps(value),)
            )
            self.store.db.execute(
                "INSERT OR REPLACE INTO kv VALUES('operations_binding',?)", (json.dumps(binding),)
            )
        return self.policy()

    def effective(self, channel, base):
        result = dict(base)
        policy = self.policy()
        if policy["enabled"] and not policy["emergency"]:
            result.update(OVERLAYS.get(channel, {}))
        if policy["emergency"]:
            for key in result:
                if (
                    (channel == "browsing" and key in OVERLAYS[channel])
                    or (channel == "dynamics" and key == "enabled")
                    or key.startswith("auto_")
                ):
                    if isinstance(result[key], bool):
                        result[key] = False
        return result

    def assert_allowed(self, *, automatic, channel, account_id, character, epoch=None):
        policy = self.policy()
        if automatic and policy["emergency"]:
            raise ValueError("紧急停止：禁止所有自动收发")
        if epoch is not None and (not policy["enabled"] or policy["epoch"] != epoch):
            raise ValueError("自动化授权版本已撤销，保留草稿")
        if (
            automatic
            and policy["enabled"]
            and (
                str(account_id) != policy["account_id"]
                or character["id"] != policy["character_id"]
                or character["version"] != policy["character_version"]
            )
        ):
            raise ValueError("自动化账号或角色版本已变化")
        current = self.store.character()
        if character["id"] != current["id"] or character["version"] != current["version"]:
            raise ValueError("当前账号任务的角色版本已变化")
        if automatic and self.store.one(
            "SELECT reason FROM operation_channel_blocks WHERE account_id=? AND channel=?",
            (str(account_id), channel),
        ):
            raise ValueError("该渠道存在不确定写入，等待核对平台状态")

    def get_attempt(self, token):
        return self.revisions.get(token)

    def issue_revision_ticket(self, previous_token, *, binding, content_hash, automatic=None, epoch=None):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            previous = self.revisions.rejected_head(previous_token)
            automatic = bool(previous["automatic"]) if automatic is None else automatic
            character = {"id": previous["character_id"], "version": previous["character_version"]}
            self.assert_allowed(
                automatic=automatic,
                channel=previous["channel"],
                account_id=previous["account_id"],
                character=character,
                epoch=epoch,
            )
            return self.revisions.issue(
                previous,
                binding=binding,
                content_hash=content_hash,
                automatic=automatic,
                epoch=self.policy()["epoch"],
            )

    def reconcile_attempt(self, token, *, account_id, binding, receipt):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            previous = self.revisions.resolve(token, account_id=account_id, binding=binding, receipt=receipt)
            if not self.store.db.execute(
                "SELECT 1 FROM operation_write_attempts WHERE account_id=? AND channel=? AND resolved_status='uncertain'",
                (previous["account_id"], previous["channel"]),
            ).fetchone():
                self.store.db.execute(
                    "DELETE FROM operation_channel_blocks WHERE account_id=? AND channel=?",
                    (previous["account_id"], previous["channel"]),
                )
        return self.get_attempt(token)

    def reserve(
        self,
        account_id,
        character,
        intent,
        channel,
        *,
        recipient="",
        automatic=False,
        limits=None,
        epoch=None,
        binding=None,
        revision_ticket=None,
        content_hash=None,
    ):
        self.assert_allowed(
            automatic=automatic, channel=channel, account_id=account_id, character=character, epoch=epoch
        )
        if not isinstance(intent, str) or not intent or len(intent) > 1000 or not str(account_id):
            raise ValueError("发送需要固定账号与来源意图")
        policy, now, token = self.policy(), time.time(), str(uuid4())
        limits = limits or {}
        db = self.store.db
        # No await occurs in this transaction. Separate processes contend on
        # SQLite's write lock rather than racing independent in-memory locks.
        with db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_allowed(
                automatic=automatic, channel=channel, account_id=account_id, character=character, epoch=epoch
            )
            policy = self.policy()
            previous = None
            if revision_ticket is not None:
                previous = self.revisions.validate_ticket(
                    revision_ticket,
                    binding=binding,
                    content_hash=content_hash,
                    automatic=automatic,
                    epoch=self.policy()["epoch"],
                    identity={
                        "account_id": str(account_id),
                        "character_id": character["id"],
                        "character_version": character["version"],
                        "intent": intent,
                        "channel": channel,
                        "recipient": str(recipient),
                    },
                )
            elif db.execute(
                "SELECT 1 FROM account_writes WHERE account_id=? AND character_id=? AND intent=?",
                (str(account_id), character["id"], intent),
            ).fetchone():
                raise ValueError("此回应意图已尝试提交，禁止重复发送")
            for seconds, maximum in ((3600, policy["max_per_hour"]), (86400, policy["max_per_day"])):
                count = db.execute(
                    "SELECT COUNT(*) FROM operation_write_attempts WHERE account_id=? AND started_at>?",
                    (str(account_id), now - seconds),
                ).fetchone()[0]
                if count >= maximum:
                    raise ValueError("达到账号全渠道发送上限，保留草稿")
            for seconds, name in ((3600, "max_per_hour"), (86400, "max_per_day")):
                if (
                    name in limits
                    and db.execute(
                        "SELECT COUNT(*) FROM operation_write_attempts WHERE account_id=? AND channel=? AND started_at>?",
                        (str(account_id), channel, now - seconds),
                    ).fetchone()[0]
                    >= limits[name]
                ):
                    raise ValueError("达到渠道发送上限")
            cooldown = max(policy["recipient_cooldown_seconds"], limits.get("recipient_cooldown_seconds", 0))
            if (
                recipient
                and db.execute(
                    "SELECT 1 FROM operation_write_attempts WHERE account_id=? AND recipient=? AND started_at>? "
                    "AND NOT(root_token=? AND resolved_status='rejected')",
                    (
                        str(account_id),
                        str(recipient),
                        now - cooldown,
                        previous["root_token"] if previous else "",
                    ),
                ).fetchone()
            ):
                raise ValueError("该用户跨渠道回复仍在冷却中")
            values = (
                token,
                str(account_id),
                character["id"],
                character["version"],
                intent,
                channel,
                str(recipient),
                int(automatic),
                policy["epoch"],
                now,
                None,
            )
            if previous is None:
                db.execute("INSERT INTO account_writes VALUES(?,?,?,?,?,?,?,?,?,'sending',?,?,NULL)", values)
                self.revisions.bind_initial(token, binding)
            else:
                self.revisions.insert_retry(revision_ticket, previous, values)
        return token

    def finish(self, token, status, *, receipt=None, error=None):
        if status not in {"published", "rejected", "uncertain", "cancelled"}:
            raise ValueError("无效发送结果")
        with self.store.db:
            row = self.get_attempt(token)
            if row is None:
                raise ValueError("发送预约不存在")
            if row["status"] != "sending":
                raise ValueError("发送结果已终结，不能重写")
            table = "account_writes" if row["previous_token"] is None else "operation_write_retries"
            self.store.db.execute(
                f"UPDATE {table} SET status=?,receipt=?,error=? WHERE token=?",
                (status, json.dumps(receipt, ensure_ascii=False) if receipt else None, error, token),
            )
            if status == "uncertain":
                self.store.db.execute(
                    "INSERT OR REPLACE INTO operation_channel_blocks VALUES(?,?,?)",
                    (row["account_id"], row["channel"], "发送结果不确定，等待核对平台状态"),
                )

    def reconcile_channel(self, account_id, channel):
        """Explicit operator reconciliation only; intent records remain immutable."""
        self.store.execute(
            "DELETE FROM operation_channel_blocks WHERE account_id=? AND channel=?",
            (str(account_id), channel),
        )

    def status(self):
        return {
            "policy": self.policy(),
            "channel_blocks": self.store.all("SELECT * FROM operation_channel_blocks"),
            "attempts": self.store.all(
                "SELECT account_id,channel,resolved_status AS status,COUNT(*) AS count "
                "FROM operation_write_attempts GROUP BY account_id,channel,resolved_status"
            ),
        }
