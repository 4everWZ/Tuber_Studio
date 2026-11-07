"""An agent may continue a discussion without generating a video main reply."""

from test_browsing import BV, finished
from test_browsing import browsing as browsing


async def test_agent_chooses_comments_without_a_main_reply_or_interest_classification(browsing):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"reply_enabled": True, "recent_sample_limit": 0})

    async def fetch(video, max_pages=1):
        return {
            "items": [
                {
                    "comment_id": "601",
                    "thread_id": "601",
                    "platform_user_id": "300",
                    "display_name": "邻居",
                    "content": "鸡蛋浮起来需要多少盐？",
                }
            ]
        }

    async def read_reply(target):
        return {"author_uid": "300", "text": "鸡蛋浮起来需要多少盐？"}

    original = browsing.runtime.llm.generate_json
    actions = []

    async def choose(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision" and any(
            "read_comments" in branch["properties"]["action"].get("enum", []) for branch in schema["oneOf"]
        ):
            progress = browsing.store.one("SELECT material FROM browsing_video_progress WHERE bvid=?", (BV,))
            assert progress, "Video content must survive a stop before reply generation finishes"
            actions.append("read_comments")
            return {"action": "read_comments", "reason": "light_interaction", "text": "", "evidence_ids": []}
        return await original(messages, schema, max_tokens=max_tokens)

    browsing.adapter.fetch = fetch
    browsing.adapter.read_video_reply = read_reply
    browsing.runtime.llm.generate_json = choose
    await browsing._current_account()
    browsing.discovery.identity = "comment-choice"
    browsing.discovery.binding = browsing.discovery._binding()
    row = await finished(browsing, discovery_id="comment-choice")
    assert row["status"] == "completed"
    assert actions == ["read_comments"]
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["601"]
    assert not [name for name, _ in browsing.runtime.llm.calls if name == "ContentDecision"]
    assert not browsing.repo.video_retry(row, BV)
