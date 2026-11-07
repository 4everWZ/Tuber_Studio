import time

import pytest
from test_browsing import BV
from test_browsing import browsing as browsing


@pytest.mark.parametrize("route", ["notification", "video_comment"])
@pytest.mark.parametrize("draft_account,draft_character,blocks", [
    ("101", "whale", False), ("100", "another-oc", False), ("", "whale", False), ("100", "whale", True),
])
async def test_manual_draft_dedup_is_bound_to_account_and_oc(browsing, route, draft_account, draft_character, blocks):
    await browsing.preflight()
    config = browsing.policy() | {"reply_enabled": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    source = {"comment_id": "502", "thread_id": "501", "platform_user_id": "300", "content": "大肥鱼你怎么看盐水里的鸡蛋？"}
    browsing.comments.import_items(BV, [source])
    comment_id = browsing.store.one("SELECT id FROM comments")["id"]
    browsing.store.execute(
        "INSERT INTO drafts(comment_id,character_id,character_version,text,status,created_at,updated_at,account_id) VALUES(?,?,1,'原人工草稿','pending_review',?,?,?)",
        (comment_id, draft_character, time.time(), time.time(), draft_account),
    )
    if route == "notification":
        browsing.store.execute("INSERT INTO browsing_events VALUES('same-target','100','reply','502',?,'received','{}',NULL,?)", (time.time(), time.time()))
        event = browsing.store.one("SELECT * FROM browsing_events WHERE id='same-target'")
        await browsing._event(event, row, {"lock_wait_seconds": 0})
        if blocks:
            assert browsing.store.one("SELECT status FROM browsing_events WHERE id='same-target'")["status"] == "skipped"
    else:
        key, material = await browsing._prepare_evidence(BV, config)
        await browsing.decisions._reply_to_video_comment(row, material, key, {"11:0"}, source, [source], {"lock_wait_seconds": 0})
    assert len(browsing.intents()) == (0 if blocks else 1)
    assert len(browsing.runtime.llm.calls) == (0 if blocks else 1)
    assert not browsing.adapter.sent


async def test_uncertain_main_reply_keeps_same_video_new_threads_available_without_video_failures(browsing, monkeypatch):
    from test_browsing import finished
    from test_browsing_batches import authorize

    from tuber_studio.platforms.bilibili import PublishUncertain

    await browsing.configure({
        "sources": ["specified"], "watch_videos": [BV], "workers": 1,
        "recent_sample_limit": 0, "max_replies_per_video": 0, "recipient_cooldown_seconds": 0,
    })
    await authorize(browsing)
    actions, checks = [], []
    incoming = []

    async def fetch(video, max_pages=1):
        return {"items": incoming}

    async def publish(target, text, *, before_send):
        before_send()
        actions.append(str(target["parent"]))
        if str(target["parent"]) == "0":
            raise PublishUncertain("提交响应超时")
        return {"code": 0, "rpid": "901"}

    async def reconcile(target, **options):
        checks.append(target)
        return {"status": "not_found"}

    async def read_reply(target):
        return {"author_uid": "300", "text": "盐水浮鸡蛋要准备多少盐？"}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "publish_top_level", publish)
    monkeypatch.setattr(browsing.adapter, "reconcile_publication", reconcile, raising=False)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", read_reply, raising=False)
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed" and first["metrics"]["failures"] == 0
    assert actions == ["0"]
    incoming.append({"comment_id": "601", "thread_id": "601", "platform_user_id": "300",
                     "display_name": "新观众", "content": "盐水浮鸡蛋要准备多少盐？"})
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["status"] == "completed" and second["metrics"]["failures"] == 0
    assert actions == ["0", "601"] and checks
    assert not browsing.repo.video_retry(second, BV)
    assert not any(task["status"] == "failed" for task in browsing.tasks())
    assert {intent["status"] for intent in browsing.intents()} == {"publish_uncertain", "published"}
