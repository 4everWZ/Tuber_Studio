"""Replay held-out chat turns through the normal native live provider and reply schema."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from grounded_sft import read_jsonl

from tuber_studio.context import ContextManager
from tuber_studio.models import Reply
from tuber_studio.persona import GROUNDED_DAFEIYU_MODEL
from tuber_studio.providers import LlamaCppProvider


async def replay(args):
    config = {
        "base_url": args.endpoint, "model": GROUNDED_DAFEIYU_MODEL, "kind": "llama_cpp", "context_tokens": 4096,
        "temperature": 0.0, "max_tokens": 180, "seed": 42, "enable_thinking": False,
    }
    character = next(
        row for row in json.loads(args.characters.read_text(encoding="utf-8")) if row["id"] == "whale"
    )
    provider = LlamaCppProvider(config)
    try:
        with args.output.open("w", encoding="utf-8") as output:
            for case in read_jsonl(args.cases):
                history, replies = [], []
                for turn, question in enumerate(case["turns"], 1):
                    event_id = f"m{turn}"
                    event = {"event_id": event_id, "type": "chat", "text": question}
                    prefix, initial, pending, suffix = ContextManager.parts(
                        character, event, {}, "live", settings={"llm": config},
                    )
                    if args.instruction:
                        prefix[0]["content"] += "\n" + args.instruction
                    if args.with_trusted_state and turn == len(case["turns"]):
                        prefix[0]["content"] += "\n已核对的当前状态：" + case["trusted_state"]
                    messages = ContextManager.render(prefix, initial, history, pending, suffix)
                    started = time.perf_counter()
                    note = None
                    try:
                        if args.revision_note and history:
                            reply, note = await reply_with_revision_note(provider, messages, event_id)
                        else:
                            reply = await provider.generate(messages, character, event_id, question)
                    except (ValueError, RuntimeError) as error:
                        replies.append({"error": str(error)})
                        break
                    replies.append(reply.model_dump() | {
                        "seconds": round(time.perf_counter() - started, 3), "usage": provider.last_usage,
                        **({"revision_note": note} if note is not None else {}),
                    })
                    history.extend([pending, {"role": "assistant", "content": reply.spoken_text}])
                output.write(json.dumps(case | {"replies": replies}, ensure_ascii=False) + "\n")
                output.flush()
    finally:
        await provider.close()


async def reply_with_revision_note(provider, messages, event_id):
    """Isolated probe of finding the updated fact before writing the same reply."""
    schema = Reply.model_json_schema()
    props = schema["properties"]
    props["reply_to"]["items"] = {"type": "string", "enum": [event_id]}
    props["reply_to"]["maxItems"] = 1
    schema["properties"] = {"revision_note": {"type": "string", "maxLength": 72}} | props
    schema["required"].insert(0, "revision_note")
    messages = [*messages, {"role": "system", "content": (
        "先填内部字段 revision_note。回看观众最近一次更正或补充，用第三人称记下与当前请求有关的事实，"
        "分清是谁做什么、现在哪一条有效。当前请求和旧对话无关时填空字符串。"
        "然后在 spoken_text 写一句自然回应，按这条事实接话。内部备注不对观众念。"
    )}]
    value = await provider.generate_json(messages, schema, max_tokens=provider.config["max_tokens"])
    note = value.pop("revision_note")
    reply = Reply.model_validate(value)
    if any(ref != event_id for ref in reply.reply_to):
        raise ValueError("模型引用了其他事件")
    return reply, note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--characters", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instruction")
    parser.add_argument("--with-trusted-state", action="store_true", help="inject the case's checked state on the final turn")
    parser.add_argument("--revision-note", action="store_true", help="probe a private note before multi-turn replies")
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
