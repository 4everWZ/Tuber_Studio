"""Private creative goals and versioned deliverables. Task receipts remain immutable inputs."""

import json
import math
import time


class CreativeWorkspace:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS creative_goals(
                id TEXT PRIMARY KEY REFERENCES agent_tasks(id), title TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS creative_editions(
                task_id TEXT NOT NULL REFERENCES agent_tasks(id), task_revision INTEGER NOT NULL,
                edition INTEGER NOT NULL, body TEXT NOT NULL, author TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(task_id, task_revision, edition));
            CREATE TABLE IF NOT EXISTS creative_attention(
                task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id), updated REAL NOT NULL);
        """)
        if "creative_goal_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(agent_tasks)")}:
            store.execute("ALTER TABLE agent_tasks ADD COLUMN creative_goal_id TEXT REFERENCES creative_goals(id)")
        store.execute("CREATE INDEX IF NOT EXISTS agent_tasks_goal ON agent_tasks(creative_goal_id)")

    def promote(self, task):
        if task["creative_goal_id"] and task["creative_goal_id"] != task["id"]:
            raise ValueError("这次执行已属于另一个目标")
        with self.store.db as database:
            database.execute("INSERT OR IGNORE INTO creative_goals(id,title) VALUES (?,?)", (task["id"], task["goal"]))
            database.execute("UPDATE agent_tasks SET creative_goal_id=id WHERE id=?", (task["id"],))

    def goal(self, identity):
        return self.store.one("SELECT * FROM creative_goals WHERE id=?", (identity,))

    def runs(self, identity):
        return self.store.all("SELECT id,goal,status,revision FROM agent_tasks WHERE creative_goal_id=? ORDER BY created,id",
                              (identity,))

    def stop_goal(self, identity, scope, confirmed, stops_for):
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            goal = self.goal(identity)
            if not goal:
                raise LookupError(identity)
            if goal["closed"]:
                return set()
            runs = self.store.all(
                "SELECT * FROM agent_tasks WHERE creative_goal_id=? AND scope=? "
                "AND status NOT IN ('completed','failed','cancelled')", (identity, scope),
            )
            if {run["id"]: run["revision"] for run in runs} != confirmed:
                raise ValueError(f"当前授权下有 {len(runs)} 项在途执行，请刷新并确认整体停止范围。")
            db.execute("UPDATE creative_goals SET closed=1 WHERE id=?", (identity,))
            now = time.time()
            for run in runs:
                previous = json.loads(run["pending_control"]) if run["pending_control"] else None
                stops = previous["stops"] if previous and "stops" in previous else stops_for(run)
                message = "长期目标已结束，本次执行已停止。"
                pending = json.dumps({"stops": stops, "status": "cancelled", "message": message,
                                      "goal_stop": True}) if stops else None
                db.execute(
                    "UPDATE agent_tasks SET revision=revision+1,status=?,message=?,due=?,updated=?,pending_control=? "
                    "WHERE id=?", ("waiting" if stops else "cancelled", "长期目标已结束，正在核对原操作。" if stops else message,
                                   now, max(now, math.nextafter(run["updated"], math.inf)), pending, run["id"]),
                )
                db.execute("DELETE FROM creative_attention WHERE task_id=?", (run["id"],))
            return set(confirmed)

    def close(self, identity, executable):
        if any(run["status"] not in {"completed", "failed", "cancelled"} and executable(run["id"])
               for run in self.runs(identity)):
            raise ValueError("请先停止或完成这个目标下仍在进行的任务")
        self.store.execute("UPDATE creative_goals SET closed=1 WHERE id=?", (identity,))

    def record_delivery(self, database, task, body):
        database.execute(
            "INSERT OR IGNORE INTO creative_editions VALUES (?,?,1,?,'agent',?)",
            (task["id"], task["revision"], body, time.time()),
        )

    def notify(self, database, identity, updated):
        database.execute("INSERT OR REPLACE INTO creative_attention VALUES (?,?)", (identity, updated))

    def latest(self, identity):
        return self.store.one(
            "SELECT * FROM creative_editions WHERE task_id=? ORDER BY task_revision DESC,edition DESC LIMIT 1",
            (identity,),
        )

    def edit(self, task, revision, edition, body):
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self.latest(task["id"])
            if not latest or (latest["task_revision"], latest["edition"]) != (revision, edition):
                raise ValueError("成果已有新版本，请刷新后再保存")
            db.execute("INSERT INTO creative_editions VALUES (?,?,?,?,'owner',?)",
                       (task["id"], revision, edition + 1, body, time.time()))
            return self.latest(task["id"])
