"""Replay the private P2 visual controls against the application gateway or a frozen run."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from tuber_studio.agent.tasks.tools import decode_review_frame
from tuber_studio.agent.tasks.visual_specificity import review_visual_specificity
from tuber_studio.infra.inference.api_providers import APIProvider

from .creative_browser_receipt import inspect_file
from .omni_chat_loop import connection
from .owner_memory_video import await_admission

CASES = {
    "hat": {"items.1.title": {"红色盔帽"}},
    "hat-shared": {},
    "screen": {f"items.1.{f}": {"屏风"} for f in ("title", "rationale")},
    "screen-shared": {},
    "lights": {},
    "r5": {f"items.0.{f}": {"红色头盔", "头盔"} for f in ("title", "rationale")},
    "r5-title": {"items.0.title": {"红色头盔", "头盔"}},
}


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


async def run(args):
    inputs = load(args.directory / "cases.json")
    if set(inputs) != set(CASES):
        raise ValueError("缺少原有对照或 r5 返修字段")
    pair, title_only = inputs["r5"], inputs["r5-title"]
    if (not any("头盔" in text for text in pair["inventory"]) or not pair["raw_observation"]
            or title_only != pair | {"fields": {"items.0.title": pair["fields"]["items.0.title"]}}):
        raise ValueError("r5 的同词上游观察、原窗或标题单审输入未绑定")
    frozen = load(args.frozen) if args.frozen else None
    provider = None
    if args.live:
        endpoint = connection(os.environ)
        if not endpoint:
            raise ValueError("真实回放需要普通应用网关连接")
        url, key = endpoint
        provider = APIProvider(dict(base_url=url + "/v1", model=os.getenv("OMNI_MODEL_ALIAS", "omni-default"),
                                    context_source="model_card", structured_output="structural_tag",
                                    prompt_token_endpoint=True), key)
    results = {}
    try:
        for name, expected in CASES.items():
            case = inputs[name]
            slot = 0 if name in {"lights", "r5", "r5-title"} else 1
            suffixes = ("title",) if name == "r5-title" else ("title", "rationale")
            if set(case["fields"]) != {f"items.{slot}.{suffix}" for suffix in suffixes}:
                raise ValueError(f"{name} 送审字段不完整")
            media = args.directory / case["file"]
            if hashlib.sha256(media.read_bytes()).hexdigest() != case["sha256"]:
                raise ValueError("源窗文件与冻结原件不一致")
            inspect_file(media, case["clip"])
            image = await decode_review_frame(media, case["clip"], case["frame"]["seconds"])
            calls = []
            audit = []

            async def generate(messages, schema, *, max_tokens):
                packet = json.dumps([messages, schema, max_tokens], ensure_ascii=False)
                signature = hashlib.sha256(packet.encode()).hexdigest()
                if provider:
                    async def infer():
                        return await provider.generate_json(messages, schema, max_tokens=max_tokens, use_remaining_window=True)

                    reply = await await_admission(name, infer, asyncio.get_running_loop().time() + 360, [], lambda *_: None)
                else:
                    call = frozen[name]["calls"][len(calls)]
                    if call["request"] != signature:
                        raise ValueError(f"{name} 请求与冻结回放不一致")
                    reply = call["reply"]
                calls.append({"request": signature, "reply": reply})
                return reply

            rows = await review_visual_specificity(case["fields"], image, case["primary_frame"],
                case["raw_observation"], generate, 4096, inventory=case["inventory"], reviews=audit, frame=case["frame"])
            targets = {p: sorted({c["target"] for c in row["claims"]}) for p, row in rows.items()}
            passed = (set(targets) == set(expected) and all(
                values and set(values) <= expected[path] and not rows[path].get("unlocated")
                for path, values in targets.items()))
            result = dict(passed=passed, targets=targets, audit=audit, calls=calls)
            if frozen and result != frozen[name]:
                raise ValueError(f"{name} 冻结回包未用尽或裁定发生变化")
            results[name] = result
            if args.live:
                args.live.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")
            print(name, "PASS" if passed else "FAIL", targets, flush=True)
    finally:
        if provider:
            await provider.close()
    return all(row["passed"] for row in results.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="私有 cases.json 与已归档原窗 MP4 所在目录")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", type=Path, help="运行普通网关，将真实回包写到指定私有文件")
    mode.add_argument("--frozen", type=Path, help="逐请求比对冻结回包，纯离线运行")
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
