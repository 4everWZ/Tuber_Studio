"""Versioned, task-scoped control of an offline Chromium reading session."""

import asyncio
import json
import time
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from ...core.models import StrictModel
from ...core.store import dump
from .browser_chromium import ChromiumReaders
from .browser_document import MAX_SOURCES, read_document, source_list

MAX_TURNS = 120
LEASE = 3600
TERMINAL = {"completed", "cancelled", "failed"}


class BrowserGrant(StrictModel):
    revision: int = Field(strict=True, ge=1)
    version: int = Field(default=0, strict=True, ge=0)
    sources: list[str] = Field(min_length=1, max_length=MAX_SOURCES)


class Command(StrictModel):
    kind: Literal["read", "open", "activate", "find", "next", "previous"]
    index: int = Field(default=0, strict=True, ge=0, le=5000)
    text: str = Field(default="", max_length=160)
    version: int = Field(strict=True, ge=1)


class BrowserAction(Command):
    task_id: UUID


class Control(StrictModel):
    action: Literal["takeover", "handback", "end"]
    version: int = Field(strict=True, ge=1)


def browser_grant(task):
    return next((s for s in reversed(task["steps"]) if s["tool"] == "owner_browser_grant"), None)


class PrivateBrowser:
    def __init__(self, tasks):
        self.tasks, self.store = tasks, tasks.store
        self.inflight = {}
        self.readers = ChromiumReaders()
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS task_browser_sessions(
                task_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, task_revision INTEGER NOT NULL, scope TEXT NOT NULL,
                mode TEXT NOT NULL, version INTEGER NOT NULL, sources TEXT NOT NULL, position INTEGER NOT NULL,
                view TEXT NOT NULL, turns INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS task_browser_documents(
                grant_id TEXT NOT NULL,position INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(grant_id,position));
            CREATE TABLE IF NOT EXISTS task_browser_receipts(
                grant_id TEXT NOT NULL,step_id TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(grant_id,step_id));
            CREATE TRIGGER IF NOT EXISTS browser_ended AFTER UPDATE OF mode ON task_browser_sessions
                WHEN NEW.mode='ended' BEGIN
                  DELETE FROM task_browser_documents WHERE grant_id=OLD.grant_id;
                  DELETE FROM task_browser_receipts WHERE grant_id=OLD.grant_id;
                END;
            CREATE TRIGGER IF NOT EXISTS browser_task_finished AFTER UPDATE OF revision,status ON agent_tasks
                WHEN NEW.revision<>OLD.revision OR NEW.status IN ('cancelled','failed','completed') BEGIN
                  UPDATE task_browser_sessions SET mode='ended',version=version+1,view='{}' WHERE task_id=NEW.id AND mode<>'ended';
                END;
        """)
        if tasks.identity:
            self.store.execute("""
                CREATE TRIGGER IF NOT EXISTS browser_authority_revoked AFTER UPDATE OF binding ON agent_task_authority
                WHEN OLD.binding IS NOT NEW.binding BEGIN
                    UPDATE task_browser_sessions SET mode='ended',version=version+1,view='{}' WHERE mode<>'ended';
                END
            """)

    def task(self, identity):
        task = self.tasks.executable(identity)
        if not task or not self.tasks.can_control(task):
            self.readers.release(identity, force=True)
            raise LookupError("当前授权下找不到任务")
        return task

    def session(self, task):
        self.store.execute("UPDATE task_browser_sessions SET mode='ended',version=version+1,view='{}' "
                           "WHERE mode<>'ended' AND expires<=?", (time.time(),))
        row = self.store.one("SELECT * FROM task_browser_sessions WHERE task_id=?", (task["id"],))
        marker = browser_grant(task)
        if (marker and (not row or row["mode"] == "ended" and row["grant_id"] != marker["id"])
                and task["revision"] == marker["result"]["revision"]
                and task["status"] not in TERMINAL and not task["pending_control"]):
            expires = marker["result"].get("granted_at", task["created"]) + LEASE
            mode = "agent" if expires > time.time() else "ended"
            self.store.execute("INSERT OR REPLACE INTO task_browser_sessions VALUES(?,?,?,?,?,?,?,1,'{}',0,?)",
                               (task["id"], marker["id"], task["revision"], dump(task["scope"]),
                                mode, row["version"] + 1 if row else 1, dump(marker["result"]["sources"]), expires))
            row = self.store.one("SELECT * FROM task_browser_sessions WHERE task_id=?", (task["id"],))
        return row

    def state(self, identity):
        task = self.task(identity)
        row = self.session(task)
        if not row:
            return {"mode": "ungranted", "revision": task["revision"]}
        if row["mode"] == "ended":
            self.readers.release(identity)
        return {key: row[key] for key in ("mode", "version", "position", "turns", "expires")} | {
            "revision": task["revision"], "sources": json.loads(row["sources"]), "view": json.loads(row["view"])}

    def grant(self, identity, grant):
        task = self.task(identity)
        if (grant.revision != task["revision"] or task["status"] in TERMINAL or task["pending_control"]
                or task["status"] == "awaiting_approval" or any("result" not in s for s in task["steps"])
                or any(s["tool"] in {"owner_attachment", "owner_code_grant"} for s in task["steps"])
                or isinstance(task["plan"], dict) and task["plan"].get("video_selection")):
            raise ValueError("当前任务版本或已有能力与浏览授权不相容")
        row = self.session(task)
        if (row and row["mode"] != "ended") or grant.version != (row["version"] if row else 0):
            raise ValueError("当前浏览授权已变化，请刷新")
        sources = source_list(grant.sources)
        if row:
            self.readers.release(identity, force=True)
        marker = {"tool": "owner_browser_grant", "id": str(uuid4()), "revision": 0, "arguments": {},
                  "result": {"sources": sources, "revision": task["revision"], "granted_at": time.time()}}
        plan = dict(task["plan"]) if isinstance(task["plan"], dict) else {"backend": "responses", "prior_goals": []}
        plan.pop("candidate", None)
        if not self.tasks._save(task, steps=[*task["steps"], marker], plan=plan,
                                status="scheduled" if task["status"] == "scheduled" else "queued",
                                due=task["due"] if task["status"] == "scheduled" else time.time()):
            raise ValueError("任务刚刚发生变化")
        self.tasks.wake.set()
        return self.state(identity)

    def control(self, identity, command):
        task = self.task(identity)
        row = self.session(task)
        transitions = {"takeover": ("agent", "owner"), "handback": ("owner", "agent")}
        if not row or row["mode"] == "ended" or command.version != row["version"]:
            raise ValueError("控制状态已变化，请刷新")
        mode = "ended"
        if command.action != "end":
            expected, mode = transitions[command.action]
            if row["mode"] != expected:
                raise ValueError("当前控制权不支持此操作")
        self.store.execute("UPDATE task_browser_sessions SET mode=?,version=version+1,"
                           "view=CASE WHEN ?='ended' THEN '{}' ELSE view END WHERE task_id=?",
                           (mode, mode, identity))
        if running := self.inflight.get(identity):
            running.cancel()
        if task["status"] not in TERMINAL and task["status"] != "scheduled":
            status = "queued" if mode == "agent" and task["status"] == "needs_input" else task["status"]
            self.tasks._save(task, status=status, due=time.time())
        self.tasks.wake.set()
        return self.state(identity)

    def menu(self, task, allowed):
        if not browser_grant(task):
            return {name: tool for name, tool in allowed.items() if name != "private_browser"}
        return {name: tool for name, tool in allowed.items() if name in {"private_browser", "delegate_text"}}

    def bind(self, task, tools):
        function = next((tool for tool in tools if tool["name"] == "private_browser"), None)
        if function:
            state = self.state(task["id"])
            if state["mode"] != "agent":
                tools.remove(function)
                return
            schema = function["parameters"]["properties"]
            schema["task_id"]["enum"] = [task["id"]]
            schema["version"]["enum"] = [state["version"]]
            menu = {"sources": state["sources"], "position": state["position"],
                    "controls": state["view"].get("controls", [])}
            function["description"] += " 当前页面范围：" + dump(menu)

    async def tool(self, request, command):
        identity = str(command.task_id)
        task = self.task(identity)
        if (request.scope != task["scope"] or not any(s["id"] == request.id and s["revision"] == task["revision"]
                and s["tool"] == "private_browser" and "result" not in s for s in task["steps"])):
            return {"error": "此调用不属于获准任务的当前步骤"}
        try:
            return await self.turn(identity, command, "agent", step=request.id)
        except (LookupError, ValueError) as exc:
            return {"error": str(exc)}

    async def turn(self, identity, command, actor, *, step=None):
        task = self.task(identity)
        row = self.session(task)
        if (not row or row["mode"] != actor or row["task_revision"] != task["revision"]
                or task["pending_control"] or task["status"] in TERMINAL):
            raise ValueError("当前任务或浏览控制权已变化")
        if step:
            saved = self.store.one("SELECT result FROM task_browser_receipts WHERE grant_id=? AND step_id=?",
                                   (row["grant_id"], step))
            if saved:
                return json.loads(saved["result"])
        if command.version != row["version"] or identity in self.inflight:
            raise ValueError("浏览状态已变化，请重读当前页面")
        if row["turns"] >= MAX_TURNS or len(self.inflight) >= 2:
            raise ValueError("浏览操作预算已满或浏览器正在使用中")
        sources, view = json.loads(row["sources"]), json.loads(row["view"])
        position = row["position"]
        action = {"kind": command.kind, "index": command.index, "value": command.text}
        if command.kind == "open":
            if not 1 <= command.index <= len(sources):
                raise ValueError("来源编号超出授权")
            position, view = command.index, {}
        elif command.kind == "activate":
            control = next((c for c in view.get("controls", []) if c["index"] == command.index), None)
            if not control:
                raise ValueError("该控件不在当前可见页面")
            if control["kind"] == "link":
                if control["source"] != position:
                    view = {}
                position = control["source"]
                action = {"kind": "anchor", "value": control["fragment"]}
        elif command.kind == "find" and not command.text.strip():
            raise ValueError("请填写页内查找内容")
        version = row["version"] + 1
        self.store.execute("UPDATE task_browser_sessions SET version=?,turns=turns+1 WHERE task_id=?", (version, identity))

        def valid():
            latest = self.task(identity)
            current = self.store.one("SELECT version,mode,expires FROM task_browser_sessions WHERE task_id=?", (identity,))
            if (latest["pending_control"] or latest["revision"] != row["task_revision"] or latest["status"] in TERMINAL
                    or current["version"] != version or current["mode"] != actor or current["expires"] <= time.time()):
                raise ValueError("浏览授权已撤回或控制权已移交")

        async def render():
            cached = self.store.one("SELECT document FROM task_browser_documents WHERE grant_id=? AND position=?",
                                    (row["grant_id"], position))
            url = sources[position - 1]
            valid()
            document = cached["document"] if cached else await read_document(url, guard=valid)
            valid()
            saved_state = {k: view[k] for k in ("opened", "checked", "scroll") if k in view}
            result = await self.readers.render(identity, {"document": document, "state": saved_state,
                                                         "action": action, "url": url, "sources": sources})
            valid()
            receipt = {"url": url, "excerpt": result["text"], "controls": result["controls"], "position": position,
                       "truncated": True}
            with self.store.db as db:
                if not cached:
                    db.execute("INSERT INTO task_browser_documents VALUES(?,?,?)", (row["grant_id"], position, document))
                db.execute("UPDATE task_browser_sessions SET position=?,view=?,version=version+1 WHERE task_id=?",
                           (position, dump(result), identity))
                if step:
                    db.execute("INSERT INTO task_browser_receipts VALUES(?,?,?)", (row["grant_id"], step, dump(receipt)))
            return receipt

        job = self.inflight[identity] = asyncio.create_task(render())
        try:
            return await job
        except asyncio.CancelledError:
            if job.cancelled() and self.state(identity)["version"] != version:
                raise ValueError("浏览操作已随控制权变更而停止") from None
            raise
        except (TimeoutError, OSError, httpx.HTTPError) as exc:
            raise ValueError("隔离读取超时或无法连接指定来源") from exc
        finally:
            if job.cancelled() or job.exception() is not None:
                self.readers.release(identity, force=True)
            self.inflight.pop(identity, None)

    async def close(self):
        jobs = list(self.inflight.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.readers.close()


def attach_private_browser(tasks, *, tool_cls=None):
    """Attach the private Chromium reader tool onto the task coordinator."""
    tool_factory = tool_cls or tasks.Tool
    browser = tasks.private_browser = PrivateBrowser(tasks)
    tasks.register("private_browser", tool_factory("操作独立 Chromium 阅读页。open 用来源编号，activate 用可见控件编号，"
                   "find 用页内文字，read/next/previous 读取或翻页。", BrowserAction, browser.tool,
                   responses_only=True, own_deadline=True))
    return browser
