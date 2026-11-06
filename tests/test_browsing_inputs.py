"""Model aliases are local presentation, never durable evidence identities."""

import copy
import json
from itertools import product

import httpx
import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet

from tuber_studio.browsing_inputs import (
    CLASSIFICATION_INSTRUCTION,
    ContentDecisionInput,
    ResponseDecisionInput,
    bind_content_decision,
    classification_source_groups,
    classification_view,
    compact_content_input,
    encode_repeated_command_prefix,
    format_content_input,
    join_content_sources,
    persona_fingerprint,
)
from tuber_studio.browsing_models import (
    RESPONSE_INSTRUCTION,
    BrowsingPolicy,
    ContentDecision,
    ResponseDecision,
    VideoEngagementDecision,
    content_decision_schema,
)
from tuber_studio.dynamics import dynamic_decision_messages
from tuber_studio.operation_text import operation_character
from tuber_studio.persona import (
    DAFEIYU_IDENTITY,
    GROUNDED_DAFEIYU_CARD,
    GROUNDED_DAFEIYU_MODEL,
    built_in_anchor,
)
from tuber_studio.providers import LlamaCppProvider, build_messages


def test_prepared_intent_combination_has_specific_field_feedback():
    value = {
        "interest": "like",
        "worth_commenting": True,
        "reason": "specific_detail",
        "evidence_ids": ["e1"],
        "coverage_required": "provided",
    }
    assert ContentDecision.model_validate(value).worth_commenting
    with pytest.raises(ValueError, match="字段组合不一致") as failure:
        ContentDecision.model_validate(value | {"reason": "unsafe_interaction"})
    for field in (
        "interest=like",
        "worth_commenting=true",
        "coverage_required=provided",
        "reason=unsafe_interaction",
    ):
        assert field in str(failure.value)


@pytest.mark.parametrize(
    "choice",
    [
        {
            "interest": "skip",
            "worth_commenting": False,
            "reason": "no_interest",
            "coverage_required": "provided",
        },
        {
            "interest": "like",
            "worth_commenting": False,
            "reason": "nothing_to_add",
            "coverage_required": "provided",
        },
        {
            "interest": "need_more",
            "worth_commenting": False,
            "reason": "visual_required",
            "coverage_required": "visual",
        },
    ],
)
def test_other_participation_choices_remain_available(choice):
    assert ContentDecision.model_validate(choice).model_dump() == choice | {"evidence_ids": []}


def schema_branch_matches(branch, value):
    """Evaluate generated field constraints independently of the intent rule."""
    if set(value) != set(branch["required"]):
        return False
    for name, rule in branch["properties"].items():
        field = value[name]
        if "const" in rule and field != rule["const"] or "enum" in rule and field not in rule["enum"]:
            return False
        if rule.get("type") == "array":
            if not rule["minItems"] <= len(field) <= rule["maxItems"]:
                return False
            if "enum" in rule["items"] and any(item not in rule["items"]["enum"] for item in field):
                return False
    return True


def test_content_decoder_accepts_exactly_the_existing_144_intent_combinations():
    properties = ContentDecision.model_json_schema()["properties"]
    schema = content_decision_schema({"e1"})
    combinations = list(
        product(
            properties["interest"]["enum"],
            (False, True),
            properties["reason"]["enum"],
            properties["coverage_required"]["enum"],
        )
    )
    assert len(combinations) == 144
    for interest, worth, reason, coverage in combinations:
        value = {
            "interest": interest,
            "worth_commenting": worth,
            "reason": reason,
            "coverage_required": coverage,
            "evidence_ids": ["e1"],
        }
        try:
            ContentDecision.model_validate(value)
            accepted = True
        except ValueError:
            accepted = False
        matches = sum(schema_branch_matches(branch, value) for branch in schema["oneOf"])
        assert matches == int(accepted), value


