import copy
import importlib
import json

import httpx
import pytest

from evaluations.dafeiyu_holdout import (
    DEFAULT_CASES,
    DEFAULT_MANIFEST,
    ROOT,
    audit_sources,
    cases_digest,
    load_frozen,
    messages_for_turn,
    prepare_blind,
    replay_protocol,
    run_endpoint,
    score_packet,
    select_replay_cases,
    write_jsonl,
)


def test_frozen_holdout_and_independent_training_sources(tmp_path, monkeypatch):
    cases, digest = load_frozen(DEFAULT_CASES, DEFAULT_MANIFEST)
    training = ROOT / "tools/training"
    monkeypatch.syspath_prepend(str(training))
    compiled = tmp_path / "compiled"
    importlib.import_module("build_authentic_sft_dataset").build_dataset(training, compiled)
    audit = audit_sources(cases, [compiled / "sft_train.json", compiled / "sft_val.json"], training)
    assert not audit["collisions"]
    assert audit == json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))["independence"]
    assert digest == cases_digest(cases)

    source = tmp_path / "compiled.json"
    prompt = cases[0]["turns"][0]["text"]
    source.write_text(json.dumps([{"messages": [{"role": "user", "content": prompt + "。"}]}]), encoding="utf-8")
    audit = audit_sources(cases, [source], None)
    assert audit["collisions"] == [{"case": cases[0]["id"], "turn": 1, "kind": "exact", "sources": ["compiled-1:1"]}]

    cases[0]["turns"][0]["text"] = "与冻结题不一致"
    edited = tmp_path / DEFAULT_CASES.name
    write_jsonl(edited, cases)
    with pytest.raises(ValueError, match="manifest"):
        load_frozen(edited, DEFAULT_MANIFEST)


def test_protocol_keeps_rubrics_out_and_preserves_prior_replies():
    cases, _ = load_frozen(DEFAULT_CASES, DEFAULT_MANIFEST)
    protocol = replay_protocol()
    general = next(case for case in cases if case["suite"] == "general")
    oc = next(case for case in cases if case["suite"] == "oc")
    assert "大肥鱼" not in messages_for_turn(general, [], protocol)[0]["content"]
    messages = messages_for_turn(oc, ["自己的上一轮回复"], protocol)
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[2]["content"] == "自己的上一轮回复"
    assert messages[-1]["content"].startswith(oc["turns"][1]["speaker"] + "：")
    assert all(oc["criteria"][0] not in message["content"] for message in messages)


def replay_bundle(cases, holdout, condition, suites):
    rows = []
    for case in cases:
        if case["suite"] not in suites:
            continue
        text = "占位回复，仅用于计分测试"
        rows.append({"id": case["id"], "replies": [{"text": text, "finish_reason": "stop"} for _ in case["turns"]]})
    return {
        "holdout_sha256": holdout,
        "protocol_sha256": cases_digest(replay_protocol()),
        "artifact": condition,
        "base_revision": "base-rev" if condition != "deployed" else "deployed-rev",
        "rows": rows,
    }


def test_paired_scoring_honors_general_per_category_and_oc_thresholds():
    cases, digest = load_frozen(DEFAULT_CASES, DEFAULT_MANIFEST)
    base = replay_bundle(cases, digest, "base", {"general"})
    deployed = replay_bundle(cases, digest, "deployed", {"oc"})
    candidate = replay_bundle(cases, digest, "candidate", {"general", "oc"})
    packet, key = prepare_blind(cases, digest, base, deployed, candidate, seed=1)
    assert [row["id"] for row in packet] == [row["id"] for row in select_replay_cases(cases)]
    assert all("candidate_position" not in row for row in packet)
    assert sum(evidence["candidate_position"] == "a" for evidence in key["cases"].values()) == 80
    with pytest.raises(ValueError, match="missing"):
        score_packet(cases, digest, packet, key)

    reading_errors, oc_wins = 0, 0
    for row in packet:
        position = key["cases"][row["id"]]["candidate_position"]
        if row["suite"] == "oc":
            row["grading"]["preference"] = position if oc_wins < 24 else "tie"
            oc_wins += 1
        else:
            row["grading"] = {"a_correct": True, "b_correct": True}
            if row["category"] == "reading" and reading_errors < 3:
                row["grading"][f"{position}_correct"] = False
                reading_errors += 1
    report = score_packet(cases, digest, packet, key)
    assert report["general"]["additional_errors"] == 3
    assert report["general"]["categories"]["reading"]["additional_errors"] == 3
    assert not report["general"]["pass"]
    assert report["oc"] == {"wins": 24, "ties": 16, "losses": 0, "pass": True}
    assert not report["release_holdout_gate_pass"]

    row = packet[0]
    row["grading"][f"{key['cases'][row['id']]['candidate_position']}_correct"] = True
    report = score_packet(cases, digest, packet, key)
    assert report["general"]["additional_errors"] == 2
    assert report["release_holdout_gate_pass"]
    assert len(report["details"]) == 160
    assert "synthetic_operations" not in report

    changed = copy.deepcopy(packet)
    changed[0]["a"][0] = "替换了实际输出"
    with pytest.raises(ValueError, match="changed"):
        score_packet(cases, digest, changed, key)


def test_endpoint_replay_keeps_completed_cases_across_transport_failure(tmp_path, monkeypatch):
    cases, digest = load_frozen(DEFAULT_CASES, DEFAULT_MANIFEST)
    cases = cases[:2]
    protocol = replay_protocol()
    actual_client = httpx.Client
    sent = []

    def endpoint(request):
        sent.append(json.loads(request.content))
        if len(sent) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "测试回复"}, "finish_reason": "stop"}],
        })

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: actual_client(transport=httpx.MockTransport(endpoint), **kwargs))
    output = tmp_path / "result.json"
    args = {"endpoint": "http://127.0.0.1:8080/v1", "model": "synthetic",
            "artifact": "model-base", "base_revision": "test-rev", "output": output}
    with pytest.raises(ValueError, match="incomplete replay at"):
        run_endpoint(cases, digest, protocol, **args)
    assert not output.exists()
    assert output.with_name("result.json.partial").is_file()
    run_endpoint(cases, digest, protocol, **args)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["case_ids"] == [case["id"] for case in cases]
    assert [row["id"] for row in result["rows"]] == result["case_ids"]
    assert len(sent) == 3
    assert sent[0]["messages"][-1]["content"] != sent[2]["messages"][-1]["content"]
