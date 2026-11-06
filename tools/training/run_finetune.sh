#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

CONDA_ENV="/home/research-2/miniconda3/envs/research_cu13"
export PATH="$CONDA_ENV/bin:$PATH"
export CUDA_VISIBLE_DEVICES=0,1
export PYTHONUNBUFFERED=1

echo "=========================================================="
echo "🚀 启动 Tuber Studio · 大肥鱼双 3090 并行微调 (DoRA/LoRA)"
echo "时间: $(date)"
echo "GPU 设备: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader)"
echo "基座模型: openbmb/MiniCPM-V-4.7-1B"
echo "训练数据集: $DIR/data/sft_train.json"
echo "=========================================================="

torchrun --nproc_per_node=2 "$DIR/tools/train_dora.py" \
  --model_name_or_path "openbmb/MiniCPM-V-4.7-1B" \
  --data_dir "$DIR/data" \
  --output_dir "$DIR/output_dafeiyu_dora" \
  --batch_size 4 \
  --gradient_accumulation_steps 2 \
  --learning_rate 3e-4 \
  --epochs 5 \
  --max_seq_length 1024 \
  --bf16

echo ""
echo "=========================================================="
echo "✅ 微调完成！正在执行端到端 OC 拟真度与防坍缩评测..."
echo "=========================================================="

python "$DIR/tools/evaluate_finetuned_model.py" \
  --base_model "openbmb/MiniCPM-V-4.7-1B" \
  --adapter "$DIR/output_dafeiyu_dora" | tee "$DIR/eval_report.txt"

echo "🎉 训练与评测全部结束！"
