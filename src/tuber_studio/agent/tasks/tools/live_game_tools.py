"""Live session handoffs, simulated game controls and Stardew Valley device tools via LiveFacade."""

import asyncio
import os
import time
from uuid import UUID

import httpx

from ....core.models import StrictModel
from ....infra.inference.http_api import InferenceQueueBusy, response_json
from ....live.facade import DeviceCommand, LiveFacade, live_facade
from ..handoff import PublicText, handoff_text, supplied_private_fragments
from ..runtime import Tool, ToolWait
from .schemas import FarmAction, GameAction, LiveText


def observed_game(steps):
    for step in reversed(steps):
        if step["tool"] == "game_status":
            result = step.get("result", {})
            if not result.get("error") and result.get("simulated") is True:
                return result.get("game")
            return None
    return None


async def bound_owner(runtime, character):
    binding = character.get("bilibili_owner") or {}
    if not binding.get("uid"):
        return {"error": "这个角色尚未绑定 B 站号主。"}
    profile = await runtime.account.owner_profile(binding["uid"])
    return {"name": profile.name, "uid": profile.uid, "url": f"https://space.bilibili.com/{profile.uid}"}


async def observed_live_status(runtime):
    result = {}
    address = os.getenv("TUBER_NATIVE_PROGRAM_STATUS_URL", "").strip()
    if address:
        state = "unavailable"
        try:
            url = httpx.URL(address)
            if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.fragment:
                raise ValueError("Invalid native program status URL")
            async with asyncio.timeout(1.5), httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                health = await response_json(client, "GET", str(url))
                program = health.get("native_program")
                if health.get("process") == "ready" and isinstance(program, dict):
                    reported = program.get("state")
                    if reported in ("idle", "starting", "publishing", "stopping"):
                        state = reported
        except (TimeoutError, ValueError, InferenceQueueBusy, httpx.InvalidURL, httpx.HTTPError):
            pass
        result["native_program"] = {"state": state}
    status = live_facade(runtime).status()
    return result | {"state": status["state"], "muted": status["muted"], "session_id": status["session_id"]}


def build_live_game_tools(runtime):
    async def live(request, args):
        return await observed_live_status(runtime)

    async def game(request, args):
        state = live_facade(runtime).game_status()
        return {"simulated": True, "game": state["active_game_id"], "state": state["active_status"]}

    def review_live(args):
        facade = live_facade(runtime)
        ok, session_id = facade.can_accept_handoff()
        if not runtime.settings.data.get("features", {}).get("live_enabled") or not ok:
            raise ValueError("当前没有可交接的直播场次")
        return {"target": "当前直播场次", "session": session_id,
                "text": handoff_text(PublicText(text=args.text))}

    async def live_handoff(request, args):
        approval = request.approval
        facade = live_facade(runtime)
        text = handoff_text(PublicText(text=approval["text"]), private_fragments=supplied_private_fragments(request.goal))
        checkpoint = request.checkpoint
        if checkpoint is None:
            receipt = await facade.submit_handoff(request.id, approval["session"], text, account=request.scope[0])
            if not receipt.get("job_id"):
                return {"status": receipt["status"], "accepted": False}
            checkpoint = {"job_id": receipt["job_id"], "expires": time.time() + 180}
        result = facade.handoff_result(approval["session"], checkpoint["job_id"])
        if result["status"] in {"queued", "generating", "synthesizing", "ready_to_play", "playing"}:
            if checkpoint["expires"] > time.time():
                return ToolWait(checkpoint, delay=2)
            await facade.cancel_handoff(request.id, approval["session"])
            result = facade.handoff_result(approval["session"], checkpoint["job_id"])
        return result

    async def stop_handoff(identity, checkpoint):
        if session := checkpoint.get("session"):
            return await live_facade(runtime).cancel_handoff(identity, session)

    def review_game(args):
        return live_facade(runtime).validate_game_handoff(
            args.game,
            args.action,
            live_enabled=bool(runtime.settings.data.get("features", {}).get("live_enabled")),
        )

    async def game_action(request, args):
        facade = live_facade(runtime)
        if receipt := facade.game_handoff_receipt(request.id, args.game, args.action):
            return receipt
        if observed_game(request.receipts) != args.game:
            return {"error": "执行前须取得本轮对应模拟游戏的局面回执。"}
        current = review_game(args)
        if request.approval != current:
            return {"error": "模拟游戏局面或控制权已变化，须重新核对动作。"}
        return facade.execute_game_handoff(request.id, args.game, args.action)

    return {
        "live_status": Tool("读取平台直播会话与播放控制状态，以及独立的 Omni 原生节目状态（若已配置）。", StrictModel, live,
                            parallel_read=True, foreground_read=True),
        "live_handoff": Tool("提议向当前直播讲一段可公开的材料。需号主确认正文，实际播完才返回台词。",
                             LiveText, live_handoff, stop_handoff, review_live),
        "game_status": Tool("读取当前模拟游戏局面。", StrictModel, game, foreground_read=True),
        "game_action": Tool("提议一个模拟游戏动作，必须先核对游戏局面，需号主确认。",
                            GameAction, game_action, review=review_game),
    }