def test_content_decoder_preserves_reference_bounds_and_empty_source_behavior():
    value = {
        "interest": "like",
        "worth_commenting": True,
        "reason": "specific_detail",
        "coverage_required": "provided",
        "evidence_ids": ["e1"],
    }
    schema = content_decision_schema({"e1"})
    for refs, expected in ((["e1"], True), ([], False), (["unknown"], False), (["e1"] * 9, False)):
        assert (
            any(schema_branch_matches(branch, value | {"evidence_ids": refs}) for branch in schema["oneOf"])
            is expected
        )
    no_sources = content_decision_schema(set())
    assert len(no_sources["oneOf"]) == 1
    assert not schema_branch_matches(no_sources["oneOf"][0], value)
    declined = value | {
        "interest": "skip",
        "worth_commenting": False,
        "reason": "no_interest",
        "evidence_ids": [],
    }
    assert schema_branch_matches(no_sources["oneOf"][0], declined)


def test_content_budget_new_default_keeps_explicit_saved_allowance(browsing):
    assert BrowsingPolicy().decision_max_tokens == 8192
    policy = browsing.policy() | {"decision_max_tokens": 2048}
    browsing.store.set("browsing_policy", policy)
    assert browsing.policy()["decision_max_tokens"] == 2048


def test_repeated_command_prefix_is_generic_multiline_and_reversible():
    text = "\r\n".join(
        [
            "  python worker.py --mode train a",
            "python worker.py --mode train b",
            "python worker.py --mode train c",
        ]
    )
    encoded, mapping = encode_repeated_command_prefix(text)
    assert mapping == ("§", "python worker.py --mode train")
    assert encoded.replace(*mapping) == text
    assert len(encoded) + len("=".join(mapping)) + 1 < len(text)
    assert encoded.startswith("  § a\r\n")


def test_command_prefix_never_factors_only_part_of_a_quoted_argument():
    text = "\n".join(f'python worker.py --mode "train {name}"' for name in ("alpha", "beta", "gamma"))
    encoded, mapping = encode_repeated_command_prefix(text)
    assert mapping == ("§", "python worker.py --mode")
    assert encoded.replace(*mapping) == text


@pytest.mark.parametrize(
    "text",
    [
        "python worker.py --mode train a\npython worker.py --mode train b",
        "a b --x\na b --y\na b --z",  # Mapping overhead equals the characters saved.
        "python worker.py --mode train a b c",
        "§\npython worker.py --mode train a\npython worker.py --mode train b\npython worker.py --mode train c",
        "hello friends today\nhello friends tomorrow\nhello friends again",
        None,
    ],
)
def test_command_factoring_keeps_small_single_line_and_plain_text_intact(text):
    assert encode_repeated_command_prefix(text) == (text, None)


def material():
    return {
        "metadata": {
            "bvid": "BV1d8pU6xEZX",
            "title": "多P实验",
            "parts": [{"cid": "101", "title": "原理", "page": 1}, {"cid": "202", "title": "演示", "page": 2}],
        },
        "coverage": "sampled_visual",
        "missing_parts": [{"cid": "303", "reason": "permission"}],
        "parts": [
            {
                "cid": "101",
                "language": "zh-CN",
                "track_id": "human-track",
                "is_ai": False,
                "segments": [
                    {"id": "101:human:0", "start": 0, "end": 3.2, "text": "第一段\n原文与空白  全部保留。"},
                    {"id": "101:human:1", "start": 3.2, "end": 6, "text": "第二段。"},
                ],
            },
            {
                "cid": "202",
                "is_ai": True,
                "language": "ai-zh",
                "segments": [{"id": "202:ai:0", "start": 1.1, "end": 5, "text": "另一P的字幕。"}],
            },
            {
                "cid": "202",
                "visual": True,
                "visual_provider": {"provider_id": "current-model", "version": "2"},
                "segments": [{"id": "frame:202:actual", "start": 4, "end": 4, "text": "手拿着透明杯。"}],
            },
        ],
    }


def segments(value):
    return [dict(segment, cid=part["cid"]) for part in value["parts"] for segment in part["segments"]]


def decision(refs):
    return ContentDecision(
        interest="like",
        worth_commenting=True,
        reason="specific_detail",
        evidence_ids=refs,
    )


