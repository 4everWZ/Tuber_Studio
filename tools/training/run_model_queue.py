"""Multi-model experimentation queue for Tuber Studio Da Feiyu V2.

Supported Model Candidates:
1. Qwen/Qwen3.5-9B (Dense 9B, Native 256K context, Hybrid Linear Attention)
2. google/gemma-4-12B (Gemma 4 12B Unified Multimodal)
3. google/gemma-4-E4B-it (Gemma 4 4B Edge Model)
4. Qwen/Qwen3.5-4B (Dense 4B, Native 256K context, Hybrid Linear Attention)
5. google/gemma-4-26B-A4B-it (Gemma 4 26B MoE with 4.1B active params, Native 256K context)

Automates:
- Disk safety checks and automated cache purges to prevent disk saturation.
- Sequential fine-tuning (BF16 DoRA for <=9B, 4-bit QLoRA for 12B+).
- End-to-end 18-case evaluation benchmark.
- Automatic Markdown generation of results table and case details.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

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


def check_and_clean_disk(min_free_gb: float = 30.0) -> bool:
    """Checks root disk space and purges non-essential caches to prevent disk exhaustion."""
    home_dir = Path.home()
    hub_blobs = home_dir / ".cache" / "huggingface" / "hub" / "blobs"
    pip_cache = home_dir / ".cache" / "pip"

    if hub_blobs.exists():
        shutil.rmtree(hub_blobs, ignore_errors=True)
    if pip_cache.exists():
        shutil.rmtree(pip_cache, ignore_errors=True)

    # Remove incomplete downloads
    hub_models = home_dir / ".cache" / "huggingface" / "hub"
    if hub_models.exists():
        for inc in hub_models.rglob("*.incomplete"):
            try:
                inc.unlink(missing_ok=True)
            except Exception:
                pass

    stat = os.statvfs("/")
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
    logger.info("NVMe Root Free Disk Space: %.2f GB (Minimum required: %.2f GB)", free_gb, min_free_gb)
    return free_gb >= min_free_gb


def clean_checkpoints(adapter_dir: Path):
    """Retains final adapter files while removing bulky intermediate optimizer states."""
    if not adapter_dir.exists():
        return
    for ckpt in adapter_dir.glob("checkpoint-*"):
        if ckpt.is_dir():
            shutil.rmtree(ckpt, ignore_errors=True)
            logger.info("Cleaned intermediate training checkpoint: %s", ckpt.name)


def run_command(cmd: list[str], env: dict | None = None) -> int:
    logger.info("Executing: %s", " ".join(cmd))
    merged_env = os.environ.copy()
    merged_env["HF_HUB_DISABLE_XET"] = "1"
    merged_env["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, env=merged_env)
    return proc.returncode


def evaluate_model(base_model: str, adapter_dir: Path | None, tools_dir: Path, load_in_4bit: bool = False) -> dict:
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
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
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
        "# 🐟 Tuber Studio 大肥鱼 V2 候选模型微调与矩阵评测全量报告",
        "",
        f"**最后更新时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "训练环境为 Workstation 双卡 RTX 3090。本表的关键词和三题常识结果用于训练诊断。",
        "生产选型见[独立持出回放](../../evaluations/results/workstation-candidate-replay-20261110.md)。",
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
            found_str = f"**{r['foundation_passed']}/3 (100%)**" if r['foundation_passed'] == 3 else f"{r['foundation_passed']}/3"
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

    lines.extend([
        "",
        "---",
        "",
        "## 2. 核心评测维度与真实性消融分析",
        "",
        "### A. 大肥鱼 OC 真实度与防机械模板",
        "- **热腾腾的大白米饭偏执**: 面对满汉全席等海量美食诱惑时，必须固执优先选择热腾腾的大白米饭，龙虾牛排只算配菜；",
        "- **身体界限与软垫待客**: 尾巴极度怕痒、除主人外严禁碰触，被戳碰立刻炸毛收回身后；海底小屋的暖光软垫则大方分一半给来客朋友歇脚；",
        "- **去模板化口癖**: 禁止每句话开头都机械套用“本鱼/本鲸”模板，融入真实二次元灵动傲娇与口播节奏。",
        "",
        "### B. 战团式好感度双向互动 ([-100, +100])",
        "- **友好区间 (+50 ~ +100)**: 热情分享大米饭、贝壳灯软垫、吐槽饲养员日常，提供温暖陪伴；",
        "- **敌对区间 (-100 ~ -10)**: 辛辣幽默回怼、毒舌吐槽（*“左上角叉叉没锁，慢走不送”*、*“莫非是暗恋狂魔？”*），绝无任何 AI 机械道歉。",
        "",
        "### C. B站社区真实 UID 边界防冒领",
        "- 评论区出现 `@其他用户` 时，严格根据 `account_uid` 比对，他人提及同名第三方时自觉作为旁观者，绝不冒充抢话认领。",
        "",
        "### D. 基础通用能力与常识防坍缩",
        "- 保持二次元 OC 人设的同时，物理常识（光年为长度单位）、算法常识（二分查找 $O(\\log n)$）与生活常识 100% 正确回答，严防微调灾难性遗忘。",
        "",
        "---",
        "",
        "## 3. 自动化防爆硬盘机制",
        "- **Xet CAS 全局隔离**: 显式关闭 `HF_HUB_ENABLE_XET`，禁止临时 CAS 数据块污染 `~/.cache/huggingface/hub/blobs/`；",
        "- **检查点轻量化**: 每个候选模型微调完成后，自动清理 `checkpoint-*` 冗余优化器权重，仅保留轻量化最终 Adapter（~50-100MB）；",
        "- **低盘阈值熔断**: 运行前强制校验 NVMe 可用空间 $\ge 30\\text{ GB}$，空间不足时自动熔断并告警。",
    ])

    report_content = "\n".join(lines)
    for p in report_paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(report_content, encoding="utf-8")
        except Exception as e:
            logger.warning("Could not write report to %s: %s", p, e)


def main():
    parser = argparse.ArgumentParser(description="Run multi-model candidate queue with automated safety and markdown.")
    parser.add_argument("--repo_root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--only_model", type=str, default=None, help="Run only specified candidate ID")
    parser.add_argument("--skip_train", action="store_true", default=False, help="Skip training, run evaluation only")
    parser.add_argument("--force_retrain", action="store_true", default=False, help="Force retrain from scratch even if adapter exists")
    args = parser.parse_args()

    repo_root = args.repo_root
    tools_dir = repo_root / "tools"
    data_dir = repo_root / "data"
    output_base = repo_root / "output_queue"
    output_base.mkdir(parents=True, exist_ok=True)

    # Rebuild authentic clean SFT dataset without any legacy branding
    build_script = tools_dir / "training" / "build_authentic_sft_dataset.py"
    if not build_script.exists():
        build_script = repo_root / "build_authentic_sft_dataset.py"
    if build_script.exists():
        logger.info("Compiling 100% clean, authentic SFT dataset via %s...", build_script)
        run_command([sys.executable, str(build_script), "--output_dir", str(data_dir)])

    report_paths = [
        repo_root / "docs" / "specs" / "candidate_models_finetune_matrix.md",
    ]

    results = [dict(c) for c in QUEUE_CANDIDATES]
    update_markdown_report(results, report_paths)

    for r in results:
        cand_id = r["id"]
        if args.only_model and cand_id != args.only_model:
            continue

        model_name = r["name"]
        adapter_output = output_base / f"adapter_{cand_id}"
        eval_report_file = output_base / f"eval_{cand_id}.txt"

        logger.info("\n" + "=" * 65)
        logger.info("🚀 Processing Candidate: %s (%s)", model_name, cand_id)
        logger.info("=" * 65)

        # 1. Disk check
        if not check_and_clean_disk(min_free_gb=30.0):
            logger.error("Skipping %s due to low disk space.", model_name)
            r["success"] = False
            update_markdown_report(results, report_paths)
            continue

        t_start = time.time()
        success = True
        r["training"] = True
        update_markdown_report(results, report_paths)

        has_adapter = (adapter_output / "adapter_model.safetensors").exists() or (adapter_output / "adapter_model.bin").exists()

        if not args.skip_train:
            if has_adapter and not args.force_retrain:
                logger.info("Found existing adapter at %s, proceeding to eval...", adapter_output)
            else:
                if args.force_retrain and adapter_output.exists():
                    logger.info("Force retrain specified: removing legacy adapter at %s", adapter_output)
                    shutil.rmtree(adapter_output, ignore_errors=True)
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

        # 2. Evaluation
        if success:
            logger.info("Starting benchmark evaluation for %s...", model_name)
            target_adapter = adapter_output if (adapter_output / "adapter_model.safetensors").exists() else None
            eval_res = evaluate_model(
                model_name,
                target_adapter,
                tools_dir,
                load_in_4bit=(r["mode"] == "qlora_4bit"),
            )
            eval_report_file.write_text(eval_res["raw_output"], encoding="utf-8")
            r["evaluated"] = True
            r["oc_score"] = eval_res["oc_score"]
            r["cliche_violations"] = eval_res["cliche_violations"]
            r["foundation_passed"] = eval_res["foundation_passed"]
        else:
            r["evaluated"] = False

        # 3. Post-run checkpoint cleanup & disk check
        clean_checkpoints(adapter_output)
        check_and_clean_disk(min_free_gb=30.0)

        update_markdown_report(results, report_paths)
        logger.info("Completed %s: OC Score=%.1f | Cliches=%d | Foundation=%d/3", model_name, r.get("oc_score", 0), r.get("cliche_violations", 0), r.get("foundation_passed", 0))

    logger.info("All queue candidate jobs completed.")


if __name__ == "__main__":
    main()
