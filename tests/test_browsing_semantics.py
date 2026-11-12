"""Native product-schema evaluation stays isolated and keeps explicit budgets."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "browsing_semantics", ROOT / "evaluations/browsing_semantics.py"
)
semantics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(semantics)


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
    assert policy["decision_max_tokens"] == 8192
    assert settings.data["llm"]["max_tokens"] == 1024
    assert overrides == {}
    assert all(
        getattr(args, key) is None
        for key in ("top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")
    )
    assert {path.name: path.read_bytes() for path in production.iterdir()} == before
    args.decision_max_tokens = 4096
    _, policy, overrides = semantics.evaluation_settings(args)
    assert policy["decision_max_tokens"] == overrides["decision_max_tokens"] == 4096


def test_file_and_package_cli_help(tmp_path):
    script = ROOT / "evaluations/browsing_semantics.py"
    for command, cwd in [
        ([sys.executable, str(script), "--help"], tmp_path),
        ([sys.executable, "-m", "evaluations.browsing_semantics", "--help"], ROOT),
    ]:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
        assert "--cases" in completed.stdout and "--decision-max-tokens" in completed.stdout


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
    assert policy["decision_max_tokens"] == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize("official_sampling", [False, True])
async def test_public_cases_use_real_native_provider_json_with_isolated_overrides(
    evaluation_args, tmp_path, monkeypatch, official_sampling
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
    gold = json.loads(args.cases.read_text(encoding="utf-8"))
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
            case = gold[len(generations)]
            generations.append(payload)
            generation_bytes.append(request.content)
            decision = {
                "reason": "insufficient_evidence",
                "explanation": "没有足够的视频细节来接话。",
                "action": "skip",
                "evidence_ids": [],
                "text": "",
            }
            if case["id"] == "light-interaction":
                source_alias = payload["response_format"]["schema"]["$defs"]["evidence_id"]["enum"][0]
                decision = {
                    "reason": "light_interaction",
                    "explanation": "对方也喜欢这段实验，可以顺着接话。",
                    "action": "reply_public",
                    "evidence_ids": [source_alias],
                    "text": "嘿嘿，鸡蛋漂起来那一下，本鱼也看开心了。",
                }
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
    assert len(result["results"]) == len(generations) == len(gold) == 11
    assert all(item["structural_complete"] and item["elapsed_ms"] > 0 for item in result["results"])
    assert all(item["budget"]["model_calls"] == 1 for item in result["results"])
    assert all(item["action_matches"] and item["references_valid"] for item in result["results"])
    for item, body in zip(result["results"], generation_bytes, strict=True):
        audit = item["generation_request_audit"]
        assert len(audit) == 1 and audit[0]["payload_sha256"] == hashlib.sha256(body).hexdigest()
        assert audit[0]["max_tokens"] == 4096
        assert len(audit[0]["messages_sha256"]) == len(audit[0]["response_format_sha256"]) == 64
    expected_instruction = semantics.RESPONSE_INSTRUCTION
    assert (
        result["loaded_instruction_hashes"]["response_instruction_sha256"]
        == hashlib.sha256(expected_instruction.encode()).hexdigest()
    )
    assert all(expected_instruction in json.dumps(item, ensure_ascii=False) for item in generations)
    assert all(item["max_tokens"] == 4096 for item in generations)
    assert all(item["seed"] == 42 and item["temperature"] == args.temperature for item in generations)
    if official_sampling:
        for key in ("top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"):
            assert all(item[key] == getattr(args, key) for item in generations)
            assert result["explicit_overrides"][key] == getattr(args, key)
    assert all(item["chat_template_kwargs"]["enable_thinking"] for item in generations)
    assert all(item["response_format"]["schema"]["title"] == "ResponseDecision" for item in generations)
    assert all(
        "handoff_private" not in branch["properties"].get("action", {}).get("enum", [])
        for item in generations
        for branch in item["response_format"]["schema"]["oneOf"]
    )
    assert all(
        any("web_fetch" in branch["properties"] for branch in item["response_format"]["schema"]["oneOf"])
        for item in generations
    )
    assert all("public fixture persona" in json.dumps(item) for item in generations)
    assert "private-lore-must-not-import" not in json.dumps(requests)
    assert "private-memory-must-not-import" not in json.dumps(requests)
    assert result["llm_config"]["max_tokens"] == source_config["llm"]["max_tokens"]
    assert result["explicit_overrides"]["decision_max_tokens"] == 4096
    assert result["context_contract"]["native_context_tokens"] == 262144
    assert result["production_configuration_written"] is False
    assert result["platform_messages_sent"] == 0
    assert "text-only" in result["evaluation_scope"].casefold()
    assert "fixture-private-credential" not in args.output.read_text()
    assert {path.name: path.read_bytes() for path in production.iterdir()} == before
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
