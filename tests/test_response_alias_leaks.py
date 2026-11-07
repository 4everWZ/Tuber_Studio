"""Internal references have a namespace; speech defects never require regeneration."""

import json

import pytest
from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.inputs import ResponseDecisionInput


def response(text):
    return {
        "action": "reply_public", "reason": "specific_detail", "explanation": "接着当前材料回应",
        "text": text, "evidence_ids": ["ref:1"],
    }


def source_input(extra=None):
    return ResponseDecisionInput({
        "evidence": [{"id": f"source:{number}", "text": "真实字幕片段"} for number in range(84)],
        **(extra or {}),
    })


@pytest.mark.parametrize("text", ["start with ref:84.", "先看ref:84吧", "先看REF:84吧", "读[ref:1]再答", "先ref:84后ref:2。", "再看ref:999。"])
def test_prose_reference_is_diagnostic_while_citations_remain_valid(text):
    packet = source_input()
    value = packet.validate_wire(response(text))
    assert packet.diagnose(value).startswith("正文含内部引用标识：")
    assert packet.resolve(value).evidence_ids == ["source:0"]
    assert packet.resolve(value).text == text


@pytest.mark.parametrize("text", [
    "DeepSeek R1的语气挺有意思。", "这台R84看起来挺小。", "先看r84吧",
    "r999还挺有意思。", "br84和r84_model是产品型号。",
])
def test_product_names_are_not_internal_references_with_or_without_source_occurrences(text):
    # The text namespace is independent of the reference transport. No source
    # scanning, web-page exemption or restore-time whitelist is necessary.
    for extra in ({}, {"video_source": {"title": text}}, {"thread": {"source": {"text": text}}}):
        packet = source_input(extra)
        value = packet.validate_wire(response(text))
        assert packet.diagnose(value) is None and value.text == text


def test_reference_namespace_has_exact_token_boundaries_and_invocation_local_citations():
    packet = source_input()
    for text in ("pref:84", "ref:84_model"):
        assert packet.diagnose(packet.validate_wire(response(text))) is None
    assert packet.allowed_aliases == {f"ref:{i}" for i in range(1, 85)}
    for invalid in ("r1", "ref:999", "source:0"):
        with pytest.raises(ValueError, match="本次短编号"):
            packet.validate_wire(response("继续聊这个细节。") | {"evidence_ids": [invalid]})


@pytest.mark.parametrize("fetch_ok", [False, True])
async def test_web_product_needs_no_whitelist_on_completed_generation_reuse(browsing, monkeypatch, fetch_ok):
    from tuber_studio import web_fetch
    from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, ResponseDecision

    await browsing.preflight()
    browsing.runtime.llm.supports_operation_web_fetch = True
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character,
        browsing.configured() | {"autonomous_batch": True}, 0, True,
    )
    data = {"kind": "reply_public", "evidence": [{"id": "public:42", "text": "你怎么看这款键盘？"}], "thread": {}}
    calls, fetched = [], []

    async def fetch(url):
        fetched.append(url)
        if not fetch_ok:
            raise TimeoutError
        return {"url": url, "title": "R84键盘", "excerpt": "R84是一款键盘。"}

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        result = {"web_fetch": "https://example.org/R84"} if len(calls) == 1 else response("R84这款键盘我也想看看。")
        browsing.runtime.llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)

    async def run():
        return await browsing.decisions._model(row, ResponseDecision, data, RESPONSE_INSTRUCTION)

    result = await run()
    assert result.text == "R84这款键盘我也想看看。" and result.evidence_ids == ["public:42"]
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


async def test_reference_in_speech_keeps_candidate_for_review_without_repair_or_automatic_send(browsing, monkeypatch):
    from test_browsing import BV, finished

    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV], "recent_sample_limit": 0})
    original = browsing.runtime.llm.generate_json
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        value = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision":
            value["text"] = "先看ref:1吧，这里加盐以后鸡蛋浮起来了。"
        browsing.runtime.llm.last_content = json.dumps(value, ensure_ascii=False)
        return value

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed"
    assert calls == ["CandidateBatchPlan", "ResponseDecision"]
    intent, = browsing.intents()
    assert intent["status"] == "pending_review" and not intent["auto_authorized"]
    assert "ref:1" in intent["text"] and intent["evidence"] == ["11:0"]
    episode = browsing.runtime.continuations.get(browsing.store.one(
        "SELECT id FROM operation_continuations WHERE channel='generation.browsing' "
        "AND json_extract(binding,'$.schema')='ResponseDecision'",
    )["id"])
    assert episode["state"] == "awaiting_review"
    assert "内部引用标识" in episode["feedback"]["message"]
    assert episode["current_output"]["text"] == intent["text"]
    assert episode["current_output"]["evidence_ids"] == ["ref:1"]
    assert not browsing.adapter.sent
