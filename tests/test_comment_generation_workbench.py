"""Imported sources and their current generation results share one workbench."""

import asyncio
import json
import time

from fastapi.testclient import TestClient
from test_comments import Platform, draft
from test_comments import comments as comments

from tuber_studio.app import create_app
from tuber_studio.comment_workbench import list_queue


async def test_current_generation_filters_out_old_selected_drafts_and_prioritizes_its_results(
    comments, store
):
    old = await draft(comments, store)
    store.execute("UPDATE drafts SET updated_at=? WHERE id=?", (time.time() + 1000, old["id"]))
    comments.import_items(
        "import-current",
        [
            {"comment_id": "first", "content": "第一条鱼鱼今天想聊什么？"},
            {"comment_id": "second", "content": "第二条鱼鱼喜欢哪种白饭？"},
            {"comment_id": "untouched", "content": "这条暂时不生成。"},
        ],
    )
    new = store.all(
        "SELECT id FROM comments WHERE video_id='import-current' AND comment_id<>'untouched' ORDER BY id"
    )
    selected = [old["comment_id"], *(row["id"] for row in new)]
    task = comments.enqueue(selected)
    pending = list_queue(store, "review", 30, 0, task_id=task["task_id"], comment_ids=selected)
    assert pending["items"] == []
    assert pending["task"]["status"] == "pending"
    assert pending["task"]["comment_ids"] == selected
    assert pending["task"]["existing_drafts"][0]["id"] == str(old["id"])
    await asyncio.wait_for(comments.queue.join(), 3)
    completed = list_queue(store, "review", 30, 0, task_id=task["task_id"], comment_ids=selected)
    assert completed["task"]["status"] == "completed"
    assert completed["task"]["done"] == 3
    assert {row["comment_id"] for row in completed["items"]} == {row["id"] for row in new}
    assert all(row["generation_task_id"] == task["task_id"] for row in completed["items"])
    assert [row["updated_at"] for row in completed["items"]] == sorted(
        [row["updated_at"] for row in completed["items"]], reverse=True
    )
    restored_page = list_queue(store, "review", 1, 999, task_id=task["task_id"], comment_ids=selected)
    assert restored_page["offset"] == 0 and restored_page["total"] == 2
    assert restored_page["items"][0]["id"] != str(old["id"])
    assert comments.list_comments("import-current")[0]["draft"] is not None
    assert comments.list_comments("import-current")[-1]["comment_id"] == "untouched"
    assert store.one("SELECT text FROM drafts WHERE id=?", (old["id"],))["text"] == old["text"]
    assert not comments.adapter.sent


async def test_processing_tasks_are_scoped_to_selected_video_and_comment_ids(comments, store):
    comments.import_items("import-video-current", [{"comment_id": "a", "content": "鱼鱼来聊第一楼？"}])
    comments.import_items("import-video-unrelated", [{"comment_id": "b", "content": "鱼鱼来聊第二楼？"}])
    ids = {row["video_id"]: row["id"] for row in store.all("SELECT id,video_id FROM comments")}
    first = comments.enqueue([ids["import-video-current"]])
    comments.enqueue([ids["import-video-unrelated"]])
    rows = list_queue(
        store, "processing", 30, 0, video_id="import-video-current", comment_ids=[ids["import-video-current"]]
    )
    assert rows["total"] == 1
    assert rows["items"][0]["id"] == first["task_id"]
    assert rows["items"][0]["status"] == "pending"
    await asyncio.wait_for(comments.queue.join(), 3)


async def test_existing_draft_is_identified_without_claiming_it_was_regenerated(comments, store):
    old = await draft(comments, store)
    task = comments.enqueue([old["comment_id"]])
    await asyncio.wait_for(comments.queue.join(), 3)
    result = list_queue(store, "review", 30, 0, task_id=task["task_id"])
    assert result["items"] == []
    assert result["task"]["status"] == "completed"
    existing = result["task"]["existing_drafts"]
    assert len(existing) == 1
    assert existing[0]["id"] == str(old["id"])
    assert existing[0]["channel"] == "comments"
    assert existing[0]["video_id"] == "BV1d8pU6xEZX"
    assert existing[0]["excerpt"]


def test_comment_workbench_http_accepts_exact_generation_scope_and_fetch_video_alias(settings):
    app = create_app(settings, Platform())
    with TestClient(app) as client:
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        response = client.post("/api/comments/fetch", json={"video_id": "BV1d8pU6xEZX", "max_pages": 1})
        assert response.status_code == 200, response.text
        fetched = response.json()
        assert fetched["video_id"] == "BV1d8pU6xEZX"
        selected = client.get("/api/comments", params={"video_id": fetched["video_id"]}).json()[0]["id"]
        now = time.time()
        app.state.store.execute(
            "INSERT INTO comment_tasks(id,status,total,character_id,created_at,updated_at,comment_ids) VALUES('this-task','running',1,'whale',?,?,?)",
            (now, now, json.dumps([selected])),
        )
        app.state.store.execute(
            "INSERT INTO drafts(comment_id,text,status,character_id,character_version,created_at,updated_at,generation_task_id) VALUES(?,'本次生成正文','pending_review','whale',1,?,?,'this-task')",
            (selected, now, now),
        )
        result = (
            client.get(
                "/api/comments/workbench",
                params={
                    "task_id": "this-task",
                    "comment_ids": str(selected),
                    "video_id": fetched["video_id"],
                },
            )
            .raise_for_status()
            .json()
        )
        assert result["total"] == 1
        assert result["task"]["comment_ids"] == [selected]
        assert client.get("/api/comment-tasks?task_id=this-task").json()[0]["id"] == "this-task"
        assert client.get("/api/comments/workbench?comment_ids=1,bad").status_code == 400
        assert client.get("/api/comments/workbench?comment_ids=0").status_code == 400
        assert client.get("/api/comments/workbench?task_id=").status_code == 422