def test_compact_packet_keeps_every_text_time_part_track_and_current_visual_source():
    value = material()
    snapshot = copy.deepcopy(value)
    chunk = segments(value)
    original_chunk = copy.deepcopy(chunk)
    data, aliases = ContentDecisionInput(value).present(chunk)
    assert data["metadata"] == {"title": "多P实验"}
    assert data["missing_parts"] == [{"page": None, "title": "", "reason": "permission"}]
    presentation = ContentDecisionInput(value)
    assert presentation.missing_parts == value["missing_parts"]
    assert data["coverage"] == value["coverage"]
    assert data["segments"] == [{"id": "e1", "part": 1}, {"id": "e2", "part": 2}, {"id": "e3", "part": 3}]
    bound = [source for block in aliases.values() for source in block.sources]
    assert [source.text for source in bound] == [s["text"] for s in chunk]
    assert [(source.part.index, source.start, source.end) for source in bound] == [
        (1, 0, 3.2),
        (1, 3.2, 6),
        (2, 1.1, 5),
        (3, 4, 4),
    ]
    assert [(source.part.cid, source.part.page, source.part.title) for source in bound] == [
        ("101", 1, "原理"),
        ("101", 1, "原理"),
        ("202", 2, "演示"),
        ("202", 2, "演示"),
    ]
    assert aliases["e1"].part.metadata()["track_id"] == "human-track"
    assert data["parts"][0]["is_ai"] is False
    assert data["parts"][1]["is_ai"] is True
    assert aliases["e3"].part.metadata()["visual_provider"] == {
        "provider_id": "current-model",
        "version": "2",
    }
    assert data["source_kinds"] == ["subtitle", "visual_observation"] and data["has_visual"] is True
    assert [source.id for source in bound] == [s["id"] for s in chunk]
    assert data["content"].count("视频第2部分（P2） 演示") == 1
    assert "[e1|00:00]第一段\n原文与空白  全部保留。 第二段。" in data["content"]
    assert "[e3|00:04]手拿着透明杯。" in data["content"]
    assert all(s["text"] in format_content_input(data) for s in chunk)
    assert value == snapshot and chunk == original_chunk
    data["metadata"]["title"] = "presentation edit"
    aliases["e3"].part.metadata()["visual_provider"]["version"] = "presentation edit"
    assert value == snapshot


def test_optional_cjk_renderer_keeps_source_characters_ascii_boundaries_and_default_rendering():
    value = material()
    original = copy.deepcopy(value)
    default, _ = ContentDecisionInput(value).present(segments(value))
    compact, aliases = ContentDecisionInput(value, text_renderer=join_content_sources).present(
        segments(value)
    )
    assert "全部保留。 第二段。" in default["content"]
    assert "全部保留。第二段。" in compact["content"]
    assert "原文与空白  全部保留。" in compact["content"]
    assert compact["segments"] == default["segments"]
    assert value == original
    assert [s.id for b in aliases.values() for s in b.sources] == [s["id"] for s in segments(value)]
    for first, second, expected in [
        ("hello", "world", "hello world"),
        ("12", "34", "12 34"),
        ("你好", " 世界", "你好 世界"),
    ]:
        local = material()
        local["parts"] = local["parts"][:1]
        local["parts"][0]["segments"][0]["text"] = first
        local["parts"][0]["segments"][1]["text"] = second
        presented, _ = ContentDecisionInput(local, text_renderer=join_content_sources).present(
            segments(local)
        )
        assert expected in presented["content"]


def test_wider_classification_groups_expand_back_to_exact_individual_response_sources():
    value = material()
    value["parts"] = value["parts"][:1]
    value["parts"][0]["segments"] = [
        {
            "id": f"original-{i}",
            "start": i,
            "end": i + 0.5,
            "text": "贝壳灯亮起以后鱼鱼坐在窗边讲一段海底故事呀",
        }
        for i in range(30)
    ]
    source = copy.deepcopy(value)
    chunk = segments(value)
    _, regular = ContentDecisionInput(value).present(chunk)
    _, grouped = ContentDecisionInput(
        value, source_grouper=classification_source_groups, text_renderer=join_content_sources
    ).present(chunk)
    assert len(grouped) < len(regular)
    assert [s for block in grouped.values() for s in block.sources] == [
        s for block in regular.values() for s in block.sources
    ]
    bound = bind_content_decision(decision(["e1"]), grouped)
    selected = [segment for segment in chunk if segment["id"] in set(bound.evidence_ids)]
    response = ResponseDecisionInput({"kind": "top_level", "evidence": selected})
    assert len(response.references) == len(selected) > 8
    wire = {
        "action": "reply_public",
        "text": "鱼鱼也想听听这盏灯下的故事。",
        "reason": "specific_detail",
        "evidence_ids": [response.references[0].alias, response.references[-1].alias],
    }
    assert response.resolve(wire).evidence_ids == [selected[0]["id"], selected[-1]["id"]]
    assert value == source


