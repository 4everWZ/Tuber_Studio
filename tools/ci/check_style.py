"""Run read-only Ruff checks; optionally report formatting differences as warnings."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format-advisory",
        action="store_true",
        help="Report formatting differences without failing; lint and tool errors still fail",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="Optional paths; defaults to src, tests, tools")
    args = parser.parse_args(argv)
    paths = [str(path.resolve()) for path in args.paths] or ["src", "tests", "tools"]
    failed = False
    for label, options in (("Ruff lint", ["check"]), ("Ruff format", ["format", "--check"])):
        result = subprocess.run([sys.executable, "-m", "ruff", *options, *paths], cwd=ROOT, check=False)
        passed = result.returncode == 0
        advisory = args.format_advisory and label == "Ruff format" and result.returncode == 1
        status = "passed" if passed else "warning (non-blocking)" if advisory else "failed"
        print(f"{label}: {status}", flush=True)
        if advisory:
            print(
                "::warning title=Python formatting::Ruff found formatting differences; CI continues.",
                flush=True,
            )
        failed |= not passed and not advisory
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
