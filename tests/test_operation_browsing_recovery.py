"""Source-bound browsing checkpoints continue under a fresh authorized round."""

import asyncio
import json
from copy import deepcopy

from test_browsing import browsing as browsing
from test_browsing import finished, model_packet

from tuber_studio.browsing_models import BrowsingPolicy


async def test_generation_wait_checkpoint_resumes_original_material_and_stays_preview(browsing):
    service = browsing
    platform, model = service.adapter, service.runtime.llm
    original = model.generate_json
    repairing = False
    calls = []

    async def generate(messages, schema, *, max_tokens):
        title = schema.get("title")
        calls.append(title)
        if title == "ContinuationDecision":
            return {"action": "revise" if repairing else "wait", "reason": "继续核对本次原文引用", "read": ""}
        if title == "ContentDecision":
            source = next(message["content"] for message in messages if message["role"] == "user")
            data = model_packet(source)
            return {
                "interest": "like",
                "worth_commenting": True,
                "reason": "specific_detail",
                "evidence_ids": [data["segments"][0]["id"] if repairing else "wrong"],
                "coverage_required": "provided",
            }
        return await original(messages, schema, max_tokens=max_tokens)

    model.generate_json = generate
    result = await finished(service, preview=True)
    task = service.tasks()[0]
    assert task["status"] == "checkpoint" and task["continuation"]["can_resume"]
    continuation_id = task["continuation"]["id"]
    assert not platform.sent
    repairing = True
    resumed = await service.resume_continuation("generation:" + task["id"], continuation_id)
    await asyncio.wait_for(service.active[resumed["id"]], 3)
    assert resumed["id"] != result["id"]
    assert service.runtime.continuations.get(continuation_id)["state"] == "completed"
    assert any(row["status"] == "selected" for row in service.tasks())
    assert not platform.sent and service.intents()[0]["status"] == "pending_review"
    assert calls.count("ContinuationDecision") == 2


async def test_private_failed_response_is_redacted_from_public_task_and_visible_only_in_dm(browsing):
    service = browsing
    platform, model = service.adapter, service.runtime.llm
    original = model.generate_json
    private_body = "这段仅用于私域查看的候选正文"

    async def generate(messages, schema, *, max_tokens):
        if schema.get("title") == "ContinuationDecision":
            return {"action": "wait", "reason": "稍后修正来源引用", "read": ""}
        if schema.get("title") == "ResponseDecision":
            return {
                "action": "handoff_private",
                "text": private_body,
                "reason": "user_requested",
                "evidence_ids": ["wrong"],
            }
        return await original(messages, schema, max_tokens=max_tokens)

    model.generate_json = generate
    await finished(service, preview=True)
    public = service.tasks()
    assert public[0]["continuation"]["private_handoff"]
    assert private_body not in json.dumps(public, ensure_ascii=False)
    private = service.messages.list()["private_continuations"]
    assert private_body in json.dumps(private, ensure_ascii=False)
    assert private[0]["resume_target"]["local_id"] == "generation:" + public[0]["id"]
    assert not service.messages.list()["drafts"] and not platform.sent


def full_input_counter(model, fixed):
    counts, reservations = [], []

    def prepare(messages, schema, *, max_tokens):
        reservations.append(max_tokens)
        return [*messages, {"role": "system", "content": "decoder_schema:" + json.dumps(schema)}]

    async def count(messages):
        assert messages[0]["role"] == "system" and messages[-1]["content"].startswith("decoder_schema:")
        body = "\n".join(message["content"] for message in messages)
        value = fixed + 500 * body.count("唯一原文段")
        counts.append(value)
        return value

    model.prepare_json_messages = prepare
    model.count_tokens = count
    return counts, reservations


async def test_full_prepared_input_chunks_preserve_text_with_65536_output_reservation(browsing):
    await browsing.preflight()
    browsing.settings.data["llm"]["context_tokens"] = 131072
    config = browsing.policy() | {"decision_max_tokens": 65536}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    material = {
        "metadata": {"parts": [{"cid": "11", "title": "第一P"}]},
        "parts": [
            {
                "cid": "11",
                "segments": [
                    {"id": str(index), "start": index, "end": index + 1, "text": f"唯一原文段{index}。"}
                    for index in range(10)
                ],
            }
        ],
        "coverage": "all_available_cc",
        "missing_parts": [],
    }
    original = deepcopy(material)
    counts, reservations = full_input_counter(browsing.runtime.llm, 57000)
    chunks = await browsing._chunks(material, row)
    assert [source["id"] for chunk in chunks for source in chunk] == [str(index) for index in range(10)]
    assert [len(chunk) for chunk in chunks] == [3, 3, 3, 1]
    assert all(57000 + 500 * len(chunk) + 65536 <= 131072 * 95 // 100 for chunk in chunks)
    assert max(counts) > 131072 * 95 // 100 - 65536 and material == original
    assert set(reservations) == {65536}


async def test_fixed_role_and_output_capacity_checkpoint_does_not_retry_same_settings(browsing):
    await browsing.preflight()
    browsing.settings.data["llm"]["context_tokens"] = 131072
    config = BrowsingPolicy(
        sources=["specified"],
        watch_videos=["BV1d8pU6xEZX"],
        top_level_enabled=True,
        decision_max_tokens=65536,
    ).model_dump()
    browsing.store.set("browsing_policy", config)
    counts, reservations = full_input_counter(browsing.runtime.llm, 60000)
    await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert task["status"] == "checkpoint" and task["capacity_signature"]
    assert "输出预留" in task["error"] and not browsing.runtime.llm.calls
    count = len(counts)
    await finished(browsing, preview=True)
    assert len(counts) == count and len(browsing.tasks()) == 1
    # An explicit reread remains available after changing the actual capacity settings.
    config["decision_max_tokens"] = 8192
    assert browsing._capacity_signature(config) != task["capacity_signature"]
    browsing.store.set("browsing_policy", config)
    await finished(browsing, preview=True)
    assert len(counts) > count and 8192 in reservations
    assert browsing.tasks()[0]["status"] == "selected" and not browsing.adapter.sent
