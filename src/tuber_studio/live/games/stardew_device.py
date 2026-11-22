"""Local filesystem transport to the isolated SMAPI game process."""

import json
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import Field

from ...core.models import StrictModel


class DeviceCommand(StrictModel):
    session: UUID
    id: UUID
    epoch: int = Field(ge=0)
    action: Literal["takeover", "release", "turn_right", "next_slot", "till_front", "plant_front", "water_front"]
    target: str | None = Field(default=None, max_length=160)
    item: str | None = Field(default=None, max_length=160)


class StardewDevice:
    def __init__(self, directory):
        self.directory = Path(directory) if directory else None
        if self.directory and not self.directory.is_absolute():
            raise ValueError("实机通信目录需要使用绝对路径")

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_bytes())
        except FileNotFoundError:
            return None

    def status(self):
        snapshot = self._read(self.directory / "state.json") if self.directory else None
        online = bool(snapshot and -1000 <= time.time() * 1000 - snapshot["observed"] < 5000)
        return {"configured": bool(self.directory), "online": online, "state": snapshot}

    def receipt(self, session: UUID, identity: UUID):
        if not self.directory:
            return None
        folder = self.directory / str(session)
        binding = self._read(folder / f"{identity}.command")
        if not binding:
            return None
        result = self._read(folder / f"{identity}.result")
        if result:
            return result
        if (folder / f"{identity}.cancelled").exists():
            return binding | {"status": "cancelled"}
        return binding | {"status": "pending" if time.time() * 1000 <= binding["expires"] else "unknown"}

    def submit(self, command: DeviceCommand):
        if not self.directory:
            raise ValueError("尚未连接隔离游戏")
        data = command.model_dump(mode="json")
        previous = self.receipt(command.session, command.id)
        if previous:
            binding = self._read(self.directory / data["session"] / (data["id"] + ".command"))
            if any(binding[k] != v for k, v in data.items()):
                raise ValueError("动作编号已绑定其他请求")
            return previous
        status = self.status()
        state = status["state"]
        if (not status["online"] or state["session"] != data["session"] or
                command.action != "takeover" and state["epoch"] != data["epoch"]):
            raise ValueError("游戏已断开或控制权已变化")
        if command.action not in {"takeover", "release"} and (state["owner"] != "studio" or not state["can_act"]):
            raise ValueError("当前由号主控制或角色不可行动")
        if command.action.endswith("_front"):
            farm = state["farm"]
            equipped = farm["inventory"][farm["slot"]]
            if command.target != farm["front"]["key"] or not equipped or command.item != equipped["item"]:
                raise ValueError("面前地块或手持物品已变化")
        folder = self.directory / data["session"]
        binding = folder / (data["id"] + ".command")
        prefix = "takeover-" if command.action == "takeover" else ""
        with NamedTemporaryFile(dir=folder, delete=False) as tmp:
            tmp.write(json.dumps(data | {"expires": int(time.time() * 1000) + 4000}).encode())
        try:
            try:
                os.link(tmp.name, binding)
            except FileExistsError:
                return self.submit(command)
            # Only the creator publishes. A crash after binding is reconciled by ID.
            os.link(binding, folder / (prefix + data["id"] + ".request"))
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return self.receipt(command.session, command.id)

    def cancel(self, session, identity):
        if not self.directory:
            return None
        folder = self.directory / str(session)
        try:
            (folder / f"{identity}.request").rename(folder / f"{identity}.cancelled")
        except FileNotFoundError:
            original = self.receipt(session, identity)
            if original and original["status"] in {"pending", "unknown"}:
                return original | {"status": "unknown", "error": "动作可能已执行，请按原编号核对游戏回执"}
            return original
        return {"status": "cancelled", "session": str(session), "id": str(identity)}


def register_stardew_device(app, require_admin, *, on_device=None):
    device = StardewDevice(os.environ.get("TUBER_STARDEW_BRIDGE"))
    app.state.stardew_device = device
    deps = [Depends(require_admin)]
    if device.directory and on_device is not None:
        on_device(device)

    @app.get("/api/games/device/stardew", dependencies=deps)
    def state():
        return device.status()

    @app.post("/api/games/device/stardew/actions", dependencies=deps)
    def submit(command: DeviceCommand):
        try:
            return device.submit(command)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/games/device/stardew/actions/{session}/{identity}", dependencies=deps)
    def receipt(session: UUID, identity: UUID):
        result = device.receipt(session, identity)
        if result is None:
            raise HTTPException(404, "没有这个实机会话的动作记录")
        return result
    return device