@pytest.mark.parametrize("second_start", [0.5, 3])
def test_classification_groups_keep_overlapping_and_gapped_source_boundaries(second_start):
    value = material()
    value["parts"] = value["parts"][:1]
    first, second = value["parts"][0]["segments"]
    first.update(start=0, end=1)
    second.update(start=second_start, end=second_start + 1)
    _, grouped = ContentDecisionInput(value, source_grouper=classification_source_groups).present(
        segments(value)
    )
    assert len(grouped) == 2


def test_compact_input_keeps_process_metadata_in_backend_without_mutating_source():
    value = material()
    value["metadata"].update(fetched_at=123, published_at=456, tags=[], tags_missing=True)
    data, _ = ContentDecisionInput(value).present(segments(value))
    original = copy.deepcopy(data)
    rendered = compact_content_input(data)
    assert "读取Unix:" not in rendered and "标签:[]" not in rendered
    assert "发布Unix:456" in rendered and "标签缺失:true" in rendered
    assert data == original
    assert data["metadata"]["fetched_at"] == 123 and data["metadata"]["tags"] == []


def test_compact_metadata_keeps_description_facts_and_expands_command_prefix_reversibly():
    value = material()
    prefix = "dsh plugin --profile desktop add"
    description = "\n".join(prefix + " " + plugin for plugin in ("one", "two", "three"))
    value["metadata"].update(description=description, fetched_at=123.25, published_at=100, tags_missing=False)
    data, _ = ContentDecisionInput(value).present(segments(value))
    before = copy.deepcopy(data)
    text = compact_content_input(data)
    assert "§=" + prefix in text
    assert "简介:\n§ one\n§ two\n§ three" in text
    encoded = text.split("简介:\n", 1)[1].split('\n{"missing_parts"', 1)[0]
    assert encoded.replace("§", prefix) == description
    assert "读取Unix:" not in text
    assert data["metadata"]["fetched_at"] == 123.25
    assert "发布Unix:100" in text
    assert "标签缺失:false" in text
    assert data == before


def test_registered_classification_view_is_opt_in_and_follows_complete_persona_changes(tmp_path):
    character = {
        "id": "whale",
        "core": {"identity": "whale", "examples": ["full dialogue"]},
        "style": {"language": "zh"},
        "rules": ["listen"],
    }
    directory = tmp_path / "config"
    directory.mkdir()
    view = {"character_id": "whale", "persona_sha256": persona_fingerprint(character), "brief": "compact"}
    path = directory / "operations-persona-views.json"
    path.write_text(json.dumps({"format": 1, "enabled": False, "views": [view]}))
    assert classification_view(tmp_path, character) is None
    path.write_text(json.dumps({"format": 1, "enabled": True, "views": [view]}))
    assert classification_view(tmp_path, character) == view
    for field, changed in (
        ("core", {"identity": "edited"}),
        ("style", {"language": "en"}),
        ("rules", ["different rules"]),
    ):
        assert classification_view(tmp_path, character | {field: changed}) is None


