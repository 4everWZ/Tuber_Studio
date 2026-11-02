"""Integrity of synthetic held-out routing data, without model or platform access."""

import json
import re
from pathlib import Path


def test_heldout_cases_preserve_existing_shape_and_source_reference_integrity():
    cases = json.loads(
        (Path(__file__).resolve().parents[1] / "evaluations/cases/operations-heldout.json").read_text(
            encoding="utf-8"
        )
    )
    assert 6 <= len(cases) <= 8 and len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert {"id", "data", "allowed_evidence_ids", "expected_actions", "criterion"} <= case.keys()
        assert set(case["expected_actions"]) <= {"reply_public", "handoff_private", "skip", "wait"}
        assert case["expected_actions"] and case["criterion"].strip()
        data = case["data"]
        assert data["kind"] in {"top_level", "reply_public"} and 0 < data["max_chars"] <= 500
        references = [item["id"] for item in data["evidence"]]
        assert references and len(set(references)) == len(references)
        assert set(references) == set(case["allowed_evidence_ids"])
        assert all(item["text"].strip() for item in data["evidence"])
        assert re.fullmatch(r"[1-9]\d{0,19}", data["thread"]["source"]["author_uid"])
        assert re.fullmatch(r"[1-9]\d{0,19}", data["metadata"]["owner_uid"])
