"""Native ARM/disk gate; cleanup is limited to ephemeral GitHub-hosted SDKs.

No user/self-hosted runner files, project data, Docker images or system settings
are deleted. The build fails visibly if the 40GiB requirement is still unmet.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

MINIMUM_BYTES = 40 * 1024**3
HOSTED_SDKS = (
    "/usr/share/dotnet",
    "/usr/local/lib/android",
    "/usr/local/swift",
    "/opt/ghc",
    "/opt/hostedtoolcache",
    "/usr/local/share/powershell",
)


def hosted_cleanup_allowed(environment):
    return (
        environment.get("GITHUB_ACTIONS") == "true"
        and environment.get("RUNNER_ENVIRONMENT") == "github-hosted"
    )


def space(paths):
    return {str(path): shutil.disk_usage(path).free for path in paths}


def cleanup_sdk(path):
    if str(path) not in HOSTED_SDKS or not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("Cleanup target is not an exact known hosted SDK directory")
    subprocess.run(["sudo", "rm", "-rf", "--", str(path)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-hosted", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "passed": False,
        "architecture": platform.machine(),
        "minimum_free_bytes": MINIMUM_BYTES,
        "runner_environment": os.getenv("RUNNER_ENVIRONMENT", "unknown"),
        "cleaned_sdk_paths": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system() != "Linux" or platform.machine().lower() not in {"aarch64", "arm64"}:
            raise RuntimeError("Jetson program builds require a native Linux ARM64 cloud runner")
        docker = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"], check=True, capture_output=True, text=True
        )
        paths = [Path.cwd(), Path(docker.stdout.strip()).resolve(strict=True)]
        report["free_before_bytes"] = space(paths)
        if args.cleanup_hosted and hosted_cleanup_allowed(os.environ):
            for name in HOSTED_SDKS:
                if min(space(paths).values()) >= MINIMUM_BYTES:
                    break
                path = Path(name)
                if path.exists() and not path.is_symlink() and path.resolve() == path:
                    cleanup_sdk(path)
                    report["cleaned_sdk_paths"].append(name)
        report["free_after_bytes"] = space(paths)
        if min(report["free_after_bytes"].values()) < MINIMUM_BYTES:
            raise RuntimeError(
                "Less than 40GiB free after allowed hosted SDK cleanup. Configure repository variable "
                "JETSON_BUILD_RUNNER as JSON for a larger ARM64 cloud runner; no local-machine fallback is used."
            )
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
