"""Explicit real-model smoke evaluation. Does not publish or edit live state.

Run: uv run python tools/evaluate_semantics.py --config runtime/studio.json
These cases detect echoes and lost literal facts, not broad persona/summary quality.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from tuber_studio.compaction import SUMMARIZATION_PROMPT, compact_history
from tuber_studio.context import DOMAIN_GUIDANCE, ContextManager
from tuber_studio.providers import LlamaCppProvider, public_error
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store

ROOT = Path(__file__).resolve().parents[1]


def missing_groups(text, groups):
    return [group for group in groups if not any(candidate in text for candidate in group)]


async def evaluate_grounded(config_path, character_id, seed=42):
    """Exercise real compression + source retrieval + one repair in isolated RAM-only state."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["llm"]["seed"] = seed
    characters = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    cases = json.loads((ROOT / "evaluations/semantic-cases.json").read_text(encoding="utf-8"))
    token = (
        (ROOT / "runtime/inference-token").read_text().strip()
        if config.get("inference_broker", {}).get("enabled")
        else None
    )
    store = Store(":memory:", characters)
    runtime = Runtime(SimpleNamespace(data=config, inference_token=token), store)
    character = store.character(character_id)
    scope = "evaluation:source-backed"
    started = time.monotonic()
    try:
        await runtime.llm.health()
        history = cases["history"]
        for user, answer in zip(history[::2], history[1::2], strict=True):
            runtime.context.record(
                scope,
                character,
                {"event_id": user["source_event_id"], "text": user["content"]},
                answer["content"],
                "comment_draft",
            )
        await runtime.context.compact(scope, character, "comment_draft")
        async with runtime.generation_lock:
            reply, audit = await runtime.generate_reply(
                scope,
                character,
                {"event_id": "evaluation:grounded-recall", "text": cases["recall_question"]},
                mode="comment_draft",
            )
        print(
            json.dumps(
                {
                    "model": config["llm"]["model"],
                    "seed": seed,
                    "server_tokens": runtime.llm.server_tokens,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "case": "source_backed_after_compaction",
                    "text": reply.spoken_text,
                    "answer_check": audit,
                    "context": runtime.context.status(),
                    "policy": "Per-case diagnostic; isolated memory only, no publishing or automatic model rejection.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return not audit["missing"]
    finally:
        await runtime.close()
        store.close()


async def evaluate(config_path, character_id, seed=42):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["llm"]["seed"] = seed
    characters = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    character = next(c for c in characters if c["id"] == character_id)
    cases = json.loads((ROOT / "evaluations/semantic-cases.json").read_text(encoding="utf-8"))
    token = (
        (ROOT / "runtime/inference-token").read_text(encoding="utf-8").strip()
        if config.get("inference_broker", {}).get("enabled")
        else None
    )
    provider = LlamaCppProvider(config["llm"], token)
    results = []
    try:
        await provider.health()
        for case in cases["reply_cases"]:
            event = {"event_id": "evaluation:" + case["id"], "text": case["text"]}
            prefix, initial, pending, suffix = ContextManager.parts(character, event, {}, "comment_draft")
            messages = ContextManager.render(prefix, initial, [], pending, suffix)
            try:
                reply = await provider.generate(messages, character, event["event_id"], event["text"])
                results.append(
                    {
                        "case": case["id"],
                        "check": "schema_and_non_echo",
                        "passed": True,
                        "text": reply.spoken_text,
                    }
                )
            except Exception as error:
                results.append({"case": case["id"], "passed": False, "error": public_error(error)})
        event = {"event_id": "evaluation:recall", "text": cases["recall_question"]}
        prefix, initial, pending, suffix = ContextManager.parts(character, event, {}, "comment_draft")
        try:
            messages = ContextManager.render(prefix, initial, cases["history"], pending, suffix)
            reply = await provider.generate(messages, character, event["event_id"], event["text"])
            missing = missing_groups(reply.spoken_text, cases["recall_fact_groups"])
            results.append(
                {
                    "case": "assistant_history",
                    "passed": not missing,
                    "missing": missing,
                    "text": reply.spoken_text,
                }
            )
        except Exception as error:
            results.append({"case": "assistant_history", "passed": False, "error": public_error(error)})
        try:
            replacement = await compact_history(
                cases["history"],
                ContextManager.compaction_prefix(prefix),
                initial,
                provider,
                summary_prompt=SUMMARIZATION_PROMPT + "\n\n" + DOMAIN_GUIDANCE,
            )
            summary = replacement[-1]["content"]
            missing = missing_groups(summary, cases["summary_fact_groups"])
            results.append(
                {"case": "summary_literal_facts", "passed": not missing, "missing": missing, "text": summary}
            )
        except Exception as error:
            results.append({"case": "summary_literal_facts", "passed": False, "error": public_error(error)})
        else:
            # Assistant facts now exist only in the model-produced summary;
            # retaining real viewer messages alone cannot satisfy this check.
            try:
                messages = ContextManager.render(prefix, initial, replacement, pending, suffix)
                reply = await provider.generate(messages, character, event["event_id"], event["text"])
                missing = missing_groups(reply.spoken_text, cases["recall_fact_groups"])
                results.append(
                    {
                        "case": "after_compaction_history",
                        "passed": not missing,
                        "missing": missing,
                        "text": reply.spoken_text,
                    }
                )
            except Exception as error:
                results.append(
                    {"case": "after_compaction_history", "passed": False, "error": public_error(error)}
                )
        print(
            json.dumps(
                {
                    "model": config["llm"]["model"],
                    "seed": seed,
                    "server_tokens": provider.server_tokens,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return all(result["passed"] for result in results)
    finally:
        await provider.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "runtime/studio.json")
    parser.add_argument("--character", default="whale")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--grounded", action="store_true", help="Only the integrated source-backed recall case"
    )
    arguments = parser.parse_args()
    runner = evaluate_grounded if arguments.grounded else evaluate
    raise SystemExit(0 if asyncio.run(runner(arguments.config, arguments.character, arguments.seed)) else 1)
