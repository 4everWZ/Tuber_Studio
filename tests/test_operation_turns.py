import asyncio

from tuber_studio.operation_turns import OperationTurns


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