def test_content_view_does_not_replace_full_persona_for_response_generation(browsing, monkeypatch):
    import tuber_studio.browsing_inputs as inputs
    from tuber_studio.browsing_models import ResponseDecision
    from tuber_studio.providers import SYSTEM_RULES

    character = operation_character(browsing.runtime.character)
    view = {
        "character_id": character["id"],
        "persona_sha256": persona_fingerprint(character),
        "brief": "我喜欢新鲜故事与朋友。",
    }
    monkeypatch.setattr(
        inputs, "classification_registry", lambda root: {"format": 1, "enabled": True, "views": [view]}
    )
    row = {"character_id": character["id"], "character_version": character["version"]}
    value = material()
    data, _ = browsing._content_presentation(value, row).present(segments(value))
    _, messages, schema = browsing._model_input(row, ContentDecision, data, browsing._content_instruction())
    assert messages[0]["content"] == "角色：" + view["brief"]
    assert messages[1]["content"] == CLASSIFICATION_INSTRUCTION
    assert messages[-1]["content"] == "公开来源材料：" + compact_content_input(data)
    assert schema == browsing._model_input(row, ContentDecision, data, "same invocation")[2]
    response = {
        "kind": "top_level",
        "evidence": [{"id": "source", "text": "原始素材", "start": 0, "end": 2}],
        "allowed_evidence_ids": ["source"],
    }
    _, body_messages, _ = browsing._model_input(row, ResponseDecision, response, "写角色回应")
    assert body_messages[0]["content"].startswith(SYSTEM_RULES)
    assert character["core"]["identity"] in body_messages[0]["content"]
    assert character["core"]["examples"][0]["reply"] in body_messages[0]["content"]


def test_grounded_model_shares_response_card_and_keeps_browsing_decisions_short(browsing):
    settings = browsing.settings.data
    settings["llm"].update(kind="llama_cpp", model=GROUNDED_DAFEIYU_MODEL)
    character = browsing.runtime.character
    row = {"character_id": character["id"], "character_version": character["version"]}
    event = {"event_id": "m1", "text": "刚才画面里发生了什么？"}
    for mode in ("live", "comment_draft", "direct_message"):
        messages = build_messages(character, event, {}, mode, settings=settings)
        assert messages[0]["content"] == GROUNDED_DAFEIYU_CARD
    channel = build_messages(character, event, {}, "comment_channel_decision", settings=settings)
    assert channel[0]["content"] == DAFEIYU_IDENTITY
    dynamic = dynamic_decision_messages(character, settings, [], [], current_time="2026-11-06T10:00:00Z")
    assert json.loads(dynamic[-1]["content"])["character"] == GROUNDED_DAFEIYU_CARD

    value = material()
    data, _ = browsing._content_presentation(value, row).present(segments(value))
    _, classify, _ = browsing._model_input(row, ContentDecision, data, browsing._content_instruction())
    _, engagement, _ = browsing._model_input(row, VideoEngagementDecision, {"title": "盐水实验"}, "判断是否点赞")
    assert classify[0]["content"] == engagement[0]["content"] == DAFEIYU_IDENTITY

    for kind in ("top_level", "thread_reply"):
        response = {
            "kind": kind, "coverage": "sampled_visual",
            "evidence": [{"id": "source", "text": "原始素材", "start": 0, "end": 2}],
        }
        _, reply, schema = browsing._model_input(row, ResponseDecision, response, RESPONSE_INSTRUCTION)
        assert reply[0]["content"] == GROUNDED_DAFEIYU_CARD
        assert "evidence_ids选择本次短编号" in reply[1]["content"]
        assert "原始素材" in reply[-1]["content"] and schema["title"] == "ResponseDecision"
        if kind == "top_level":
            assert "分别引用" in reply[1]["content"]

    custom = character | {
        "persona_type": "fine_tuned", "fine_tuned_model": GROUNDED_DAFEIYU_MODEL,
        "compact_system_prompt": "一张明确绑定的角色短卡。",
    }
    assert built_in_anchor(custom, settings) == custom["compact_system_prompt"]
    settings["llm"]["model"] = "MiniCPM-V-4.7-1B"
    assert build_messages(character, event, {}, settings=settings)[0]["content"].startswith("以当前角色")


def test_chunk_local_aliases_bind_to_different_originals_and_reject_other_chunk_refs():
    value = material()
    presentation = ContentDecisionInput(value)
    first, first_refs = presentation.present(segments(value)[:2])
    second, second_refs = presentation.present(segments(value)[2:3])
    assert first["segments"][0]["id"] == second["segments"][0]["id"] == "e1"
    assert bind_content_decision(decision(["e1"]), first_refs).evidence_ids == ["101:human:0", "101:human:1"]
    assert bind_content_decision(decision(["e1"]), second_refs).evidence_ids == ["202:ai:0"]
    assert second["segments"][0]["part"] == 2
    assert second["source_kinds"] == ["subtitle"] and second["has_visual"] is False
    for ref in ["e2", "e999", "101:human:0", "202:ai:0", "E1"]:
        with pytest.raises(ValueError, match="引用"):
            bind_content_decision(decision([ref]), second_refs)
    with pytest.raises(ValueError, match="为空"):
        bind_content_decision(decision([]), second_refs)


