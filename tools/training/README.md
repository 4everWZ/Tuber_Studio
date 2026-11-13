# 大肥鱼 9B 训练、评估与导出

现行运营和直播默认使用同一份大肥鱼 Qwen3.5-9B。已部署版本从 `checkpoint-38` 合并，配套 Q4_K_M 主权重和 Q8_0 视觉投影器。训练选择、离线结果与实机条件分别见[配对评估](../../evaluations/results/dafeiyu-clean-9b-20261111.md)、[装配记录](../../evaluations/results/agx-clean-9b-deployment-20261111.md)和[运营审核回放](../../evaluations/results/review-agx-mainline-20261111.md)。

## 编译与训练

在工作站项目专用训练环境执行。`RUN` 为本地版本目录。准备实际训练使用的 Qwen3.5-9B 固定快照，首版基座提交为 `c202236235762e1c871ad0ccb60c8ee5ba337b9a`。编译先合并同源和近重复会话，再分训练与开发。各分片和清单写入 `RUN/data`，原始语料及来源保持在本目录。

```bash
BASE_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
HOLDOUT=evaluations/cases/dafeiyu-release-holdout-v1.jsonl

python tools/training/build_authentic_sft_dataset.py --output_dir "$RUN/data" --seed 42
python -m evaluations.dafeiyu_holdout audit \
  --compiled "$RUN/data/sft_train.json" "$RUN/data/sft_val.json" --raw-dir tools/training

TRAIN_ARGS=(--revision "$BASE_REVISION" --local_files_only --holdout "$HOLDOUT"
  --data_dir "$RUN/data" --r 16 --lora_alpha 32 --batch_size 1
  --gradient_accumulation_steps 4 --learning_rate 0.00015 --epochs 2
  --max_seq_length 1024 --seed 42)

WORLD_SIZE=2 python tools/training/train_dora.py "${TRAIN_ARGS[@]}" \
  --prepare_only --output_dir "$RUN/prepared"
torchrun --standalone --nproc_per_node=2 tools/training/train_dora.py "${TRAIN_ARGS[@]}" \
  --output_dir "$RUN/adapter"
```

准备入口用真实 tokenizer 检查原生聊天模板、assistant 正文和 EOS 监督边界，拒绝截断及带视觉或音频的样本。训练仅更新语言适配器。训练侧按实际监督 token 平衡，角色深度和二次元宽度合计约 70%，通用约 30%，允许偏差 3 个百分点。`training_metrics.json` 保存分片与模板身份、留出集、监督 token 和检查点。按开发 loss 选点后再做冻结回放。

## 冻结回放与配对评审

冻结集由 120 条通用题和 40 个 OC 场景组成。用同一角色卡和回放协议生成同基座通用对照、现行角色模型 OC 对照及候选的两套结果。`ADAPTER` 指向已选检查点，`DEPLOYED_ENDPOINT` 和 `DEPLOYED_MODEL` 指向待对照的已部署模型服务。`DEPLOYED_BASE_REVISION` 与 `DEPLOYED_ARTIFACT` 使用可追溯的实际版本标识。

```bash
python -m tools.training.replay_dafeiyu_holdout_workstation \
  --model Qwen/Qwen3.5-9B --revision "$BASE_REVISION" --artifact base \
  --suite general --output "$RUN/base.json"
python -m tools.training.replay_dafeiyu_holdout_workstation \
  --model Qwen/Qwen3.5-9B --revision "$BASE_REVISION" --adapter "$ADAPTER" \
  --artifact candidate --output "$RUN/candidate.json"
python -m evaluations.dafeiyu_holdout replay --endpoint "$DEPLOYED_ENDPOINT" \
  --model "$DEPLOYED_MODEL" --base-revision "$DEPLOYED_BASE_REVISION" \
  --artifact "$DEPLOYED_ARTIFACT" --suite oc --output "$RUN/deployed.json"
python -m evaluations.dafeiyu_holdout blind \
  --base "$RUN/base.json" --candidate "$RUN/candidate.json" --deployed "$RUN/deployed.json" \
  --packet "$RUN/grading.jsonl" --key "$RUN/grading-key.json"
```

独立评审员只接收 `grading.jsonl`。通用题把 `grading.a_correct` 和 `grading.b_correct` 填为布尔值，OC 题的 `grading.preference` 填 `a`、`b` 或 `tie`。完成后用同一次配对的密钥评分。真实运营语义走实际来源和回执的[只读评估入口](../../evaluations/results/operations-feedback-20261112.md#真实运营的模型评估)。

```bash
python -m evaluations.dafeiyu_holdout score \
  --packet "$RUN/grading.jsonl" --key "$RUN/grading-key.json" --output "$RUN/score.json"
```

## 合并与 GGUF

`BASE_SNAPSHOT` 为与训练同一提交的本地基座快照，`LLAMA_CPP` 为已安装转换依赖且构建 `llama-quantize` 的 llama.cpp 工作目录。首版使用导出提交 `0ec4027a2a0183de87c007069adddc06e665f7de`。主权重从合并结果导出，投影器使用同一固定基座的视觉权重。选定检查点只训练语言层。

```bash
python tools/training/merge_and_export_gguf.py --revision "$BASE_REVISION" \
  --adapter_path "$ADAPTER" --output_dir "$RUN/merged"
mkdir "$RUN/gguf"
python "$LLAMA_CPP/convert_hf_to_gguf.py" "$RUN/merged" \
  --no-mtp --outtype f16 --outfile "$RUN/model-f16.gguf"
"$LLAMA_CPP/build/bin/llama-quantize" "$RUN/model-f16.gguf" \
  "$RUN/gguf/dafeiyu-qwen35-9b-Q4_K_M.gguf" Q4_K_M
python "$LLAMA_CPP/convert_hf_to_gguf.py" "$BASE_SNAPSHOT" \
  --mmproj --outtype q8_0 --outfile "$RUN/gguf/mmproj-dafeiyu-qwen35-9b-Q8_0.gguf"
sha256sum "$RUN/gguf/"*.gguf
```

发布工件保存基座、适配器、导出引擎和两份 GGUF 的身份。目标设备复验真实窗口、K/V、文本模板和图像输入。现行装载使用[固定发布版本](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/tree/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/releases/1.0.0)。

## 历史复现

MiniCPM/Grounded、旧 DoRA 试验、18 题初筛及旧派生 `data/sft_*.json` 可用提交 `744ddd7` 复现。该版本保留视频标注及署名，1B 训练实证位于 `evaluations/results/authentic-dora-20261107/README.md`。含 12 条合成运营题的旧冻结回放可从 `58652d3` 回查，独立合成评论协议及题集可从 `69b2ed2` 回查。现行独立性审计读取本目录 13 份原始 `grounded_*.txt`；运营模型评估使用真实候选和回执。 [早期 9B 配对评估](../../evaluations/results/dafeiyu-clean-9b-20261111.md) · [真实运营核查](../../evaluations/results/operations-feedback-20261112.md#真实运营的模型评估)
