import asyncio

import pytest


async def test_same_discussion_waits_while_another_discussion_can_prepare(runtime):
    scope = ("100", "whale", "BV1d8pU6xEZX", "500")
    first_started, release_first, other_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def first():
        async with runtime.conversation_turn(scope) as waited:
            assert not waited
            first_started.set()
            await release_first.wait()
            observed.append("first")

    async def second():
        async with runtime.conversation_turn(scope) as waited:
            assert waited
            observed.append("second")

    async def other():
        async with runtime.conversation_turn((*scope[:-1], "600")) as waited:
            assert not waited
            observed.append("other")
            other_finished.set()

    async with asyncio.timeout(2):
        task = asyncio.create_task(first())
        await first_started.wait()
        queued = asyncio.create_task(second())
        independent = asyncio.create_task(other())
        await other_finished.wait()
        assert observed == ["other"]
        release_first.set()
        await asyncio.gather(task, queued, independent)
    assert observed == ["other", "first", "second"]
    assert not runtime._conversation_turns


async def test_cancelled_discussion_waiter_releases_its_reference_and_the_scope_can_be_reused(runtime):
    scope = ("100", "whale", "BV1d8pU6xEZX", "500")

    async def waiting():
        async with runtime.conversation_turn(scope):
            raise AssertionError("Cancelled waiter must not enter the discussion")

    async with runtime.conversation_turn(scope):
        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        assert runtime._conversation_turns[scope]["users"] == 2
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert runtime._conversation_turns[scope]["users"] == 1
    assert not runtime._conversation_turns
    async with runtime.conversation_turn(scope) as waited:
        assert not waited
    assert not runtime._conversation_turns
