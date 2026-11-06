"""Propose original Da Feiyu dialogues from licensed Chinese role-play scenes."""

import argparse
import json
import random
from pathlib import Path

SOURCE = (
    "https://huggingface.co/datasets/shibing624/roleplay-zh-sharegpt-gpt4-data/"
    "blob/9c8bd488eb5cb4489a8c2c31518bd0d64154578d/sharegpt_formatted_data-evol-gpt4.jsonl"
)
CHATANIME_SOURCE = (
    "https://github.com/LanlanQiu/ChatAnime/blob/2169710041b1130b79530be610c0adef886f96fb/data/human.json"
)
EXCLUDE = (
    "原神",
    "崩坏",
    "火影",
    "海贼王",
    "宝可梦",
    "漫威",
    "哈利波特",
    "哆啦",
    "初音",
    "路飞",
    "纳西妲",
    "鸣人",
    "淫",
    "性爱",
    "色情",
    "裸",
    "性奴",
    "强奸",
    "纳粹",
)
SETTINGS = (
    "这次是日常聊天，从原材料提取一个具体生活难题，重新给出能实行的一步。",
    "这次是观众失手或遭遇尴尬，大肥鱼先接住情绪，然后提出一个小行动。",
    "这次是观众来分享一件小喜事，她抓住里面一个具体细节打趣。",
    "这次是一起做东西或玩游戏，两轮之间应有具体的进展，保留对象或数量。",
    "这次是意见不一致，说清她的选择和理由，并接住观众的追问。",
    "这次是主播和观众一起即兴编故事，有具体动作，不要把故事重推回观众。",
    "这次是认识一阵子的观众提起之前的计划，回合内给足可以追溯的事实。",
    "这次是朋友互相拆台，可以嘴硬，第二轮要用一个实际动作表现关心。",
)


def candidates(path):
    rows = []
    with path.open(encoding="utf-8") as source:
        for index, line in enumerate(source):
            row = json.loads(line)
            card = row.get("system_prompt") or ""
            turns = row.get("conversations", [])[:6]
            if len(turns) < 4:
                continue
            text = card + "\n" + "\n".join(turn["value"] for turn in turns)
            if not any(word in text for word in EXCLUDE):
                rows.append((index, text[:1600]))
    random.Random(42).shuffle(rows)
    return rows


def chatanime_candidates(path, variants):
    # All anime characters answer the same source situations. Retain just the
    # original viewer question and group every rewrite by that situation.
    scenarios = {}
    for rows in json.loads(path.read_text(encoding="utf-8")).values():
        for row in rows:
            identity = int(row["dialog_id"])
            question = row["question_r1"].strip()
            if scenarios.setdefault(identity, question) != question:
                raise ValueError(f"ChatAnime scenario {identity} has divergent first questions")
    return [
        (
            f"chatanime-{identity:02d}-{variant}",
            f"chatanime-{identity:02d}",
            question,
            "validation" if identity % 7 == 0 else "train",
            SETTINGS[(identity + variant) % len(SETTINGS)],
            CHATANIME_SOURCE,
        )
        for identity, question in sorted(scenarios.items())
        for variant in range(variants)
    ]


