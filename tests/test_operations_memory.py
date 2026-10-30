import json

import pytest

from tuber_studio.operations_memory import OperationsMemory

SOURCES = [
    {
        "id": "memory:7",
        "type": "public_memory",
        "fictional": True,
        "text": "这是虚构的海底厨房。热饭已经端上了桌。",
        "event_id": "story7",
    }
]


class Extractor:
    def __init__(self):
        self.calls = []
        self.schemas = []
        self.counts = []
        self.tokens = 120
        self.raw = '{"entries":[{"source_id":"memory:7","quote":"热饭已经端上了桌。"}]}'
        self.after_count = None

    async def generate_json(self, messages, schema, *, max_tokens):
        self.calls.append((messages, max_tokens))
        self.schemas.append(schema)
        return json.loads(self.raw)

    async def count_tokens(self, messages):
        self.counts.append(messages)
        if self.after_count:
            self.after_count()
        return self.tokens


@pytest.fixture
def summary(store):
    return OperationsMemory(store), Extractor(), store.character()


async def build(summary, **kwargs):
    memory, llm, char = summary
    return await memory.build(
        llm,
        char,
        kwargs.pop("sources", SOURCES),
        kwargs.pop("budget", 256),
        kwargs.pop("model", "model-a"),
        authorized=kwargs.pop("authorized", lambda: True),
        **kwargs,
    )


async def test_exact_extract_program_metadata_and_whole_json_token_measurement(summary):
    memory, llm, char = summary
    value = await build(summary)
    assert value["valid"] and value["token_count"] == 120 and value["max_tokens"] == 256
    assert value["entries"] == [
        {"source_id": "memory:7", "quote": "热饭已经端上了桌。", "type": "public_memory", "fictional": True}
    ]
    measured = json.loads(llm.counts[0][0]["content"])
    assert measured == {"entries": value["entries"]}
    assert memory.public_sources(value) == [
        {"id": "memory:7", "text": "热饭已经端上了桌。", "type": "public_memory", "fictional": True}
    ]
    assert llm.calls[0][1] == 256
    assert memory.status(char, SOURCES, 256, "model-a") == value


async def test_long_source_keeps_token_budget_without_unsupported_finite_grammar_repetition(summary):
    _, llm, _ = summary
    quote = "海" * 2500
    sources = [SOURCES[0] | {"text": quote}]
    llm.raw = json.dumps({"entries": [{"source_id": "memory:7", "quote": quote}]})
    llm.tokens = 3000
    value = await build(summary, sources=sources, budget=4096)
    assert value["valid"] and value["token_count"] == 3000
    assert value["entries"][0]["quote"] == quote
    # Current pinned llama.cpp silently loses a grammar with char{1,6000}.
    # The independently measured token bound must own long quoted sources.
    quote_rule = llm.schemas[0]["properties"]["entries"]["items"]["properties"]["quote"]
    assert "maxLength" not in quote_rule


@pytest.mark.parametrize(
    "raw",
    [
        '{"entries":[',
        '```json\n{"entries":[]}\n```',
        '{"entries":[]} extra',
        '{"entries":[],"extra":true}',
        '{"entries":{}}',
        '{"entries":[{"source_id":"memory:7","quote":"饭做好了。"}]}',
        '{"entries":[{"source_id":"memory:999","quote":"热饭已经端上了桌。"}]}',
        '{"entries":[{"source_id":"memory:7","quote":"热饭已经端上了桌。","fictional":false}]}',
        '{"entries":[{"source_id":"memory:7","quote":""}]}',
        '{"entries":[{"source_id":"memory:7","quote":"热饭"},{"source_id":"memory:7","quote":"桌"}]}',
    ],
)
async def test_incomplete_rewritten_or_unproven_summary_is_not_saved(summary, raw):
    memory, llm, char = summary
    llm.raw = raw
    with pytest.raises(ValueError):
        await build(summary)
    assert not memory.status(char, SOURCES, 256, "model-a")["valid"]
    assert llm.counts == []


@pytest.mark.parametrize("tokens", [257, 0, -1, True, "120"])
async def test_actual_over_budget_or_invalid_measurement_fails_without_truncation(summary, tokens):
    memory, llm, char = summary
    llm.tokens = tokens
    with pytest.raises(ValueError, match="token预算"):
        await build(summary)
    assert not memory.status(char, SOURCES, 256, "model-a")["valid"]
    assert json.loads(llm.counts[0][0]["content"])["entries"][0]["quote"] == "热饭已经端上了桌。"


async def test_same_sources_reuse_and_new_sources_replace_one_row(summary):
    memory, llm, _ = summary
    first = await build(summary)
    assert await build(summary) == first
    assert len(llm.calls) == len(llm.counts) == 1
    changed = SOURCES + [{"id": "memory:8", "type": "public_memory", "fictional": True, "text": "新故事"}]
    await build(summary, sources=changed)
    assert len(llm.calls) == 2
    assert memory.store.one("SELECT COUNT(*) AS n FROM dynamic_memory_summaries")["n"] == 1


@pytest.mark.parametrize("change", ["source", "budget", "model", "version"])
async def test_invalid_preview_never_exposes_old_quote(summary, change):
    memory, _, char = summary
    await build(summary)
    sources, budget, model = SOURCES, 256, "model-a"
    if change == "source":
        sources = []
    elif change == "budget":
        budget = 512
    elif change == "model":
        model = "model-b"
    else:
        char = char | {"version": char["version"] + 1}
    status = memory.status(char, sources, budget, model)
    assert status["valid"] is False and status["entries"] == [] and status["token_count"] is None


async def test_revoked_during_token_count_cannot_commit(summary):
    memory, llm, char = summary
    allowed = True

    def revoke():
        nonlocal allowed
        allowed = False

    llm.after_count = revoke
    with pytest.raises(ValueError, match="授权已变化"):
        await build(summary, authorized=lambda: allowed)
    assert not memory.status(char, SOURCES, 256, "model-a")["valid"]


async def test_valid_cache_still_requires_current_authorization(summary):
    await build(summary)
    with pytest.raises(ValueError, match="授权已变化"):
        await build(summary, authorized=lambda: False)


async def test_empty_extract_is_valid_but_cannot_invent_memory(summary):
    memory, llm, _ = summary
    llm.raw = '{"entries":[]}'
    value = await build(summary)
    assert value["valid"] and memory.public_sources(value) == []


async def test_generation_schema_limits_structure_and_only_supplied_source_ids(summary):
    _, llm, _ = summary
    await build(summary)
    schema = llm.schemas[0]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["required"] == ["entries"]
    entries = schema["properties"]["entries"]
    assert entries["type"] == "array" and entries["maxItems"] <= 8
    item = entries["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == {"source_id", "quote"}
    assert item["properties"]["source_id"]["enum"] == ["memory:7"]
    assert item["properties"]["quote"]["type"] == "string"


@pytest.mark.parametrize(
    "echo",
    [
        {"public_sources": SOURCES},
        {"entries": [], "public_sources": SOURCES},
        [{"source_id": "memory:7", "quote": "热饭"}],
        '```json\n{"entries":[]}\n```',
    ],
)
async def test_nonconforming_provider_object_or_input_echo_never_becomes_memory(summary, monkeypatch, echo):
    memory, llm, char = summary

    async def incorrect(messages, schema, *, max_tokens):
        return echo

    monkeypatch.setattr(llm, "generate_json", incorrect)
    with pytest.raises(ValueError):
        await build(summary)
    assert llm.counts == [] and not memory.status(char, SOURCES, 256, "model-a")["valid"]
