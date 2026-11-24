"""Internal references have a namespace; speech defects never require regeneration."""

import json

import pytest

from tests.support.operations_helpers import browsing as browsing
from tuber_studio.operations.kernel.inputs import ResponseDecisionInput


def response(text):
    return {
        "action": "reply_public", "reason": "specific_detail", "explanation": "接着当前材料回应",
        "text": text,
    }


def source_input(extra=None):
    return ResponseDecisionInput({
        "evidence": [{"id": f"source:{number}", "text": "真实字幕片段"} for number in range(84)],
        **(extra or {}),
    })


@pytest.mark.parametrize("text", ["start with ref:84.", "先看ref:84吧", "先看REF:84吧", "读[ref:1]再答", "先ref:84后ref:2。", "再看ref:999。"])
def test_prose_reference_leak_is_rejected_while_python_provenance_remains_clean(text):
    packet = source_input()
    with pytest.raises(ValueError, match="ref:N"):
        packet.validate_wire(response(text))


@pytest.mark.parametrize("text", [
    "DeepSeek R1的语气挺有意思。", "这台R84看起来挺小。", "先看r84吧",
    "r999还挺有意思。", "br84和r84_model是产品型号。",
])
def test_product_names_are_not_internal_references_with_or_without_source_occurrences(text):
    # Product identifiers are never flagged as internal protocol leaks.
    for extra in ({}, {"video_source": {"title": text}}, {"thread": {"source": {"text": text}}}):
        packet = source_input(extra)
        value = packet.validate_wire(response(text))
        assert packet.diagnose(value) is None and value.text == text


def test_reference_namespace_has_exact_token_boundaries_without_wire_aliases():
    packet = source_input()
    for text in ("pref:84", "ref:84_model"):
        assert packet.diagnose(packet.validate_wire(response(text))) is None
    assert all(ref.alias == ref.canonical_id for ref in packet.references)
    assert "[ref:" not in packet.format()
    assert packet.allowed_aliases == {f"source:{i}" for i in range(84)}
    resolved = packet.resolve(response("继续聊这个细节。"), provenance=["source:0"])
    assert resolved.evidence_ids == ["source:0"]


@pytest.mark.parametrize("fetch_ok", [False, True])
async def test_web_product_keeps_its_name_when_reusing_a_comment_decision(browsing, monkeypatch, fetch_ok):
    from tuber_studio.infra.web_client import fetch as web_fetch
    from tuber_studio.operations.kernel.models import RESPONSE_INSTRUCTION, ResponseDecision

    await browsing.preflight()
    browsing.runtime.operation_llm.supports_operation_web_fetch = True
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character,
        browsing.configured() | {"autonomous_batch": True}, 0, True,
    )
    data = {
        "kind": "reply_public",
        "thread": {"source": {"comment_id": "42", "author_uid": "200", "text": "你怎么看 R84 这款键盘？"}},
        "allowed_evidence_ids": ["public:42"],
    }
    calls, fetched = [], []

    async def fetch(url):
        fetched.append(url)
        if not fetch_ok:
            raise TimeoutError
        return {"url": url, "title": "R84键盘", "excerpt": "R84是一款键盘。"}

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        result = ({"web_fetch": "https://example.org/R84", "source_quote": "R84 这款键盘"}
                  if len(calls) == 1 else response("R84这款键盘我也想看看。"))
        browsing.runtime.operation_llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", generate)

    async def run():
        return await browsing.decisions._model(row, ResponseDecision, data, RESPONSE_INSTRUCTION)

    result = await run()
    assert result.text == "R84这款键盘我也想看看。" and result.evidence_ids == ["public:42"]
    state = browsing.runtime.continuations.get(result._generation_id)["state"]
    assert state == ("completed" if fetch_ok else "awaiting_review")
    restored = await run()
    assert restored.text == result.text and restored.evidence_ids == result.evidence_ids
    assert calls == ["ResponseDecision", "ResponseDecision"]
    assert fetched == ["https://example.org/R84"]
    receipt = json.loads(browsing.store.one(
        "SELECT context FROM operation_continuation_steps WHERE phase='web_fetch'",
    )["context"])
    assert "source_alias_literals" not in receipt and "excerpt" not in receipt
    assert receipt["status"] == ("ok" if fetch_ok else "timeout")
    assert not browsing.intents() and not browsing.adapter.sent


async def test_reference_in_speech_is_rejected_without_creating_candidate_or_automatic_send(browsing, monkeypatch):
    from tests.support.operations_helpers import BV, finished

    browsing.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV], "recent_sample_limit": 0})
    original = browsing.runtime.operation_llm.generate_json
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        value = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision":
            value["text"] = "先看ref:1吧，这里加盐以后鸡蛋浮起来了。"
        browsing.runtime.operation_llm.last_content = json.dumps(value, ensure_ascii=False)
        return value

    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", generate)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "partial"
    assert calls == ["CandidateBatchPlan", "ResponseDecision"]
    assert not browsing.intents()
    episode = browsing.runtime.continuations.get(browsing.store.one(
        "SELECT id FROM operation_continuations WHERE channel='generation.browsing' "
        "AND json_extract(binding,'$.schema')='ResponseDecision'",
    )["id"])
    assert episode["state"] == "invalid"
    assert any(
        step["phase"] == "feedback" and "ref:N" in (step.get("feedback") or {}).get("message", "")
        for step in episode["revisions"]
    )
    assert not browsing.adapter.sent
