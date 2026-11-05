"""Replay native operations generation on isolated material, retaining complete prose."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

from grounded_sft import read_jsonl
from replay_grounded_endpoint import image_part
from tuber_studio.browsing_inputs import ResponseDecisionInput
from tuber_studio.browsing_models import RESPONSE_INSTRUCTION, RESPONSE_REFERENCE_INSTRUCTION, response_schema
from tuber_studio.context import ContextManager
from tuber_studio.dynamics import Dynamics, dynamic_decision_messages
from tuber_studio.operation_text import operation_output_diagnostics
from tuber_studio.persona import GROUNDED_DAFEIYU_MODEL, built_in_anchor
from tuber_studio.providers import LlamaCppProvider

BUDGET = 8192
DATE = "2026-11-05T22:00:00+08:00"


async def conversation(provider, character, settings, case):
    mode = case["mode"]
    history, replies = [], []
    for index, question in enumerate(case["turns"], 1):
        event = {"event_id": f"m{index}", "type": "comment" if mode == "comment_draft" else "message", "text": question}
        prefix, initial, current, suffix = ContextManager.parts(character, event, {}, mode, settings=settings)
        packet = ContextManager.render(prefix, initial, history, current, suffix)
        value = await provider.generate_operation(packet, character, event["event_id"], question)
        text = value.spoken_text
        replies.append(value.model_dump() | {"usage": provider.last_usage, "diagnostics": operation_output_diagnostics(text)})
        history.extend([current, {"role": "assistant", "content": text}])
    return {"replies": replies}


async def video(provider, client, anchor, root, row):
    evidence = list(row["references"].values())
    material = ResponseDecisionInput({
        "kind": "top_level",
        "thread": {"question": row["question"]},
        "coverage": "sampled_visual" if row["frames"] else "provided",
        "evidence": evidence,
    })
    aliases = {ref.canonical_id: ref.alias for ref in material.references}
    content = [{"type": "text", "text": "公开来源材料：" + material.format()}]
    for frame in row["frames"]:
        source_id = row["references"][frame["alias"]]["id"]
        content.extend([
            {"type": "text", "text": f"[{aliases[source_id]}] 画面，{frame['seconds']} 秒"},
            image_part(root / frame["path"]),
        ])
    messages = [
        {"role": "system", "content": anchor + "\n\n" + RESPONSE_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION},
        {"role": "user", "content": content},
    ]
    response = await client.post("/v1/chat/completions", json={
        "model": GROUNDED_DAFEIYU_MODEL, "messages": messages, **provider.sampling(),
        "seed": 42, "max_tokens": BUDGET, "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object", "schema": response_schema(material.allowed_aliases, allow_handoff=False)},
    })
    response.raise_for_status()
    result = response.json()
    choice = result["choices"][0]
    raw = choice["message"]["content"]
    record = {"raw": raw, "finish": choice["finish_reason"], "usage": result.get("usage", {})}
    if choice["finish_reason"] == "stop":
        try:
            parsed = material.resolve(json.loads(raw)).model_dump()
            record.update(value=parsed, diagnostics=operation_output_diagnostics(parsed["text"]))
        except ValueError as error:
            record["error"] = str(error)
    return record


async def replay(args):
    character = next(item for item in json.loads(args.characters.read_text(encoding="utf-8")) if item["id"] == "whale")
    config = {
        "base_url": args.endpoint, "model": GROUNDED_DAFEIYU_MODEL, "kind": "llama_cpp",
        "context_tokens": 16384, "temperature": 0.0, "seed": 42, "enable_thinking": False,
        "operations_max_tokens": BUDGET,
    }
    settings = {"llm": config}
    provider = LlamaCppProvider(config)
    anchor = built_in_anchor(character, settings)
    try:
        async with httpx.AsyncClient(base_url=args.endpoint, timeout=360) as client:
            with args.output.open("w", encoding="utf-8") as output:
                for case in ([] if args.video_only else read_jsonl(args.cases)):
                    try:
                        if case["mode"] == "dynamic":
                            sources = case["sources"]
                            messages = dynamic_decision_messages(character, settings, [], sources, current_time=DATE)
                            value = await provider.generate_json(messages, Dynamics.decision_schema(sources), max_tokens=BUDGET)
                            parsed = Dynamics._parse(value, sources)
                            result = {"value": parsed, "usage": provider.last_usage, "diagnostics": operation_output_diagnostics(parsed["content"])}
                        else:
                            result = await conversation(provider, character, settings, case)
                    except (ValueError, RuntimeError, httpx.HTTPError) as error:
                        result = {"error": str(error), "raw": provider.last_content, "usage": provider.last_usage}
                    output.write(json.dumps(case | result, ensure_ascii=False) + "\n")
                    output.flush()
                async def replay_video(row):
                    try:
                        result = await video(provider, client, anchor, args.video_replay, row)
                    except (ValueError, RuntimeError, httpx.HTTPError) as error:
                        result = {"error": str(error), "error_type": type(error).__name__}
                    metadata = {k: row[k] for k in ("id", "kind", "question", "criterion", "references")}
                    return metadata | {"mode": "video"} | result

                for task in asyncio.as_completed(
                    [replay_video(row) for row in read_jsonl(args.video_replay / "cases.jsonl")]
                ):
                    output.write(json.dumps(await task, ensure_ascii=False) + "\n")
                    output.flush()
    finally:
        await provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    for option in ("cases", "characters", "video-replay", "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--video-only", action="store_true")
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
