# 候选模型微调初筛 · 2026-11-10

这轮在双 RTX 3090 工作站运行，记录了早期角色探针、三道常识题和训练资源情况。不同任务的独立对照见[候选持出回放](workstation-candidate-replay-20261110.md)，现行部署见[AGX 审核回放](review-agx-mainline-20261111.md)。

## 训练与探针

| 候选 | 当时的训练方法 | 角色探针分数 | 常识题 | 产物或结果 |
| --- | --- | --- | --- | --- |
| Qwen3.5-9B | BF16 DoRA，48.2 分钟 | 82.3 / 100 | 3 / 3 | Q4_K_M 文件约 5.23 GB |
| Qwen3.5-4B | BF16 DoRA，35.6 分钟 | 98.7 / 100 | 3 / 3 | Q4_K_M 文件约 2.51 GB |
| Gemma 4 12B | 4-bit QLoRA，32.5 分钟 | 97.7 / 100 | 3 / 3 | 4-bit 权重估算约 7.8 GB，持出回放发现处理器与模板问题 |
| Gemma 4 E4B-it | BF16 DoRA | — | — | 视觉塔与当时的全局 DoRA 目标发生冲突 |
| Gemma 4 26B-A4B-it | 尝试 4-bit QLoRA | — | — | 当时的量化路径跳过三维专家张量，记录的 BF16 静态权重估算约 49.6 GB，超出工作站可用显存 |

前三个产物在当时的关键词探针中均未命中所查的助手机械套话。题目覆盖日常、熟人接话、鲸尾动作与受纠正后的反应。角色探针和三题常识只用于初筛。训练脚本当时预检 30 GB 可用磁盘，关闭 Xet，并保留最终适配器。

## 早期设备试验

AGX 的 4B 和 9B 分别在 8,192 上下文、Q4_K_M 文本条件下测得约 32.7 和 23.1 tokens/s。文件体积、运行内存、视觉和首音分别测量。历史产物登记见 [4B 适配器](https://huggingface.co/4everImmortality/dafeiyu-qwen35-4b-dora)、[9B 适配器](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-dora)、[12B 适配器](https://huggingface.co/4everImmortality/dafeiyu-gemma4-12b-qlora)、[4B GGUF](https://huggingface.co/4everImmortality/dafeiyu-qwen35-4b-gguf)和 [9B GGUF](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf)。
