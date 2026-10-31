"""Environment identity, public program bundle and local-first launcher contracts."""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(relative):
    spec = importlib.util.spec_from_file_location(Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "project"
    for name in (
        "pyproject.toml",
        "uv.lock",
        "tools/jetson/Dockerfile",
        "tools/jetson/tts/requirements.txt",
        "tools/jetson/tts/genie-requirements.txt",
        "third_party/Genie-TTS/dependency.py",
        "components/song_requests/Dockerfile",
        "components/song_requests/package.json",
        "components/song_requests/package-lock.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("environment input\n")
    for name in (
        "src/tuber_studio/portable.py",
        "config/oc.json",
        "assets/avatar.svg",
        "components/song_requests/stdio.cjs",
        "evaluations/semantic-cases.json",
        "evaluations/speech-cases.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("program file\n")
    return root


def test_code_edits_reuse_identity_but_dependency_changes_require_new_environment(source):
    helper = load("tools/jetson/environment.py")
    before = helper.contract(source)
    (source / "src/tuber_studio/portable.py").write_text("updated application\n")
    (source / "config/oc.json").write_text("updated role\n")
    assert helper.contract(source) == before
    installed = source / "installed.json"
    installed.write_text(json.dumps(before))
    assert helper.verify(source, installed) == before
    wanted = source / "components/song_requests/package-lock.json"
    wanted.write_text('{"lockfileVersion": 3}')
    node_lock = source / "image-node-lock.json"
    shutil.copyfile(wanted, node_lock)
    assert helper.verify(source, installed, node_lock) == before
    wanted.write_text('{"lockfileVersion": 3, "packages": {"updated": {}}}')
    with pytest.raises(ValueError, match="Optional Node"):
        helper.verify(source, installed, node_lock)
    lock = source / "uv.lock"
    lock.write_bytes(lock.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert helper.contract(source) == before  # Windows checkouts keep the same environment identity.
    lock.write_text("changed locked dependency\n")
    assert helper.contract(source)["sha256"] != before["sha256"]
    with pytest.raises(ValueError, match="differs"):
        helper.verify(source, installed)


def test_program_archive_excludes_runtime_and_untracked_secrets(source):
    helper = load("tools/ci/package_program.py")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / ".env").write_text("PRIVATE")
    (source / "runtime").mkdir()
    (source / "runtime/admin-token").write_text("PRIVATE")
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    (source / "src/untracked-secret.py").write_text("PRIVATE")
    result = helper.package(source, source / "tmp/program.tar.gz")
    with tarfile.open(result["program_archive"]) as archive:
        names = archive.getnames()
        assert not any("runtime" in name or ".env" in name or "untracked" in name for name in names)
        assert "program/src/tuber_studio/portable.py" in names
        assert (
            archive.extractfile("program/environment-image.txt").read().decode().strip()
            == result["environment_image"]
        )
        assert (
            archive.extractfile("program/online-environment-image.txt").read().decode().strip()
            == result["online_environment_image"]
        )
        assert all(b"PRIVATE" not in archive.extractfile(name).read() for name in names)


def test_environment_dockerfile_never_copies_application_or_presets():
    text = (ROOT / "tools/jetson/Dockerfile").read_text()
    for forbidden in (
        "COPY src",
        "COPY config",
        "COPY assets",
        "ready_assets",
        "COPY tools/evaluate",
        "COPY --from=ready_assets",
    ):
        assert forbidden not in text
    assert "COPY --from=environment_contract /environment.json /opt/tuber-environment.json" in text


def test_preparation_reuses_public_metadata_and_preserves_operator_files(source, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools/jetson"))
    helper = load("tools/jetson/prepare-launch.py")
    environment = load("tools/jetson/environment.py")
    for name in (
        "config/jetson/downloads.json",
        "config/jetson/tts_infer.yaml",
        "config/studio.example.json",
        "tools/jetson/package-ready.py",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    installed = source / "installed.json"
    installed.write_text(json.dumps(environment.contract(source)))
    monkeypatch.setattr(helper, "verify", lambda root: environment.verify(root, installed))
    runtime = source / "runtime"
    runtime.mkdir()
    original = {
        "studio.json": b"OPERATOR CONFIG",
        "admin-token": b"PRIVATE TOKEN",
        "studio.sqlite3": b"OPERATOR DATABASE",
    }
    for name, content in original.items():
        (runtime / name).write_bytes(content)
    key = helper.prepare(source, runtime)
    preset = runtime / ".portable-launch" / key / "files/studio.json"
    timestamp = preset.stat().st_mtime_ns
    assert helper.prepare(source, runtime) == key
    assert preset.stat().st_mtime_ns == timestamp
    assert all((runtime / name).read_bytes() == content for name, content in original.items())
    preset.write_text("CORRUPTED")
    with pytest.raises(ValueError, match="differs"):
        helper.prepare(source, runtime)


@pytest.fixture
def launcher(source, tmp_path):
    git = shutil.which("git")
    alternatives = [Path(git).parent.parent / "bin/bash.exe"] if git else []
    bash = next((str(path) for path in alternatives if path.is_file()), shutil.which("bash"))
    if not bash:
        pytest.skip("Bash unavailable")
    scripts = tmp_path / "bin"
    scripts.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (scripts / "id").write_text(
        '#!/usr/bin/env bash\nif [[ "$1" == -u || "$1" == -g ]]; then echo 1000; else exit 1; fi\n',
        newline="\n",
    )
    (scripts / "docker").write_text(
        """#!/usr/bin/env bash
set -eu
printf '%q ' "$@" >> "$MOCK_STATE/log"
printf '\\n' >> "$MOCK_STATE/log"
case "$1 ${2:-}" in
  'image inspect') [[ -f "$MOCK_STATE/image" ]] || exit 1; if [[ "$*" == *--format* ]]; then printf 'sha256:%064d\\n' 0; fi ;;
  'pull '*) touch "$MOCK_STATE/image" ;;
  'inspect '*) [[ "${MOCK_EXISTING:-false}" == true ]] || exit 1; if [[ "$*" == *--format* ]]; then realpath "$MOCK_DATA"; fi ;;
  'network inspect') exit 1 ;;
  'run '*)
    if [[ "$*" == *prepare-launch.py* ]]; then
      [[ "${MOCK_PREFLIGHT_FAIL:-false}" == false ]] || exit 42
      printf '%064d\\n' 0
    elif [[ "$*" == *--initialize-only* ]]; then
      mkdir -p "$MOCK_DATA"/{models,tts/weights,tts/pretrained,tts/ref,tts/genie,nltk_data}
    fi ;;
esac
""",
        newline="\n",
    )
    for script in scripts.iterdir():
        script.chmod(0o755)
    env = {**os.environ, "MOCK_STATE": str(state), "MOCK_DATA": str(data)}

    # Resolve PATH inside Bash so both native Linux and Git-for-Windows use shell paths.
    def run(*, entry="run-ready.sh", extra=(), **overrides):
        return subprocess.run(
            [
                bash,
                "-c",
                'export PATH="$(cd "$1" && pwd):$PATH"; shift; script=$1; source=$2; data=$3; shift 3; exec bash "$script" --source "$source" --data "$data" --replace "$@"',
                "_",
                scripts.as_posix(),
                (ROOT / "tools/jetson" / entry).as_posix(),
                source.as_posix(),
                data.as_posix(),
                *extra,
            ],
            env=env | overrides,
            capture_output=True,
            text=True,
        )

    return run, state


@pytest.mark.parametrize("present", [False, True])
def test_launcher_pulls_only_missing_images_and_pins_all_runs(launcher, present):
    run, state = launcher
    if present:
        (state / "image").touch()
    for _ in range(2):
        result = run()
        assert result.returncode == 0, result.stderr
    lines = (state / "log").read_text().splitlines()
    assert sum(line.startswith("pull ") for line in lines) == (0 if present else 1)
    assert all("--pull=never" in line for line in lines if line.startswith("run "))
    assert all("sha256:" in line for line in lines if line.startswith("run "))
    assert any(
        "dst=/workspace/tuber/src,readonly" in argument for line in lines for argument in shlex.split(line)
    )


def test_preflight_failure_preserves_existing_container(launcher):
    run, state = launcher
    (state / "image").touch()
    result = run(MOCK_EXISTING="true", MOCK_PREFLIGHT_FAIL="true")
    assert result.returncode != 0
    lines = (state / "log").read_text().splitlines()
    assert not any(line.startswith(("stop ", "rm ")) for line in lines)
    assert not any("--initialize-only" in line for line in lines)


def test_repository_entry_reuses_the_locally_built_environment(launcher):
    run, state = launcher
    (state / "image").touch()
    result = run(entry="run-studio.sh")
    assert result.returncode == 0, result.stderr
    lines = (state / "log").read_text().splitlines()
    assert not any(line.startswith("pull ") for line in lines)
    assert any("image inspect tuber-studio:environment-r36.4" in line for line in lines)


def test_online_environment_survives_the_next_code_only_launch(launcher):
    run, state = launcher
    (state / "image").touch()
    reference = state.parent / "project/online-environment-image.txt"
    reference.write_text("ghcr.io/owner/repo:online-env-test\n")
    assert run(extra=("--environment", "online")).returncode == 0
    assert (state.parent / "data/environment-profile").read_text().strip() == "online"
    (state / "log").write_text("")
    result = run()
    assert result.returncode == 0, result.stderr
    log = (state / "log").read_text()
    assert "image inspect ghcr.io/owner/repo:online-env-test" in log
    assert not any(line.startswith("pull ") for line in log.splitlines())


def test_only_dependency_edits_change_the_online_extension(source):
    helper = load("tools/jetson/environment.py")
    base, online = helper.contract(source), helper.online_contract(source)
    (source / "components/song_requests/helper.cjs").write_text("updated helper")
    assert helper.contract(source) == base and helper.online_contract(source) == online
    (source / "components/song_requests/package-lock.json").write_text("new dependencies")
    assert helper.contract(source) == base
    assert helper.online_contract(source)["sha256"] != online["sha256"]
