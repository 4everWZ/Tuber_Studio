"""Run the same read-only Ruff gates locally and in CI, reporting both failures."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Optional paths; defaults to src, tests, tools")
    args = parser.parse_args(argv)
    paths = [str(path.resolve()) for path in args.paths] or ["src", "tests", "tools"]
    failed = False
    for label, options in (("Ruff lint", ["check"]), ("Ruff format", ["format", "--check"])):
        result = subprocess.run([sys.executable, "-m", "ruff", *options, *paths], cwd=ROOT, check=False)
        passed = result.returncode == 0
        print(f"{label}: {'passed' if passed else 'failed'}", flush=True)
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
