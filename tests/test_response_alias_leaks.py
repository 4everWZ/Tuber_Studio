import pytest
from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.inputs import ResponseDecisionInput


def response(text):
    return {
        "action": "reply_public", "reason": "specific_detail", "explanation": "接着当前材料回应",
        "text": text, "evidence_ids": ["r1"],
    }


def source_input(extra=None):
    return ResponseDecisionInput({
        "evidence": [{"id": f"source:{number}", "text": "真实字幕片段"} for number in range(84)],
        **(extra or {}),
    })


@pytest.mark.parametrize("text", ["start with r84.", "先看r84吧", "先看R84吧", "读[r1]再答", "先r84后r2。"])
def test_unquoted_current_short_reference_gets_specific_format_feedback(text):
    with pytest.raises(ValueError, match="内部来源短编号") as error:
        source_input().validate_wire(response(text))
    assert "evidence_ids" in str(error.value)


@pytest.mark.parametrize(
    "text,extra",
    [
        ("DeepSeek R1的语气挺有意思。", {"video_source": {"title": "DeepSeek r1演示"}}),
        ("这台R84看起来挺小。", {"evidence": [{"id": "product", "text": "R84是一款键盘。"}]}),
        ("先看r84吧", {"thread": {"source": {"text": "你怎么看R84这个产品？"}}}),
        ("我也注意到了r84。", {"video_source": {"description": "今天讨论R84"}}),
        ("r999还挺有意思。", {}),
        ("br84和r84_model是产品型号。", {}),
    ],
)
def test_source_products_and_nonreference_product_names_remain_available(text, extra):
    packet = source_input(extra)
    assert packet.validate_wire(response(text)).text == text


def test_actor_bio_and_old_responses_do_not_turn_an_internal_reference_into_source_text():
    packet = source_input({
        "self_actor": {"account_bio": "r84", "text": "r84"},
        "recent_public_responses": [{"text": "先看r84吧"}],
    })
    with pytest.raises(ValueError, match="r84"):
        packet.validate_wire(response("先看r84吧"))


def test_canonical_resolution_and_evidence_selection_keep_working_without_alias_in_speech():
    packet = source_input()
    answer = response("原评论说还没做过安全检查，这一点得看清楚。")
    assert packet.resolve(answer).evidence_ids == ["source:0"]
    assert packet.validate_wire(answer).text == answer["text"]


async def test_browsing_alias_format_feedback_revises_without_publishing(browsing, monkeypatch):
    import json

    from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, ResponseDecision

    await browsing.preflight()
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character, browsing.configured() | {"autonomous_batch": True}, 0, True,
    )
    calls, generated = [], []
    corrected = "原评论说还没做过安全检查，这一点得看清楚。"

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        if schema["title"] == "ContinuationDecision":
            context = "\n".join(message["content"] for message in messages)
            assert "内部来源短编号" in context and "r1" in context
            result = {"action": "revise", "reason": "把内部编号改成能理解的正文", "read": ""}
        else:
            text = "先看r1吧" if not generated else corrected
            generated.append(text)
            result = response(text)
        browsing.runtime.llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    result = await browsing.decisions._model(
        row, ResponseDecision,
        {"kind": "reply_public", "evidence": [{"id": "public:42", "text": "这还是半成品，没做过安全检查。"}], "thread": {}},
        RESPONSE_INSTRUCTION,
    )
    assert result.text == corrected and result.evidence_ids == ["public:42"]
    assert calls == ["ResponseDecision", "ContinuationDecision", "ResponseDecision"]
    assert not browsing.intents() and not browsing.adapter.sent


@pytest.mark.parametrize("literal_field", ["excerpt", "title", "url"])
@pytest.mark.parametrize("resume", [False, True])
async def test_real_web_product_literal_survives_completed_reuse_and_checkpoint_resume(
    browsing, monkeypatch, literal_field, resume,
):
    import json

    from tuber_studio import web_fetch
    from tuber_studio.operation_generation import GenerationDeferred
    from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, ResponseDecision
    from tuber_studio.operations.browsing.round_context import generation_resume

    await browsing.preflight()
    browsing.runtime.llm.supports_operation_web_fetch = True
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character,
        browsing.configured() | {"autonomous_batch": True, "decision_max_tokens": 512}, 0, True,
    )
    data = {"kind": "reply_public", "evidence": [
        {"id": f"source:{number}", "text": "真实字幕片段"} for number in range(84)
    ], "thread": {}}
    calls, fetched = [], []
    resumed = False

    async def fetch(url):
        fetched.append(url)
        page = {"url": url, "title": "产品资料", "excerpt": "真实网页摘录"}
        page[literal_field] = "https://example.org/R84" if literal_field == "url" else "R84是一款键盘。"
        return page

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        if schema["title"] == "ContinuationDecision":
            result = {"action": "revise" if resumed else "wait", "reason": "继续核对引用", "read": ""}
        elif not fetched:
            result = {"web_fetch": "https://example.org/product"}
        else:
            result = response("R84这款键盘我也想看看。")
            if resume and not resumed:
                result["evidence_ids"] = ["r999"]
        browsing.runtime.llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)

    async def run():
        return await browsing.decisions._model(row, ResponseDecision, data, RESPONSE_INSTRUCTION)

    if resume:
        with pytest.raises(GenerationDeferred):
            await run()
        resumed = True
        result = await run()
    else:
        result = await run()
        call_count = len(calls)
        token = generation_resume.set(True)
        try:
            assert (await run()).text == result.text
        finally:
            generation_resume.reset(token)
        assert len(calls) == call_count
    assert result.text == "R84这款键盘我也想看看。" and result.evidence_ids == ["source:0"]
    assert fetched == ["https://example.org/product"]
    steps = browsing.store.all("SELECT context FROM operation_continuation_steps WHERE phase='web_fetch' AND status='ok'")
    receipt = json.loads(steps[0]["context"])
    assert receipt["source_alias_literals"] == ["r84"] and "excerpt" not in receipt
    assert not browsing.intents() and not browsing.adapter.sent


async def test_failed_fetch_and_assistant_selected_product_url_do_not_exempt_alias(browsing, monkeypatch):
    import json

    from tuber_studio import web_fetch
    from tuber_studio.operation_generation import GenerationDeferred
    from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, ResponseDecision

    await browsing.preflight()
    browsing.runtime.llm.supports_operation_web_fetch = True
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character,
        browsing.configured() | {"autonomous_batch": True, "decision_max_tokens": 512}, 0, True,
    )
    calls = []

    async def fetch(url):
        raise TimeoutError

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        if schema["title"] == "ContinuationDecision":
            assert "内部来源短编号" in "\n".join(message["content"] for message in messages)
            result = {"action": "wait", "reason": "网页未读到，先保留任务", "read": ""}
        elif len(calls) == 1:
            result = {"web_fetch": "https://example.org/R84"}
        else:
            result = response("R84这款键盘我也想看看。")
        browsing.runtime.llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    with pytest.raises(GenerationDeferred):
        await browsing.decisions._model(
            row, ResponseDecision,
            {"kind": "reply_public", "evidence": [
                {"id": f"source:{number}", "text": "真实字幕片段"} for number in range(84)
            ], "thread": {}}, RESPONSE_INSTRUCTION,
        )
    step = browsing.store.one("SELECT context FROM operation_continuation_steps WHERE phase='web_fetch'")
    assert "source_alias_literals" not in json.loads(step["context"])
    assert not browsing.intents() and not browsing.adapter.sent
