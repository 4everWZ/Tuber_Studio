from copy import deepcopy

from tuber_studio.agent.task_tools import apply_repair_candidate_receipts


def test_same_rejected_fact_survives_another_fields_visual_pass_on_its_original_frame():
    fact = "中央人物头戴红色头盔"
    prior = {"field": "items.0.title", "stage": "review_repair_candidate", "primary_frame": "a:1",
             "candidate": fact, "claims": [{"target": "头盔"}], "unlocated": [{"modifiers": ["读者改写"]}]}
    path = "items.0.rationale"
    text = f"画面依据：{fact}。剪辑安排：用于开场。"
    fresh = {path: {"field": path, "text": text, "primary_frame": "a:1", "claims": [{"target": "红色"}]}}
    snapshot = deepcopy(prior)
    apply_repair_candidate_receipts(fresh, {path: text}, "a:1", [prior, prior])
    assert fresh[path]["claims"] == [{"target": "红色"}, {"target": "头盔"}]
    assert "unlocated" not in fresh[path]
    assert fresh[path]["repair_candidate_sources"] == [{"field": "items.0.title", "candidate": fact}]
    assert prior == snapshot
    originally_clear = {}
    apply_repair_candidate_receipts(originally_clear, {path: text}, "a:1", [prior])
    assert originally_clear[path]["claims"] == prior["claims"]
    unlocated_only = {}
    apply_repair_candidate_receipts(unlocated_only, {path: text}, "a:1", [prior | {"claims": []}])
    assert unlocated_only == {}

    for frame, field, other in [
        ("a:2", path, text),
        ("a:1", path, "画面依据：中央人物头戴红色帽子。剪辑安排：用于开场。"),
        ("a:1", path, "画面依据：轮廓可见。剪辑安排：" + fact),
        ("a:1", "items.0.title", "能看清是不是头盔吗？"),
    ]:
        decisions = {}
        apply_repair_candidate_receipts(decisions, {field: other}, frame, [prior])
        assert decisions == {}
