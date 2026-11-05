"""Run only the authenticated inference broker and its owned model workers."""

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def healthcheck(root, port):
    """Read this node's status without constructing a broker or loading workers."""
    token = (root / "runtime/inference-token").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health", headers={"X-Tuber-Inference-Key": token}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        status = json.load(response)
    if (
        not isinstance(status, dict)
        or status.get("mode") not in {"text", "live"}
        or not isinstance(status.get("providers"), dict)
        or status.get("state") not in {"cold", "active", "switching", "stopping"}
    ):
        raise ValueError("Inference node is not healthy")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("TUBER_ROOT", "/workspace/tuber")))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default=os.environ.get("TUBER_INFERENCE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=9012)
    parser.add_argument("--no-warm-start", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.healthcheck:
        try:
            healthcheck(root, args.port)
        except (OSError, ValueError, urllib.error.URLError):
            parser.exit(1, "Inference node health check failed\n")
        return
    import uvicorn

    from tuber_studio.inference import create_app

    configuration = (args.config or root / "runtime/inference-node.json").resolve()
    if not configuration.is_relative_to(root / "runtime"):
        parser.error("Node configuration must be inside this node's runtime directory")
    application = create_app(root, node_config=configuration, warm_start=not args.no_warm_start)
    uvicorn.run(application, host=args.host, port=args.port, access_log=False, ws="none")


if __name__ == "__main__":
    main()
