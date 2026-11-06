"""Replay frozen evidence against a native llama.cpp decoder on an isolated endpoint."""

import argparse
import base64
import json
import time
from pathlib import Path

import httpx
from evaluate_grounded_sft import replay_schema
from grounded_sft import read_jsonl


def ask(client, model, messages, *, schema=None):
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "seed": 42,
        "max_tokens": 384,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }
    if schema is not None:
        body["response_format"] = {"type": "json_object", "schema": schema}
    started = time.perf_counter()
    response = client.post("chat/completions", json=body)
    response.raise_for_status()
    result = response.json()
    answer = result["choices"][0]
    return {
        "text": answer["message"].get("content") or "",
        "finish_reason": answer["finish_reason"],
        "usage": result.get("usage", {}),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def image_part(path):
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")},
    }


def source_notes_schema(aliases, frame_aliases):
    """Let the decoder find a short fact in each source before writing its reply."""
    schema = replay_schema(aliases)
    for branch in schema["anyOf"]:
        has_answer = branch["properties"]["action"]["enum"] == ["reply_public"]
        if not has_answer:
            continue
        notes = {}
        for name, choices in (
            ("caption", [alias for alias in aliases if alias not in frame_aliases]),
            ("visual", [alias for alias in aliases if alias in frame_aliases]),
        ):
            notes[name] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["fact", "refs"],
                "properties": {
                    "fact": {"type": "string", "maxLength": 48 if choices else 0},
                    "refs": {
                        "type": "array",
                        "maxItems": min(2, len(choices)),
                        "items": {"type": "string", "enum": choices} if choices else {"type": "string"},
                    },
                },
            }
        branch["properties"] = notes | branch["properties"]
        branch["required"] = list(branch["properties"])
    return schema


def replay(client, model, root, row, prompt, staged, *, evidence_first=False, source_notes=False):
    messages = [dict(message) for message in row["messages"][:-1]]
    messages[0]["content"] = prompt + "\n" + messages[0]["content"]
    viewer = messages[-1]
    images, observations, metrics = [], [], []
    for index, frame in enumerate(row["frames"], 1):
        image = image_part(root / frame["path"])
        reference = f"[{frame['alias']}] " if frame.get("alias") else ""
        label = f"{reference}画面 {index}｜{frame['seconds']} 秒"
        if staged:
            query = (
                label
                + "。只描述这一帧真实可见、与问题有关的对象、文字或动作。看不清或画面没显示就说明。"
                + "当前问题："
                + row["question"]
            )
            capture = ask(
                client,
                model,
                [
                    {
                        "role": "user",
                        "content": [
                            image,
                            {"type": "text", "text": query},
                        ],
                    }
                ],
            )
            metrics.append(capture)
            if capture["finish_reason"] != "stop":
                raise ValueError(f"视觉观察未完整结束：{capture['finish_reason']}")
            observations.append(label + "：" + capture["text"])
        else:
            images.extend([{"type": "text", "text": label}, image])
    if observations:
        viewer["content"] += "\n所附画面的观察资料：\n" + "\n".join(observations)
    elif evidence_first or source_notes:
        frame_aliases = {frame["alias"] for frame in row["frames"]}
        captions = "\n".join(
            f"[{alias}] {source['text']}"
            for alias, source in row["references"].items()
            if alias not in frame_aliases
        )
        instruction = (
            "先在 caption 和 visual 各找一句与问题直接相关的事实及短编号，无关或缺失就留空。"
            "再把找到的事实合成台词，evidence_ids 汇合两边的编号。"
            if source_notes
            else "问题有几部分就分别答几部分。字幕事实与画面事实分别引用支持它们的编号。"
        )
        viewer["content"] = [
            {"type": "text", "text": "字幕资料：\n" + (captions or "（无字幕）")},
            *images,
            {
                "type": "text",
                "text": (
                    "\n观众问：" + row["question"] + "\n" + instruction + "\n"
                    "关键细节在资料中找不到时，选择 wait。"
                ),
            },
        ]
    elif images:
        viewer["content"] = [*images, {"type": "text", "text": viewer["content"]}]
    schema = (
        source_notes_schema(row["allowed"], frame_aliases) if source_notes else replay_schema(row["allowed"])
    )
    result = ask(client, model, messages, schema=schema)
    record = (
        {key: row[key] for key in ("id", "kind", "question", "criterion", "allowed", "references")}
        | result
        | {"observations": metrics}
    )
    if result["finish_reason"] == "stop":
        try:
            record["parsed"] = json.loads(result["text"])
        except ValueError:
            pass
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--characters", type=Path)
    parser.add_argument("--instruction", help="additional system text for a separately labelled condition")
    input_mode = parser.add_mutually_exclusive_group()
    input_mode.add_argument("--staged", action="store_true")
    input_mode.add_argument("--evidence-first", action="store_true")
    input_mode.add_argument("--source-notes", action="store_true")
    args = parser.parse_args()
    prompt = "你是大肥鱼。先用一两句回答实际问题。证据不足先暂缓，平常说话自然一点。"
    if args.instruction:
        prompt += "\n" + args.instruction
    if args.characters:
        character = next(
            item for item in json.loads(args.characters.read_text(encoding="utf-8")) if item["id"] == "whale"
        )
        prompt += "角色资料：" + json.dumps(
            {key: character[key] for key in ("core", "style", "rules")}, ensure_ascii=False
        )
    with (
        httpx.Client(base_url=args.endpoint.rstrip("/") + "/", timeout=180, trust_env=False) as client,
        args.output.open("w", encoding="utf-8") as output,
    ):
        for row in read_jsonl(args.replay / "cases.jsonl"):
            try:
                result = replay(
                    client,
                    args.model,
                    args.replay,
                    row,
                    prompt,
                    args.staged,
                    evidence_first=args.evidence_first,
                    source_notes=args.source_notes,
                )
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
                result = {"id": row["id"], "error": str(error)}
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()


if __name__ == "__main__":
    main()
