"""Goal-scoped consumption of local, account-bound inbox receipts."""

import logging
import sqlite3
import time
from typing import Literal
from uuid import UUID, uuid5

from fastapi import Depends, HTTPException, Query
from pydantic import Field

from ..models import StrictModel
from ..store import dump
from .goal_comment_events import GoalCommentEvents, register_comment_events
from .task_delivery import deliver_from_receipts

log = logging.getLogger(__name__)
BATCH = 16


class EventGrant(StrictModel):
    source: Literal["bilibili_inbox"]
    sender_uid: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    epoch: int = Field(strict=True, ge=1)
    revision: int = Field(strict=True, ge=0)


class GoalEvents:
    def __init__(self, tasks):
        self.tasks, self.store = tasks, tasks.store
        self.retry_at = 0
        self.comments = GoalCommentEvents(tasks)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS goal_event_watches(
                goal_id TEXT PRIMARY KEY REFERENCES creative_goals(id), scope TEXT NOT NULL,
                account TEXT NOT NULL, character TEXT NOT NULL, sender_uid TEXT NOT NULL,
                enabled INTEGER NOT NULL, revision INTEGER NOT NULL, cursor INTEGER NOT NULL, consumed_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS goal_event_enabled ON goal_event_watches(enabled,scope,consumed_at);
            CREATE TRIGGER IF NOT EXISTS goal_event_closed AFTER UPDATE OF closed ON creative_goals
            WHEN NEW.closed=1 BEGIN
                UPDATE goal_event_watches SET enabled=0,revision=revision+1 WHERE goal_id=NEW.id AND enabled=1;
            END;
        """)

    def get(self, identity):
        goal = self.tasks.workspace.goal(identity)
        if not goal or not self.tasks.current(identity):
            raise HTTPException(404, "找不到这个长期目标")
        scope = self.tasks.scope()
        row = self.store.one("SELECT * FROM goal_event_watches WHERE goal_id=?", (identity,))
        return {"source": "bilibili_inbox", "account": scope[0], "character": scope[1] if self.tasks.identity else None,
                "epoch": scope[-1] if self.tasks.identity else None, "closed": bool(goal["closed"]),
                "enabled": bool(row and row["enabled"] and row["scope"] == dump(scope) and not goal["closed"]),
                "sender_uid": row["sender_uid"] if row else None, "revision": row["revision"] if row else 0,
                "cursor": row["cursor"] if row else None}

    def configure(self, identity, grant):
        state = self.get(identity)
        if state["closed"] or grant.epoch != state["epoch"] or state["revision"] != grant.revision:
            raise HTTPException(409, "目标、授权或配置已变化，请刷新")
        if not state["account"] or state["account"] == grant.sender_uid:
            raise HTTPException(409, "须先核验接收账号，并指定另一位用户的 UID")
        if not self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_inbox'"):
            raise HTTPException(409, "私信收件箱未接入")
        if state["enabled"] and state["sender_uid"] == grant.sender_uid:
            return state
        scope = dump(self.tasks.scope())
        self.store.execute("""
            INSERT OR REPLACE INTO goal_event_watches(goal_id,scope,account,character,sender_uid,enabled,revision,cursor,consumed_at)
            SELECT ?,?,?,?,?,1,?,COALESCE(MAX(id),0),0 FROM dm_inbox
        """, (identity, scope, state["account"], state["character"], grant.sender_uid, grant.revision + 1))
        self.tasks.wake.set()
        return self.get(identity)

    def disable(self, identity, revision):
        state = self.get(identity)
        if state["revision"] != revision:
            raise HTTPException(409, "配置已变化，请刷新")
        self.store.execute("UPDATE goal_event_watches SET enabled=0,revision=revision+1 WHERE goal_id=? AND enabled=1",
                           (identity,))
        return self.get(identity)

    def tick(self):
        if time.monotonic() < self.retry_at:
            return
        try:
            self.consume()
        except sqlite3.Error:
            self.retry_at = time.monotonic() + 5
            log.exception("私有事件跟踪未提交，将从原游标重试")

    def consume(self):
        self.comments.consume(self.record, BATCH)
        if not self.store.one("SELECT 1 FROM goal_event_watches WHERE enabled=1 LIMIT 1"):
            return
        scope = dump(self.tasks.scope())
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE goal_event_watches SET enabled=0,revision=revision+1 WHERE enabled=1 AND scope<>?", (scope,))
            watch = db.execute("""
                SELECT w.* FROM goal_event_watches w WHERE w.enabled=1 AND w.scope=? AND EXISTS (
                    SELECT 1 FROM dm_inbox m WHERE m.platform='bilibili' AND m.account_id=w.account
                    AND m.character_id=w.character AND m.platform_user_id=w.sender_uid AND m.id>w.cursor)
                ORDER BY w.consumed_at,w.goal_id LIMIT 1
            """, (scope,)).fetchone()
            if not watch:
                return
            receipts = db.execute("""
                SELECT id,message_id FROM dm_inbox WHERE platform='bilibili' AND account_id=? AND character_id=?
                AND platform_user_id=? AND id>? ORDER BY id LIMIT ?
            """, (watch["account"], watch["character"], watch["sender_uid"], watch["cursor"], BATCH)).fetchall()
            now = time.time()
            for receipt in receipts:
                event_key = dump(["bilibili_inbox", watch["account"], receipt["message_id"]])
                result = {"sender_uid": watch["sender_uid"], "account_id": watch["account"],
                          "message_id": receipt["message_id"], "inbox_id": receipt["id"]}
                self.record(db, watch["goal_id"], scope, event_key, "inbox_received", result,
                            f"UID {watch['sender_uid']} 的来信已入库", now)
            db.execute("UPDATE goal_event_watches SET cursor=?,consumed_at=? WHERE goal_id=?",
                       (receipts[-1]["id"], now, watch["goal_id"]))

    def record(self, db, goal_id, scope, event_key, tool, result, title, now):
        run_id = str(uuid5(UUID(goal_id), event_key))
        steps = [{"id": run_id, "revision": 1, "tool": tool, "arguments": {}, "result": result}]
        _, message = deliver_from_receipts(steps, 1)
        inserted = db.execute("""
            INSERT INTO agent_tasks(id,scope,goal,revision,status,steps,message,source_key,due,created,updated,creative_goal_id)
            VALUES(?,?,?,1,'completed',?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING
        """, (run_id, scope, title, dump(steps), message, event_key, now, now, now, goal_id))
        if inserted.rowcount:
            self.tasks.workspace.notify(db, run_id, now)


def register_goal_events(app, tasks, require_admin):
    events = tasks.goal_events = GoalEvents(tasks)
    register_comment_events(app, events.comments, require_admin)
    path = "/api/agent/tasks/{identity}/goal/events"
    deps = [Depends(require_admin)]

    @app.get(path, dependencies=deps)
    async def event_watch(identity: UUID):
        return events.get(str(identity))

    @app.put(path, dependencies=deps)
    async def configure_events(identity: UUID, body: EventGrant):
        return events.configure(str(identity), body)

    @app.delete(path, dependencies=deps)
    async def disable_events(identity: UUID, revision: int = Query(ge=0)):
        return events.disable(str(identity), revision)
