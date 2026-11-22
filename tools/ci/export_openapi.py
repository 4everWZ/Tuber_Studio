"""Offline CLI to export or validate the Tuber Studio `/api/v1/*` OpenAPI JSON contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main(argv: list[str] | None = None) -> int:
    from tuber_studio.app.openapi import build_openapi_schema, validate_openapi_contract

    parser = argparse.ArgumentParser(description="Export or validate Tuber Studio OpenAPI schema.")
    parser.add_argument("--output", type=Path, help="Optional destination path to write openapi.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate generated OpenAPI schema contract (and compare against --output if file exists).",
    )
    args = parser.parse_args(argv)

    schema = build_openapi_schema(root=ROOT)
    rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    errors = validate_openapi_contract(schema)
    if errors:
        for item in errors:
            print(f"[openapi] ERROR: {item}", file=sys.stderr)
        return 1

    if args.check:
        if args.output is not None:
            if not args.output.exists():
                print(f"[openapi] ERROR: missing OpenAPI file: {args.output}", file=sys.stderr)
                return 1
            existing = args.output.read_text(encoding="utf-8")
            if existing != rendered:
                print(f"[openapi] ERROR: {args.output} is out of date with app.openapi()", file=sys.stderr)
                return 1
        api_routes = sum(1 for path in schema["paths"] if path.startswith("/api/v1/"))
        print(f"OpenAPI contract OK ({api_routes} /api/v1/* paths, ErrorEnvelope present)")
        return 0

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote OpenAPI schema to {args.output}")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
