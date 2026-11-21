"""Project task transitions into the private conversation that started them."""

import json
from uuid import UUID, uuid5

from ..operation_continuations import content_hash
from ..store import dump


def waiting_delivery(steps):
    checkpoint = (steps[-1].get("checkpoint") or {}) if steps else {}
    return checkpoint.get("interaction") or checkpoint.get("status") == "needs_reconciliation"


def delivery_turn_id(task, status, steps):
    step = steps[-1]["id"] if status in {"awaiting_approval", "waiting"} and steps else None
    event = f"chat:{task['revision']}:{status}:{step or ''}"
    if status == "waiting" and step:
        checkpoint = steps[-1].get("checkpoint") or {}
        items = checkpoint.get("items") or {}
        event += ":" + content_hash({
            "interaction": checkpoint.get("interaction"),
            "status": checkpoint.get("status"),
            "items": {key: (item.get("status"), item.get("number")) for key, item in items.items()}
            if isinstance(items, dict) else {},
        })
    return str(uuid5(UUID(task["id"]), event)), step


def record_chat_delivery(database, task, status, steps, created):
    origin_id = task["id"]
    origin = database.execute("SELECT * FROM operator_chat_turns WHERE request_id=?", (origin_id,)).fetchone()
    if not origin and task.get("creative_goal_id"):
        origin_id = task["creative_goal_id"]
        origin = database.execute("SELECT * FROM operator_chat_turns WHERE request_id=?", (origin_id,)).fetchone()
    if not origin:
        return
    saved = json.loads(origin["result"])
    card = saved.get("card") or {}
    account, character, version, _, owner, _ = task["scope"]
    if (card.get("type") != "agent_task" or card["data"]["id"] != origin_id
            or origin["character_id"] != character or origin["character_version"] != version
            or saved.get("owner_source") != owner or saved.get("account_id") != account):
        return
    identity, step = delivery_turn_id(task, status, steps)
    delivery = {"task_id": task["id"], "revision": task["revision"], "status": status, "step_id": step}
    result = {"task_delivery": delivery, "account_id": account, "owner_source": owner,
              "conversation_id": saved.get("conversation_id"),
              "character_name": saved.get("character_name"), "memory_intent": "task_delivery"}
    if saved.get("call_origin"):
        result["call_origin"] = saved["call_origin"]
    database.execute("INSERT OR IGNORE INTO operator_chat_turns VALUES (?,?,?,?,?,?)",
                     (identity, character, version, "", dump(result), created))