def _stardew_binding(state, action):
    farm = state["farm"]
    item = farm["inventory"][farm["slot"]]
    return {"session": state["session"], "epoch": state["epoch"], "action": action, "save": farm["save"],
            "location": farm["location"], "target": farm["front"]["key"], "slot": farm["slot"],
            "item": item["item"] if item else None, "facing": farm["facing"], "front": farm["front"]}


def register_device_tools(tasks, device_or_facade):
    is_facade = isinstance(device_or_facade, LiveFacade)

    def dev_status():
        return device_or_facade.stardew_status() if is_facade else device_or_facade.status()

    def dev_receipt(session, identity):
        return device_or_facade.stardew_receipt(session, identity) if is_facade else device_or_facade.receipt(session, identity)

    def dev_submit(cmd):
        return device_or_facade.stardew_submit(cmd) if is_facade else device_or_facade.submit(cmd)

    def dev_cancel(session, identity):
        return device_or_facade.stardew_cancel(session, identity) if is_facade else device_or_facade.cancel(session, identity)

    async def read(request, args):
        status = dev_status()
        if not status["online"]:
            return {"error": "星露谷实机已断开"}
        return {"state": status["state"]}

    def review(args):
        status = dev_status()
        state = status["state"]
        if not status["online"] or state["owner"] != "studio" or not state["can_act"]:
            raise ValueError("实机当前未交回工坊或角色不可行动")
        return _stardew_binding(state, args.action)

    async def act(request, args):
        approved = request.approval
        if not approved or args.action != approved["action"]:
            return {"error": "缺少对应实机动作审批"}
        session, identity = UUID(approved["session"]), UUID(request.id)
        result = dev_receipt(session, identity)
        if result is None:
            if request.checkpoint:
                return {"error": "原动作回执不可用，结果未知"}
            seen = next((r["result"].get("state") for r in reversed(request.receipts)
                         if r["tool"] == "stardew_status"), None)
            if (not seen or not seen.get("farm") or seen["owner"] != "studio" or
                    _stardew_binding(seen, args.action) != approved or review(args) != approved):
                return {"error": "实机局面或控制权已变化，请重新观察"}
            result = dev_submit(DeviceCommand(session=session, id=identity, epoch=approved["epoch"],
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
        return dev_cancel(UUID(session), UUID(identity))

    tasks.register("stardew_status", Tool("读取隔离星露谷实机当前存档、地块、手持物品和控制权。",
                                         StrictModel, read))
    tasks.register("stardew_action", Tool("先观察实机，再提议单个动作。游戏读回结果，F8 可接管。",
                                         FarmAction, act, stop=stop, review=review))