async def test_native_http_schema_exposes_only_short_refs_and_database_keeps_real_ids(browsing):
    bodies = []

    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096}})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "mock rendered template"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(32))})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        bodies.append(body)
        data = model_packet(body["messages"][-1]["content"])
        if body["response_format"]["schema"]["title"] == "ContentDecision":
            result = decision(["e1"]).model_dump()
        else:
            result = {
                "action": "reply_public",
                "text": "盐水托起鸡蛋，好有意思！",
                "reason": "specific_detail",
                "evidence_ids": [data["evidence"][0]["id"]],
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)}}
                ],
                "usage": {"prompt_tokens": 32, "completion_tokens": 20},
            },
        )

    native = LlamaCppProvider(
        {"base_url": "http://offline.invalid", "context_tokens": 4096, "enable_thinking": True}
    )
    await native.client.aclose()
    native.client = httpx.AsyncClient(
        base_url="http://offline.invalid", transport=httpx.MockTransport(handle)
    )
    controlled = browsing.runtime.llm
    browsing.runtime.llm = native
    browsing.settings.data["llm"]["context_tokens"] = 4096
    browsing.store.set("browsing_policy", browsing.policy() | {"decision_max_tokens": 1024})
    try:
        row = await finished(browsing, preview=True)
        assert row["status"] == "completed"
        schema = bodies[0]["response_format"]["schema"]
        assert all(b["properties"]["evidence_ids"]["items"]["enum"] == ["e1"] for b in schema["oneOf"])
        packet = model_packet(bodies[0]["messages"][-1]["content"])
        assert packet["segments"][0] == {"id": "e1"}
        assert "[e1|00:00]加盐之后，鸡蛋浮了起来。" in packet["content"]
        assert "parts" not in packet
        assert bodies[0]["chat_template_kwargs"]["enable_thinking"] is True
        response_body = next(
            body for body in bodies if body["response_format"]["schema"]["title"] == "ResponseDecision"
        )
        assert all(
            branch["properties"]["evidence_ids"]["items"]["enum"] == ["r1"]
            for branch in response_body["response_format"]["schema"]["oneOf"]
        )
        response_user = response_body["messages"][-1]["content"]
        assert "[r1|" in response_user and "11:0" not in response_user
        assert browsing.tasks()[0]["decisions"][0]["evidence_ids"] == ["11:0"]
        assert browsing.intents()[0]["evidence"] == ["11:0"]
        assert browsing.intents()[0]["status"] == "pending_review" and not browsing.adapter.sent
    finally:
        browsing.runtime.llm = controlled
        await native.close()


@pytest.mark.parametrize("bad_ref", ["e99", "11:0"])
async def test_invalid_or_original_model_ref_is_checkpointed_without_being_adopted(browsing, bad_ref):
    async def generate(messages, schema, *, max_tokens):
        assert schema["title"] == "ContentDecision"
        return decision([bad_ref]).model_dump()

    browsing.runtime.llm.generate_json = generate
    row = await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert row["status"] == "partial" and task["status"] == "checkpoint"
    assert task["continuation"]["id"] and task["continuation"]["phase"] == "generation"
    episode = browsing.runtime.continuations.get(task["continuation"]["id"])
    assert episode["binding"]["schema"] == "ContentDecision"
    assert episode["original_output"] == decision([bad_ref]).model_dump()
    assert any(
        step["phase"] == "feedback"
        and "内容判断引用不存在或为空" in (step.get("feedback") or {}).get("message", "")
        for step in episode["revisions"]
    )
    assert task["decisions"] == []
    assert not browsing.intents() and not browsing.adapter.sent


