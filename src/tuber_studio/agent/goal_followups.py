"""Calendar-bound private observations on the existing task worker."""

import asyncio
import json
import time
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, HTTPException
from pydantic import Field, model_validator

from ..core.models import StrictModel
from ..core.store import dump
from ..infra.web_client.fetch import fetch_excerpt, source_url
from .task_delivery import deliver_from_receipts
from .task_runtime import ToolRequest

StatusSource = Literal["live_status", "game_status", "operation_status", "today_activity"]
FOLLOWUP_SOURCE = dump("goal_followup")


def observations(steps):
    by_source = {}
    for step in steps:
        name, result = step["tool"], step["result"]
        page = step["arguments"].get("web_fetch", "")
        if result.get("error"):
            value = {"error": result["error"]}
        elif name == "today_activity":
            value = sorted(json.dumps(event, sort_keys=True) for event in result.get("events", []))
        else:
            fields = {
                "web_page": ("url", "title", "excerpt", "truncated"),
                "live_status": ("state", "session_id", "muted", "native_program"),
                "game_status": ("simulated", "game", "state"),
                "operation_status": ("state", "account_id", "started_at"),
            }[name]
            value = {key: result[key] for key in fields if key in result}
        by_source[name, page] = value
    return by_source


def previous_observation(db, goal_id):
    """Use the original periodic receipt even if the owner later steered its task."""
    for prior in db.execute("""
        SELECT id,source_key,due,steps,status,revision FROM agent_tasks
        WHERE creative_goal_id=? AND (source_key=? OR source_key IS NULL)
        ORDER BY rowid DESC
    """, (goal_id, FOLLOWUP_SOURCE)):
        if prior["source_key"] is None and prior["id"] != str(uuid5(UUID(goal_id), f"followup:{int(prior['due'])}")):
            continue
        steps = [step for step in json.loads(prior["steps"]) if step["revision"] == 1]
        success = prior["status"] == "completed" if prior["revision"] == 1 else deliver_from_receipts(steps, 1)[0]
        return success, observations(steps)
    return None


