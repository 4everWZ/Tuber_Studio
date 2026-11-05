"""Intervene on one part of the live dialogue input at a time."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

from grounded_sft import read_jsonl
from tuber_studio.context import ContextManager
from tuber_studio.persona import GROUNDED_DAFEIYU_MODEL
from tuber_studio.providers import LlamaCppProvider, single_system_messages


def event(index, text):
    return {"event_id": f"m{index}", "type": "chat", "text": text}


def live_messages(character, settings, previous, answer, current):
    _, _, pending, _ = ContextManager.parts(character, event(1, previous), {}, "live", settings=settings)
    prefix, initial, latest, suffix = ContextManager.parts(
        character, event(2, current), {}, "live", settings=settings,
    )
    history = [pending, {"role": "assistant", "content": answer}]
    return ContextManager.render(prefix, initial, history, latest, suffix)


def plain_messages(previous, answer, current):
    return [
        {"role": "system", "content": "你是大肥鱼，正在直播。直接接观众的话，只说一两句当场会说的台词。"},
        {"role": "user", "content": previous},
        {"role": "assistant", "content": answer},
        {"role": "user", "content": current},
    ]


async def complete(client, config, sampling, messages, schema=None):
    payload = {
        "model": config["model"], "messages": messages, **sampling,
        "seed": config["seed"], "max_tokens": 24 if schema else config["max_tokens"],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if schema:
        payload["response_format"] = {"type": "json_object", "schema": schema}
    response = await client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()
    result = response.json()
    choice = result["choices"][0]
    raw = choice["message"]["content"]
    structured = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                structured = parsed
        except json.JSONDecodeError:
            pass
    return {
        "raw": raw, "spoken_text": structured.get("spoken_text", raw), "pick": structured.get("pick"),
        "finish": choice["finish_reason"], "usage": result.get("usage", {}),
    }


async def replay(args):
    cases = read_jsonl(args.cases)
    specs = {row["pair"]: row for row in read_jsonl(args.interventions)}
    baseline = {row["id"]: row["replies"][0]["spoken_text"] for row in read_jsonl(args.history)}
    character = next(row for row in json.loads(args.characters.read_text(encoding="utf-8")) if row["id"] == "whale")
    config = {
        "base_url": args.endpoint, "model": GROUNDED_DAFEIYU_MODEL, "kind": "llama_cpp", "context_tokens": 4096,
        "temperature": 0.0, "max_tokens": 180, "seed": 42, "enable_thinking": False,
    }
    settings = {"llm": config}
    provider = LlamaCppProvider(config)
    sampling = provider.sampling()
    labels = ("A", "B")
    schema = {
        "type": "object", "properties": {"pick": {"type": "string", "enum": list(labels)}},
        "required": ["pick"], "additionalProperties": False,
    }
    try:
        async with httpx.AsyncClient(base_url=args.endpoint, timeout=180) as client:
            with args.output.open("w", encoding="utf-8") as output:
                for case in cases:
                    previous, question = case["turns"]
                    spec = specs[case["pair"]]
                    neutral = spec["history_reply"]
                    recapped = f"前面我说过「{previous}」现在问你，{question}"
                    natural = live_messages(character, settings, previous, neutral, question)
                    replies = {}
                    for name, history, latest in (
                        ("live_generated", baseline[case["id"]], question),
                        ("live_neutral", neutral, question),
                        ("live_recapped", neutral, recapped),
                    ):
                        messages = live_messages(character, settings, previous, history, latest)
                        try:
                            reply = await provider.generate(messages, character, "m2", latest)
                            replies[name] = {"spoken_text": reply.spoken_text, "usage": provider.last_usage}
                        except (ValueError, RuntimeError) as error:
                            replies[name] = {"error": str(error), "raw": provider.last_content}
                    replies["live_without_grammar"] = await complete(
                        client, config, sampling, single_system_messages(natural),
                    )
                    for name, latest in (("plain_neutral", question), ("plain_recapped", recapped)):
                        replies[name] = await complete(
                            client, config, sampling, plain_messages(previous, neutral, latest),
                        )
                    picks = {}
                    for name, latest in (("history", question), ("recapped", recapped)):
                        for order in ((0, 1), (1, 0)):
                            alternatives = "\n".join(
                                f"{label}．{spec['options'][option]}" for label, option in zip(labels, order, strict=True)
                            )
                            choice_prompt = f"{latest}\n选符合前文的一项，只给选项字母。\n{alternatives}"
                            response = await complete(
                                client, config, sampling, plain_messages(previous, neutral, choice_prompt), schema,
                            )
                            expected = 0 if case["id"].endswith("-a") else 1
                            response["expected"] = labels[order.index(expected)]
                            picks[f"{name}_{order[0]}"] = response
                    result = case | {"replies": replies, "picks": picks, "neutral_history": neutral}
                    output.write(json.dumps(result, ensure_ascii=False) + "\n")
                    output.flush()
    finally:
        await provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    for name in ("characters", "cases", "interventions", "history", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
