"""Validate and publish the pinned 9B GGUF model and projector together."""

import argparse
import json
from pathlib import Path

from tuber_studio.inference_profiles import (
    DAFEIYU_9B_DIGESTS,
    DAFEIYU_9B_MODEL,
    DAFEIYU_9B_PROJECTOR,
    DAFEIYU_9B_WEIGHTS,
    verify_artifact,
)

ROOT = Path(__file__).resolve().parents[2]
REPO_ID = "4everImmortality/dafeiyu-qwen35-9b-gguf"
RELEASE = "releases/" + DAFEIYU_9B_MODEL.rsplit(":", 1)[1]


def release_files(directory):
    manifest = json.loads((ROOT / "config/jetson/downloads.json").read_text(encoding="utf-8"))
    assets = {entry["path"]: entry for entry in manifest["files"]}
    files = {}
    cache = {}
    for role, filename in (("model", DAFEIYU_9B_WEIGHTS), ("projector", DAFEIYU_9B_PROJECTOR)):
        path = directory / filename
        expected = assets[f"models/{filename}"]
        digest = DAFEIYU_9B_DIGESTS[role]
        if expected["sha256"] != digest or path.stat().st_size != expected["bytes"]:
            raise ValueError(f"{filename} does not match the pinned release identity")
        verify_artifact(path, digest, cache)
        files[f"{RELEASE}/{filename}"] = (path, digest)
    return files


def publish(files):
    from huggingface_hub import CommitOperationAdd, HfApi

    api = HfApi()
    current = api.model_info(REPO_ID, revision="main", files_metadata=True)
    existing = {item.rfilename: item for item in current.siblings or [] if item.rfilename in files}
    if existing:
        if any(
            name not in existing
            or getattr(existing[name].lfs, "sha256", None) != digest
            or existing[name].size != path.stat().st_size
            for name, (path, digest) in files.items()
        ):
            raise ValueError("The release already contains different or incomplete files")
        return {"status": "already_published", "revision": current.sha}

    result = api.create_commit(
        repo_id=REPO_ID,
        repo_type="model",
        revision="main",
        parent_commit=current.sha,
        operations=[CommitOperationAdd(path_in_repo=name, path_or_fileobj=path) for name, (path, _) in files.items()],
        commit_message=f"Publish {DAFEIYU_9B_MODEL} GGUF model and projector",
    )
    return {"status": "published", "revision": result.oid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "runtime/models")
    parser.add_argument("--check-only", action="store_true", help="Verify the local files without contacting the Hub")
    args = parser.parse_args()
    files = release_files(args.directory)
    result = {"status": "verified"} if args.check_only else publish(files)
    print(json.dumps({"repository": REPO_ID, "files": list(files), **result}))


if __name__ == "__main__":
    main()
