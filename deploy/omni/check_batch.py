"""Fail a Remotion startup when the shared gateway is not in batch mode."""

import os
from pathlib import Path

import httpx


def main():
    token = Path(os.environ["OMNI_GATEWAY_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    address = os.environ["OMNI_GATEWAY_MODE_URL"]
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = client.get(address, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        status = response.json()
    if status["mode"] != "batch":
        raise SystemExit("The Omni gateway is not admitting Remotion renders outside batch mode.")


if __name__ == "__main__":
    main()
