# 大肥鱼 OC 训练与导出

大肥鱼 OC 训练覆盖表达、关系和互动，并检查通用能力保留。平台能力、调度、联网取材与发送由业务程序提供。训练输入由既有语料派生，保留原始文本及历史训练集。

## 当前部署

运营档使用 Qwen3.5-9B 的 `checkpoint-38`，部署包包含同一基座的 Q4_K_M 权重和 Q8_0 视觉投影器。AGX 运行 llama.cpp CUDA，当前窗口为 262,144、K/V 为 `q4_0`，思考开启。训练选点、独立评估、导出和运行条件分别见[9B 配对评估](../../evaluations/results/dafeiyu-clean-9b-20261111.md)、[装配实测](../../evaluations/results/agx-clean-9b-deployment-20261111.md)和[运营审核回放](../../evaluations/results/review-agx-mainline-20261111.md)。

MiniCPM-V-4.7-1B 的历史训练使用 612 条 SFT 样本、assistant-only loss 和 DoRA rank 16／alpha 32，在双 RTX 3090 上训练五轮。字幕、帧引用和复读样例见[1B 实测](../../evaluations/results/authentic-dora-20261107/README.md)。

## Qwen3.5-9B 派生基线

使用工作站项目专用 Python 环境。编译器按二次元宽度、大肥鱼 OC 深度、通用能力归类，先合并同源与近重复会话，再分训练和开发。原始文本、历史样本和已有适配器保留。编译器向指定目录写入组别及文件摘要，发现不同内容时要求新目录。

干净基线使用模型原生模板，逐样本验证 system 与对话原文均实际进入输入。监督只覆盖 assistant 正文及其 EOS，超过序列长度、缺少终止符或带视觉／音频资产的样本直接失败。语言适配器目标排除视觉组件。训练数据按实际监督 token 平衡，宽度与深度合计约 70%，通用约 30%，允许偏差 3 个百分点。

```bash
python tools/training/build_authentic_sft_dataset.py --output_dir "$RUN/data" --seed 42
python -m evaluations.dafeiyu_holdout audit \
  --compiled "$RUN/data/sft_train.json" "$RUN/data/sft_val.json" --raw-dir tools/training
torchrun --standalone --nproc_per_node=2 tools/training/train_dora.py \
  --model_name_or_path Qwen/Qwen3.5-9B --revision "$BASE_REVISION" \
  --local_files_only --clean_baseline \
  --holdout evaluations/cases/dafeiyu-next-holdout-v1.jsonl \
  --data_dir "$RUN/data" --output_dir "$RUN/adapter" \
  --r 16 --lora_alpha 32 --batch_size 1 --gradient_accumulation_steps 4 \
  --learning_rate 0.00015 --epochs 2 --max_seq_length 1024 --seed 42
```

`BASE_REVISION` 是已缓存的 40 位基座提交，`RUN` 是独立输出目录。`training_metrics.json` 记录训练输入、模板、留出集、实际 token 配额及检查点。先用开发集选检查点，再进入配对留出回放。训练或开发 loss 只用于训练诊断。历史 1B 包装器 `run_finetune.sh` 仍支持显式指定数据与新输出。

编号工件、分片摘要、开发选择与留出回放进度记录在本次实验结果中。[9B 干净基线](../../evaluations/results/dafeiyu-clean-9b-20261111.md)

冻结持出默认回放和评分 120 条通用题及 40 个 OC 场景。复现旧 12 条合成运营时，在回放加 `--suite all`，匿名配对和评分均加 `--include-historical-operations`。合成结果单列，现行 `release_holdout_gate_pass` 取通用与 OC 的结果。真实运营从业务库只读复算，并用本地评审包核对逐条语义反馈。 [真实运营评估入口](../../evaluations/results/operations-feedback-20261112.md#真实运营的模型评估)

历史批量训练和 18 题文本初筛可用退役前提交 `744ddd7` 的 `tools/training/run_model_queue.py` 复现。现行入口使用上文的分组编译、固定基座训练与配对持出。

## 历史 1B 的合并与 GGUF 导出

复现 MiniCPM 1B 时，`export_grounded_sft.py` 完成 PEFT 合并、4× 视觉配置、GGUF 转换、Q4_K_M 量化及 `bundle.json` 元数据输出。要求底模快照目录名为 `ad9bf379feb71204c888b22923d63322452407f9`，llama.cpp 固定为 `0ec4027a2a0183de87c007069adddc06e665f7de`，并已构建量化工具。输出使用新目录。

```bash
python tools/training/export_grounded_sft.py \
  --base "$BASE" --adapter "$ADAPTER" --llama-cpp "$LLAMA_CPP" --output "$EXPORT"
```

该 1B 适配器与原始底模合并。导出包括以下文件。

- `MiniCPM-V-4.7-1B-Dafeiyu-Grounded-Q4_K_M.gguf`
- `mmproj-4x-f16.gguf`
- `bundle.json`：底模／引擎版本、adapter 和产物来源信息。

`merge_and_export_gguf.py` 也可将 adapter 合并成 Hugging Face checkpoint；它本身不完成 GGUF 与配套投影器导出，不能以脚本文件名代替检查输出。

已发布的同批权重与投影器见[固定发布版本](https://huggingface.co/4everImmortality/dafeiyu-minicpmv/tree/68f9542e3c7dee1b41a1be12fe97f3095819b952)。Hub 主权重文件名为 `MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf`，节点安装使用上面的 Grounded 文件名；按固定下载清单映射，不混用旧 projector。安装、节点配对与视觉验证见[部署指南](../../docs/guides/deployment.md#电脑业务主机与独立推理节点)。

## 评估与其他训练工具

`evaluate_finetuned_model.py`使用固定旧SYSTEM_PROMPT（含旧自称与AI OC描述），`evaluate_4groups.py`也使用固定题目和判据；它们仅供原条件离线对照，不读取当前角色卡，旧评分不代表当前`characters.json`或当前运营服务已通过。主线验收须通过实际服务，使用当前人格和真实caption／frame回放，覆盖多轮回复、引用、连续复读、未完成输出和工具补读。报告须保留素材、模型／引擎、采样、上下文与输出设置，以及失败原答；小集合通过不宣称 100% 稳定。

`grounded_sft.py`、`train_grounded_preference.py` 及 `build_grounded_*`／`augment_grounded_*` 是独立视觉与偏好实验工具，其语料、起始权重和判据不自动替代当前 OC 数据。历史条件和逐例结果见[视觉实验](../../evaluations/results/grounded-persona-v6-20261105/README.md)、[后续回放](../../evaluations/results/grounded-persona-v7-20261105/README.md)、[偏好实验](../../evaluations/results/grounded-preference-20261105/README.md)。这些记录用于比较与追查，不作为当前部署说明。

`prepare_grounded_replay.py`、`evaluate_grounded_sft.py` 和 `replay_grounded_operations.py` 服务于旧 Grounded 实验。旧 endpoint 的逐帧、证据先行和来源笔记协议，以及旧八题合成运营集 `evaluations/cases/operations-heldout.json`，可在提交 `744ddd7` 复现。早期评估随结果保留当时实际用的[八题副本](../../evaluations/results/operations-vlm-20261101/resume2/mini4-7-legacy/operations-heldout.json)。现行冻结回放默认读取 `evaluations/cases/dafeiyu-next-holdout-v1.jsonl` 的通用与 OC 题，实跑模型评估从业务库提取。
