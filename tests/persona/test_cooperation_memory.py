import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from tuber_studio.agent.memory_organizer import MemorySuggestion, OwnerUtterance
from tuber_studio.agent.memory_organizer.owner_memory import private_rows, read_stamp
from tuber_studio.agent.memory_organizer.private_editor_memory import connect_private_editor
from tuber_studio.app import create_app
from tuber_studio.persona.memory.cooperation import (
    RoutineProposal,
    RoutineReview,
    bound_statement,
    corroborates,
    locate_rule,
    with_scope_context,
)

SCOPE = "咱俩对稿有个默契"
PRACTICE = "存疑处圈红，定稿处描蓝"
RULE = SCOPE + "：" + PRACTICE + "。"
GREETING = "辛苦啦，跟我聊两句吧。"
ONE_OFF = "明天交接时帮我圈红两处。"
EXAMPLES = {
    RULE: (SCOPE, PRACTICE, "号主本人的持续协作约定"),
    GREETING: ("辛苦啦", "跟我聊两句吧", "寒暄玩笑或举例转述"),
    ONE_OFF: ("明天交接时", "帮我圈红两处", "单次请求或邀请"),
    "有人说每次都要倒着念稿，我只是开个玩笑。": ("每次", "倒着念稿", "寒暄玩笑或举例转述"),
}


async def test_private_chat_admission_requires_independent_original_evidence(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    provider = runtime.llm
    originals = {}
    memory = runtime.daily_memory
    owner_scope = runtime.memory_organizer.scope()
    confirmed = "Group related points and separate distinct concerns."
    # Both entries may have the same editing topic while specifying independent practices.
    manual = {"kind": "private", "key": "settings:formatting", "text": confirmed, "at": time.time(),
              "private_owner": memory.private_owner, "owner_confirmed": True}
    assert memory.save_organized(owner_scope, manual, MemorySuggestion(action="add", existing_id=None,
                                 headline="对稿", note=confirmed, tags=[]), None)
    baseline, = memory.organized(owner_scope)

    class OvereagerOrganizer:
        async def generate_json(self, messages, schema, **kwargs):
            if schema["title"] == "OwnerUtterance":
                quote = messages[-1]["content"]
                assert len(messages) == 2 and "冷泡茶" not in messages[0]["content"]
                scope, practice, _ = EXAMPLES[quote]
                return {"statement": quote, "scope_quote": "咱俩" if quote == RULE else scope,
                        "practice_quote": practice, "meaning": "当前事项" if quote == ONE_OFF else "持续合作方式"}
            packet = json.loads(messages[-1]["content"])
            if schema["title"] == "ChangeOfPractice":
                assert packet == {"original": confirmed, "new": RULE}
                # The deployed model judged unrelated layout/title preferences to be a replacement.
                return {"relation": "新做法取代原做法", "common_reference": "排版",
                        "old_value": confirmed, "new_value": PRACTICE}
            quote = packet["original"]
            assert quote != ONE_OFF
            if quote == RULE:
                assert packet["scope_quote"] == SCOPE
            return {"meaning": EXAMPLES[quote][2], "scope_supported": quote == RULE,
                    "practice_supported": True, "same_agreement": quote == RULE, "existing_id": baseline["id"],
                    "summary_kind":"preference", "topic_quote":"对稿"}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost",
                                     headers={"Authorization": "Bearer " + settings.admin_token}) as client:
            for quote in EXAMPLES:
                identity = str(uuid4())
                response = await client.post("/api/chat/intelligent", json={"request_id": identity, "message": quote,
                                             "intent": {"action": "chat"}})
                response.raise_for_status()
                originals[quote] = "turn:" + identity
            runtime.operation_llm = OvereagerOrganizer()
            assert await runtime.memory_organizer.run_once() == len(EXAMPLES)
            assert await runtime.memory_organizer.run_once() == 0
            rows = runtime.daily_memory.organized(runtime.memory_organizer.scope())
            row, = [r for r in rows if r["id"] != baseline["id"]]
            assert memory.organized(owner_scope, item_id=baseline["id"]) == [baseline]
            assert row["quote"] == RULE
            stored = row["proof"]["cooperation_rule"]
            assert bound_statement(RULE, stored) == RULE
            assert RULE[slice(*stored["proposed_scope_span"])] == "咱俩"
            assert RULE[slice(*stored["scope_span"])] == SCOPE and RULE[slice(*stored["practice_span"])] == PRACTICE
            for quote, source in originals.items():
                status = runtime.store.one("SELECT status FROM memory_organization WHERE source=?", (source,))["status"]
                assert status == ("saved" if quote == RULE else "skipped")
                assert [r["id"] for r in private_rows(runtime, source)] == ([row["id"]] if quote == RULE else [])
    finally:
        await provider.close()
        await app.state.bilibili_account.adapter.close()
        await app.state.browsing.close()
        runtime.store.close()


def test_private_interpretation_cannot_return_an_unevidenced_storage_action():
    for invalid in ({"action": "add", "routine": None}, {"action": "skip"}):
        with pytest.raises(ValidationError):
            OwnerUtterance.model_validate(invalid)


