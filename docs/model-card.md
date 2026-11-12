---
license: other
base_model: Qwen/Qwen3.5-9B
base_model_relation: finetune
tags:
- gguf
- llama.cpp
- vtuber
- dafeiyu
- roleplay
pipeline_tag: image-text-to-text
language:
- zh
- en
---

# 大肥鱼 · Qwen3.5-9B GGUF 1.0.0

`dafeiyu-qwen35-9b:1.0.0` 是 [Tuber Studio](https://github.com/4everWZ/Tuber_Studio) 当前运营和直播的默认模型。应用读取工作台当前选中的角色卡、实际互动来源及参与者关系。工作台可更换文本、视觉和语音 Provider。

## 下载与使用

| 1.0.0 工件 | 用途 |
| --- | --- |
| [Q4_K_M 主权重](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/blob/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/releases/1.0.0/dafeiyu-qwen35-9b-clean-v1-Q4_K_M.gguf) | 文本生成和图文生成 |
| [Q8_0 视觉投影器](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/blob/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/releases/1.0.0/mmproj-dafeiyu-qwen35-9b-clean-v1-Q8_0.gguf) | 与同版主权重配套处理图片和视频采样帧 |
| [工件清单](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/blob/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/releases/1.0.0/bundle.json) | 固定基座、导出条件、字节数和 SHA-256 |

```sh
hf download 4everImmortality/dafeiyu-qwen35-9b-gguf --revision 21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff --include 'releases/1.0.0/*' --local-dir ./models
```

Tuber Studio 的安装流程会按固定版本下载并校验配对文件。使用 llama.cpp 时，文本用途加载主权重，图文用途同时通过 `--mmproj` 加载投影器。AGX 运营配置实开 262,144 上下文、`q4_0` K/V 并开启思考，其他设备可按实测容量设置窗口。工作台的直播和运营各自管理输出预算。 [部署指南](https://github.com/4everWZ/Tuber_Studio/blob/main/docs/guides/deployment.md)

## 训练与验证

1.0.0 将 DoRA `checkpoint-38` 合并至固定的 Qwen3.5-9B 基座。训练按原始会话与同源变体分组切分，在训练分片中加权，保留基座的视觉层。导出的 GGUF 未包含 MTP。仓库根目录另有一份早期 9B 文本实验权重，当前部署使用上表固定版本。 [训练与配对评估](https://github.com/4everWZ/Tuber_Studio/blob/main/evaluations/results/dafeiyu-clean-9b-20261111.md)

- 冻结配对评估中，微调版通用题为 93/120，同基座为 100/120。40 个角色场景相对当时部署模型胜 23 个、平 12 个、负 5 个。两项均低于当时设定的下一轮发布门槛。 [配对评估记录](https://github.com/4everWZ/Tuber_Studio/blob/main/evaluations/results/dafeiyu-clean-9b-20261111.md)
- AGX 审核回放使用 30 个真实视频。另一次运营实跑保存 220 条模型候选、159 条成功评论回执，其中 155 条发布正文与对应候选一致。号主总体反馈大部分满意，逐条语义仍待标注。 [审核回放](https://github.com/4everWZ/Tuber_Studio/blob/main/evaluations/results/review-agx-mainline-20261111.md) · [实跑核查](https://github.com/4everWZ/Tuber_Studio/blob/main/evaluations/results/operations-feedback-20261112.md)
- 直播与 Genie 的同节点首音、长时共驻和真实平台直播仍待验收。 [当前计划](https://github.com/4everWZ/Tuber_Studio/blob/main/docs/plans/current.md)
