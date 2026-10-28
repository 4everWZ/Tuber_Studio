import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.bilibili import PublishUncertain
from tuber_studio.comments import Comments
from tuber_studio.models import Reply
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store


class Platform:
    def __init__(self):
        self.sent = []
        self.failure = None

    async def close(self):
        pass

    async def popular(self):
        return [{"bvid": "BV1d8pU6xEZX"}]

    async def fetch(self, video, max_pages):
        return {"video_id": "BV1d8pU6xEZX", "items": [{"comment_id": "123", "content": "鲸鱼娘怎么画？"}]}

    async def publish(self, video, comment, text):
        self.sent.append((video, comment, text))
        if self.failure:
            raise self.failure
        return {"rpid": 321}


@pytest.fixture
async def comments(settings, store, runtime, monkeypatch):
    async def generate(scope, character, event, relationship=None, mode="live"):
        messages = await runtime.context.prepare(scope, character, event, relationship, mode)
        reply = await runtime.llm.generate(messages, character, event["event_id"], event["text"])
        return reply, {"sources": [], "missing": [], "retried": False, "memory_kind": "role_story"}

    # Comments owns scheduling, not the runtime's fact checking/retry algorithm.
    monkeypatch.setattr(runtime, "generate_reply", generate, raising=False)
    service = Comments(settings, store, runtime, Platform())
    # Isolated worker only: the browsing loop must never make a real network call.
    service.worker = asyncio.create_task(service._worker())
    yield service
    await service.close()


async def draft(comments, store, automatic=False):
    comments.import_items("BV1d8pU6xEZX", [{"comment_id": "123", "content": "鱼鱼今天吃什么？"}])
    cid = store.one("SELECT id FROM comments")["id"]
    comments.enqueue([cid], automatic=automatic)
    await asyncio.wait_for(comments.queue.join(), 3)
    return store.one("SELECT * FROM drafts")


async def test_default_review_and_manual_approval_never_sends(comments, store):
    row = await draft(comments, store)
    assert not comments.policy()["auto_reply"]
    assert row["status"] == "pending_review"
    with pytest.raises(ValueError):
        comments.copy(row["id"])
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    comments.review(row["id"], "approve", "修改后的候选回复")
    assert comments.copy(row["id"])["text"] == "修改后的候选回复"
    assert comments.adapter.sent == []
    assert store.one("SELECT status FROM drafts")["status"] == "copied"


async def test_auto_reply_requires_credentials_and_explicit_enable(comments, store):
    with pytest.raises(ValueError, match="需要先配置"):
        await comments.set_policy({"auto_reply": True})
    assert not comments.policy()["auto_reply"]
    comments.credentials = {"sessdata": "test-session", "csrf": "test-csrf"}
    store.set("comment_policy", {"auto_reply": True})
    row = await draft(comments, store, automatic=True)
    assert row["status"] == "published"
    assert len(comments.adapter.sent) == 1
    with pytest.raises(ValueError):
        await comments.publish(row["id"], automatic=True)
    assert len(comments.adapter.sent) == 1
    assert "test-session" not in str(comments.policy())


@pytest.mark.parametrize("failure", [PublishUncertain("uncertain"), RuntimeError("unexpected")])
async def test_uncertain_send_cannot_be_retried(comments, store, failure):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    comments.adapter.failure = failure
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert len(comments.adapter.sent) == 1


