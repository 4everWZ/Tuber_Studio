import asyncio
import json

from test_browsing import browsing as browsing

from tuber_studio.operation_turns import operation_lane
from tuber_studio.operations.browsing.models import ResponseDecision


async def test_parallel_notifications_for_the_same_source_refresh_after_waiting_and_generate_once(
    browsing, monkeypatch
):
    await browsing.preflight()
    config = browsing.policy() | {"reply_enabled": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    event = {"id": "same-source", "kind": "reply", "payload": json.dumps({"sender_name": "观众"})}
    entered, release, second_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    read_thread, response = browsing.adapter.read_public_thread, browsing.decisions._response
    reads = calls = 0

    async def read(event):
        nonlocal reads
        reads += 1
        if reads == 2:
            second_read.set()
        return await read_thread(event)

    async def prepare_response(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await response(*args, **kwargs)

    monkeypatch.setattr(browsing.adapter, "read_public_thread", read)
    monkeypatch.setattr(browsing.decisions, "_response", prepare_response)
    metrics = {"lock_wait_seconds": 0}
    async with asyncio.timeout(3):
        first = asyncio.create_task(browsing._event(event, row, metrics))
        await entered.wait()
        second = asyncio.create_task(browsing._event(event, row, metrics))
        await second_read.wait()
        assert calls == 1
        release.set()
        await asyncio.gather(first, second)
    assert calls == 1
    assert reads == 3  # The queued turn reloads its platform thread after the first turn.
    assert len(browsing.intents()) == 1
    assert not browsing.runtime._conversation_turns


async def test_video_comment_and_notification_share_the_same_discussion_turn(browsing, monkeypatch):
    await browsing.preflight()
    config = browsing.policy() | {"reply_enabled": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    key, material = await browsing._prepare_evidence("BV1d8pU6xEZX", config)
    item = {
        "comment_id": "502",
        "thread_id": "501",
        "parent_comment_id": "501",
        "platform_user_id": "300",
        "display_name": "观众",
        "content": "大肥鱼你怎么看盐水里的鸡蛋？",
    }
    event = {"id": "same-source", "kind": "reply", "payload": json.dumps({"sender_name": "观众"})}
    entered, release = asyncio.Event(), asyncio.Event()
    original = browsing.decisions._response
    calls = 0

    async def prepare_response(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(browsing.decisions, "_response", prepare_response)
    metrics = {"lock_wait_seconds": 0}
    async with asyncio.timeout(3):
        notification = asyncio.create_task(browsing._event(event, row, metrics))
        await entered.wait()
        scan = asyncio.create_task(
            browsing.decisions._reply_to_video_comment(row, material, key, {"11:0"}, item, [item], metrics)
        )
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        _, scan_result = await asyncio.gather(notification, scan)
    assert scan_result is None
    assert calls == 1 and len(browsing.intents()) == 1
    assert not browsing.runtime._conversation_turns


async def test_waiting_video_comment_refreshes_its_discussion_without_scrolling_and_uses_reply_share(
    browsing, monkeypatch
):
    await browsing.preflight()
    config = browsing.policy() | {"reply_enabled": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    key, material = await browsing._prepare_evidence("BV1d8pU6xEZX", config)
    item = {"comment_id": "503", "thread_id": "501", "platform_user_id": "300", "content": "旧上下文"}
    scope = ("100", row["character_id"], "BV1d8pU6xEZX", "501")
    reads = []

    async def read_thread(target):
        reads.append(target)
        return {
            "source": {
                "comment_id": "503",
                "author_uid": "300",
                "author_name": "观众",
                "text": "刚更新的评论",
            },
            "items": [item | {"content": "刚更新的评论"}],
        }

    async def response(row, material, context, *args):
        assert operation_lane.get() == "reply"
        assert context["source"]["text"] == "刚更新的评论"
        assert context["timeline"][0]["text"] == "刚更新的评论"
        return ResponseDecision(action="skip", reason="no_interest")

    async def no_scroll(*args):
        raise AssertionError("A discussion refresh must not move the comment browsing cursor")

    monkeypatch.setattr(browsing.adapter, "read_video_thread", read_thread, raising=False)
    monkeypatch.setattr(browsing, "video_comment_context", no_scroll)
    monkeypatch.setattr(browsing.decisions, "_response", response)
    async with asyncio.timeout(3):
        async with browsing.runtime.conversation_turn(scope):
            task = asyncio.create_task(
                browsing.decisions._reply_to_video_comment(row, material, key, {"11:0"}, item, [item], {})
            )
            await asyncio.sleep(0)
            assert browsing.runtime._conversation_turns[scope]["users"] == 2
        assert await task is None
    assert len(reads) == 1 and reads[0]["parent"] == "503"
    assert operation_lane.get() is None
    assert not browsing.runtime._conversation_turns
