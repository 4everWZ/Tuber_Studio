import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.models import response_schema


@pytest.mark.parametrize("preference", ["like", "neutral", "not_interested"])
async def test_watch_only_records_real_preference_without_fetching_comments_or_generating_a_reply(
    browsing, monkeypatch, preference
):
    browsing.runtime.llm.interest = preference
    await browsing.configure({"top_level_enabled": False, "reply_enabled": False})
    await browsing.preflight()
    browsing.operations.configure(
        {"enabled": True}, account_id=browsing.account, character=browsing.runtime.character
    )

    async def no_comments(*args, **kwargs):
        raise AssertionError("Watching alone must not read comments")

    monkeypatch.setattr(browsing.adapter, "fetch", no_comments)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed", row
    decision = browsing.repo.content_progress(row, BV)["main_decision"]
    assert decision["action"] == "finish"
    assert decision["video_preference"] == preference
    assert "鸡蛋浮起来" in decision["preference_reason"]
    assert not browsing.intents() and not browsing.adapter.sent
    assert [name for name, _ in browsing.runtime.llm.calls] == ["CandidateBatchPlan", "ResponseDecision"]


def test_video_preference_is_independent_of_permission_to_reply():
    wire = response_schema(
        ["e0"], video_actions=True, allow_public=False, allow_comments=False, allow_handoff=False
    )
    actions = {branch["properties"]["action"]["enum"][0] for branch in wire["oneOf"]}
    assert "finish" in actions and not {"reply_public", "read_comments", "handoff_private"} & actions
    assert all("preference_reason" in branch["required"] for branch in wire["oneOf"])
