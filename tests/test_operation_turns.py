import asyncio

from tuber_studio.operation_turns import OperationTurns


async def order(lanes, *, skip=()):
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    output = []
    await lock.acquire()

    async def run(name, lane, ready):
        ticket = await turns.acquire(lane, ready)
        if name not in skip:
            turns.started(ticket)
        output.append(name)
        turns.release(ticket)

    tasks = [asyncio.create_task(run(*job)) for job in lanes]
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 3)
    return output


async def test_direct_heads_share_wait_order_and_yield_after_two_requests():
    assert await order([
        ("older public", "direct", 1), ("video", "background", 1),
        ("newer public", "direct", 4), ("dm", "direct", 2),
        ("dynamic", "background", 2), ("chat", "chat", 1),
    ]) == ["chat", "older public", "dm", "video", "newer public", "dynamic"]


async def test_reused_proposal_does_not_consume_a_turn():
    assert await order([
        ("reuse", "direct", 1), ("dm", "direct", 2), ("reply", "direct", 3),
        ("mention", "direct", 4), ("video", "background", 1),
    ], skip={"reuse"}) == ["reuse", "dm", "reply", "video", "mention"]


async def test_priority_video_yields_to_other_background_after_its_step():
    assert await order([
        ("old", "background", 1), ("video P1", "priority", 2), ("video P2", "priority", 2),
    ]) == ["video P1", "old", "video P2"]


async def test_late_direct_head_runs_at_next_resource_release():
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    first = await turns.acquire("background")
    turns.started(first)
    older = asyncio.create_task(turns.acquire("background"))
    await asyncio.sleep(0)
    direct = asyncio.create_task(turns.acquire("direct"))
    await asyncio.sleep(0)
    turns.release(first)
    ticket = await asyncio.wait_for(direct, 2)
    assert not older.done()
    turns.release(ticket)
    turns.release(await asyncio.wait_for(older, 2))


async def test_second_request_reenters_queue_at_safe_point():
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    first = await turns.acquire("background")
    turns.started(first)
    direct = asyncio.create_task(turns.acquire("direct"))
    await asyncio.sleep(0)
    second = asyncio.create_task(turns.next_request(first))
    ticket = await asyncio.wait_for(direct, 2)
    assert not second.done()
    turns.started(ticket)
    turns.release(ticket)
    await asyncio.wait_for(second, 2)
    turns.release(first)
