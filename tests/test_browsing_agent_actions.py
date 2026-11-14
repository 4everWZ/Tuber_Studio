"""An agent may continue a discussion without generating a video main reply."""

import json

from test_browsing import BV, finished, wait_for_sends
from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.models import ContentDecision
from tuber_studio.operations.browsing.round_context import agent_depth


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
            return {
                "action": "read_comments",
                "reason": "light_interaction",
                "text": "",
                "evidence_ids": [],
                "explanation": "盐水浮鸡蛋这个话题，先看看评论里大家试过的比例",
                "video_preference": "like",
                "preference_reason": "想看看大家如何做盐水浮鸡蛋实验",
            }
        return await original(messages, schema, max_tokens=max_tokens)

    browsing.adapter.fetch = fetch
    browsing.adapter.read_video_reply = read_reply
    browsing.runtime.llm.generate_json = choose
    await browsing._current_account()
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed"
    assert actions == ["read_comments"]
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["601"]
    assert not [name for name, _ in browsing.runtime.llm.calls if name == "ContentDecision"]
    assert not browsing.repo.video_retry(row, BV)
    task = next(
        item for item in browsing.tasks(round_id=row["id"])
        if item.get("task_kind") == "new_video"
    )
    assert task["payload"]["reason"] == "盐水浮鸡蛋这个话题，先看看评论里大家试过的比例"
    progress = browsing.repo.content_progress(row, BV)
    assert progress["main_decision"]["action"] == "read_comments"


async def test_no_main_reply_decision_survives_revisit_and_character_version(browsing):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"reply_enabled": True, "recent_sample_limit": 0})
    original = browsing.runtime.llm.generate_json
    decisions = []

    async def choose(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            decisions.append("skip")
            return {
                "action": "skip",
                "reason": "no_interest",
                "text": "",
                "evidence_ids": [],
                "explanation": "盐水浮鸡蛋的过程看明白了，先看看下一个视频",
                "video_preference": "neutral",
                "preference_reason": "已经看明白鸡蛋浮沉的过程，没特别喜欢或反感",
            }
        return await original(messages, schema, max_tokens=max_tokens)

    browsing.runtime.llm.generate_json = choose
    await browsing._current_account()
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed"
    assert browsing.repo.content_progress(first, BV)["read_state"] == "complete"
    browsing.store.save_character(browsing.runtime.character)
    browsing.operations.configure(
        {"enabled": True}, account_id="100", character=browsing.runtime.character
    )
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["status"] == "completed"
    assert decisions == ["skip"]
    assert len([call for call in browsing.adapter.calls if call[0] == "subtitles"]) == 1
    assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 1
    assert not browsing.intents()


async def test_visual_action_is_persisted_and_delegated_under_the_original_round(browsing, monkeypatch):
    await browsing._current_account()
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    identity = browsing.repo.task(row["id"], {"bvid": BV, "title": "盐水实验"})
    generation = browsing.repo.claim(identity, "test-owner")
    key, material = await browsing._prepare_evidence(BV, row["config"])
    evidence_id = browsing._segments(material)[0]["id"]
    result = {"id": identity, "owner": "test-owner", "generation": generation,
              "material": material, "material_key": key}
    original = ContentDecision(interest="need_more", worth_commenting=False, reason="visual_required",
                               evidence_ids=[evidence_id], coverage_required="visual")
    monkeypatch.setattr(browsing.evidence, "has_visual_provider", lambda: True)

    async def collect(video, config, *, visual_source_ids, task, deadline):
        record = json.loads(browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"])
        assert agent_depth.get() == 1 and task["round_id"] == row["id"]
        assert record["visual_followup"]["id"] == task["id"]
        assert record["visual_followup"]["status"] == "requested"
        assert video == BV and visual_source_ids == {evidence_id}
        return key, material | {"visual_source_ids": sorted(visual_source_ids)}

    async def decide(material, row, metrics):
        assert material["visual_source_ids"] == [evidence_id]
        return [original]

    monkeypatch.setattr(browsing, "_prepare_evidence", collect)
    monkeypatch.setattr(browsing.decisions, "_content_decisions", decide)
    followup = {}
    await browsing.decisions._followup_visual(result, row, {}, [original], followup)
    assert agent_depth.get() == 0 and followup["status"] == "completed"
    assert followup["material_key"] == key and not browsing.intents()
