"""Account-scoped write admission, including authorized rejected-write revisions."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from pydantic import Field

from .models import StrictModel
from .operations.browsing.quotas import BrowsingQuotas
from .operations_revisions import OperationRevisions
from .publication_errors import SendLimitReached


class OperationsPolicy(StrictModel):
    enabled: bool = False
    emergency: bool = False
    max_per_hour: int = Field(default=5, ge=0)
    max_per_day: int = Field(default=20, ge=0)
    recipient_cooldown_seconds: int = Field(default=120, ge=0, le=86400)
    max_recoveries: int = Field(default=8, ge=0, le=64)


REACTION_CHANNELS = frozenset({"comment_likes", "video_likes", "video_favorites", "dynamic_likes",
                               "comment_dislikes", "video_dislikes"})
COMMENT_WRITE_FILTER = "COALESCE(CASE WHEN json_valid(intent) THEN json_extract(intent,'$[2]') END,'') NOT IN ('like','favorite')"


AUTOMATIC_SWITCHES = {
    "comments": {"auto_reply"},
    "messages": {"auto_poll", "auto_send"},
    "browsing": {
        "discovery_enabled",
        "mentions_enabled",
        "replies_enabled",
        "mention_reply_enabled",
        "message_poll_enabled",
        "message_reply_enabled",
    },
    "dynamics": {"enabled"},
}


def canonical_intent(platform, resource, source, character_id):
    """Stable across OC versions and public/private delivery of the same reply."""
    return json.dumps([str(platform), str(resource), str(source), str(character_id)], separators=(",", ":"))


class OperationsControl:
    def __init__(self, store):
        self.store = store
        self.starting = False
        self.draining = False
        self.last_send_error = None
        self.on_reserved = None
        self._admitted = set()
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS account_writes(
                token TEXT PRIMARY KEY,account_id TEXT NOT NULL,character_id TEXT NOT NULL,
                character_version INTEGER NOT NULL,intent TEXT NOT NULL,channel TEXT NOT NULL,
                recipient TEXT NOT NULL,automatic INTEGER NOT NULL,epoch INTEGER NOT NULL,
                status TEXT NOT NULL,started_at REAL NOT NULL,receipt TEXT,error TEXT,
                UNIQUE(account_id,character_id,intent));
            CREATE INDEX IF NOT EXISTS account_writes_quota ON account_writes(account_id,started_at);
            DROP TABLE IF EXISTS operation_channel_blocks;
        """)
        self.revisions = OperationRevisions(store)
        self.browsing_quotas = BrowsingQuotas(store)
        # No HTTP write is resumed after process loss, regardless of worker lease.
        with store.db:
            store.db.execute(
                "UPDATE account_writes SET status='uncertain',error='发送中重启，等待核对状态' WHERE status='sending'"
            )
            store.db.execute(
                "UPDATE operation_write_retries SET status='uncertain',error='发送中重启，等待核对状态' WHERE status='sending'"
            )
            # Cold start: Force autonomous operations into standby (enabled=False).
            # Requires explicit manual activation by the user clicking "开启自主运营" in UI.
            policy = store.get("operations_policy", {})
            if policy.get("enabled"):
                store.set("operation_send_resume_epoch", self.policy()["epoch"])
                policy["enabled"] = False
                store.set("operations_policy", policy)
            review = store.get("browsing_review_session", {})
            if review.get("state") in {"starting", "running"}:
                store.set("browsing_review_session", review | {"state": "paused", "paused_at": time.time()})

    @property
    def accepting_work(self):
        return not (self.starting or self.draining) and self.policy()["enabled"]

    @property
    def admitted_work(self):
        return tuple(self._admitted)

    @asynccontextmanager
    async def automatic_work(self):
        if not self.accepting_work:
            yield False
            return
        done = asyncio.get_running_loop().create_future()
        self._admitted.add(done)
        try:
            yield True
        finally:
            self._admitted.discard(done)
            done.set_result(None)

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
            if limits := {key: value[key] for key in ("max_per_hour", "max_per_day") if key in patch}:
                self.store.db.execute("INSERT OR REPLACE INTO kv VALUES('operation_explicit_account_limits',?)",
                                      (json.dumps(self.account_limits() | limits),))
        return self.policy()

    def account_limits(self):
        return self.store.get("operation_explicit_account_limits", {})

    def effective(self, channel, base):
        result = dict(base)
        policy = self.policy()
        if not policy["enabled"] or policy["emergency"]:
            for key in AUTOMATIC_SWITCHES.get(channel, ()):
                if key in result:
                    result[key] = False
        return result

    def assert_review_writable(self, account_id):
        """A running review can generate drafts, but cannot write to its account."""
        review = self.store.get("browsing_review_session", {})
        if review.get("state") in {"starting", "running"} and str(account_id) == review.get("account_id"):
            raise ValueError("当前为验收模式，草稿已保存，等待审核")

    def assert_allowed(self, *, automatic, channel, account_id, character, epoch=None):
        self.assert_review_writable(account_id)
        if automatic and self.draining:
            raise ValueError("运营正在收尾，保留待发送草稿")
        return self.assert_authorized(automatic=automatic, channel=channel, account_id=account_id,
                                      character=character, epoch=epoch)

    def assert_authorized(self, *, automatic, channel, account_id, character, epoch=None):
        """Validate processing ownership; unlike write admission this permits review."""
        policy = self.policy()
        if automatic and policy["emergency"]:
            raise ValueError("紧急停止：禁止所有自动收发")
        if automatic and not policy["enabled"]:
            raise ValueError("自主运营已停止，禁止自动提交")
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
        # A manual approval can publish a retained draft from this OC's history.
        # Automatic work remains bound to the currently active version.
        if character["id"] != current["id"] or automatic and character["version"] != current["version"]:
            raise ValueError("当前账号任务的角色版本已变化")
        return policy

    def get_attempt(self, token):
        return self.revisions.get(token)

    def issue_revision_ticket(self, previous_token, *, binding, content_hash, automatic=None, epoch=None):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            previous = self.revisions.rejected_head(previous_token)
            automatic = bool(previous["automatic"]) if automatic is None else automatic
            character = {"id": previous["character_id"], "version": previous["character_version"]}
            policy = self.assert_allowed(
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
                epoch=policy["epoch"],
            )

    def reconcile_attempt(self, token, *, account_id, binding, receipt):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self.revisions.resolve(token, account_id=account_id, binding=binding, receipt=receipt)
            self.browsing_quotas.settle_reply(token, "published", receipt=receipt)
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
        admission_hook=None,
    ):
        if not isinstance(intent, str) or not intent or len(intent) > 1000 or not str(account_id):
            raise ValueError("发送需要固定账号与来源意图")
        now, token = time.time(), str(uuid4())
        limits = limits or {}
        db = self.store.db
        # No await occurs in this transaction. Separate processes contend on
        # SQLite's write lock rather than racing independent in-memory locks.
        with db:
            db.execute("BEGIN IMMEDIATE")
            policy = self.assert_allowed(
                automatic=automatic, channel=channel, account_id=account_id, character=character, epoch=epoch
            )
            previous = None
            # An unresolved turn blocks later sends to that inbox, including other entry points.
            if channel == "messages" and recipient and db.execute(
                "SELECT 1 FROM operation_write_attempts WHERE account_id=? AND channel='messages' "
                "AND recipient=? AND resolved_status IN ('sending','uncertain') LIMIT 1",
                (str(account_id), str(recipient)),
            ).fetchone():
                raise ValueError("同一私信会话有在途或待核对的发送")
            target = (binding or {}).get("target", {})
            if channel == "comments" and target.get("root") not in (None, "", "0", 0) and db.execute(
                "SELECT 1 FROM operation_write_attempts w JOIN operation_write_bindings b ON b.root_token=w.root_token "
                "WHERE w.account_id=? AND w.channel='comments' AND w.resolved_status IN ('sending','uncertain') "
                "AND json_extract(b.binding_json,'$.target.root')=? "
                "AND json_extract(b.binding_json,'$.target.resource_type')=? LIMIT 1",
                (str(account_id), str(target["root"]), target.get("resource_type")),
            ).fetchone():
                raise ValueError("同一评论楼有在途或待核对的发送")
            if revision_ticket is not None:
                previous = self.revisions.validate_ticket(
                    revision_ticket,
                    binding=binding,
                    content_hash=content_hash,
                    automatic=automatic,
                    epoch=policy["epoch"],
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
                "SELECT 1 FROM account_writes WHERE account_id=? AND intent=? AND "
                "(character_id=? OR (channel=? AND ? IN ('comment_likes','dynamic_likes')))",
                (str(account_id), intent, character["id"], channel, channel),
            ).fetchone():
                raise ValueError("此回应意图已尝试提交，禁止重复发送")
            blocks = []
            where = "account_id=? AND started_at>? AND channel=?"
            if channel == "comments":
                where += " AND " + COMMENT_WRITE_FILTER
            account_where = ("account_id=? AND started_at>? AND channel NOT IN (SELECT value FROM json_each(?)) "
                             f"AND (channel<>'comments' OR {COMMENT_WRITE_FILTER})")
            for detail, quotas, condition, channel_filter in (
                ("渠道", limits, where, channel),
                ("账号全渠道", self.account_limits() if channel not in REACTION_CHANNELS else {},
                 account_where, json.dumps(sorted(REACTION_CHANNELS))),
            ):
                for seconds, name, label in ((3600, "max_per_hour", "每小时"), (86400, "max_per_day", "滚动24小时")):
                    maximum = quotas.get(name, 0)
                    if not maximum:
                        continue
                    cutoff = db.execute(
                        f"SELECT started_at FROM operation_write_attempts WHERE {condition} "
                        "ORDER BY started_at DESC LIMIT 1 OFFSET ?",
                        (str(account_id), now - seconds, channel_filter, maximum - 1),
                    ).fetchone()
                    if cutoff:
                        blocks.append(SendLimitReached(f"达到{detail}{label}发送频率上限", cutoff[0] + seconds))
            if blocks:
                raise max(blocks, key=lambda error: error.next_at)
            cooldown = max(policy["recipient_cooldown_seconds"], limits.get("recipient_cooldown_seconds", 0))
            if recipient and cooldown:
                last = db.execute(
                    "SELECT MAX(started_at) FROM operation_write_attempts WHERE account_id=? "
                    "AND recipient=? AND started_at>? AND NOT(root_token=? AND resolved_status='rejected')",
                    (str(account_id), str(recipient), now - cooldown, previous["root_token"] if previous else ""),
                ).fetchone()[0]
                if last is not None:
                    raise SendLimitReached("该用户跨渠道回复仍在冷却中", last + cooldown)
            if admission_hook is not None:
                admission_hook(token)
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
        if self.on_reserved:
            self.on_reserved()
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
            self.browsing_quotas.settle_reply(token, status, receipt=receipt)

    def status(self):
        return {
            "policy": self.policy(),
            "channel_blocks": [],
            "attempts": self.store.all(
                "SELECT account_id,channel,resolved_status AS status,COUNT(*) AS count "
                "FROM operation_write_attempts GROUP BY account_id,channel,resolved_status"
            ),
        }
