"""Check receipt-backed compaction and one comment reply on an already loaded model.

Run: uv run --locked python tools/evaluate_semantics.py --config runtime/studio.json
The input and receipts are synthetic. The report includes lexical diagnostics.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from tuber_studio.app.runtime import Runtime
from tuber_studio.core.config import Settings, redact_credentials
from tuber_studio.core.store import Store
from tuber_studio.infra.inference.providers import public_error
from tuber_studio.infra.platforms.bilibili.account import BilibiliAccount
from tuber_studio.operations.channels.comments import Comments
from tuber_studio.persona.context.compaction import is_summary_message

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "9000001"
VIDEO = "import-semantic-diagnostic"
THREAD = "9000002"
MODE = "comment_draft"


def configured_paths():
    runtime = Path(os.environ.get("TUBER_RUNTIME", ROOT / "runtime"))
    return runtime, Path(os.environ.get("TUBER_CONFIG", runtime / "studio.json"))


def isolated_settings(config_path, runtime_path):
    source_runtime, default_config = configured_paths()
    credentials = source_runtime if config_path == default_config.resolve() else config_path.parent
    target = runtime_path / "studio.json"
    config = redact_credentials(json.loads(config_path.read_text(encoding="utf-8")))
    config["features"] = config.get("features", {}) | {"live_enabled": False}
    target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    previous = os.environ.get("TUBER_CONFIG")
    os.environ["TUBER_CONFIG"] = str(target)
    try:
        settings = Settings(root=ROOT, runtime=runtime_path)
    finally:
        if previous is None:
            os.environ.pop("TUBER_CONFIG", None)
        else:
            os.environ["TUBER_CONFIG"] = previous
    settings.provider_keys = Settings.read_provider_keys(credentials / "provider-credentials.json")
    return settings


async def ready_model(runtime):
    async def inference_only(request):
        routes = ("/chat/completions", "/chat/prompt_tokens")
        if request.method != "GET" and not (request.method == "POST" and request.url.path.endswith(routes)):
            raise ValueError("The diagnostic cannot warm up or reconfigure an inference service")

    runtime.llm.client.event_hooks["request"].append(inference_only)
    if not (await runtime.llm.health())["available"]:
        raise ValueError("The configured Omni gateway is unavailable")
    runtime.llm.supports_operation_web_fetch = False


def fixture_event(store, identifier, text):
    local = store.execute(
        "INSERT INTO comments(platform,video_id,thread_id,comment_id,platform_user_id,display_name,content) "
        "VALUES('bilibili',?,?,?,?,?,?)",
        (VIDEO, THREAD, str(identifier), "9000003", "来访观众", text),
    ).lastrowid
    row = store.one("SELECT * FROM comments WHERE id=?", (local,))
    return Comments._comment_event(row) | {"account_id": ACCOUNT}


def seed_history(runtime, comments, character, case):
    scope = comments.account_context_scope(VIDEO, THREAD, account_id=ACCOUNT)
    delivered = set()
    for number, turn in enumerate(case["history"], 1):
        event = fixture_event(runtime.store, 9000100 + number, turn["source"])
        if turn["delivered"]:
            delivered.add(event["event_id"])
        runtime.store.execute(
            "INSERT INTO drafts(comment_id,text,status,account_id,character_id,character_version,receipt) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(event["event_id"].split(":")[1]), turn["reply"], "published" if turn["delivered"] else "approved",
             ACCOUNT, character["id"], character["version"], json.dumps({"code": 0, "rpid": str(9000200 + number)})
             if turn["delivered"] else None),
        )
        # Include the old unsent body in stored history so the receipt filter is exercised.
        runtime.context.record(scope, character, event, turn["reply"], MODE)
    return scope, delivered


async def evaluate(config_path, character_id):
    source = ROOT / "evaluations/semantic-cases.json"
    case = json.loads(source.read_text(encoding="utf-8"))
    characters = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    tmp = ROOT / "tmp"
    with tempfile.TemporaryDirectory(prefix="semantic-probe-", dir=tmp if tmp.is_dir() else None) as folder:
        settings = isolated_settings(config_path.resolve(), Path(folder))
        store = Store(":memory:", characters)
        try:
            character = store.character(character_id)
            store.set("active_character", character["id"])
            runtime = Runtime(settings, store)
            try:
                await ready_model(runtime)
                account = BilibiliAccount(settings, store, adapter=object())
                comments = Comments(settings, store, runtime, account)
                scope, delivered = seed_history(runtime, comments, character, case)
                history = runtime.context.history_for_model(scope, character)
                visible = {item["source_event_id"] for item in history if item["role"] == "assistant"}
                if not delivered or visible != delivered:
                    raise ValueError("Compaction input did not isolate the receipt-backed replies")
                async with runtime.generation_lock:
                    started = time.perf_counter()
                    context = await runtime.context.compact(scope, character, MODE)
                    compaction_seconds = time.perf_counter() - started
                    summary_sources = {
                        item["event_id"] for entry in runtime.context.history_for_model(scope, character)
                        if is_summary_message(entry) for item in entry["delivery_provenance"]["sources"]
                    }
                    if summary_sources != delivered:
                        raise ValueError("Compacted history lost its receipt provenance")
                    event = fixture_event(store, 9000199, case["question"])
                    started = time.perf_counter()
                    reply, audit = await runtime.generate_reply(scope, character, event, mode=MODE)
                    generation_seconds = time.perf_counter() - started
                sources = {item["event_id"] for item in audit["sources"] if item["role"] == "assistant"}
                if sources != delivered or set(audit["supported_slots"]) != set(case["source_slots"]):
                    raise ValueError("The reply did not recover the expected receipt-backed source facts")
                return {
                    "case": "source_backed_after_compaction", "model": settings.data["omni"]["model"],
                    "compaction_seconds": round(compaction_seconds, 3),
                    "generation_seconds": round(generation_seconds, 3), "text": reply.spoken_text,
                    "answer_check": audit, "context": context, "platform_writes": 0,
                }
            finally:
                await runtime.close()
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=configured_paths()[1])
    parser.add_argument("--character", default="whale")
    args = parser.parse_args()
    try:
        report = asyncio.run(evaluate(args.config, args.character))
    except Exception as error:
        print(json.dumps({"error": public_error(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
