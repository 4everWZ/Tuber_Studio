#!/usr/bin/env bash
set -euo pipefail

TRAINING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$TRAINING_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Use the caller's activated training environment, never a fixed user's conda path.
TRAINING_PYTHON="${TUBER_TRAINING_PYTHON:-python}"
TRAINING_DATA="${TUBER_TRAINING_DATA:-$PROJECT_ROOT/runtime/training_data}"
TRAINING_OUTPUT="${TUBER_TRAINING_OUTPUT:-$PROJECT_ROOT/runtime/checkpoints/dafeiyu_dora}"
TRAINING_WORKERS="${TUBER_TRAINING_WORKERS:-2}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1}"
export PYTHONUNBUFFERED=1

if [[ ! -f "$TRAINING_DATA/sft_train.json" ]]; then
  echo "缺少 $TRAINING_DATA/sft_train.json；请先显式编译 OC 数据集。" >&2
  exit 1
fi

"$TRAINING_PYTHON" -m torch.distributed.run --nproc_per_node="$TRAINING_WORKERS" \
  "$TRAINING_DIR/train_dora.py" \
  --model_name_or_path "openbmb/MiniCPM-V-4.7-1B" \
  --data_dir "$TRAINING_DATA" \
  --output_dir "$TRAINING_OUTPUT" \
  --batch_size 4 \
  --gradient_accumulation_steps 2 \
  --learning_rate 3e-4 \
  --epochs 5 \
  --max_seq_length 1024 \
  --bf16 "$@"

echo "训练已完成：$TRAINING_OUTPUT"
echo "尚未进行真实视频与多轮评估；合并、配套投影器导出及回放步骤见 tools/training/README.md。"
