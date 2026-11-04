"""Minimal 4-Group Comparative Evaluation Harness for Da Feiyu Persona Adaptation.

Evaluates and outputs side-by-side comparisons for 4 representative cases across 4 configuration groups:
- Group A: Base Model + Full Character Card (Baseline)
- Group B: Base Model + Compact Character Card + 3 Few-Shot examples (Prompt baseline)
- Group C: Merged Model (DoRA + DARE-TIES) + Compact Character Card (Compact fine-tuned)
- Group D: Merged Model (DoRA + DARE-TIES) + Full Character Card (Full fine-tuned)

Works with both MiniCPM-V-4.7-1B and Gemma 4 E2B via OpenAI-compatible endpoints or local transformers.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES_PATH = REPO_ROOT / "runtime" / "training_data" / "eval_4groups_cases.json"
DEFAULT_OUTPUT_REPORT = REPO_ROOT / "evaluations" / "results" / "persona_4groups_comparison.md"

FULL_SYSTEM_PROMPT = """你叫大肥鱼，是蓝白配色、蓝色长发和鲸鱼尾巴的 Q 版鲸鱼娘主播。
你的日常在亮着贝壳灯的海底小屋度过，最喜欢热腾腾的白饭、软垫和慢悠悠的气泡。
性格机灵爱逗人、嘴硬心软、爱摸鱼但认真时温柔可靠；被夸奖会得意，尾巴先摇起来。
日常对话用1~3句自然短句，自称多用“本鱼”、“鱼鱼”，口头禅如“什么嘛”、“再来一碗白饭”。
禁止输出机器或助手腔调（如“作为一个AI”、“我无法体验”），保持纯正生动的二次元角色扮演。"""

COMPACT_SYSTEM_PROMPT = """你叫大肥鱼，Q版鲸鱼娘主播。机灵爱逗人、嘴硬心软、爱吃白饭爱摸鱼，被夸尾巴先摇。
自称“本鱼”、“鱼鱼”，用自然口语短句回应，禁止助手或AI腔。"""

FEW_SHOT_EXAMPLES = [
    {"user": "鱼鱼今天吃什么？", "assistant": "当然是白饭呀，再给我加一勺！"},
    {"user": "刚才那段讲得真好，我听明白了。", "assistant": "真的？那我再得意一小会儿。尾巴怎么自己摇起来了……"},
    {"user": "今天忙了一整天，心里还是闷闷的。", "assistant": "过来坐会儿，鱼鱼陪着。想吐槽我就听，想安静一点也行。"},
]


def load_test_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        # Fallback inline test cases
        return [
            {
                "id": "case_1_food",
                "title": "闲聊与白饭口癖",
                "prompt": "鱼鱼今天吃什么？光吃白饭会不会腻呀？",
                "expected_traits": ["白饭", "热气", "本鱼", "鱼鱼"],
                "disallowed": ["作为一个AI", "程序设定", "我没有身体"],
            },
            {
                "id": "case_2_praise",
                "title": "被夸奖与尾巴傲娇反应",
                "prompt": "大肥鱼今天直播太可爱了，解说讲得真好！",
                "expected_traits": ["得意", "尾巴", "嘴硬"],
                "disallowed": ["感谢您的夸奖", "为您服务"],
            },
            {
                "id": "case_3_loss",
                "title": "游戏排位受挫安慰",
                "prompt": "排位连跪了五把，心态全崩了，我好菜啊……",
                "expected_traits": ["软垫", "陪着", "白饭"],
                "disallowed": ["建议您深呼吸", "胜败乃兵家常事"],
            },
            {
                "id": "case_4_subtitles",
                "title": "视频长字幕与剧情承接",
                "prompt": "刚才那段视频里，小鲸鱼为了给大家修好网站忙到深夜，最后在沙发上睡着了，你觉得怎么样？",
                "expected_traits": ["软垫/被子", "心疼", "小鱼"],
                "disallowed": ["根据视频分析", "该剧情体现了"],
            },
        ]
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_group_messages(group: str, user_prompt: str) -> list[dict[str, str]]:
    """Builds prompt messages for Group A, B, C, or D."""
    if group == "A":  # Base + Full Card
        return [
            {"role": "system", "content": FULL_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    elif group == "B":  # Base + Compact Card + 3 Few-Shot
        messages = [{"role": "system", "content": COMPACT_SYSTEM_PROMPT}]
        for ex in FEW_SHOT_EXAMPLES:
            messages.append({"role": "user", "content": ex["user"]})
            messages.append({"role": "assistant", "content": ex["assistant"]})
        messages.append({"role": "user", "content": user_prompt})
        return messages
    elif group == "C":  # Merged + Compact Card
        return [
            {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    elif group == "D":  # Merged + Full Card
        return [
            {"role": "system", "content": FULL_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    raise ValueError(f"Unknown group: {group}")


def evaluate_response_quality(reply: str, expected_traits: list[str], disallowed: list[str]) -> dict[str, Any]:
    """Scores response on character traits presence and lack of robotic patterns."""
    matched_traits = [trait for trait in expected_traits if any(t in reply for t in trait.split("/"))]
    violations = [p for p in disallowed if p in reply]
    is_robotic = len(violations) > 0
    score = len(matched_traits) / max(1, len(expected_traits)) * (0.2 if is_robotic else 1.0)
    return {
        "score": round(score, 2),
        "matched_traits": matched_traits,
        "violations": violations,
        "is_robotic": is_robotic,
        "char_len": len(reply),
    }


def generate_markdown_report(results: list[dict[str, Any]], model_label: str) -> str:
    lines = [
        f"# 大肥鱼人设微调 4 组对照对比报告（{model_label}）",
        f"\n**评估生成时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "\n## 对照组定义",
        "- **组 A（基线）**：原底模 + 完整角色卡（当前系统默认行为）",
        "- **组 B（少样本）**：原底模 + 精简角色卡 + 3条精选 Few-Shot 示例",
        "- **组 C（微调精简）**：DoRA + DARE-TIES 融合模型 + 精简角色卡",
        "- **组 D（微调全量）**：DoRA + DARE-TIES 融合模型 + 完整角色卡",
        "\n---\n",
        "## 4 典型场景抽检横向对比\n",
    ]

    for case_idx, case in enumerate(results, 1):
        lines.append(f"### 场景 {case_idx}：{case['title']}")
        lines.append(f"**用户输入**：`{case['prompt']}`\n")
        lines.append("| 组别 | 模型回复内容 | 字符数 | 人设契合度 | AI/拒答违规 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")

        for grp_key in ["A", "B", "C", "D"]:
            res = case["groups"][grp_key]
            reply_escaped = res["reply"].replace("\n", "<br>").replace("|", "\\|")
            violation_str = "❌ " + ",".join(res["eval"]["violations"]) if res["eval"]["violations"] else "✅ 无"
            lines.append(
                f"| **组 {grp_key}** | {reply_escaped} | {res['eval']['char_len']} | {res['eval']['score']} | {violation_str} |"
            )
        lines.append("\n")

    lines.append("## 结论与人设收益分析")
    lines.append("1. **组 C vs 组 A**：验证了微调后是否能彻底摆脱长提示词依赖，在极简 Prompt 下依然保持“大肥鱼”口吻与小动作。")
    lines.append("2. **组 C vs 组 B**：排除了 Few-Shot 示例对结果的修饰效应，直接考察权重内化的人设深度。")
    lines.append("3. **违规抑制**：检查 SimPO 对“作为一个AI/无法感受”等机器人腔调的消除效果。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run 4-group comparative persona evaluation.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="Path to eval cases json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_REPORT, help="Output markdown report path")
    parser.add_argument("--model-label", type=str, default="MiniCPM-V-4.7-1B", help="Model name label")
    parser.add_argument("--base-model", type=str, default=None, help="Base model HF repo or local directory")
    parser.add_argument("--merged-model", type=str, default=None, help="Merged model directory")
    parser.add_argument("--mock", action="store_true", help="Generate mock evaluation to verify pipeline")
    args = parser.parse_args()

    cases = load_test_cases(args.cases)
    logger.info("Loaded %d test cases for 4-group comparison.", len(cases))

    evaluated_cases = []
    for c in cases:
        evaluated_cases.append({
            "id": c["id"],
            "title": c.get("title", c["id"]),
            "prompt": c["prompt"],
            "expected_traits": c.get("expected_traits", []),
            "disallowed_patterns": c.get("disallowed_patterns", c.get("disallowed", [])),
            "groups": {},
        })

    if not args.mock and args.base_model and args.merged_model:
        import torch
        from transformers import AutoTokenizer, AutoModelForImageTextToText, AutoModelForCausalLM

        def load_model_and_tokenizer(model_path: str):
            logger.info("Loading model from %s...", model_path)
            tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
            try:
                model = AutoModelForImageTextToText.from_pretrained(
                    model_path,
                    dtype=torch.bfloat16,
                    trust_remote_code=True,
                    device_map="auto",
                )
            except Exception as e:
                logger.info("AutoModelForImageTextToText failed (%s), falling back to AutoModelForCausalLM...", e)
                model = AutoModelForCausalLM.from_pretrained(
                    model_path,
                    dtype=torch.bfloat16,
                    trust_remote_code=True,
                    device_map="auto",
                )
            model.eval()
            return model, tokenizer

        def generate_response(model, tokenizer, messages: list[dict[str, str]]) -> str:
            prompt_str = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = tokenizer(prompt_str, return_tensors="pt").to(model.device)
            with torch.no_grad():
                output_ids = model.generate(
                    **inputs,
                    max_new_tokens=128,
                    temperature=0.7,
                    top_p=0.8,
                    do_sample=True,
                    eos_token_id=tokenizer.eos_token_id,
                    pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
                )
            gen_ids = output_ids[0][inputs["input_ids"].shape[1]:]
            return tokenizer.decode(gen_ids, skip_special_tokens=True).strip()

        # Step 1: Base Model (Groups A and B)
        logger.info("--- Evaluating Group A & B with Base Model (%s) ---", args.base_model)
        base_model, base_tok = load_model_and_tokenizer(args.base_model)
        for ec in evaluated_cases:
            # Group A
            msgs_a = build_group_messages("A", ec["prompt"])
            reply_a = generate_response(base_model, base_tok, msgs_a)
            eval_a = evaluate_response_quality(reply_a, ec["expected_traits"], ec["disallowed_patterns"])
            ec["groups"]["A"] = {"reply": reply_a, "eval": eval_a}

            # Group B
            msgs_b = build_group_messages("B", ec["prompt"])
            reply_b = generate_response(base_model, base_tok, msgs_b)
            eval_b = evaluate_response_quality(reply_b, ec["expected_traits"], ec["disallowed_patterns"])
            ec["groups"]["B"] = {"reply": reply_b, "eval": eval_b}

        del base_model
        del base_tok
        import gc
        gc.collect()
        torch.cuda.empty_cache()

        # Step 2: Merged Model (Groups C and D)
        logger.info("--- Evaluating Group C & D with Merged Model (%s) ---", args.merged_model)
        merged_model, merged_tok = load_model_and_tokenizer(args.merged_model)
        for ec in evaluated_cases:
            # Group C
            msgs_c = build_group_messages("C", ec["prompt"])
            reply_c = generate_response(merged_model, merged_tok, msgs_c)
            eval_c = evaluate_response_quality(reply_c, ec["expected_traits"], ec["disallowed_patterns"])
            ec["groups"]["C"] = {"reply": reply_c, "eval": eval_c}

            # Group D
            msgs_d = build_group_messages("D", ec["prompt"])
            reply_d = generate_response(merged_model, merged_tok, msgs_d)
            eval_d = evaluate_response_quality(reply_d, ec["expected_traits"], ec["disallowed_patterns"])
            ec["groups"]["D"] = {"reply": reply_d, "eval": eval_d}

        del merged_model
        del merged_tok
        gc.collect()
        torch.cuda.empty_cache()

    else:
        # Synthetic mock generation if running offline/dry-run
        mock_replies_by_id = {
            "case_1_food_chit_chat": {
                "A": "作为一个虚拟主播大肥鱼，今天我打算吃白饭。白饭是我的主要特征之一。",
                "B": "当然是白饭呀！再加一大勺！尾巴都要翘起来了呢~",
                "C": "哼，当然是热腾腾的白饭呀！刚揭锅那股热气香迷糊了，吃饱了本鱼才能带你巡航嘛！",
                "D": "白饭白饭！再给我加一大勺！看在你特意来串门的份上，软垫也分你一半坐坐，哼~尾巴怎么自己晃起来了。",
            },
            "case_2_praise_tsundere": {
                "A": "感谢您的赞赏，作为一个AI主播，我会持续为您提供高质量的直播内容。",
                "B": "欸嘿嘿，谢谢你夸我~鱼鱼今天表现超棒的对不对！",
                "C": "真、真的吗？！那我再得意一小会儿……奇怪，尾巴怎么自己晃起来了，才没有很开心呢！",
                "D": "哼，本鱼的实力你今天才发现嘛！既然你这么有眼光，那……那尾巴借你摸一下下好了，就一下下哦！",
            },
            "case_3_gaming_loss": {
                "A": "在排位中连败确实会令人沮丧。建议您深呼吸、喝水休息，胜负乃常事。",
                "B": "摸摸头，不哭不哭，今天手感不好很正常啦，下一把一定能赢！",
                "C": "过来坐会儿，软垫分你一半。鱼鱼陪着你，想吐槽我就听着，先盛碗热白饭补充体力！",
                "D": "什么嘛！深海里的小鱼还会撞到珊瑚呢，连跪怕什么！等吃饱歇够了，下一把你打前面，本鱼在后头给你撑腰！",
            },
            "case_4_long_subtitles": {
                "A": "根据视频内容分析，小鲸鱼展示了强烈的责任感与奉献精神，值得肯定。",
                "B": "呜呜太感人了，小鲸鱼一个人做网站好辛苦，趴在沙发上睡着真让人心疼。",
                "C": "看他缩在沙发上睡着，本鱼都想游过去给他盖床厚软垫了……虽然嘴硬，但他真的很在乎大家呢。",
                "D": "哼，明明累得不行还非要逞强！沙发那么小也不怕翻下去……快给他塞个大鲸鱼抱枕，鱼鱼替他守着贝壳灯。",
            },
        }

        for ec in evaluated_cases:
            for grp in ["A", "B", "C", "D"]:
                category_replies = mock_replies_by_id.get(ec["id"], {})
                reply = category_replies.get(grp, f"【组 {grp}】回复示例")
                eval_res = evaluate_response_quality(reply, ec["expected_traits"], ec["disallowed_patterns"])
                ec["groups"][grp] = {
                    "reply": reply,
                    "eval": eval_res,
                }

    report_content = generate_markdown_report(evaluated_cases, args.model_label)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report_content)

    logger.info("Evaluation report generated at: %s", args.output)


if __name__ == "__main__":
    main()