@pytest.mark.parametrize("quote,fragment,practice,expected", [
    (RULE, "咱俩", PRACTICE, SCOPE),
    ("老规矩每回对稿疑处圈红。", "每回", "疑处圈红", "老规矩每回对稿"),
    ("疑处圈红是咱们对稿的习惯。", "咱们", "疑处圈红", "是咱们对稿的习惯"),
])
def test_qualifying_clause_and_original_model_span_are_both_bound(quote, fragment, practice, expected):
    words = RoutineProposal(statement=quote, scope_quote=fragment, practice_quote=practice)
    original = locate_rule(quote, words)
    expanded = with_scope_context(quote, original)
    assert quote[slice(*expanded["scope_span"])] == expected
    assert quote[slice(*expanded["proposed_scope_span"])] == fragment
    assert bound_statement(quote, original) == bound_statement(quote, expanded) == quote
    assert bound_statement(quote, expanded | {"scope_span": expanded["statement_span"]}) is None


def test_binding_rejects_unsupported_ambiguous_and_overlapping_source_spans():
    proposal = RoutineProposal(statement=RULE, scope_quote=SCOPE, practice_quote=PRACTICE)
    source = GREETING + RULE
    rule = locate_rule(source, proposal)
    assert bound_statement(source, rule) == RULE
    assert not bound_statement(source + RULE, rule)
    assert not bound_statement(source, rule | {"scope_span": [0, 10]})
    assert not locate_rule(source, proposal.model_copy(update={"scope_quote": "以后"}))
    assert not locate_rule(source, proposal.model_copy(update={"scope_quote": PRACTICE}))
    review = RoutineReview(meaning="号主本人的持续协作约定", scope_supported=True,
                           practice_supported=True, same_agreement=True)
    assert corroborates(source, rule, review)
    for changed in ({"scope_supported": False}, {"practice_supported": False}, {"same_agreement": False},
                    {"meaning": "单次请求或邀请"}):
        assert not corroborates(source, rule, review.model_copy(update=changed))


@pytest.mark.parametrize("scope,practice", [
    ("以后", "每个标题都用问句，以问号结尾"),
    ("以后写直播素材提纲", "都用问句，以问号结尾"),
    ("每个标题", "标题都用问句，以问号结尾"),
])
def test_different_valid_readings_of_same_standing_instruction(scope, practice):
    quote = "以后写直播素材提纲，每个标题都用问句，以问号结尾。"
    rule = locate_rule(quote, RoutineProposal(statement=quote, scope_quote=scope, practice_quote=practice))
    assert corroborates(quote, rule, RoutineReview(meaning="号主本人的持续协作约定", scope_supported=True,
                                                  practice_supported=True, same_agreement=True))


@pytest.mark.parametrize("archived", [False, True], ids=["portable-old-row", "spy32cvj-readonly-backup"])
async def test_old_greeting_is_inspectable_but_never_reaches_retry_or_reedition(settings, archived):
    if archived:
        db = Path("tmp/creative-browser-spy32cvj/studio.sqlite3").resolve()
        if not db.is_file():
            pytest.skip("local QA database unavailable")
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(settings.runtime / "studio.sqlite3")) as copy:
                source.backup(copy)
    app = create_app(settings)
    runtime = app.state.runtime
    memory = runtime.daily_memory
    scope = runtime.memory_organizer.scope()
    try:
        if archived:
            task_id = runtime.store.one("SELECT id FROM agent_tasks WHERE status='failed'")["id"]
            task = runtime.tasks.current(task_id)
            assert "items.1.title" in task["message"]
            snapshot = task["plan"]["editor_memory"]
            cite, = snapshot["memory_citations"]
            assert (cite["id"], cite["revision"], cite["quote"]) == (1, 1, GREETING)
        else:
            source = {"kind": "private", "text": GREETING, "key": "turn:" + str(uuid4()),
                      "private_owner": memory.private_owner, "at": time.time()}
            decision = MemorySuggestion(action="add", headline=GREETING, note=GREETING, tags=[], existing_id=None)
            assert memory.save_organized(scope, source, decision, None)
            row, = memory.organized(scope)
            snapshot = {"memory_use": {"ids": [row["id"]], "stamp": read_stamp(runtime, [row])}}
            task = {"goal": "准备私有视频提纲", "steps": [{"tool": "inspect_video"}],
                    "plan": {"editor_memory": snapshot, "editor_draft": {}, "editor_issues": {"items.0.title": GREETING},
                             "candidate": {"items": []}}}
        row, = memory.organized(scope)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost",
                                     headers={"Authorization": "Bearer " + settings.admin_token}) as client:
            result = await client.post("/api/agent/memory.get", json={"id": row["id"]})
            assert result.raise_for_status().json()["quote"] == GREETING
            lookup = await client.post("/api/agent/memory.search", json={"query": row["source"]["key"]})
            assert [item["quote"] for item in lookup.raise_for_status().json()["items"]] == [GREETING]
        assert not private_rows(runtime, row["source"]["key"])
        assert not memory.recall_organized(scope, "operator", GREETING)
        async def observe_final_input(request, evidence):
            assert "owner_writing_preferences" not in request
            assert not {"editor_memory", "editor_draft", "editor_issues", "retained_candidates",
                        "private_previous_edition", "previous_delivery"}.intersection(request["plan"])
            return request["plan"]["candidate"]
        runtime.tasks.edit_delivery = observe_final_input
        connect_private_editor(runtime)
        candidate = task["plan"]["candidate"]
        assert await runtime.tasks.edit_delivery(task, None) is candidate
        task["plan"]["private_previous_edition"] = {"memory_use": snapshot["memory_use"],
                                                    "fields": {"previous_delivery": GREETING}}
        assert await runtime.tasks.edit_delivery(task, None) is candidate
        assert memory.organized(scope, item_id=row["id"])[0]["revision"] == row["revision"]
    finally:
        await runtime.llm.close()
        await app.state.bilibili_account.adapter.close()
        await app.state.browsing.close()
        runtime.store.close()