class FollowupGrant(StrictModel):
    epoch: int = Field(strict=True, ge=1)
    cadence: Literal["daily", "weekly"]
    local_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=1, max_length=80)
    weekday: int | None = Field(default=None, strict=True, ge=0, le=6)
    states: list[StatusSource] = Field(default_factory=list, max_length=4)
    pages: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def valid_grant(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("请选择有效时区") from error
        if (self.weekday is not None) != (self.cadence == "weekly"):
            raise ValueError("仅每周跟进须指定星期")
        if not self.pages and not self.states:
            raise ValueError("请明确授权至少一个只读来源")
        canonical = []
        for page in self.pages:
            try:
                url = source_url(page)
            except httpx.InvalidURL as error:
                raise ValueError("网页地址无效") from error
            if (url.scheme != "https" or not url.host or url.userinfo or url.port is not None
                    or len(page) > 2048 or any(char.isspace() or char == "\\" for char in page)):
                raise ValueError("仅可授权公开 HTTPS 页面")
            canonical.append(str(url))
        self.pages = list(dict.fromkeys(canonical))
        self.states = list(dict.fromkeys(self.states))
        return self

    def slot(self, now, *, future):
        zone = ZoneInfo(self.timezone)
        local = datetime.fromtimestamp(now, zone)
        hour, minute = map(int, self.local_time.split(":"))
        for offset in range(16):
            day = date.fromordinal(local.date().toordinal() + (offset if future else -offset))
            if self.weekday is not None and day.weekday() != self.weekday:
                continue
            wall = datetime(day.year, day.month, day.day, hour, minute)
            stamp = int(wall.replace(tzinfo=zone, fold=0).timestamp())
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == wall and (stamp > now) == future:
                return stamp
        raise ValueError("找不到有效的本地调度时间")


class GoalFollowups:
    def __init__(self, tasks):
        self.tasks, self.store = tasks, tasks.store
        self.last_scope = None
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS goal_followups(
                goal_id TEXT PRIMARY KEY REFERENCES creative_goals(id), scope TEXT NOT NULL,
                grant_id TEXT NOT NULL, config TEXT NOT NULL, next_due INTEGER NOT NULL, enabled INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS goal_followups_due ON goal_followups(enabled,next_due);
            CREATE TRIGGER IF NOT EXISTS goal_followup_closed AFTER UPDATE OF closed ON creative_goals
            WHEN NEW.closed=1 BEGIN UPDATE goal_followups SET enabled=0 WHERE goal_id=NEW.id; END;
        """)

    def project(self, goal_id):
        project = self.tasks.workspace.goal(goal_id)
        if not project or not self.tasks.current(goal_id):
            raise HTTPException(404, "找不到这个长期目标")
        return project

    def get(self, goal_id):
        project = self.project(goal_id)
        scope = self.tasks.scope()
        row = self.store.one("SELECT * FROM goal_followups WHERE goal_id=?", (goal_id,))
        active = bool(row and row["enabled"] and row["scope"] == dump(scope) and not project["closed"])
        return {"epoch": scope[-1] if self.tasks.identity else None, "enabled": active,
                "schedule": json.loads(row["config"]) if row else None, "next_due": row["next_due"] if active else None}

    def configure(self, goal_id, grant, now):
        project = self.project(goal_id)
        scope = self.tasks.scope()
        if project["closed"] or not self.tasks.identity or grant.epoch != scope[-1]:
            raise HTTPException(409, "目标已结束或执行授权已变化，请刷新")
        if any(name not in self.tasks.tools or self.tasks.tools[name].review for name in grant.states):
            raise HTTPException(409, "所选只读来源不可用")
        current = self.get(goal_id)
        if current["enabled"] and current["schedule"] == grant.model_dump():
            return current
        due = grant.slot(now, future=True)
        self.store.execute("INSERT OR REPLACE INTO goal_followups VALUES(?,?,?,?,?,1)",
                           (goal_id, dump(scope), uuid4().hex, grant.model_dump_json(), due))
        self.tasks.wake.set()
        return self.get(goal_id)

    def disable(self, goal_id):
        self.project(goal_id)
        self.store.execute("UPDATE goal_followups SET enabled=0 WHERE goal_id=?", (goal_id,))
        return self.get(goal_id)

    def granted(self, row):
        return row["scope"] == dump(self.tasks.scope()) and self.store.one(
            "SELECT 1 FROM goal_followups WHERE goal_id=? AND grant_id=? AND enabled=1 AND next_due=?",
            (row["goal_id"], row["grant_id"], row["next_due"]),
        )

    async def tick(self, now=None):
        if not self.store.one("SELECT 1 FROM goal_followups WHERE enabled=1 LIMIT 1"):
            return
        now = time.time() if now is None else now
        scope = dump(self.tasks.scope())
        if scope != self.last_scope:
            self.store.execute("UPDATE goal_followups SET enabled=0 WHERE enabled=1 AND scope<>?", (scope,))
            self.last_scope = scope
        row = self.store.one("SELECT * FROM goal_followups WHERE enabled=1 AND next_due<=? ORDER BY next_due,goal_id LIMIT 1",
                             (now,))
        if not row:
            return
        grant = FollowupGrant.model_validate_json(row["config"])
        slot = grant.slot(now, future=False)
        run_id = str(uuid5(UUID(row["goal_id"]), f"followup:{slot}"))
        next_due = grant.slot(now, future=True)
        if self.store.one("SELECT 1 FROM agent_tasks WHERE id=?", (run_id,)):
            self.store.execute("UPDATE goal_followups SET next_due=? WHERE goal_id=? AND grant_id=?",
                               (next_due, row["goal_id"], row["grant_id"]))
            return
        steps = []
        for name, page in [*((state, None) for state in grant.states), *(("web_page", url) for url in grant.pages)]:
            if not self.granted(row):
                return
            step_id = str(uuid5(UUID(run_id), page or name))
            try:
                async with asyncio.timeout(4):
                    if page:
                        result = await fetch_excerpt(page, exact_source=True)
                    else:
                        tool = self.tasks.tools[name]
                        if tool.review:
                            raise ValueError("授权不包含外部动作")
                        result = await tool.run(ToolRequest(step_id, "", json.loads(scope), None, None),
                                                tool.arguments.model_validate({}))
            except Exception:
                result = {"error": "本次未能读取已授权来源。"}
            steps.append({"id": step_id, "tool": name, "arguments": {"web_fetch": page} if page else {},
                          "revision": 1, "result": result})
        if not self.granted(row):
            return
        success, body = deliver_from_receipts(steps, 1)
        with self.store.db as db:
            moved = db.execute("UPDATE goal_followups SET next_due=? WHERE goal_id=? AND grant_id=? AND enabled=1 AND next_due=?",
                               (next_due, row["goal_id"], row["grant_id"], row["next_due"]))
            if not moved.rowcount:
                return
            previous = previous_observation(db, row["goal_id"])
            label = datetime.fromtimestamp(slot, ZoneInfo(grant.timezone)).strftime("%Y-%m-%d %H:%M")
            inserted = db.execute("""
                INSERT OR IGNORE INTO agent_tasks(id,scope,goal,revision,status,steps,message,due,created,updated,creative_goal_id,source_key)
                VALUES(?,?,?,1,?,?,?,?,?,?,?,?)
            """, (run_id, scope, f"周期跟进 · {label}", "completed" if success else "failed", dump(steps),
                  body, slot, now, now, row["goal_id"], FOLLOWUP_SOURCE))
            if not inserted.rowcount:
                return
            if success:
                self.tasks.workspace.record_delivery(db, {"id": run_id, "revision": 1, "scope": json.loads(scope)}, body)
            if previous != (success, observations(steps)):
                if previous is not None:
                    self.tasks.workspace.notify(db, run_id, now)
                if self.tasks.record_chat_delivery:
                    self.tasks.record_chat_delivery(db, {
                        "id": run_id, "scope": json.loads(scope), "revision": 1, "creative_goal_id": row["goal_id"],
                    }, "completed" if success else "failed", steps, now)


def register_goal_followups(app, tasks, require_admin):
    followups = tasks.followups = GoalFollowups(tasks)
    path = "/api/agent/tasks/{identity}/goal/followup"
    deps = [Depends(require_admin)]

    @app.get(path, dependencies=deps)
    async def get_followup(identity: UUID):
        return followups.get(str(identity))

    @app.put(path, dependencies=deps)
    async def set_followup(identity: UUID, body: FollowupGrant):
        return followups.configure(str(identity), body, time.time())

    @app.delete(path, dependencies=deps)
    async def disable_followup(identity: UUID):
        return followups.disable(str(identity))
