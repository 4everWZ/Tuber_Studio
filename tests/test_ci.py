"""Publication/asset safeguards run on CPU without Docker, GPUs or credentials."""

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from tuber_studio.inference_profiles import (
    DAFEIYU_9B_CONTEXT,
    DAFEIYU_9B_DIGESTS,
    DAFEIYU_9B_MODEL,
    DAFEIYU_9B_PROJECTOR,
    DAFEIYU_9B_WEIGHTS,
)

ROOT = Path(__file__).parents[1]


def tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools/ci" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "ref,event,expected",
    [
        ("refs/heads/main", "push", {"environment-r36.4"}),
        ("refs/heads/feature", "workflow_dispatch", set()),
        ("refs/tags/v1.2.3", "push", set()),
        ("refs/tags/v1.2.3-rc.1", "push", set()),
    ],
)
def test_tags_keep_default_pointers_off_release_and_manual_feature_builds(ref, event, expected):
    result = tool("image_metadata").image_tags("4everWZ/Tuber_Studio", "a" * 40, ref, event, "main", "b" * 64)
    tags = {tag.split(":")[-1] for tag in result["tags"].splitlines()}
    assert tags == {"env-" + "b" * 24} | expected
    assert result["image"] == "ghcr.io/4everwz/tuber_studio" and result["short_sha"] == "a" * 12


@pytest.mark.parametrize(
    "ref,event",
    [("refs/pull/4/merge", "pull_request"), ("refs/heads/feature", "push"), ("refs/tags/vbad", "push")],
)
def test_pull_requests_and_nonrelease_inputs_cannot_produce_publish_tags(ref, event):
    with pytest.raises(ValueError):
        tool("image_metadata").image_tags("owner/repo", "a" * 40, ref, event, "main", "b" * 64)


def test_only_ephemeral_hosted_runners_are_eligible_for_sdk_cleanup():
    helper = tool("runner_preflight")
    assert helper.hosted_cleanup_allowed({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"})
    assert not helper.hosted_cleanup_allowed({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"})
    assert not helper.hosted_cleanup_allowed({})
    with pytest.raises(ValueError):
        helper.cleanup_sdk(Path("/home/runner/work"))
    assert helper.MINIMUM_BYTES == 40 * 1024**3


def test_online_environment_extends_the_matching_base_instead_of_rebuilding_it():
    result = tool("image_metadata").image_tags(
        "owner/repo", "a" * 40, "refs/heads/main", "push", "main", "c" * 64, "b" * 64
    )
    assert result["reference"] == "ghcr.io/owner/repo:online-env-" + "c" * 24
    assert result["base_reference"] == "ghcr.io/owner/repo:env-" + "b" * 24


def context(tmp_path):
    assets = tmp_path / "ready"
    files = assets / "builtin/files"
    (files / "tts").mkdir(parents=True)
    model = {"model": DAFEIYU_9B_MODEL, "context_tokens": DAFEIYU_9B_CONTEXT}
    studio = {
        "llm": model,
        "tts": {"voice_id": "yaoyao_sovits_v2", "local_backend": "genie_onnx_int8"},
        "inference_broker": model | {"mode": "live", "kv_cache_type": "q4_0"},
    }
    entries = []
    for name, data in (("studio.json", json.dumps(studio).encode()), ("tts/tts_infer.yaml", b"custom: v2")):
        (files / name).write_bytes(data)
        entries.append(
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "policy": "preserve",
                "mode": 0o600,
            }
        )
    entries.extend(
        {
            "path": "models/" + filename,
            "bytes": 100,
            "sha256": DAFEIYU_9B_DIGESTS[role],
            "urls": ["https://huggingface.co/public/" + filename],
            "mode": 0o444,
        }
        for role, filename in (("model", DAFEIYU_9B_WEIGHTS), ("projector", DAFEIYU_9B_PROJECTOR))
    )
    (assets / "builtin/manifest.json").write_text(json.dumps({"format": 1, "files": entries}))
    (assets / "run-ready.sh").write_text("#!/bin/sh\n")
    return assets


def test_program_contract_accepts_only_small_presets_and_public_download_metadata(tmp_path):
    report = tool("audit_program").audit_assets(context(tmp_path))
    assert report["program_only"] and report["download_artifact_count"] == 2
    assert report["model_weights_embedded_in_builtin"] is False
    assert report["gpu_or_voice_acceptance_verified"] is False


@pytest.mark.parametrize("extra", ["builtin/files/weights.ckpt", "runtime/inference-token", "private.env"])
def test_extra_weights_or_installation_data_cannot_enter_named_context(tmp_path, extra):
    assets = context(tmp_path)
    path = assets / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not program metadata")
    with pytest.raises(ValueError):
        tool("audit_program").audit_assets(assets)


def test_public_manifest_rejects_embedded_url_credentials(tmp_path):
    assets = context(tmp_path)
    path = assets / "builtin/manifest.json"
    data = json.loads(path.read_text())
    data["files"][-1]["urls"] = ["https://huggingface.co/file?access_token=private"]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="private"):
        tool("audit_program").audit_assets(assets)


def test_fixed_action_revisions_and_publish_permission_scope():
    source = (ROOT / ".github/workflows/ci.yml").read_text()
    actions = re.findall(r"uses:\s+(\S+)", source)
    assert actions and all(
        re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+@[0-9a-f]{40}", action) for action in actions
    )
    yaml = pytest.importorskip("yaml")
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["image"]["permissions"] == {"contents": "read", "packages": "write"}
    assert "pull_request" in workflow["on"] and "workflow_dispatch" in workflow["on"]
    assert "workflow_dispatch" in workflow["jobs"]["image"]["if"]
    assert "github.event_name == 'push'" in workflow["jobs"]["image"]["if"]
    steps = workflow["jobs"]["image"]["steps"]
    build = next(step for step in steps if step.get("id") == "build")
    assert build["if"] == "steps.lookup.outputs.missing == 'true'"
    assert build["with"]["build-contexts"] == "environment_contract=tmp/ci-environment"


def test_jetson_release_requires_independent_cpu_and_program_verification():
    yaml = pytest.importorskip("yaml")
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs = yaml.load(source, Loader=yaml.BaseLoader)["jobs"]
    prereqs = {"checks", "program-package"}
    for name in prereqs:
        assert not jobs[name].get("needs")
        assert "if" not in jobs[name]
    for name in ("program-release", "image"):
        assert set(jobs[name]["needs"]) == prereqs
        assert not any(
            status in jobs[name].get("if", "") for status in ("always()", "failure()", "cancelled()")
        )
    assert set(jobs["online-image"]["needs"]) == {"image", "song-component"}

    package_steps = jobs["program-package"]["steps"]
    package_commands = "\n".join(step.get("run", "") for step in package_steps)
    assert all(
        command in package_commands
        for command in (
            "tools/jetson/package-ready.py",
            "tools/ci/audit_program.py",
            "tools/ci/package_program.py",
        )
    )
    candidate = next(
        step["with"] for step in package_steps if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    download, publish = jobs["program-release"]["steps"]
    assert download["uses"].startswith("actions/download-artifact@")
    assert publish["uses"].startswith("actions/upload-artifact@")
    assert download["with"]["name"] == candidate["name"] == "jetson-program-candidate-${{ github.sha }}"
    assert {"run-id", "repository", "github-token"}.isdisjoint(download["with"])
    assert publish["with"]["name"] == "jetson-program-${{ github.sha }}"
    assert {Path(path).name for path in publish["with"]["path"].splitlines()} == {
        "program.tar.gz",
        "run-ready.sh",
        "program-evidence.json",
    }
