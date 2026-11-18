"""Agent-side reads and single-action approval for the real, isolated farm."""

from typing import Literal
from uuid import UUID

from ..agent.task_runtime import Tool, ToolWait
from ..models import StrictModel
from .stardew_device import DeviceCommand


class FarmAction(StrictModel):
    action: Literal["turn_right", "next_slot", "till_front", "plant_front", "water_front"]


def binding(state, action):
    farm = state["farm"]
    item = farm["inventory"][farm["slot"]]
    return {"session": state["session"], "epoch": state["epoch"], "action": action, "save": farm["save"],
            "location": farm["location"], "target": farm["front"]["key"], "slot": farm["slot"],
            "item": item["item"] if item else None, "facing": farm["facing"], "front": farm["front"]}


def register_device_tools(tasks, device):
    async def read(request, args):
        status = device.status()
        if not status["online"]:
            return {"error": "星露谷实机已断开"}
        return {"state": status["state"]}

    def review(args):
        status = device.status()
        state = status["state"]
        if not status["online"] or state["owner"] != "studio" or not state["can_act"]:
            raise ValueError("实机当前未交回工坊或角色不可行动")
        return binding(state, args.action)

    async def act(request, args):
        approved = request.approval
        if not approved or args.action != approved["action"]:
            return {"error": "缺少对应实机动作审批"}
        session, identity = UUID(approved["session"]), UUID(request.id)
        result = device.receipt(session, identity)
        if result is None:
            if request.checkpoint:
                return {"error": "原动作回执不可用，结果未知"}
            seen = next((r["result"].get("state") for r in reversed(request.receipts)
                         if r["tool"] == "stardew_status"), None)
            if (not seen or not seen.get("farm") or seen["owner"] != "studio" or
                    binding(seen, args.action) != approved or review(args) != approved):
                return {"error": "实机局面或控制权已变化，请重新观察"}
            result = device.submit(DeviceCommand(session=session, id=identity, epoch=approved["epoch"],
                                                 action=args.action, target=approved["target"], item=approved["item"]))
        if result["status"] == "pending":
            return ToolWait({"session": str(session)}, 0.2)
        if result["status"] != "succeeded":
            return result | {"error": "游戏未确认动作成功"}
        return result

    async def stop(identity, checkpoint):
        session = checkpoint.get("session")
        if not session:
            return None
        session, identity = UUID(session), UUID(identity)
        folder = device.directory / str(session)
        try:
            (folder / f"{identity}.request").rename(folder / f"{identity}.cancelled")
        except FileNotFoundError:
            original = device.receipt(session, identity)
            if original and original["status"] in {"pending", "unknown"}:
                return original | {"status": "unknown", "error": "动作可能已执行，请按原编号核对游戏回执"}
            return original
        return {"status": "cancelled", "session": str(session), "id": str(identity)}

    tasks.register("stardew_status", Tool("读取隔离星露谷实机当前存档、地块、手持物品和控制权。",
                                         StrictModel, read))
    tasks.register("stardew_action", Tool("先观察实机，再提议单个动作。游戏读回结果，F8 可接管。",
                                         FarmAction, act, stop=stop, review=review))
