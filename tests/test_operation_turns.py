import asyncio

from tuber_studio.operation_turns import OperationTurns


async def test_chat_interleaves_without_advancing_the_operations_rotation():
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    order = []

    async def request(lane):
        await turns.acquire(lane)
        try:
            order.append(lane)
        finally:
            lock.release()

    async with asyncio.timeout(2):
        async with lock:
            tasks = [asyncio.create_task(request(lane)) for lane in
                     ("video", "dm", "dm", "reply", "reply", "chat", "chat", "chat")]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
    assert order == ["chat", "reply", "chat", "dm", "chat", "video", "reply", "dm"]


async def test_contended_model_turns_rotate_two_replies_two_dms_one_video():
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    processed = []

    async def request(lane, number):
        await turns.acquire(lane)
        try:
            processed.append((lane, number))
            await asyncio.sleep(0)
        finally:
            lock.release()

    async with asyncio.timeout(2):
        async with lock:
            tasks = [
                asyncio.create_task(request(lane, i))
                for lane, count in (
                    ("video", 2),
                    ("dm", 4),
                    ("reply", 4),
                )
                for i in range(count)
            ]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
    assert processed == [
        ("reply", 0),
        ("dm", 0),
        ("video", 0),
        ("reply", 1),
        ("dm", 1),
        ("reply", 2),
        ("dm", 2),
        ("video", 1),
        ("reply", 3),
        ("dm", 3),
    ]


async def test_empty_dm_share_is_borrowed_by_replies_and_video_without_waiting():
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    processed = []

    async def request(lane, number):
        await turns.acquire(lane)
        try:
            processed.append((lane, number))
            await asyncio.sleep(0)
        finally:
            lock.release()

    async with asyncio.timeout(2):
        async with lock:
            tasks = [
                asyncio.create_task(request(lane, number))
                for lane, count in (("video", 2), ("reply", 4))
                for number in range(count)
            ]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
    assert processed == [
        ("reply", 0),
        ("video", 0),
        ("reply", 1),
        ("reply", 2),
        ("video", 1),
        ("reply", 3),
    ]


async def test_new_dm_gets_a_turn_before_an_existing_video_backlog_is_drained():
    lock = asyncio.Lock()
    turns = OperationTurns(lock, lambda: False)
    first_started, continue_first = asyncio.Event(), asyncio.Event()
    processed = []

    async def request(lane, number):
        await turns.acquire(lane)
        try:
            processed.append((lane, number))
            if lane == "video" and number == 0:
                first_started.set()
                await continue_first.wait()
            await asyncio.sleep(0)
        finally:
            lock.release()

    async with asyncio.timeout(2):
        tasks = [asyncio.create_task(request("video", number)) for number in range(5)]
        await first_started.wait()
        tasks.append(asyncio.create_task(request("dm", 0)))
        await asyncio.sleep(0)
        continue_first.set()
        await asyncio.gather(*tasks)
    assert processed[:2] == [("video", 0), ("dm", 0)]
    assert [number for lane, number in processed if lane == "video"] == list(range(5))
