"""A real-review cohort counts consumed source material, never titles or starts."""

import json

from evaluations.live_review import material_proof


def fixture_material(tmp_path):
    value = {"metadata": {"bvid": "BV1RLH367EQN", "parts": [{"cid": "123"}]},
             "source_version": "actual-source-version", "coverage": "all_available_cc",
             "parts": [{"bvid": "BV1RLH367EQN", "cid": "123", "segments": [
                 {"id": "123:track:0", "start": 0, "end": 3, "text": "盐水里鸡蛋浮起来了"}]}]}
    path = tmp_path / "source.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    row = {"status": "viewed_without_comment", "path": path.name, "bvid": "BV1RLH367EQN",
           "progress_material": json.dumps(value), "decisions": '[{"reason":"nothing_to_add"}]',
           "main_decision": None, "material_key": "source", "read_state": "complete"}
    return path, row, value


def test_review_counts_a_finished_source_bound_view_separately_from_full_material_coverage(tmp_path):
    _, row, _ = fixture_material(tmp_path)
    proof = material_proof(row, tmp_path)
    assert proof["source_segments"] == 1 and proof["cids"] == ["123"]
    assert proof["fully_read_available_material"]
    assert not material_proof(row | {"read_state": "reading"}, tmp_path)["fully_read_available_material"]


def test_review_rejects_titles_starts_unconsumed_caches_and_other_video_sources(tmp_path):
    path, row, value = fixture_material(tmp_path)
    assert material_proof(row | {"status": "evidence_ready"}, tmp_path) is None
    assert material_proof(row | {"decisions": "[]"}, tmp_path) is None
    assert material_proof(row | {"bvid": "another-video"}, tmp_path) is None
    path.write_text(json.dumps(value | {"parts": []}), encoding="utf-8")
    assert material_proof(row, tmp_path) is None
