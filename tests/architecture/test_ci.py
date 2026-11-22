import re
from pathlib import Path


def test_workbench_ci_uses_pinned_actions_and_read_only_permissions():
    workflow = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    actions = re.findall(r"uses:\s+(\S+)", workflow)
    assert actions and all(re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+@[0-9a-f]{40}", action) for action in actions)
    assert "permissions:\n  contents: read" in workflow
    assert not re.search(r":\s*write(?:-all)?\s*$", workflow, re.MULTILINE)
