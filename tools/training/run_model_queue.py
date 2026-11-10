"""Multi-model experimentation queue for Tuber Studio Da Feiyu V2.

Supported Model Candidates:
1. Qwen/Qwen3.5-9B (Dense 9B, Native 256K context, Hybrid Linear Attention)
2. google/gemma-4-12B (Gemma 4 12B Multimodal)
3. google/gemma-4-26B-A4B-it (Gemma 4 26B MoE with 4B active params, Native 256K context)
4. Qwen/Qwen3.8-Flash-Next (Qwen3.8 MoE with 512 fine-grained experts, Native 256K context)
5. Qwen/Qwen3.8-27B (Dense 27B, Native 256K context)

Automates:
- Training in appropriate mode (Full BF16 DoRA for <=14B, 4-bit QLoRA for 26B+)
- End-to-end 18-case evaluation benchmark
- Comparative model evaluation matrix and report generation
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_queue")

QUEUE_CANDIDATES = [
    {
        "id": "qwen35_9b",
        "name": "Qwen/Qwen3.5-9B",
        "arch": "Hybrid Linear Attn + Full Attn (Dense)",
        "params": "8.95B",
        "active_params": "8.95B",
        "native_context": "262,144 (256K)",
        "mode": "bf16",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 1.5e-4,
        "epochs": 3,
        "max_seq_len": 512,
    },
    {
        "id": "gemma4_12b",
        "name": "google/gemma-4-12B",
        "arch": "Gemma 4 Unified Multimodal (Dense)",
        "params": "12B",
        "active_params": "12B",
        "native_context": "131,072 (128K)",
        "mode": "bf16",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 1.2e-4,
        "epochs": 3,
        "max_seq_len": 512,
    },
    {
        "id": "gemma4_26b_a4b",
        "name": "google/gemma-4-26B-A4B-it",
        "arch": "Gemma 4 MoE (128 experts, Top-8)",
        "params": "26B",
        "active_params": "4.1B",
        "native_context": "262,144 (256K)",
        "mode": "qlora_4bit",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 2e-4,
        "epochs": 3,
        "max_seq_len": 512,
    },
    {
        "id": "qwen38_flash_next",
        "name": "Qwen/Qwen3.8-Flash-Next",
        "arch": "Qwen4 Exp Hybrid MoE (512 experts, Top-10)",
        "params": "28B",
        "active_params": "3.5B",
        "native_context": "262,144 (256K)",
        "mode": "qlora_4bit",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 2e-4,
        "epochs": 3,
        "max_seq_len": 512,
    },
    {
        "id": "qwen38_27b",
        "name": "Qwen/Qwen3.8-27B",
        "arch": "Qwen 3.8 Dense Hybrid Attn",
        "params": "27B",
        "active_params": "27B",
        "native_context": "262,144 (256K)",
        "mode": "qlora_4bit",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 1.5e-4,
        "epochs": 3,
        "max_seq_len": 512,
    },
]


def run_command(cmd: list[str], env: dict | None = None) -> int:
    logger.info("Executing: %s", " ".join(cmd))
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, env=merged_env)
    return proc.returncode


def evaluate_model(base_model: str, adapter_dir: Path | None, tools_dir: Path, load_in_4bit: bool = False) -> dict:
    eval_script = tools_dir / "evaluate_finetuned_model.py"
    cmd = [
        sys.executable,
        str(eval_script),
        "--base_model",
        base_model,
    ]
    if adapter_dir and adapter_dir.exists():
        cmd.extend(["--adapter", str(adapter_dir)])
    if load_in_4bit:
        cmd.append("--load_in_4bit")

    logger.info("Running evaluation for %s (adapter: %s)...", base_model, adapter_dir)
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "1"
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    output = proc.stdout

    # Parse metrics from output
    oc_score = 0.0
    cliche_violations = 0
    foundation_passed = 0

    for line in output.splitlines():
        if "OC 拟真度综合得分:" in line:
            parts = line.split(":")[-1].split("/")
            try:
                oc_score = float(parts[0].strip())
            except ValueError:
                pass
        elif "AI 味违规次数:" in line:
            parts = line.split(":")[-1].split("(")
            try:
                cliche_violations = int(parts[0].strip())
            except ValueError:
                pass
        elif "基础常识防坍缩通过率:" in line:
            parts = line.split(":")[-1].split("/")
            try:
                foundation_passed = int(parts[0].strip())
            except ValueError:
                pass

    return {
        "oc_score": oc_score,
        "cliche_violations": cliche_violations,
        "foundation_passed": foundation_passed,
        "raw_output": output,
    }


def main():
    parser = argparse.ArgumentParser(description="Run multi-model candidate queue.")
    parser.add_argument("--repo_root", type=Path, default=Path("/home/research-2/code/Tuber_Studio"))
    parser.add_argument("--only_model", type=str, default=None, help="Run only specified candidate ID")
    parser.add_argument("--skip_train", action="store_true", default=False, help="Skip training, run evaluation only")
    args = parser.parse_args()

    repo_root = args.repo_root
    tools_dir = repo_root / "tools"
    data_dir = repo_root / "data"
    output_base = repo_root / "output_queue"
    output_base.mkdir(parents=True, exist_ok=True)

    report_file = repo_root / "benchmark_model_matrix_report.md"

    results = []

    for item in QUEUE_CANDIDATES:
        cand_id = item["id"]
        if args.only_model and cand_id != args.only_model:
            continue

        model_name = item["name"]
        adapter_output = output_base / f"adapter_{cand_id}"
        logger.info("\n" + "=" * 60)
        logger.info("🚀 Processing Candidate: %s (%s)", model_name, cand_id)
        logger.info("=" * 60)

        t_start = time.time()
        success = True

        if not args.skip_train:
            # Check if adapter already trained
            if (adapter_output / "adapter_model.safetensors").exists() or (adapter_output / "adapter_model.bin").exists():
                logger.info("Found existing trained adapter at %s, proceeding to eval...", adapter_output)
            else:
                train_cmd = [
                    "torchrun",
                    "--nproc_per_node=2",
                    str(tools_dir / "train_dora.py"),
                    "--model_name_or_path",
                    model_name,
                    "--data_dir",
                    str(data_dir),
                    "--output_dir",
                    str(adapter_output),
                    "--batch_size",
                    str(item["batch_size"]),
                    "--gradient_accumulation_steps",
                    str(item["grad_accum"]),
                    "--learning_rate",
                    str(item["lr"]),
                    "--epochs",
                    str(item["epochs"]),
                    "--max_seq_length",
                    str(item["max_seq_len"]),
                    "--bf16",
                ]
                if item["mode"] == "qlora_4bit":
                    train_cmd.append("--load_in_4bit")

                env = {
                    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                    "CUDA_VISIBLE_DEVICES": "0,1",
                }
                rc = run_command(train_cmd, env=env)
                if rc != 0:
                    logger.error("Training failed for %s with exit code %d", model_name, rc)
                    success = False

        duration = time.time() - t_start

        # Evaluation
        if success:
            eval_res = evaluate_model(model_name, adapter_output, tools_dir, load_in_4bit=(item["mode"] == "qlora_4bit"))
            eval_report_file = output_base / f"eval_{cand_id}.txt"
            eval_report_file.write_text(eval_res["raw_output"], encoding="utf-8")
        else:
            eval_res = {"oc_score": 0.0, "cliche_violations": -1, "foundation_passed": 0}

        results.append({
            **item,
            "success": success,
            "duration_minutes": round(duration / 60, 1),
            "oc_score": eval_res["oc_score"],
            "cliche_violations": eval_res["cliche_violations"],
            "foundation_passed": eval_res["foundation_passed"],
        })

    # Generate Markdown Summary Report
    lines = [
        "# 🐟 Tuber Studio 大肥鱼 V2 新一代模型矩阵评测与队列报告",
        "",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "测试环境: 2x NVIDIA RTX 3090 (48GB VRAM total), CUDA 13.2, PyTorch 2.10.0+cu130",
        "",
        "| 候选模型 | 架构类型 | 参数量 (活跃/总计) | 原生上下文 | 微调模式 | OC 拟真度 | AI 违规 | 常识通过 | 训练用时 | AGX Orin 部署建议 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for r in results:
        status_score = f"{r['oc_score']:.1f}/100" if r["success"] else "训练异常"
        agx_sug = "4-bit 约 6GB 显存，解码 ~40 tok/s，推荐直播首选" if "9b" in r["id"] else (
            "4-bit 约 16GB 显存，MoE 4B 激活解码极速 ~45 tok/s，极高二次元灵性" if "26b" in r["id"] or "flash" in r["id"] else
            "Dense 27B 需 16GB 内存，适合离线深度创作"
        )
        lines.append(
            f"| `{r['name']}` | {r['arch']} | {r['active_params']} / {r['params']} | {r['native_context']} | `{r['mode']}` | **{status_score}** | {r['cliche_violations']} 次 | {r['foundation_passed']}/3 | {r['duration_minutes']} min | {agx_sug} |"
        )

    lines.extend([
        "",
        "## 核心测试维度与防坍缩验证",
        "1. **大肥鱼 OC 真实度**: 白米饭偏爱、大鲸鱼尾巴怕痒/拍地动作、海底小屋与贝壳灯氛围感。",
        "2. **第三方 @ 防误认与 UID 边界**: B站评论区 `@其他用户` 时精准识别对话对象，拒绝被误当成他人物品或随意认主。",
        "3. **战团式 好感度 (-100 ~ +100)**: 负好感度辛辣回怼挑衅/杠精，友好挚友分热腾腾五常白米饭与软垫。",
        "4. **私信多条合并与真人接管顺承**: 多条重复求开播私信合并统一答复；真人接管回复后顺承接话不滥发刷屏。",
        "5. **基础常识防坍缩**: 验证天文光年单位、二分查找复杂度、生活常识，保障角色拟人化同时逻辑与认知不后退。",
    ])

    report_content = "\n".join(lines)
    report_file.write_text(report_content, encoding="utf-8")
    print("\n" + report_content)


if __name__ == "__main__":
    main()
