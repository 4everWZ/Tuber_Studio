"""Offline OpenAPI schema builder without requiring live DB locks, secrets, or services."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI

from ..agent.api import create_workbench_task_runtime as create_task_runtime
from ..core.auth import AuthSessions
from ..core.config import ROOT, Settings
from ..core.store import Store
from ..infra.platforms.bilibili.account import BilibiliAccount
from ..operations.channels.comments import Comments
from ..operations.channels.direct_messages import DirectMessages
from ..operations.channels.dynamics import Dynamics
from ..operations.service import BrowsingService
from ..persona.assets.illustration_policy import migrate_illustration_policy
from ..persona.characters.presets import upgrade_bundled_characters
from .factory import register_all_routes
from .runtime import Runtime

API_V1_DOMAINS = ("system", "persona", "operations", "live", "agent")
API_V1_PREFIXES = tuple(f"/api/v1/{domain}/" for domain in API_V1_DOMAINS)


@contextmanager
def _isolated_schema_env(config_path: Path):
    saved = {key: os.environ.get(key) for key in ("TUBER_CONFIG", "TUBER_RUNTIME", "TUBER_ADMIN_TOKEN")}
    os.environ["TUBER_CONFIG"] = str(config_path)
    os.environ.pop("TUBER_RUNTIME", None)
    os.environ.pop("TUBER_ADMIN_TOKEN", None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_openapi_schema(*, root: Path = ROOT) -> dict:
    """Build the full `/api/v1/*` OpenAPI schema using an isolated ephemeral Store."""
    resolved_root = root.resolve()
    example_config = resolved_root / "config/studio.example.json"
    seeds = json.loads((resolved_root / "config/characters.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="tuber-openapi-") as tmp_dir, _isolated_schema_env(example_config):
        tmp_runtime = Path(tmp_dir)
        settings = Settings(root=resolved_root, runtime=tmp_runtime)
        AuthSessions.prepare_runtime(settings)
        store = Store(
            tmp_runtime / "studio.sqlite3",
            seeds,
            migrations=(migrate_illustration_policy, upgrade_bundled_characters),
        )
        try:
            sessions = AuthSessions(settings, store)
            runtime = Runtime(settings, store)
            account = BilibiliAccount(settings, store)
            comments = Comments(settings, store, runtime, account)
            runtime.account = account
            memes = runtime.pets.catalog
            runtime.operation_images.catalog = memes
            comments.memes = memes
            dynamics = Dynamics(settings, store, runtime, comments, account=account, memes=memes)
            messages = DirectMessages(settings, store, runtime, comments, account=account, memes=memes)
            comments.direct_messages = messages
            browsing = BrowsingService(
                settings, store, runtime, comments, messages, dynamics, bilibili_account=account
            )
            runtime.browsing = browsing
            runtime.tasks = create_task_runtime(runtime, store, browsing)

            app = FastAPI(title="Tuber Studio", version="1.0.0", docs_url=None, redoc_url=None)
            register_all_routes(
                app,
                settings=settings,
                runtime=runtime,
                store=store,
                account=account,
                comments=comments,
                dynamics=dynamics,
                messages=messages,
                browsing=browsing,
                memes=memes,
                sessions=sessions,
                call_guards={},
                login_attempts={},
                mount_static=False,
            )
            return app.openapi()
        finally:
            store.close()


def validate_openapi_contract(schema: dict) -> list[str]:
    """Validate `/api/v1/*` route namespace & error schema invariants; return error list."""
    errors: list[str] = []
    if not str(schema.get("openapi", "")).startswith("3."):
        errors.append(f"Unexpected OpenAPI version: {schema.get('openapi')!r}")
    paths = schema.get("paths") or {}
    if not paths:
        errors.append("OpenAPI schema contains no paths")
        return errors
    api_paths = sorted(path for path in paths if path.startswith("/api"))
    if not api_paths:
        errors.append("OpenAPI schema contains no /api routes")
    for path in api_paths:
        if not path.startswith("/api/v1/"):
            errors.append(f"Legacy unversioned API route exposed: {path}")
        elif not any(path.startswith(prefix) or path == prefix.rstrip("/") for prefix in API_V1_PREFIXES):
            errors.append(f"API v1 route outside approved domain namespaces {API_V1_DOMAINS}: {path}")
    schemas = (schema.get("components") or {}).get("schemas") or {}
    if "ErrorEnvelope" not in schemas:
        errors.append("Missing ErrorEnvelope component schema in OpenAPI components.schemas")
    return errors
