import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from evaluations import owner_memory_video as replay
from tuber_studio.infra.inference.http_api import InferenceBudgetExceeded, check_response_status


def gateway_response(code, reason, delay=None):
    return httpx.Response(code, json={"error": {"code": reason}},
                          headers={} if delay is None else {"Retry-After": str(delay)},
                          request=httpx.Request("POST", "https://gateway.example/v1/chat/completions"))


async def test_organizer_retries_the_saved_turn_without_submitting_another_chat(monkeypatch):
    report = {}
    post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("POST", "http://localhost")))
    replies = iter([gateway_response(429, "gateway_busy", 3), gateway_response(429, "gateway_busy", 7),
                    gateway_response(200, "")])
    async def organize():
        assert report["stage"] == "organizing_owner_chat_turn"
        await check_response_status(next(replies))

    # Stop at the next stage, after it tries to read the exact source from the second device.
    class ReachedRemoteLookup(Exception):
        pass

    remote = AsyncMock(side_effect=ReachedRemoteLookup)
    sleep = AsyncMock()
    monkeypatch.setattr(replay, "device_call", remote)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    runtime = SimpleNamespace(memory_organizer=SimpleNamespace(run_once=organize))
    with pytest.raises(ReachedRemoteLookup):
        await replay.compare(SimpleNamespace(post=post), runtime, 1, "device", "login", {}, report.__setitem__,
                             None, asyncio.get_running_loop().time() + 30)
    post.assert_awaited_once()
    source = "turn:" + post.await_args.kwargs["json"]["request_id"]
    assert report["saved_owner_turn"]["source_key"] == source
    assert [receipt["operation"] for receipt in report["queue_waits"]] == ["organize " + source] * 2
    assert [call.args[0] for call in sleep.await_args_list] == [3, 7]
    remote.assert_awaited_once_with("device", 1, "login", "POST", replay.SEARCH, {"query": source})


@pytest.mark.parametrize("response, error, diagnostic", [
    (gateway_response(429, "gateway_busy", 10), TimeoutError, "deadline_diagnostic"),
    (gateway_response(400, "request_budget_exceeded"), InferenceBudgetExceeded, "budget_diagnostic"),
])
async def test_no_reattempt_when_wait_exceeds_deadline_or_budget_is_exhausted(monkeypatch, response, error, diagnostic):
    report = {}
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    async def submit():
        await check_response_status(response)

    call = AsyncMock(wraps=submit)
    with pytest.raises(error):
        await replay.await_admission("saved operation", call, asyncio.get_running_loop().time() + 1,
                                     [], report.__setitem__)
    call.assert_awaited_once()
    sleep.assert_not_awaited()
    assert report[diagnostic]["operation"] == "saved operation"
