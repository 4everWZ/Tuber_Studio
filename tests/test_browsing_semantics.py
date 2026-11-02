"""Native product-schema evaluation stays isolated and keeps explicit budgets."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "browsing_semantics", ROOT / "evaluations/browsing_semantics.py"
)
semantics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(semantics)


@pytest.mark.asyncio
async def test_reason_label_variant_changes_only_enum_literal_and_keeps_private_model_choice(monkeypatch):
    from tuber_studio.browsing_models import response_schema

    provider = semantics.LlamaCppProvider({"base_url": "http://127.0.0.1:19030"})
    original = response_schema(["11:0"], maximum=60)
    before = deepcopy(original)
    expected = deepcopy(original)
    for branch in expected["oneOf"]:
        reasons = branch["properties"]["reason"]["enum"]
        reasons[:] = ["explicit_private_request" if value == "user_requested" else value for value in reasons]
    raw = {
        "reason": "explicit_private_request",
        "action": "handoff_private",
        "evidence_ids": ["11:0"],
        "text": "模型选择私聊",
    }
    sent = []

    async def generate(messages, schema, *, max_tokens):
        sent.append((deepcopy(messages), deepcopy(schema), max_tokens))
        return raw

    monkeypatch.setattr(provider, "generate_json", generate)
    observations = []
    semantics.install_private_label_experiment(provider, observations)
    messages = [{"role": "user", "content": "普通公开提问，不替模型改写渠道"}]
    try:
        actual = await provider.generate_json(messages, original, max_tokens=4096)
    finally:
        await provider.close()
    assert original == before
    assert sent == [(messages, expected, 4096)]
    assert actual == {**raw, "reason": "user_requested"}
    assert observations == [{"raw_model_decision": raw, "canonical_decision": actual}]
    assert raw["reason"] == "explicit_private_request"


@pytest.fixture
def evaluation_args(tmp_path, monkeypatch):
    production = tmp_path / "production"
    production.mkdir()
    config = json.loads((ROOT / "config/studio.example.json").read_text())
    config["llm"].update(
        model="original-model", temperature=0.65, enable_thinking=True, api_key="fixture-private-credential"
    )
    (production / "studio.json").write_text(json.dumps(config), encoding="utf-8")
    (production / "studio.sqlite3").write_bytes(b"production-database-untouched")
    (production / "admin-token").write_text("fixture-production-secret", encoding="utf-8")
    monkeypatch.setenv("TUBER_CONFIG", str(production / "studio.json"))
    monkeypatch.setenv("TUBER_RUNTIME", str(production))
    args = semantics.arguments(
        [
            "--root",
            str(ROOT),
            "--runtime",
            str(tmp_path / "evaluation"),
            "--cases",
            str(ROOT / "evaluations/cases/browsing-semantics.json"),
            "--output",
            str(tmp_path / "report.json"),
        ]
    )
    return args, production, config


def test_default_cli_keeps_configured_sampling_thinking_and_decision_budget(evaluation_args):
    args, production, config = evaluation_args
    before = {path.name: path.read_bytes() for path in production.iterdir()}
    settings, policy, overrides = semantics.evaluation_settings(args)
    assert settings.data["llm"] == config["llm"]
    assert policy["decision_max_tokens"] == 2048
    assert settings.data["llm"]["max_tokens"] == 1024
    assert overrides == {}
    assert args.response_strategy == "combined"
    assert all(
        getattr(args, key) is None
        for key in ("top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")
    )
    assert {path.name: path.read_bytes() for path in production.iterdir()} == before
    args.decision_max_tokens = 4096
    _, policy, overrides = semantics.evaluation_settings(args)
    assert policy["decision_max_tokens"] == overrides["decision_max_tokens"] == 4096


def test_stage_budget_defaults_and_invalid_allowances_do_not_change_combined(evaluation_args):
    args, _, _ = evaluation_args
    args.response_strategy = "staged"
    _, policy, _ = semantics.evaluation_settings(args)
    assert policy["decision_max_tokens"] == 4096
    assert semantics.strategy_options(args, 4096)["source_guard_mode"] == "necessary"
    args.decision_max_tokens = 8192
    with pytest.raises(ValueError, match="4096"):
        semantics.evaluation_settings(args)
    args.decision_max_tokens = 2048
    args.response_strategy = "evidence_first"
    with pytest.raises(ValueError, match="leave room"):
        semantics.evaluation_settings(args)
    args.response_strategy = "combined"
    assert semantics.evaluation_settings(args)[1]["decision_max_tokens"] == 2048


def test_file_and_package_cli_help_and_staged_import_work(tmp_path):
    script = ROOT / "evaluations/browsing_semantics.py"
    for command, cwd in [
        ([sys.executable, str(script), "--help"], tmp_path),
        ([sys.executable, "-m", "evaluations.browsing_semantics", "--help"], ROOT),
    ]:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
        assert "--response-strategy" in completed.stdout and "evidence_first" in completed.stdout
    # A file entrypoint has its own directory on sys.path; test that actual import mode too.
    command = [
        sys.executable,
        "-c",
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "import browsing_semantics as m;print(m.staged_evaluator().__name__)",
        str(script.parent),
    ]
    imported = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "evaluate_staged_case"


def test_production_runtime_output_and_native_window_mislabels_are_rejected(evaluation_args):
    args, production, _ = evaluation_args
    original_runtime, original_output = args.runtime, args.output
    args.runtime = production
    with pytest.raises(ValueError, match="dedicated"):
        semantics.evaluation_settings(args)
    args.runtime, args.output = original_runtime, production / "studio.json"
    with pytest.raises(ValueError, match="overwrite"):
        semantics.evaluation_settings(args)
    args.output = original_output
    args.context_tokens, args.native_context_tokens = 131072, 128000
    with pytest.raises(ValueError, match="上下文"):
        semantics.evaluation_settings(args)
    args.context_tokens = 128000
    settings, policy, _ = semantics.evaluation_settings(args)
    assert settings.data["llm"]["native_context_tokens"] == 128000
    assert policy["decision_max_tokens"] == 2048


@pytest.mark.asyncio
@pytest.mark.parametrize("official_sampling", [False, True])
@pytest.mark.parametrize("private_label", ["user_requested", "explicit_private_request"])
async def test_same_twelve_cases_use_real_native_provider_json_with_isolated_overrides(
    evaluation_args, tmp_path, monkeypatch, official_sampling, private_label
):
    args, production, source_config = evaluation_args
    before = {path.name: path.read_bytes() for path in production.iterdir()}
    args.model_base_url, args.model = "http://127.0.0.1:19030", "candidate"
    args.context_tokens, args.native_context_tokens = 131072, 262144
    args.thinking, args.temperature, args.seed = "on", 0.2, 42
    if official_sampling:
        args.temperature, args.top_p, args.top_k, args.min_p = 1.0, 0.95, 20, 0.0
        args.presence_penalty, args.repeat_penalty = 1.5, 1.0
    args.decision_max_tokens = 4096
    args.private_request_label = private_label
    card = tmp_path / "character.json"
    card.write_text(
        json.dumps(
            {
                "core": {"identity": "public fixture persona"},
                "style": {"min_chars": 1, "max_chars": 60},
                "rules": ["public rule"],
                "lore": [{"text": "private-lore-must-not-import"}],
                "private_memory": "private-memory-must-not-import",
            }
        ),
        encoding="utf-8",
    )
    args.character_file = card
    requests, generations, generation_bytes = [], [], []

    def handler(request):
        payload = json.loads(request.content) if request.content else None
        requests.append((request.url.path, payload))
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": 131072}, "model_alias": "candidate"}
            )
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(payload["messages"])})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(17))})
        if request.url.path == "/v1/chat/completions":
            generations.append(payload)
            generation_bytes.append(request.content)
            decision = {"reason": "insufficient_evidence", "action": "skip", "evidence_ids": [], "text": ""}
            for branch in payload["response_format"]["schema"]["oneOf"]:
                props = branch["properties"]
                if "explicit_private_request" in props["reason"]["enum"]:
                    decision = {
                        "reason": "explicit_private_request",
                        "action": "handoff_private",
                        "evidence_ids": props["evidence_ids"]["items"]["enum"][:1],
                        "text": "保留模型的原始渠道选择。",
                    }
                    break
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(decision)},
                        }
                    ],
                    "usage": {"prompt_tokens": 17, "completion_tokens": 7},
                },
            )
        pytest.fail("Evaluation attempted an unexpected network or platform endpoint: " + request.url.path)

    original_client = httpx.AsyncClient

    def controlled_client(*positional, **keywords):
        keywords["transport"] = httpx.MockTransport(handler)
        return original_client(*positional, **keywords)

    monkeypatch.setattr(httpx, "AsyncClient", controlled_client)
    result = await semantics.evaluate(args)
    assert result["provider_class"] == "LlamaCppProvider"
    assert len(result["results"]) == len(generations) == 12
    assert all(item["structural_complete"] and item["elapsed_ms"] > 0 for item in result["results"])
    assert all(item["budget"]["model_calls"] == 1 for item in result["results"])
    for item, body in zip(result["results"], generation_bytes, strict=True):
        audit = item["generation_request_audit"]
        assert len(audit) == 1 and audit[0]["payload_sha256"] == hashlib.sha256(body).hexdigest()
        assert audit[0]["max_tokens"] == 4096
        assert len(audit[0]["messages_sha256"]) == len(audit[0]["response_format_sha256"]) == 64
    expected_instruction = semantics.RESPONSE_INSTRUCTION.replace("user_requested", private_label)
    assert (
        result["loaded_instruction_hashes"]["response_instruction_sha256"]
        == hashlib.sha256(expected_instruction.encode()).hexdigest()
    )
    assert all(expected_instruction in json.dumps(item, ensure_ascii=False) for item in generations)
    if private_label == "explicit_private_request":
        decisions = [item["reason_label_observations"][0] for item in result["results"]]
        for decision in decisions:
            raw, canonical = decision["raw_model_decision"], decision["canonical_decision"]
            expected = deepcopy(raw)
            if raw["reason"] == "explicit_private_request":
                expected["reason"] = "user_requested"
            assert canonical == expected
        # Deliberately wrong model choices remain wrong; the adapter never forces public.
        assert any(
            "reply_public" in case["expected_actions"] and item["actual"]["action"] == "handoff_private"
            for item, case in zip(
                result["results"], json.loads(args.cases.read_text(encoding="utf-8")), strict=True
            )
        )
    else:
        assert all("reason_label_observations" not in item for item in result["results"])
    assert all(item["max_tokens"] == 4096 for item in generations)
    assert all(item["seed"] == 42 and item["temperature"] == args.temperature for item in generations)
    if official_sampling:
        for key in ("top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"):
            assert all(item[key] == getattr(args, key) for item in generations)
            assert result["explicit_overrides"][key] == getattr(args, key)
    assert all(item["chat_template_kwargs"]["enable_thinking"] for item in generations)
    assert all(item["response_format"]["schema"]["title"] == "ResponseDecision" for item in generations)
    assert all("public fixture persona" in json.dumps(item) for item in generations)
    assert "private-lore-must-not-import" not in json.dumps(requests)
    assert "private-memory-must-not-import" not in json.dumps(requests)
    assert result["llm_config"]["max_tokens"] == source_config["llm"]["max_tokens"]
    assert result["explicit_overrides"]["decision_max_tokens"] == 4096
    assert result["context_contract"]["native_context_tokens"] == 262144
    assert result["production_configuration_written"] is False
    assert result["platform_messages_sent"] == 0
    assert "text-only" in result["evaluation_scope"]
    assert "fixture-private-credential" not in args.output.read_text()
    assert {path.name: path.read_bytes() for path in production.iterdir()} == before
    gold = json.loads(args.cases.read_text(encoding="utf-8"))
    assert [item["id"] for item in result["results"]] == [item["id"] for item in gold]
    assert all(item["semantic_review"]["status"] == "pending" for item in result["results"])
    assert all(item["semantic_review"]["oc_interaction"] is None for item in result["results"])
    assert all(item["generation_outputs"] for item in result["results"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("top_p", float("nan")),
        ("min_p", 1.1),
        ("presence_penalty", 2.1),
        ("repeat_penalty", 0),
        ("top_k", -1),
    ],
)
def test_explicit_sampling_validation_does_not_touch_production(evaluation_args, key, value):
    args, production, _ = evaluation_args
    before = {path.name: path.read_bytes() for path in production.iterdir()}
    setattr(args, key, value)
    with pytest.raises(ValueError, match=key):
        semantics.evaluation_settings(args)
    assert {path.name: path.read_bytes() for path in production.iterdir()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["staged", "evidence_first"])
async def test_staged_cli_uses_native_stages_and_one_shared_retry_without_changing_gold(
    evaluation_args, monkeypatch, strategy
):
    args, production, _ = evaluation_args
    before = {path.name: path.read_bytes() for path in production.iterdir()}
    args.model_base_url, args.model = "http://127.0.0.1:19030", "candidate"
    args.thinking, args.temperature, args.seed = "on", 0.2, 42
    args.response_strategy = strategy
    calls, first_bad_body = [], True

    def handler(request):
        nonlocal first_bad_body
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": 131072}, "model_alias": "candidate"}
            )
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(payload["messages"])})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(17))})
        assert request.url.path == "/v1/chat/completions"
        calls.append(payload)
        schema = payload.get("response_format", {}).get("schema")
        source = next(item["content"] for item in payload["messages"] if item["role"] == "user")
        data = json.loads(source.split("：", 1)[1])
        if schema and schema["title"] == "EvidencePacket":
            evidence = data.get("evidence", [])
            content = json.dumps(
                {
                    "answerability": "answerable" if evidence else "insufficient",
                    "missing": [],
                    "quotes": [{"evidence_id": evidence[0]["id"], "quote": evidence[0]["text"]}]
                    if evidence
                    else [],
                }
            )
        elif schema:
            assert schema["title"] == "StagedDecision"
            evidence = data.get("evidence", [])
            content = json.dumps(
                {
                    "action": "reply_public" if evidence else "skip",
                    "reason": "specific_detail" if evidence else "insufficient_evidence",
                    "evidence_ids": [evidence[0]["id"]] if evidence else [],
                }
            )
        elif first_bad_body:
            first_bad_body = False
            content = ""
        else:
            content = "鱼鱼看完实验啦，我们慢慢聊呀。"
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 10, "total_tokens": 27},
            },
        )

    original_client = httpx.AsyncClient

    def controlled_client(*positional, **keywords):
        keywords["transport"] = httpx.MockTransport(handler)
        return original_client(*positional, **keywords)

    monkeypatch.setattr(httpx, "AsyncClient", controlled_client)
    report = await semantics.evaluate(args)
    assert report["response_strategy"] == strategy
    assert report["strategy_parameters"]["total_output_tokens"] == 4096
    assert report["strategy_parameters"]["source_guard_mode"] == "necessary"
    assert len(report["results"]) == 12
    for case in report["results"]:
        assert case["structural_complete"] and case["pipeline_acceptance"]
        assert case["usage_complete"]
        assert case["usage"]["completion_tokens"] == 10 * len(case["stages"])
        assert case["usage"]["completion_tokens"] == case["budget_delta"]["output_tokens"] <= 4096
        assert case["budget"]["model_calls"] == len(case["stages"])
        assert case["retries_used"] <= 1
        assert all(stage["elapsed_ms"] > 0 and stage["usage"] for stage in case["stages"])
        assert "raw_model_action_matches" in case and "final_model_action_matches" in case
    first = report["results"][0]
    assert first["retries_used"] == 1
    assert first["stages"][-2]["error"]
    visual = next(case for case in report["results"] if case["id"] == "visual-dependent")
    assert visual["raw_model_action_matches"] is False
    assert visual["action_matches"] is False  # No hidden conservative gate improves the score.
    assert any("response_format" not in call and "tools" not in call for call in calls)
    assert all(call["seed"] == 42 and call["temperature"] == 0.2 for call in calls)
    assert {path.name: path.read_bytes() for path in production.iterdir()} == before
    gold = json.loads(args.cases.read_text(encoding="utf-8"))
    assert [case["id"] for case in report["results"]] == [case["id"] for case in gold]
