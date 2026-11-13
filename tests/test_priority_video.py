"""An operator's priority BV uses the existing media and discussion state."""

import asyncio
from uuid import uuid4

from test_browsing import BV
from test_browsing import browsing as browsing
from test_browsing_evidence import PNG
from test_browsing_visual_followup import install_visual

from tuber_studio.generation_meter import BrowsingBudgetDeferred
from tuber_studio.operations.browsing.comment_reader import comment_cursor_key


async def wait_for(check):
    async with asyncio.timeout(5):
        while not (value := check()):
            await asyncio.sleep(0.01)
        return value


def comment(identity, *, parent=None, replies=0):
    return {
        "comment_id": str(identity), "thread_id": str(parent or identity),
        "parent_comment_id": str(parent) if parent else None, "reply_count": replies,
        "platform_user_id": "300", "display_name": "观众", "content": f"原评论 {identity}",
    }


async def test_priority_video_resumes_shared_cursor_and_waits_for_last_thread_page(browsing):
    await browsing._current_account()
    browsing.discovery.remember_account("100")
    # Complete captions are the first evidence, and leave a choice available.
    _, inputs = install_visual(browsing, ask=False)
    calls, frame_windows = [], []
    second_page = asyncio.Event()

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        calls.append(("newest" if newest else "root", start_page))
        if newest:
            return {"items": [comment(601)]}
        return {"video_id": BV, "aid": 42, "next_page": None,
                "items": [comment(602, replies=2)]}

    async def thread(target, *, page):
        calls.append(("thread", page))
        if page == 2:
            await second_page.wait()
        return {"items": [comment(602 + page, parent=602)], "next_page": page + 1 if page == 1 else None}

    async def frames(metadata, count, *, cid, windows):
        frame_windows.extend(windows)
        return [{"id": f"{cid}:{start}", "cid": cid, "actual_time": (start + end) / 2,
                 "width": 1, "height": 1, "image": PNG, "kind": "storyboard"} for start, end in windows]

    browsing.adapter.fetch, browsing.adapter.video_thread_page, browsing.adapter.video_frames = fetch, thread, frames
    identity = str(uuid4())
    cursor = comment_cursor_key({"account_id": "100", "character_id": browsing.runtime.character["id"]}, BV)
    browsing.store.set(cursor, 3)
    selection = {"bvid": BV, "ranges": []}
    await browsing.queue_full_video(identity, browsing.runtime.character, selection)
    assert await browsing._start_full_video()
    await wait_for(lambda: ("thread", 2) in calls)
    reading = browsing.full_video_status(browsing.repo.round(identity))
    assert reading["phase"] == "comments" and not reading["comments"]["at_end"]
    assert reading["coverage"]["viewing"] == "watched"
    assert browsing.store.get(cursor) == 3
    assert (await browsing.update_full_video(identity, resume=False))["status"] == "cancelled"
    second_page.set()
    assert (await browsing.update_full_video(identity, resume=True))["status"] == "waiting"
    assert await browsing._start_full_video()
    await wait_for(lambda: identity not in browsing.active)
    result = browsing.full_video_status(browsing.repo.round(identity))
    assert result["status"] == "completed" and result["phase"] == "complete", result["error"]
    assert result["comments"]["pages_read"] == 1 and result["comments"]["thread_pages_read"] == 2
    assert result["comments"]["at_end"] and browsing.store.get(cursor) == 1
    assert [link["comment_id"] for link in result["comments"]["links"]] == ["601", "602", "603", "604"]
    assert calls == [("newest", 1), ("root", 3), ("thread", 1), ("thread", 2), ("newest", 1), ("thread", 2)]
    assert frame_windows == [(0, 15), (15, 30)]
    assert len(inputs) == 1 and not inputs[0]["has_visual"]
    assert browsing.adapter.calls.count(("subtitles", BV, "11")) == 1
    assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1
    assert len(browsing.store.all("SELECT * FROM browsing_comment_timeline WHERE resource=?", (BV,))) == 4
    assert not browsing.store.all("SELECT * FROM browsing_events")
    assert not browsing.adapter.sent


async def test_priority_budget_yields_and_resumes_same_windows(browsing):
    await browsing._current_account()
    browsing.discovery.remember_account("100")
    _, inputs = install_visual(browsing, ask=False)
    browsing.evidence.visual.capabilities = lambda: {"images": True, "provider_id": "followup-test",
        "version": "1", "max_frames": 1, "max_pixels": 100, "max_bytes": 4096, "context_tokens": 4096}
    attempts = []

    async def frames(metadata, count, *, cid, windows):
        (start, end), = windows
        attempts.append(start)
        if len(attempts) == 2:
            raise BrowsingBudgetDeferred("测试时段预算耗尽")
        return [{"id": f"{cid}:{start}", "cid": cid, "actual_time": (start + end) / 2,
                 "width": 1, "height": 1, "image": PNG, "kind": "storyboard"}]

    browsing.adapter.video_frames = frames
    identity = str(uuid4())
    await browsing.queue_full_video(identity, browsing.runtime.character, {"bvid": BV, "ranges": []})
    assert await browsing._start_full_video()
    await wait_for(lambda: identity not in browsing.active)
    checkpoint = browsing.full_video_status(browsing.repo.round(identity))
    assert checkpoint["status"] == "waiting" and checkpoint["yielded"]
    assert checkpoint["coverage"]["parts"][0]["ranges"][0]["visual"]["samples"] == [7.5]
    assert await browsing._start_full_video()
    await wait_for(lambda: identity not in browsing.active)
    finished = browsing.full_video_status(browsing.repo.round(identity))
    assert finished["status"] == "completed" and finished["coverage"]["viewing"] == "watched", finished["error"]
    assert browsing.repo.content_progress(browsing.repo.round(identity), BV)["fully_read"]
    assert attempts == [0, 15, 15] and len(inputs) == 1
    assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1


async def test_priority_visual_retry_reason_survives_new_budget_slice(browsing):
    await browsing._current_account()
    browsing.discovery.remember_account("100")
    install_visual(browsing, ask=False)
    browsing.evidence.visual.capabilities = lambda: {"images": True, "provider_id": "followup-test",
        "version": "1", "max_frames": 1, "max_pixels": 100, "max_bytes": 4096, "context_tokens": 4096}
    attempts = []

    async def frames(metadata, count, *, cid, windows):
        (start, end), = windows
        attempts.append(start)
        if len(attempts) == 1:
            raise ValueError("视觉服务没有返回有效帧引用")
        if len(attempts) == 2:
            raise BrowsingBudgetDeferred("测试时段预算耗尽")
        return [{"id": f"{cid}:{start}", "cid": cid, "actual_time": (start + end) / 2,
                 "width": 1, "height": 1, "image": PNG, "kind": "storyboard"}]

    browsing.adapter.video_frames = frames
    identity = str(uuid4())
    await browsing.queue_full_video(identity, browsing.runtime.character, {"bvid": BV, "ranges": []})
    assert await browsing._start_full_video()
    await wait_for(lambda: identity not in browsing.active)
    assert browsing.full_video_status(browsing.repo.round(identity))["yielded"]
    assert await browsing._start_full_video()
    await wait_for(lambda: identity not in browsing.active)
    finished = browsing.full_video_status(browsing.repo.round(identity))
    windows = finished["coverage"]["parts"][0]["ranges"][0]["visual"]["windows"]
    assert finished["status"] == "partial" and attempts == [0, 15, 15]
    assert windows[0]["state"] == "retry" and windows[0]["reason"] == "视觉服务没有返回有效帧引用"
    assert windows[1]["state"] == "sampled" and "reason" not in windows[1]
