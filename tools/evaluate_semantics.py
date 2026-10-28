"""Explicit real-model smoke evaluation. Does not publish or edit live state.

Run: uv run python tools/evaluate_semantics.py --config runtime/studio.json
These cases detect echoes and lost literal facts, not broad persona/summary quality.
"""

import argparse
import asyncio
import json
from pathlib import Path

from tuber_studio.compaction import SUMMARIZATION_PROMPT, compact_history
from tuber_studio.context import DOMAIN_GUIDANCE, ContextManager
from tuber_studio.providers import LlamaCppProvider, public_error

ROOT = Path(__file__).resolve().parents[1]


def missing_groups(text, groups):
    return [group for group in groups if not any(candidate in text for candidate in group)]


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
                prefix,
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
    arguments = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(arguments.config, arguments.character, arguments.seed)) else 1)