async def test_two_real_model_invocations_reuse_alias_but_keep_chunk_ownership(browsing):
    value = material()
    chunks = [segments(value)[:2], segments(value)[2:]]

    async def split(material, row):
        return chunks

    browsing._chunks = split
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    decisions = await browsing._content_decisions(value, row, {"lock_wait_seconds": 0})
    assert [d.evidence_ids for d in decisions] == [["101:human:0", "101:human:1"], ["202:ai:0"]]
    calls = [data for kind, data in browsing.runtime.llm.calls if kind == "ContentDecision"]
    assert [data["segments"][0]["id"] for data in calls] == ["e1", "e1"]
    assert row["config"]["max_model_calls"] >= 2


async def test_one_group_expands_to_twelve_segments_but_intent_keeps_only_reply_citations(browsing):
    async def subtitles(bvid, cid, languages):
        return {
            "bvid": bvid,
            "cid": cid,
            "track_id": "complete-track-v1",
            "content_hash": "unchanged-subtitle-version",
            "language": "zh-CN",
            "is_ai": False,
            "segments": [{"id": str(i), "start": i, "end": i + 1, "text": "盐水啊"} for i in range(12)],
            "coverage": {"complete": True},
        }

    browsing.adapter.subtitles = subtitles
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed"
    expected = [f"11:{i}" for i in range(12)]
    task = browsing.tasks()[0]
    assert task["decisions"][0]["evidence_ids"] == expected
    assert browsing.intents()[0]["evidence"] == [expected[0]]
    assert browsing.intents()[0]["status"] == "pending_review"
    cached = browsing.evidence.load(browsing.intents()[0]["material_keys"][0])
    assert [segment["id"] for segment in cached["parts"][0]["segments"]] == expected
    assert cached["parts"][0]["content_hash"] == "unchanged-subtitle-version"
    model_calls = [packet for kind, packet in browsing.runtime.llm.calls if kind == "ContentDecision"]
    assert model_calls[0]["segments"] == [{"id": "e1"}]
    response = next(packet for kind, packet in browsing.runtime.llm.calls if kind == "ResponseDecision")
    assert [item["id"] for item in response["evidence"]] == [f"r{i}" for i in range(1, 13)]
    assert not browsing.adapter.sent


@pytest.mark.parametrize("correction", ["prepare", "skip"])
async def test_inconsistent_intent_reaches_generation_feedback_and_can_change_choice(browsing, correction):
    # This mock intentionally provides no usage. Three calls must therefore
    # reserve their full allowances; production still uses reported native usage.
    browsing.store.set("browsing_policy", browsing.policy() | {"max_output_tokens": 32768})
    bad = {
        "interest": "like",
        "worth_commenting": True,
        "reason": "unsafe_interaction",
        "evidence_ids": ["e1"],
        "coverage_required": "provided",
    }
    corrected = (
        bad | {"reason": "specific_detail"}
        if correction == "prepare"
        else {
            "interest": "skip",
            "worth_commenting": False,
            "reason": "no_interest",
            "evidence_ids": [],
            "coverage_required": "provided",
        }
    )
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append((schema["title"], copy.deepcopy(messages)))
        if schema["title"] == "ContinuationDecision":
            return {"action": "revise", "reason": "重新选择一致的参与意图。", "read": ""}
        assert schema["title"] == "ContentDecision"
        return copy.deepcopy(bad if sum(kind == "ContentDecision" for kind, _ in calls) == 1 else corrected)

    browsing.runtime.llm.generate_json = generate
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    results = await browsing._content_decisions(material(), row, {"lock_wait_seconds": 0})
    assert [kind for kind, _ in calls] == ["ContentDecision", "ContinuationDecision", "ContentDecision"]
    identity = browsing.store.one(
        "SELECT id FROM operation_continuations WHERE channel='generation.browsing'"
    )["id"]
    episode = browsing.runtime.continuations.get(identity)
    assert episode["state"] == "completed" and episode["recoveries"] == 1
    assert episode["original_output"] == bad
    assert episode["current_output"] == corrected
    assert any("字段组合不一致" in str(step["feedback"]) for step in episode["revisions"])
    assert (
        results[0].interest == corrected["interest"]
        and results[0].worth_commenting == corrected["worth_commenting"]
    )
    assert not browsing.adapter.sent
