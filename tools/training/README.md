# 大肥鱼 OC 训练与导出

当前主线训练大肥鱼 OC 的表达、关系和互动，不训练通用 Agent 底座。平台能力、调度、联网取材与发送由业务程序提供。现有语料与训练数据保持原样；修改训练集或重训须另行明确，不能以修工程问题为由重写角色样本。

## 当前 DoRA 与证据

当前 adapter 位于 workstation 的 `~/code/Tuber_Studio/output_dafeiyu_dora`。底模为 `openbmb/MiniCPM-V-4.7-1B`，沿用已有 612 条 SFT 样本、assistant-only loss、DoRA rank 16／alpha 32；双 RTX 3090 训练五轮。下列命令用于复现流程，执行训练会产生新权重，不代表已重新验收。

工作站真实视频回放、字幕接口限制、画面引用问题及复读样例见[DoRA 实测](../../evaluations/results/authentic-dora-20261107/README.md)。结构完整、资源可运行与角色／内容质量分别判断。先前 V6 系列不作为当前部署或后续训练起点；历史实验保留在 Git 与 `evaluations/results/`。

## 已有数据与训练

使用工作站项目专用 Python 环境。`build_authentic_sft_dataset.py` 从原始 txt 编译 SFT，支持 `--training_dir`、`--output_dir`、`--seed`；已有训练集无需重新编译。需要验证构建器时输出到独立目录，避免覆盖当前数据。

`run_finetune.sh`是双卡训练包装器，可用 `TUBER_TRAINING_DATA`、`TUBER_TRAINING_OUTPUT`、`TUBER_TRAINING_WORKERS`指定已有数据、新输出和进程数；它不自动宣称评估通过。也可从仓库根目录，在已有 `sft_train.json` 的目录上显式启动训练：

```bash
torchrun --nproc_per_node=2 tools/training/train_dora.py \
  --model_name_or_path "$BASE" --data_dir "$DATA" --output_dir "$NEW_ADAPTER" \
  --r 16 --lora_alpha 32 --batch_size 4 --gradient_accumulation_steps 2 \
  --learning_rate 3e-4 --epochs 5 --max_seq_length 1024 --bf16
```

`BASE` 指本次 adapter 对应的原始底模，`DATA` 指保留的训练集，`NEW_ADAPTER` 使用新目录。assistant-only loss 由训练器掩码实现；不能把 loss 下降当作角色、真实视频理解或多轮稳定性通过。

## 合并与 GGUF 导出

部署需要同一次合并导出的主权重和视觉投影器。`export_grounded_sft.py` 完成 PEFT 合并、4× 视觉配置、GGUF 转换、Q4_K_M 量化及 `bundle.json` 元数据输出。要求底模快照目录名为 `ad9bf379feb71204c888b22923d63322452407f9`，llama.cpp 固定为 `0ec4027a2a0183de87c007069adddc06e665f7de`，并已构建量化工具。输出目录必须尚不存在。

```bash
python tools/training/export_grounded_sft.py \
  --base "$BASE" --adapter "$ADAPTER" --llama-cpp "$LLAMA_CPP" --output "$EXPORT"
```

当前 adapter 使用原始底模合并，不传历史 V6 的 `--reference-model`。导出包含：

- `MiniCPM-V-4.7-1B-Dafeiyu-Grounded-Q4_K_M.gguf`
- `mmproj-4x-f16.gguf`
- `bundle.json`：底模／引擎版本、adapter 和产物来源信息。

`merge_and_export_gguf.py` 也可将 adapter 合并成 Hugging Face checkpoint；它本身不完成 GGUF 与配套投影器导出，不能以脚本文件名代替检查输出。

已发布的同批权重与投影器见[固定发布版本](https://huggingface.co/4everImmortality/dafeiyu-minicpmv/tree/68f9542e3c7dee1b41a1be12fe97f3095819b952)。Hub 主权重文件名为 `MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf`，节点安装使用上面的 Grounded 文件名；按固定下载清单映射，不混用旧 projector。安装、节点配对与视觉验证见[部署指南](../../docs/guides/deployment.md#电脑业务主机与独立推理节点)。

## 评估与其他训练工具

`evaluate_finetuned_model.py`使用固定旧SYSTEM_PROMPT（含旧自称与AI OC描述），`evaluate_4groups.py`也使用固定题目和判据；它们仅供原条件离线对照，不读取当前角色卡，旧评分不代表当前`characters.json`或当前运营服务已通过。主线验收须通过实际服务，使用当前人格和真实caption／frame回放，覆盖多轮回复、引用、连续复读、未完成输出和工具补读。报告须保留素材、模型／引擎、采样、上下文与输出设置，以及失败原答；小集合通过不宣称 100% 稳定。

`grounded_sft.py`、`train_grounded_preference.py` 及 `build_grounded_*`／`augment_grounded_*` 是独立视觉与偏好实验工具，其语料、起始权重和判据不自动替代当前 OC 数据。历史条件和逐例结果见[视觉实验](../../evaluations/results/grounded-persona-v6-20261105/README.md)、[后续回放](../../evaluations/results/grounded-persona-v7-20261105/README.md)、[偏好实验](../../evaluations/results/grounded-preference-20261105/README.md)。这些记录用于比较与追查，不作为当前部署说明。
