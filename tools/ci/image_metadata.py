"""Validated GHCR tags and small publication receipts; no credential handling."""

import argparse
import json
import os
import re
from pathlib import Path


def image_tags(repository, sha, ref, event, default_branch, environment_sha, base_environment_sha=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository identity")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Publication requires a full source commit")
    if not re.fullmatch(r"[0-9a-f]{64}", environment_sha):
        raise ValueError("Environment publication requires dependency identity")
    default = ref == "refs/heads/" + default_branch
    release = ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else None
    if event not in {"push", "workflow_dispatch"} or (event == "push" and not default and release is None):
        raise ValueError("PRs and unapproved events cannot publish container images")
    image = "ghcr.io/" + repository.lower()
    prefix = "online-env-" if base_environment_sha else "env-"
    alias = ":environment-online-r36.4" if base_environment_sha else ":environment-r36.4"
    reference = image + ":" + prefix + environment_sha[:24]
    tags = [reference]
    if default:
        tags += [image + alias]
    if release is not None:
        if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", release):
            raise ValueError("Release publication requires a vX.Y.Z or prerelease tag")
    return {
        "image": image,
        "reference": reference,
        "alias": image + alias if default else "",
        "base_reference": image + ":env-" + (base_environment_sha or environment_sha)[:24],
        "tags": "\n".join(tags),
        "short_sha": sha[:12],
        "labels": "\n".join(
            (
                "org.opencontainers.image.source=https://github.com/" + repository,
                "org.opencontainers.image.revision=" + sha,
                "org.opencontainers.image.description=Jetson ARM64 environment; application supplied by read-only mounts",
            )
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tags", "evidence"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    if args.command == "tags":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        descriptor = json.loads(
            Path(
                "tmp/ci-online/environment.json" if args.online else "tmp/ci-environment/environment.json"
            ).read_text()
        )
        result = image_tags(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_SHA"],
            os.environ["GITHUB_REF"],
            os.environ["GITHUB_EVENT_NAME"],
            event["repository"]["default_branch"],
            descriptor["sha256"],
            descriptor.get("base_sha256") if args.online else None,
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            for key, value in result.items():
                stream.write(key + "<<TUBER_CI_VALUE\n" + value + "\nTUBER_CI_VALUE\n")
        print(json.dumps({"image": result["image"], "tags": result["tags"].splitlines()}))
    else:
        digest = os.environ["IMAGE_DIGEST"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Registry export did not return a real image digest")
        metadata = json.loads(os.environ["BUILDX_METADATA"])
        if metadata.get("containerimage.digest", digest) != digest:
            raise ValueError("Build metadata and registry-export digests differ")
        report = {
            "image": os.environ["IMAGE_NAME"],
            "digest": digest,
            "pull_reference": os.environ["IMAGE_NAME"] + "@" + digest,
            "tags": os.environ["IMAGE_TAGS"].splitlines(),
            "source_revision": os.environ["GITHUB_SHA"],
            "run_url": "https://github.com/"
            + os.environ["GITHUB_REPOSITORY"]
            + "/actions/runs/"
            + os.environ["GITHUB_RUN_ID"],
            "platform": "linux/arm64",
            "build_record": metadata.get("buildx.build.ref"),
            "scope": "CI environment build/export; mounted program and GPU/voice/128K acceptance remain separate",
            "hardware_acceptance_verified": False,
            "anonymous_registry_pull_verified": False,
            "runtime_downloads_or_model_execution_performed": False,
        }
        if args.output is None:
            parser.error("evidence requires --output")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a") as stream:
                stream.write("Published environment image: `" + report["pull_reference"] + "`\n\n")
                stream.write(
                    "Program/launcher are in the separate jetson-program artifact. GPU, voice and 128K acceptance remain separate.\n"
                )
                stream.write(
                    "First GHCR package may require visibility changed to public or docker login before pulling.\n"
                )
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
