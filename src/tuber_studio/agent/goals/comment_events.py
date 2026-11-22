"""Private goal notifications for new verified rows in a selected public thread."""

import time
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from pydantic import Field

from ...core.models import StrictModel
from ...core.store import dump
from ...operations.facade import comment_href, ensure_comment_timeline


class CommentWatch(StrictModel):
    resource: str = Field(pattern=r"^(BV[0-9A-Za-z]{10}|dynamic:[1-9][0-9]{0,19})$")
    root_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    epoch: int = Field(strict=True, ge=1)
    revision: int = Field(strict=True, ge=0)


class GoalCommentEvents:
    def __init__(self, tasks):
        self.tasks, self.store = tasks, tasks.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS goal_comment_watches(
                goal_id TEXT PRIMARY KEY REFERENCES creative_goals(id), scope TEXT NOT NULL,
                account TEXT NOT NULL, character TEXT NOT NULL, resource TEXT NOT NULL, root_id TEXT NOT NULL,
                enabled INTEGER NOT NULL, revision INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS goal_comment_thread ON goal_comment_watches(account,character,resource,root_id)
                WHERE enabled=1;
            CREATE TABLE IF NOT EXISTS goal_pending_comments(
                id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_comment_watches(goal_id),
                revision INTEGER NOT NULL, comment_id TEXT NOT NULL, author_uid TEXT NOT NULL,
                UNIQUE(goal_id,revision,comment_id));
            CREATE TRIGGER IF NOT EXISTS goal_comment_closed AFTER UPDATE OF closed ON creative_goals
                WHEN NEW.closed=1 BEGIN
                UPDATE goal_comment_watches SET enabled=0,revision=revision+1 WHERE goal_id=NEW.id AND enabled=1;
            END;
            CREATE TRIGGER IF NOT EXISTS goal_comment_reconfigured AFTER UPDATE OF revision ON goal_comment_watches
                BEGIN DELETE FROM goal_pending_comments WHERE goal_id=NEW.goal_id AND revision<>NEW.revision; END;
        """)

    def get(self, identity):
        goal = self.tasks.workspace.goal(identity)
        if not goal or not self.tasks.current(identity):
            raise HTTPException(404, "找不到这个长期目标")
        scope = self.tasks.scope()
        row = self.store.one("SELECT * FROM goal_comment_watches WHERE goal_id=?", (identity,))
        bound = bool(row and row["scope"] == dump(scope))
        return {"account": scope[0], "character": scope[1], "epoch": scope[-1] if self.tasks.identity else None,
                "closed": bool(goal["closed"]), "revision": row["revision"] if row else 0,
                "enabled": bool(bound and row["enabled"] and not goal["closed"]),
                "resource": row["resource"] if bound else None, "root_id": row["root_id"] if bound else None}

    def configure(self, identity, grant):
        state = self.get(identity)
        if (state["closed"] or not state["account"] or state["epoch"] != grant.epoch
                or state["revision"] != grant.revision):
            raise HTTPException(409, "目标、账号或授权已变化，请刷新")
        if state["enabled"] and (state["resource"], state["root_id"]) == (grant.resource, grant.root_id):
            return state
        scope = dump(self.tasks.scope())
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_comment_timeline(self.store)
            db.execute("""
                CREATE TRIGGER IF NOT EXISTS goal_observed_comment AFTER INSERT ON browsing_comment_timeline BEGIN
                    INSERT OR IGNORE INTO goal_pending_comments(goal_id,revision,comment_id,author_uid)
                    SELECT w.goal_id,w.revision,NEW.comment_id,NEW.author_uid FROM goal_comment_watches w
                    WHERE w.enabled=1 AND w.account=NEW.account_id AND w.character=NEW.character_id
                    AND w.resource=NEW.resource AND w.root_id=NEW.root_id AND NEW.author_uid<>w.account;
                END
            """)
            db.execute("""
                INSERT INTO goal_comment_watches VALUES(?,?,?,?,?,?,1,?)
                ON CONFLICT(goal_id) DO UPDATE SET scope=excluded.scope,account=excluded.account,
                    character=excluded.character,resource=excluded.resource,root_id=excluded.root_id,
                    enabled=1,revision=excluded.revision
            """, (identity, scope, state["account"], state["character"], grant.resource, grant.root_id, grant.revision + 1))
        self.tasks.wake.set()
        return self.get(identity)

    def disable(self, identity, revision):
        state = self.get(identity)
        if state["revision"] != revision:
            raise HTTPException(409, "配置已变化，请刷新")
        self.store.execute("UPDATE goal_comment_watches SET enabled=0,revision=revision+1 WHERE goal_id=? AND enabled=1",
                           (identity,))
        return self.get(identity)

    def consume(self, record, limit):
        if not self.store.one("SELECT 1 FROM goal_comment_watches WHERE enabled=1 LIMIT 1"):
            return
        scope = dump(self.tasks.scope())
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE goal_comment_watches SET enabled=0,revision=revision+1 WHERE enabled=1 AND scope<>?", (scope,))
            rows = db.execute("""
                SELECT p.id,p.goal_id,p.comment_id,p.author_uid,w.account,w.resource,w.root_id
                FROM goal_pending_comments p JOIN goal_comment_watches w ON w.goal_id=p.goal_id
                WHERE w.enabled=1 AND w.scope=? AND w.revision=p.revision ORDER BY p.id LIMIT ?
            """, (scope, limit)).fetchall()
            now = time.time()
            for row in rows:
                result = {key: row[key] for key in ("resource", "root_id", "comment_id", "author_uid")}
                result.update(account_id=row["account"], url=comment_href(row["resource"], row["comment_id"]))
                event_key = dump(["bilibili_comment_thread", row["account"], row["resource"], row["root_id"], row["comment_id"]])
                title = f"{row['resource']} 楼 {row['root_id']} 收到 UID {row['author_uid']} 的评论 {row['comment_id']}"
                result["message"] = title
                record(db, row["goal_id"], scope, event_key, "comment_received", result, title, now)
                db.execute("DELETE FROM goal_pending_comments WHERE id=?", (row["id"],))


def register_comment_events(app, events, require_admin):
    path = "/api/agent/tasks/{identity}/goal/comment-events"
    deps = [Depends(require_admin)]

    @app.get(path, dependencies=deps)
    async def watch(identity: UUID):
        return events.get(str(identity))

    @app.put(path, dependencies=deps)
    async def configure(identity: UUID, body: CommentWatch):
        return events.configure(str(identity), body)

    @app.delete(path, dependencies=deps)
    async def disable(identity: UUID, revision: int = Query(ge=0)):
        return events.disable(str(identity), revision)
