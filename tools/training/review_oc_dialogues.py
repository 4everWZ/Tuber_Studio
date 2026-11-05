"""Screen rewritten dialogues with a separate local reviewer before corpus selection."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from grounded_sft import EXPECTED, read_jsonl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--reviewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--train-count", type=int, default=EXPECTED["train"]["oc"])
    parser.add_argument("--validation-count", type=int, default=EXPECTED["validation"]["oc"])
    parser.add_argument("--max-per-situation", type=int)
    parser.add_argument("--min-score", type=int, choices=(2, 3), default=2)
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForImageTextToText, AutoTokenizer

    rows = [row for row in read_jsonl(args.drafts) if row.get("messages")]
    tokenizer = AutoTokenizer.from_pretrained(args.reviewer, padding_side="left", local_files_only=True)
    model = AutoModelForImageTextToText.from_pretrained(
        args.reviewer, dtype=torch.bfloat16, device_map={"": "cuda:0"}, local_files_only=True
    ).eval()
    directions = (
        "审核中文虚拟主播大肥鱼的两轮对话。她机灵、会打趣、会照顾观众感受。"
        "用户说的事情和答复要有关，第二轮要承接，不能突然硬扯到白饭、尾巴或幻想世界。"
        "扣分项还有角色名串台、把观众的行为说成主播自己的、提到未给出的具体历史、语义错误、像客服、口癖过多。"
        "只输出一个数字。3是自然合格且承接具体，2是基本合格但略平，1是明显答非所问或有上述实质问题。"
    )
    seen, openings, reviewed = set(), Counter(), []
    with torch.inference_mode():
        for start in range(0, len(rows), 12):
            batch = rows[start:start + 12]
            prompts = [
                tokenizer.apply_chat_template(
                    [
                        {"role": "system", "content": directions},
                        {"role": "user", "content": row["draft"]},
                    ],
                    tokenize=False, add_generation_prompt=True, enable_thinking=False,
                )
                for row in batch
            ]
            inputs = tokenizer(prompts, padding=True, return_tensors="pt", add_special_tokens=False).to(model.device)
            predictions = model.generate(**inputs, max_new_tokens=5, do_sample=False)
            scores = tokenizer.batch_decode(predictions[:, inputs.input_ids.shape[-1]:], skip_special_tokens=True)
            for row, score in zip(batch, scores, strict=True):
                if score.strip() in {str(value) for value in range(args.min_score, 4)}:
                    reviewed.append((int(score.strip()), row))
            print(f"reviewed={start + len(batch)} accepted={len(reviewed)}", flush=True)
    result, counts, situations = [], Counter(), Counter()
    limits = {"train": args.train_count, "validation": args.validation_count}
    for score, row in sorted(reviewed, key=lambda item: -item[0]):
        split = row["split"]
        group = row["sources"][0]["group"]
        if counts[split] >= limits[split] or (
            args.max_per_situation and situations[group] >= args.max_per_situation
        ):
            continue
        normal = [re.sub(r"[\W_]+", "", m["content"]) for m in row["messages"][1:]]
        if any(not value or value in seen for value in normal) or openings[normal[1][:7]] >= 4:
            continue
        seen.update(normal)
        openings[normal[1][:7]] += 1
        counts[split] += 1
        situations[group] += 1
        result.append({key: value for key, value in row.items() if key != "draft"} | {
            "review": "model_reviewed", "review_score": score, "reviewer": args.reviewer.name,
        })
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in result), encoding="utf-8")
    print(dict(counts), flush=True)


if __name__ == "__main__":
    main()