async def test_definite_platform_rejection_preserves_reviewed_draft(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    comments.adapter.failure = ValueError("平台明确拒绝")
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert store.one("SELECT status FROM drafts")["status"] == "approved"
    assert len(comments.adapter.sent) == 1


async def test_stop_auto_reply_retains_later_drafts(comments, store):
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    await comments.set_policy({"auto_reply": False})
    row = await draft(comments, store, automatic=True)
    assert row["status"] == "pending_review"
    assert comments.adapter.sent == []


def test_import_1000_comments_is_idempotent_and_keeps_same_name_users_separate(comments, store):
    items = [
        {
            "comment_id": str(i),
            "content": f"第{i}个问题怎么处理？",
            "display_name": "同名",
            "platform_user_id": str(i),
        }
        for i in range(1000)
    ]
    assert comments.import_items("import-fixture", items)["imported"] == 1000
    assert comments.import_items("import-fixture", items)["duplicates"] == 1000
    assert comments.analyze("import-fixture")["question"] == 1000
    rows = comments.list_comments("import-fixture", limit=1000)
    assert len({r["platform_user_id"] for r in rows}) == 1000


async def test_manual_batches_stay_reviewable_when_auto_mode_is_enabled(comments, store):
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    row = await draft(comments, store, automatic=False)
    assert row["status"] == "pending_review"
    assert comments.adapter.sent == []


async def test_platform_success_followed_by_database_failure_never_allows_resend(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.execute("""CREATE TRIGGER refuse_receipt BEFORE UPDATE OF status ON drafts
        WHEN NEW.status='published' BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    with pytest.raises(ValueError, match="禁止重发"):
        await comments.publish(row["id"])
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert len(comments.adapter.sent) == 1


async def test_cancellation_after_send_starts_is_uncertain(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    started = asyncio.Event()

    async def blocked_send(*args):
        started.set()
        await asyncio.sleep(60)

    comments.adapter.publish = blocked_send
    sending = asyncio.create_task(comments.publish(row["id"]))
    await started.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"


async def test_hourly_limit_is_enforced_before_second_http_write(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"max_replies_per_hour": 1})
    await comments.publish(row["id"])
    comments.import_items("BV1d8pU6xEZX", [{"comment_id": "456", "content": "下一条问题怎么处理？"}])
    second = store.one("SELECT id FROM comments WHERE comment_id='456'")["id"]
    comments.enqueue([second])
    await asyncio.wait_for(comments.queue.join(), 3)
    second_draft = store.one("SELECT * FROM drafts WHERE comment_id=?", (second,))
    comments.review(second_draft["id"], "approve")
    with pytest.raises(ValueError, match="频率上限"):
        await comments.publish(second_draft["id"])
    assert len(comments.adapter.sent) == 1


def test_csv_mapping_preview_does_not_write_and_operator_category_survives_analyze(comments, store):
    raw = '编号,用户,名字,正文\n001,17,同名,"鱼鱼，怎么画尾巴？"\n002,,同名,今天的白饭真好吃\n'
    mapping = {"comment_id": "编号", "platform_user_id": "用户", "display_name": "名字", "content": "正文"}
    preview = comments.prepare_import("import-mapping", csv_text=raw, field_mapping=mapping)
    assert store.all("SELECT * FROM comments") == []
    assert preview[0]["comment_id"] == "001"
    assert preview[1]["platform_user_id"] is None
    assert comments.import_items("import-mapping", csv_text=raw, field_mapping=mapping)["imported"] == 2
    row = store.one("SELECT id FROM comments WHERE comment_id='001'")
    comments.set_category(row["id"], "interaction")
    counts = comments.analyze("import-mapping")
    assert counts["interaction"] == 1
    assert (
        store.one("SELECT category_source FROM comments WHERE id=?", (row["id"],))["category_source"]
        == "operator"
    )


def test_json_null_identity_is_anonymous_and_invalid_mapping_is_atomic(comments, store):
    items = [{"comment_id": "one", "content": "你好呀", "platform_user_id": None}]
    prepared = comments.prepare_import("import-null", items)
    assert prepared[0]["platform_user_id"] is None
    with pytest.raises(ValueError):
        comments.import_items("import-null", items, field_mapping={"content": "missing_column"})
    assert store.all("SELECT * FROM comments") == []


def test_thread_mapping_preview_filter_and_legacy_defaults(comments, store):
    raw = "评论,楼,父级,正文\n100,,,鱼鱼你好\n101,100,100,下期讲什么？\n102,100,101,我也想听\n"
    mapping = {"comment_id": "评论", "thread_id": "楼", "parent_comment_id": "父级", "content": "正文"}
    preview = comments.prepare_import("import-thread", csv_text=raw, field_mapping=mapping)
    assert [item["thread_id"] for item in preview] == ["100", "100", "100"]
    assert [item["parent_comment_id"] for item in preview] == [None, "100", "101"]
    assert store.all("SELECT * FROM comments") == []
    comments.import_items("import-thread", csv_text=raw, field_mapping=mapping)
    assert len(comments.list_comments("import-thread", thread_id="100")) == 3
    assert comments.list_comments("import-thread", thread_id="another") == []
    assert comments.import_items("import-thread", csv_text=raw, field_mapping=mapping)["duplicates"] == 3


def test_thread_scope_is_unambiguous_and_platform_specific():
    scope = Comments.context_scope("video:a", "b:c")
    assert json.loads(scope.removeprefix("comments:")) == ["bilibili", "video:a", "b:c"]
    assert scope != Comments.context_scope("video", "a:b:c")
    assert scope != Comments.context_scope("video:a", "b:c", "mock")
    assert scope != "comments:video:a"
    with pytest.raises(ValueError):
        Comments.context_scope("video", None)


@pytest.mark.parametrize("value", [True, False, [], {}, 0.5, "x" * 101])
def test_invalid_thread_metadata_rejects_import_atomically(comments, store, value):
    with pytest.raises(ValueError):
        comments.import_items(
            "import-invalid-thread",
            [
                {"comment_id": "one", "content": "先来一碗白饭"},
                {"comment_id": "two", "content": "然后聊什么呢？", "thread_id": value},
            ],
        )
    assert not store.all("SELECT * FROM comments")


def test_legacy_schema_backfills_threads_without_changing_reviewed_drafts(tmp_path, store):
    path = tmp_path / "legacy.sqlite3"
    legacy = Store(path, store.characters())
    legacy.execute(
        "INSERT INTO comments(video_id,comment_id,content,category,category_source) "
        "VALUES('import-legacy','kept','旧评论','suggestion','operator')"
    )
    cid = legacy.one("SELECT id FROM comments")["id"]
    legacy.execute("INSERT INTO drafts(comment_id,text,status) VALUES(?,'已审核旧稿','approved')", (cid,))
    legacy.db.execute("DROP INDEX comments_thread")
    for table, column in (
        ("comments", "thread_id"),
        ("comments", "parent_comment_id"),
        ("jobs", "answer_check"),
        ("drafts", "answer_check"),
    ):
        legacy.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    legacy.db.commit()
    legacy.close()
    restored = Store(path, store.characters())
    try:
        comment = restored.one("SELECT * FROM comments")
        assert comment["thread_id"] == "kept"
        assert comment["parent_comment_id"] is None
        assert comment["category_source"] == "operator"
        draft = restored.one("SELECT * FROM drafts")
        assert (draft["text"], draft["status"], draft["answer_check"]) == ("已审核旧稿", "approved", None)
        assert "answer_check" in {row[1] for row in restored.db.execute("PRAGMA table_info(jobs)")}
    finally:
        restored.close()


async def test_thread_contexts_never_inherit_whole_video_summary(comments, runtime, store):
    character = runtime.character
    legacy_scope = "comments:import-isolated"
    runtime.context.record(
        legacy_scope, character, {"event_id": "legacy", "text": "旧整视频秘密"}, "旧摘要内容"
    )
    comments.import_items(
        "import-isolated",
        [
            {"comment_id": "a1", "thread_id": "a", "content": "第一个楼的问题是什么？"},
            {"comment_id": "b1", "thread_id": "b", "content": "第二个楼也想问你"},
            {
                "comment_id": "a2",
                "thread_id": "a",
                "parent_comment_id": "a1",
                "content": "第一个楼补充一个问题",
            },
        ],
    )
    ids = [row["id"] for row in store.all("SELECT id FROM comments ORDER BY id")]
    comments.enqueue(ids)
    await asyncio.wait_for(comments.queue.join(), 3)
    a = json.loads(
        runtime.context.window(comments.context_scope("import-isolated", "a"), character)["history"]
    )
    b = json.loads(
        runtime.context.window(comments.context_scope("import-isolated", "b"), character)["history"]
    )
    assert [
        json.loads(item["content"].split("：", 1)[-1])["platform_comment_id"]
        for item in a
        if item["kind"] == "viewer"
    ] == ["a1", "a2"]
    assert "第一个楼" not in json.dumps(b, ensure_ascii=False)
    assert "旧整视频秘密" not in json.dumps(a + b, ensure_ascii=False)
    assert "旧整视频秘密" in runtime.context.window(legacy_scope, character)["history"]


async def test_four_thread_workflows_preserve_order_but_model_generation_is_serial(
    comments, runtime, store, monkeypatch
):
    items = [
        {
            "comment_id": f"{floor}-{turn}",
            "thread_id": f"floor-{floor}",
            "content": f"第{floor}楼第{turn}个问题怎么回答？",
        }
        for turn in range(2)
        for floor in range(6)
    ]
    comments.import_items("import-parallel", items)
    ids = [row["id"] for row in store.all("SELECT id FROM comments ORDER BY id")]
    first_four = asyncio.Event()
    release = asyncio.Event()
    workflow_active = workflow_peak = gpu_active = gpu_peak = 0
    calls = []
    original_thread = comments._process_thread

    async def thread(*args):
        nonlocal workflow_active, workflow_peak
        workflow_active += 1
        workflow_peak = max(workflow_peak, workflow_active)
        if workflow_active == 4:
            first_four.set()
        try:
            await release.wait()
            await original_thread(*args)
        finally:
            workflow_active -= 1

    async def generate(scope, character, event, **kwargs):
        nonlocal gpu_active, gpu_peak
        assert runtime.generation_lock.locked()
        gpu_active += 1
        gpu_peak = max(gpu_peak, gpu_active)
        try:
            await asyncio.sleep(0)
            calls.append((scope, event["platform_comment_id"]))
            return Reply(spoken_text="每个楼都有自己的对话，我来认真回答。"), {"memory_kind": "role_story"}
        finally:
            gpu_active -= 1

    monkeypatch.setattr(comments, "_process_thread", thread)
    monkeypatch.setattr(runtime, "generate_reply", generate)
    comments.enqueue(ids)
    await asyncio.wait_for(first_four.wait(), 2)
    assert len(comments.thread_tasks) == 4
    release.set()
    await asyncio.wait_for(comments.queue.join(), 3)
    assert workflow_peak == 4 and gpu_peak == 1
    for floor in range(6):
        scope = comments.context_scope("import-parallel", f"floor-{floor}")
        assert [cid for key, cid in calls if key == scope] == [f"{floor}-0", f"{floor}-1"]
    assert store.one("SELECT done,errors FROM comment_tasks") == {"done": 12, "errors": 0}


async def test_shutdown_collects_thread_children_and_cancels_queued_batches(
    comments, runtime, store, monkeypatch
):
    started = asyncio.Event()

    async def blocked_generate(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "generate_reply", blocked_generate)
    comments.import_items(
        "import-cancel",
        [
            {"comment_id": str(i), "thread_id": str(i), "content": f"第{i}楼的问题怎么回答？"}
            for i in range(6)
        ],
    )
    ids = [row["id"] for row in store.all("SELECT id FROM comments")]
    comments.enqueue(ids)
    comments.enqueue(ids)
    await asyncio.wait_for(started.wait(), 2)
    children = list(comments.thread_tasks)
    assert len(children) == 4
    await comments.close()
    await asyncio.wait_for(comments.queue.join(), 1)
    assert all(child.done() for child in children)
    assert not comments.thread_tasks and not runtime.generation_lock.locked()
    assert {row["status"] for row in store.all("SELECT status FROM comment_tasks")} == {"cancelled"}
    assert not store.all("SELECT * FROM drafts") and not comments.adapter.sent


async def test_checks_are_stored_and_threaded_duplicate_batches_never_republish(
    comments, runtime, store, monkeypatch
):
    check = {
        "sources": [{"event_id": "known-source", "text": "原句"}],
        "missing": ["quantity"],
        "retried": True,
        "warning": "数量未完全覆盖",
        "memory_kind": "derived_reply",
    }

    async def generate(*args, **kwargs):
        return Reply(spoken_text="我会根据原始记录回答，并保留数量缺项。"), check

    monkeypatch.setattr(runtime, "generate_reply", generate)
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    comments.import_items("BV1d8pU6xEZX", [{"comment_id": "900", "content": "鱼鱼你收到了多少礼物？"}])
    cid = store.one("SELECT id FROM comments")["id"]
    comments.enqueue([cid, cid], automatic=True)
    comments.enqueue([cid], automatic=True)
    await asyncio.wait_for(comments.queue.join(), 3)
    draft = store.one("SELECT * FROM drafts")
    assert json.loads(draft["answer_check"]) == check
    assert comments.list_comments()[0]["draft"]["answer_check"] == check
    scope = comments.context_scope("BV1d8pU6xEZX", "900")
    history = json.loads(runtime.context.window(scope, runtime.character)["history"])
    assert history[-1]["memory_kind"] == "derived_reply"
    assert draft["status"] == "published"
    assert len(comments.adapter.sent) == 1
    assert store.one("SELECT COUNT(*) AS n FROM publish_attempts")["n"] == 1


async def test_real_runtime_checker_integrates_without_borrowing_another_threads_facts(
    comments, runtime, store, monkeypatch
):
    # Exercise the actual Runtime checker and actual context/source-memory stores;
    # only its normal offline DemoProvider substitutes for a GPU model.
    monkeypatch.setattr(runtime, "generate_reply", Runtime.generate_reply.__get__(runtime, Runtime))
    scope = comments.context_scope("import-grounded", "story-floor")
    story = "我去了珊瑚铺，店主小壳送我三份海苔饭团。"
    runtime.context.record(
        scope, runtime.character, {"event_id": "original-story", "text": "刚才发生什么？"}, story
    )
    question = "你刚才去了哪里，谁送了你什么，有多少份？"
    comments.import_items(
        "import-grounded",
        [
            {"comment_id": "follow-up", "thread_id": "story-floor", "content": question},
            {"comment_id": "unrelated", "thread_id": "other-floor", "content": question},
        ],
    )
    comments.enqueue([row["id"] for row in store.all("SELECT id FROM comments ORDER BY id")])
    await asyncio.wait_for(comments.queue.join(), 3)
    rows = {row["comment_id"]: row for row in comments.list_comments("import-grounded")}
    grounded = rows["follow-up"]["draft"]
    audit = grounded["answer_check"]
    assert audit["retried"] is True and audit["missing"] and audit["warning"]
    assert any(
        source["text"] == story and source["event_id"] == "original-story" and source["fictional"]
        for source in audit["sources"]
    )
    assert rows["unrelated"]["draft"]["answer_check"]["sources"] == []
    assert (
        grounded["status"] == "pending_review"
    )  # A warning is not a fabricated success or a blanket refusal.
    assert store.one("SELECT status,errors FROM comment_tasks") == {"status": "completed", "errors": 0}
    assert comments.adapter.sent == []


async def test_auto_reply_targets_nested_comment_in_its_original_thread(comments, store):
    sent = []

    async def publish(video, comment, text, *, thread_id):
        sent.append((video, thread_id, comment))
        return {"rpid": "new-reply", "root": thread_id, "parent": comment}

    comments.adapter.publish = publish
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    comments.import_items(
        "BV1d8pU6xEZX",
        [
            {
                "comment_id": "120",
                "thread_id": "100",
                "parent_comment_id": "119",
                "content": "鱼鱼这层也有问题",
            }
        ],
    )
    comments.enqueue([store.one("SELECT id FROM comments")["id"]], automatic=True)
    await asyncio.wait_for(comments.queue.join(), 3)
    assert sent == [("BV1d8pU6xEZX", "100", "120")]
    assert store.one("SELECT status FROM drafts")["status"] == "published"


async def test_cancelling_thread_batch_collects_inflight_publish_and_never_sends_waiters(comments, store):
    started = asyncio.Event()
    sends = []

    async def blocked(*args, **kwargs):
        sends.append(args)
        started.set()
        await asyncio.Event().wait()

    comments.adapter.publish = blocked
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    comments.import_items(
        "BV1d8pU6xEZX",
        [{"comment_id": str(i), "thread_id": str(i), "content": f"鱼鱼第{i}楼的问题"} for i in (301, 302)],
    )
    comments.enqueue([row["id"] for row in store.all("SELECT id FROM comments")], automatic=True)
    await asyncio.wait_for(started.wait(), 2)
    await comments.close()
    assert len(sends) == 1 and not comments.thread_tasks
    assert store.one("SELECT status FROM publish_attempts")["status"] == "uncertain"
    statuses = [row["status"] for row in store.all("SELECT status FROM drafts")]
    assert statuses.count("publish_uncertain") == 1
    assert set(statuses) <= {"publish_uncertain", "pending_review"}


def test_job_answer_check_accepts_runtime_audit_as_json(store):
    store.execute("INSERT INTO jobs(id,status,created_at,updated_at) VALUES('checked','queued',0,0)")
    audit = {"sources": [], "missing": [], "retried": False}
    store.update_job("checked", answer_check=audit)
    assert json.loads(store.job("checked")["answer_check"]) == audit


def test_thread_http_filter_and_compaction_address_exact_scope_without_creating_empty_window(settings):
    app = create_app(settings, Platform())
    with TestClient(app) as client:
        client.post("/api/auth", json={"token": settings.admin_token})
        payload = {
            "video_id": "import-api-thread",
            "items": [
                {"comment_id": "a", "thread_id": "root-a", "content": "第一楼的问题怎么处理？"},
                {"comment_id": "b", "thread_id": "root-b", "content": "第二楼的问题怎么处理？"},
            ],
        }
        preview = client.post("/api/comments/import/preview", json=payload)
        assert preview.status_code == 200
        assert preview.json()["items"][0]["thread_id"] == "root-a"
        assert client.post("/api/comments/import", json=payload).json()["imported"] == 2
        rows = client.get("/api/comments", params={"video_id": payload["video_id"], "thread_id": "root-b"})
        assert [item["comment_id"] for item in rows.json()] == ["b"]
        before = app.state.store.one("SELECT COUNT(*) AS n FROM context_windows")["n"]
        response = client.get(
            "/api/context/status", params={"video_id": payload["video_id"], "thread_id": "root-a"}
        )
        assert response.status_code == 200 and response.json()["active_tokens"] == 0
        assert app.state.store.one("SELECT COUNT(*) AS n FROM context_windows")["n"] == before
        captured = []

        async def compact(scope, character, mode="live"):
            captured.append((scope, mode))
            return app.state.runtime.context.status()

        app.state.runtime.context.compact = compact
        assert (
            client.post(
                "/api/context/compact", json={"scope": "comments", "video_id": payload["video_id"]}
            ).status_code
            == 400
        )
        response = client.post(
            "/api/context/compact",
            json={"scope": "comments", "video_id": payload["video_id"], "thread_id": "root-a"},
        )
        assert response.status_code == 200
        assert captured == [(Comments.context_scope(payload["video_id"], "root-a"), "comment_draft")]
