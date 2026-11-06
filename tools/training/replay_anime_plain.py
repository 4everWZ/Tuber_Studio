"""Replay first-person dialogue without the live or operations JSON schema."""

import argparse
from pathlib import Path

import httpx
from build_grounded_corpus import dump
from grounded_sft import read_jsonl

from tuber_studio.persona import GROUNDED_DAFEIYU_MODEL


def replay(client, row, system, temperature):
    messages = [{"role": "system", "content": system if system is not None else row["card"]}]
    replies = []
    for user_text in row["turns"]:
        messages.append({"role": "user", "content": user_text})
        response = (
            client.post(
                "chat/completions",
                json={
                    "model": GROUNDED_DAFEIYU_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "seed": 42,
                    "max_tokens": 180,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            .raise_for_status()
            .json()
        )
        choice = response["choices"][0]
        answer = choice["message"]["content"]
        replies.append({"text": answer, "finish": choice["finish_reason"], "usage": response.get("usage")})
        messages.append({"role": "assistant", "content": answer})
    return replies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--system-file", type=Path)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    system = args.system_file.read_text(encoding="utf-8").strip() if args.system_file else None
    with (
        httpx.Client(base_url=args.endpoint.rstrip("/") + "/v1/", timeout=120, trust_env=False) as client,
        args.output.open("w", encoding="utf-8") as output,
    ):
        for row in read_jsonl(args.cases):
            try:
                result = {"replies": replay(client, row, system, args.temperature)}
            except (httpx.HTTPError, KeyError, ValueError) as error:
                result = {"error": str(error)}
            output.write(dump(row | result) + "\n")
            output.flush()


if __name__ == "__main__":
    main()