def parse_dialogue(text):
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    markers = ("观众：", "大肥鱼：", "观众：", "大肥鱼：")
    if len(lines) != len(markers) or not all(line.startswith(marker) for line, marker in zip(lines, markers)):
        return None
    contents = [line[len(marker) :].strip() for line, marker in zip(lines, markers)]
    if any(not 5 <= len(content) <= 110 for content in contents):
        return None
    return [
        {"role": "system", "content": "你是大肥鱼。"},
        *[
            {"role": "user" if index % 2 == 0 else "assistant", "content": content}
            for index, content in enumerate(contents)
        ],
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("sharegpt", "chatanime"), default="sharegpt")
    parser.add_argument("--variants", type=int, default=1, help="rewrites per ChatAnime situation")
    parser.add_argument("--teacher", type=Path, required=True)
    parser.add_argument("--characters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=480)
    parser.add_argument("--batch", type=int, default=12)
    args = parser.parse_args()
    if args.variants < 1:
        parser.error("variants must be positive")

    import torch
    from transformers import AutoModelForImageTextToText, AutoTokenizer, set_seed

    set_seed(42)
    card = next(
        item for item in json.loads(args.characters.read_text(encoding="utf-8")) if item["id"] == "whale"
    )
    core = {
        key: card["core"][key]
        for key in ("identity", "traits", "background", "interaction")
        if key in card["core"]
    }
    instruction = (
        "你是中文动画直播编剧。只借鉴给出的合成场景里的人际处境或对话走向，"
        "把场景重新写成观众和大肥鱼的两轮互动。问题和回复都要重写，原角色、身份、世界观和台词都换掉。"
        "她会回应具体细节，被逗会还一句，吃瘪会认，观众难过时会放慢。"
        "保留观众和主播各自做了什么。第二轮由观众改变一个处境或提出具体请求，大肥鱼顺着变化继续演。"
        "动作融入能念出来的短台词。只有题目自然带到时才用白饭、尾巴、海底、贝壳灯或气泡。"
        "观众从一件实际事情开口，避免用「你刚才说」开头虚构场景之前的对话。"
        "严格输出四行，依次以观众：、大肥鱼：、观众：、大肥鱼：开头。"
        "角色资料："
        + json.dumps({"core": core, "style": card["style"], "rules": card["rules"]}, ensure_ascii=False)
    )
    tokenizer = AutoTokenizer.from_pretrained(args.teacher, padding_side="left", local_files_only=True)
    model = AutoModelForImageTextToText.from_pretrained(
        args.teacher,
        dtype=torch.bfloat16,
        device_map={"": "cuda:0"},
        local_files_only=True,
    ).eval()
    if args.source_kind == "chatanime":
        rows = chatanime_candidates(args.source, args.variants)
    else:
        rows = [
            (
                f"rp-{index}",
                f"rp-{index}",
                scene,
                "validation" if index % 9 == 0 else "train",
                SETTINGS[index % len(SETTINGS)],
                SOURCE,
            )
            for index, scene in candidates(args.source)
        ]
    rows = rows[: args.limit]
    # Split by source scene before rewriting. Outputs remain drafts until reviewed.
    done = set()
    if args.output.exists():
        with args.output.open(encoding="utf-8") as previous:
            done.update(json.loads(line)["id"] for line in previous if line.strip())
    with args.output.open("a", encoding="utf-8") as output, torch.inference_mode():
        for start in range(0, len(rows), args.batch):
            batch = [row for row in rows[start : start + args.batch] if row[0] not in done]
            if not batch:
                continue
            prompts = [
                tokenizer.apply_chat_template(
                    [
                        {"role": "system", "content": instruction},
                        {"role": "user", "content": setting + "\n素材：\n" + scene},
                    ],
                    tokenize=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
                for _, _, scene, _, setting, _ in batch
            ]
            inputs = tokenizer(prompts, padding=True, return_tensors="pt", add_special_tokens=False).to(
                model.device
            )
            result = model.generate(**inputs, max_new_tokens=180, do_sample=True, temperature=0.75, top_p=0.9)
            answers = tokenizer.batch_decode(
                result[:, inputs.input_ids.shape[-1] :], skip_special_tokens=True
            )
            for (identity, group, _, split, _, uri), answer in zip(batch, answers, strict=True):
                row = {
                    "id": identity,
                    "category": "oc",
                    "review": "draft",
                    "split": split,
                    "sources": [{"group": group, "uri": uri, "license": "Apache-2.0"}],
                    "teacher": args.teacher.name,
                    "messages": parse_dialogue(answer),
                    "draft": answer,
                }
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(f"processed {start + len(batch)} / {len(rows)}", flush=True)


if __name__ == "__main__":
    main()
