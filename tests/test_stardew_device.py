import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tuber_studio.agent.task_runtime import ToolRequest, ToolWait
from tuber_studio.live.stardew_device import DeviceCommand, StardewDevice
from tuber_studio.live.stardew_tools import FarmAction, register_device_tools


def test_original_request_survives_disconnect_without_republication(tmp_path):
    device = StardewDevice(tmp_path)
    session, identity = uuid4(), uuid4()
    inbox = tmp_path / str(session)
    inbox.mkdir()
    state = {"session": str(session), "owner": "studio", "epoch": 1,
             "observed": time.time() * 1000, "can_act": True}
    (tmp_path / "state.json").write_text(json.dumps(state))
    command = DeviceCommand(session=session, id=identity, epoch=1, action="turn_right")
    assert device.submit(command)["status"] == "pending"
    (inbox / f"{identity}.request").rename(inbox / f"{identity}.claimed")
    stale = state | {"observed": 0}
    (tmp_path / "state.json").write_text(json.dumps(stale))
    assert not device.status()["online"]
    assert device.submit(command)["status"] == "pending"
    assert not list(inbox.glob("*.request"))
    with pytest.raises(ValueError, match="编号"):
        device.submit(command.model_copy(update={"action": "next_slot"}))
    with pytest.raises(ValueError, match="断开"):
        device.submit(command.model_copy(update={"id": uuid4()}))


def test_operator_control_and_bound_target(tmp_path):
    device = StardewDevice(tmp_path)
    session = uuid4()
    (tmp_path / str(session)).mkdir()
    state = {"session": str(session), "owner": "operator", "epoch": 2, "can_act": True,
             "observed": time.time() * 1000, "farm": {"slot": 0, "inventory": [{"item": "(T)Hoe"}],
                                                      "front": {"key": "Farm:2:1"}}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    command = DeviceCommand(session=session, id=uuid4(), epoch=2, action="till_front",
                            target="Farm:2:1", item="(T)Hoe")
    with pytest.raises(ValueError, match="号主"):
        device.submit(command)
    (tmp_path / "state.json").write_text(json.dumps(state | {"owner": "studio"}))
    for changes in [{"item": "(T)Axe"}, {"target": "Farm:3:3"}, {"epoch": 1}]:
        with pytest.raises(ValueError):
            device.submit(command.model_copy(update=changes))
    assert device.submit(command)["status"] == "pending"
    assert device.submit(DeviceCommand(session=session, id=uuid4(), epoch=1, action="takeover"))["status"] == "pending"


async def test_agent_requires_real_observation_and_can_cancel_unclaimed_action(tmp_path):
    device = StardewDevice(tmp_path)
    tools = {}
    register_device_tools(SimpleNamespace(register=tools.__setitem__), device)
    session = str(uuid4())
    (tmp_path / session).mkdir()
    state = {"session": session, "owner": "studio", "epoch": 1, "can_act": True, "observed": time.time() * 1000,
             "farm": {"save": "task", "location": "Farm", "slot": 0, "inventory": [{"item": "(T)Hoe"}],
                      "facing": 1, "front": {"key": "Farm:3:1", "tilled": False}}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    args = FarmAction(action="till_front")
    action = tools["stardew_action"]
    approved = action.review(args)
    identity = str(uuid4())
    def request(receipts):
        return ToolRequest(identity, "锄地", [], None, approved, receipts)
    assert "error" in await action.run(request(()), args)
    observation = await tools["stardew_status"].run(None, None)
    observed = request(({"tool": "stardew_status", "result": observation},))
    waiting = await action.run(observed, args)
    assert isinstance(waiting, ToolWait)
    assert (await action.stop(identity, waiting.checkpoint))["status"] == "cancelled"
    assert (await action.run(observed, args))["status"] == "cancelled"
    assert not list((tmp_path / session).glob("*.request"))
