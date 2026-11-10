"""Run candidate training and the legacy 18-case smoke suite in derived output directories."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_queue")

QUEUE_CANDIDATES = [
    {
        "id": "qwen35_2b",
        "name": "Qwen/Qwen3.5-2B",
        "arch": "Hybrid Linear Attn (Dense)",
        "params": "2.1B",
        "active_params": "2.1B",
        "native_context": "262,144 (256K)",
        "mode": "bf16",
        "batch_size": 2,
        "grad_accum": 2,
        "lr": 2.0e-4,
        "epochs": 3,
        "max_seq_len": 512,
        "vram": "~1.2 GB (Q4_K_M)",
        "deploy_sug": "⚡ **端侧极速核弹**：量化仅 1.2 GB，预估 AGX 吞吐 55–70 tok/s，适合秒级高并发伴播",
    },
    {
        "id": "qwen35_4b",
        "name": "Qwen/Qwen3.5-4B",
        "arch": "Hybrid Linear Attn (Dense)",
        "params": "4.2B",
        "active_params": "4.2B",
        "native_context": "262,144 (256K)",
        "mode": "bf16",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 1.5e-4,
        "epochs": 3,
        "max_seq_len": 512,
        "vram": "2.51 GB (Q4_K_M)",
        "deploy_sug": "⚡ **AGX Orin 极速伴播副模型**：256K 原生长上下文，响应超快",
    },
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
        "vram": "5.23 GB (Q4_K_M)",
        "deploy_sug": "🔥 **AGX Orin 生产主力**：首字延迟 <380ms，解码 23 tok/s",
    },
    {
        "id": "gemma4_12b",
        "name": "google/gemma-4-12B",
        "arch": "Gemma 4 Unified Multimodal (Dense)",
        "params": "12B",
        "active_params": "12B",
        "native_context": "262,144 (256K)",
        "mode": "qlora_4bit",
        "batch_size": 1,
        "grad_accum": 4,
        "lr": 2.0e-4,
        "epochs": 3,
        "max_seq_len": 512,
        "vram": "~7.8 GB (4-bit)",
        "deploy_sug": "多模态候选，独立验证处理器、聊天模板、共驻与实际窗口后再选型",
    },
]


def disk_has_room(path, min_free_gb=30.0):
    free_gb = shutil.disk_usage(path).free / (1024**3)
    logger.info("Free space %.2f GB (minimum %.2f GB)", free_gb, min_free_gb)
    return free_gb >= min_free_gb


def run_command(cmd: list[str], env: dict | None = None) -> int:
    logger.info("Executing: %s", " ".join(cmd))
    merged_env = os.environ.copy()
    merged_env["HF_HUB_DISABLE_XET"] = "1"
    merged_env["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, env=merged_env)
    return proc.returncode


def evaluate_model(
    base_model: str, adapter_dir: Path | None, tools_dir: Path, load_in_4bit: bool = False
) -> dict:
    eval_script = tools_dir / "evaluate_finetuned_model.py"
    if not eval_script.exists():
        eval_script = tools_dir / "training" / "evaluate_finetuned_model.py"
    if not eval_script.exists():
        eval_script = tools_dir.parent / "evaluate_finetuned_model.py"
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
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=True)
    output = proc.stdout

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


def update_markdown_report(results: list[dict], report_paths: list[Path]):
    lines = [
        "# 候选模型训练与旧版冒烟记录",
        "",
        f"**最后更新时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "训练环境为 Workstation 双卡 RTX 3090。本表的关键词和三题常识结果用于训练诊断。",
        "生产选型见[独立持出回放](../evaluations/results/workstation-candidate-replay-20261110.md)。",
        "",
        "## 1. 候选模型微调对比矩阵",
        "",
        "| 候选模型 | 架构特征 | 参数量 (活跃/总计) | 原生上下文 | 微调模式 | OC 拟真度 | AI 味违规 | 基础常识通过率 | 显存占用 | 训练用时 | 落地状态与建议 |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
    ]

    for r in results:
        if r.get("evaluated"):
            status_score = f"**{r['oc_score']:.1f} / 100**"
            cliche_str = f"**{r['cliche_violations']} 次**"
            found_str = (
                f"**{r['foundation_passed']}/3**"
                if r["foundation_passed"] == 3
                else f"{r['foundation_passed']}/3"
            )
        elif r.get("training"):
            status_score = "*训练中...*"
            cliche_str = "-"
            found_str = "-"
        elif not r.get("success", True):
            status_score = "*训练异常*"
            cliche_str = "-"
            found_str = "-"
        else:
            status_score = "*待排队*"
            cliche_str = "-"
            found_str = "-"

        dur_str = f"{r.get('duration_minutes', 0):.1f} min" if r.get("duration_minutes") else "-"
        lines.append(
            f"| `{r['name']}` | {r['arch']} | {r['active_params']} / {r['params']} | {r['native_context']} | `{r['mode']}` | {status_score} | {cliche_str} | {found_str} | {r.get('vram', '-')} | {dur_str} | {r['deploy_sug']} |"
        )

    report_content = "\n".join(lines)
    for p in report_paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(report_content, encoding="utf-8")
        except Exception as e:
            logger.warning("Could not write report to %s: %s", p, e)


def main():
    parser = argparse.ArgumentParser(
        description="Run multi-model candidate queue with automated safety and markdown."
    )
    parser.add_argument("--repo_root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--only_model", type=str, default=None, help="Run only specified candidate ID")
    parser.add_argument(
        "--skip_train", action="store_true", default=False, help="Skip training, run evaluation only"
    )
    parser.add_argument(
        "--force_retrain",
        action="store_true",
        default=False,
        help="Train into a new directory and retain existing adapters",
    )
    args = parser.parse_args()

    if args.skip_train and args.force_retrain:
        parser.error("--skip_train cannot be combined with --force_retrain")
    repo_root = args.repo_root
    tools_dir = repo_root / "tools"
    output_base = repo_root / "output_queue"
    data_dir = output_base / "compiled_data"
    output_base.mkdir(parents=True, exist_ok=True)

    if not args.skip_train:
        build_script = tools_dir / "training" / "build_authentic_sft_dataset.py"
        if not build_script.exists():
            build_script = repo_root / "build_authentic_sft_dataset.py"
        if run_command([sys.executable, str(build_script), "--output_dir", str(data_dir)]):
            raise SystemExit("Grouped SFT compilation failed")

    report_paths = [output_base / "candidate_models_finetune_matrix.md"]

    results = [dict(c) for c in QUEUE_CANDIDATES]
    update_markdown_report(results, report_paths)

    for r in results:
        cand_id = r["id"]
        if args.only_model and cand_id != args.only_model:
            continue

        model_name = r["name"]
        suffix = f"-{uuid4().hex[:8]}" if args.force_retrain else ""
        adapter_output = output_base / f"adapter_{cand_id}{suffix}"
        eval_report_file = output_base / f"eval_{cand_id}{suffix}.txt"

        logger.info("\n" + "=" * 65)
        logger.info("🚀 Processing Candidate: %s (%s)", model_name, cand_id)
        logger.info("=" * 65)

        # 1. Disk check
        if not args.skip_train and not disk_has_room(output_base):
            logger.error("Skipping %s due to low disk space.", model_name)
            r["success"] = False
            update_markdown_report(results, report_paths)
            continue

        t_start = time.time()
        success = True
        r["training"] = True
        update_markdown_report(results, report_paths)

        has_adapter = (adapter_output / "adapter_model.safetensors").exists() or (
            adapter_output / "adapter_model.bin"
        ).exists()

        if not args.skip_train:
            if has_adapter and not args.force_retrain:
                logger.info("Found existing adapter at %s, proceeding to eval...", adapter_output)
            else:
                if adapter_output.exists():
                    logger.error(
                        "Existing output has no adapter; select --force_retrain to keep it and use a fresh directory"
                    )
                    r["success"] = False
                    r["training"] = False
                    update_markdown_report(results, report_paths)
                    continue
                train_script = tools_dir / "train_dora.py"
                if not train_script.exists():
                    train_script = repo_root / "train_dora.py"
                train_cmd = [
                    sys.executable,
                    str(train_script),
                    "--model_name_or_path",
                    model_name,
                    "--data_dir",
                    str(data_dir),
                    "--output_dir",
                    str(adapter_output),
                    "--batch_size",
                    str(r["batch_size"]),
                    "--gradient_accumulation_steps",
                    str(r["grad_accum"]),
                    "--learning_rate",
                    str(r["lr"]),
                    "--epochs",
                    str(r["epochs"]),
                    "--max_seq_length",
                    str(r["max_seq_len"]),
                    "--bf16",
                ]
                if r["mode"] == "qlora_4bit":
                    train_cmd.append("--load_in_4bit")

                env = {
                    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                    "CUDA_VISIBLE_DEVICES": "1",
                }
                rc = run_command(train_cmd, env=env)
                if rc != 0:
                    logger.error("Training failed for %s with exit code %d", model_name, rc)
                    success = False

        duration = time.time() - t_start
        r["training"] = False
        r["success"] = success
        r["duration_minutes"] = round(duration / 60, 1)

        # The legacy prompt suite is a smoke test. Missing adapters or subprocess failure never count as evaluated.
        r["evaluated"] = False
        if success and any(
            (adapter_output / name).is_file() for name in ("adapter_model.safetensors", "adapter_model.bin")
        ):
            try:
                eval_res = evaluate_model(
                    model_name, adapter_output, tools_dir, load_in_4bit=(r["mode"] == "qlora_4bit")
                )
            except subprocess.CalledProcessError as error:
                logger.error("Candidate evaluation exited with code %s", error.returncode)
                eval_report_file.write_text(
                    f"Evaluation failed (exit {error.returncode})\n", encoding="utf-8"
                )
            else:
                eval_report_file.write_text(eval_res["raw_output"], encoding="utf-8")
                r.update(
                    evaluated=True,
                    **{key: eval_res[key] for key in ("oc_score", "cliche_violations", "foundation_passed")},
                )
        elif success:
            r["success"] = False
            logger.error("No adapter exists for this candidate")

        update_markdown_report(results, report_paths)
        logger.info(
            "Completed %s: OC Score=%.1f | Cliches=%d | Foundation=%d/3",
            model_name,
            r.get("oc_score", 0),
            r.get("cliche_violations", 0),
            r.get("foundation_passed", 0),
        )

    logger.info("All queue candidate jobs completed.")


if __name__ == "__main__":
    main()
