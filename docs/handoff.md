# 项目交接 · 2026-11-16

下一阶段先在计划中的 **2× RTX 3090 工作站**交付独立的共享推理 API，再让常驻 Agent、Chats、视频取材、运营和游戏接入同一个入口。Realtime 协议在推理服务层单独验证。OBS、Avatar 演出、音画同步和 Remotion 放到核心服务验收后。工作站尚未实测，AGX 恢复与新节点建设各自进行。 [当前计划](plans/current.md#独立-omni-部署)

## 从哪里接手

| 仓库 | 接手基线 |
| --- | --- |
| `Tuber_Studio_Omni_Deploy` | 本地 `main` 的 `0c6e918` 包含认证网关、`infra-compose.yml` 和固定模型配置。沿用 `omni-gateway`，为工作站新增经核验的运行配置。现有引擎、模型下载与兼容镜像按 AGX ARM64 构建。 |
| `Tuber_Studio` | 本地 `main` 的 `5c009c4` 记录最后一次 AGX 验收。候选 Core `7bbd881` 已含持久任务、按工具回执交付、排队与执行分开计时、动态窗口及受信记忆召回。业务入口是 `deploy/omni/app-compose.yml`。 [Core 部署](../deploy/omni/README.md) · [中文 Agent 验收](../evaluations/results/agent-grounded-20261116.md) |

## 工作站的第一项交付

1. **单独启动引擎和网关。** 工作站到位后核对系统架构、驱动、CUDA 容器、两卡显存与 GPU 互联。RTX 3090 每卡 24 GB，逐卡记录权重、KV 和工作区占用。先实测首个候选能否单卡运行，再按固定引擎支持和并发实测决定分卡部署或张量并行。把工作站配置留在独立部署仓库。 [NVIDIA 规格](https://www.nvidia.com/en-us/geforce/graphics-cards/30-series/rtx-3090-3090ti/) · [vLLM 并行方式](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/)
2. **从调用方验收 API。** 网关提供带 Bearer 鉴权的 `/v1` 和稳定别名 `omni-default`。先走 `GET /healthz`、`GET /v1/models`、`POST /v1/chat/prompt_tokens` 和 `POST /v1/chat/completions`。用 Core 和一个独立调用方的两套服务令牌跑实际并发请求，覆盖普通与 SSE、视觉输入、结构化输出、排队和取消。模型端口留在内部网络。原生支持的能力以实际回包为准，Realtime 用无 OBS 的客户端核对。 [部署及准入规范](specs/foundation/omni-deployment.md#实机准入与切换)
3. **验收实际长文质量与稳定性。** 首个复测基线是官方 MiniCPM-o 4.5 AWQ、40,960 的 Chat 窗口、4,096 的生成额度和 Thinker／Talker 的 INT4 KV。Core 继续读取模型卡实报窗口。用同一长文、中文工具任务和视觉用例核对输出与真实回执，并记录首包、完成时间、逐卡显存、主机内存和交换。达到准入后再接现有业务入口和切换默认推理端点。 [AGX 对照证据](../evaluations/results/omni-deployment-20261116.md)

## 已知故障和业务数据

- AGX 候选推理仍是 `unhealthy`。同场测试在 75 分 11.5 秒结束，整机最低可用内存 0.531 GiB，随后健康检查超时。Docker 未收到容器退出事件，重启候选失败。旧 llama.cpp 已停。 [故障记录](../evaluations/results/omni-deployment-20261116.md#单会话共驻终止记录)
- 长文实际输入 36,611 tokens，生成耗完 4,096 tokens，漏掉两处指定记录。后台任务经过 758.15 秒等待后取得三项有效工具回执。工作站复测分别核对长文语义和后台任务续跑。 [长对话与后台记录](../evaluations/results/omni-deployment-20261116.md#长-chats-与后台准入)
- 业务和账号资料已核验恢复到 AGX 暂存卷，尚未交接写入权。原 Windows 业务目录及校验过的本机备份仍可用于恢复。工作站的推理服务可用隔离数据先验收，业务迁移另按唯一写入者流程执行。恢复时保留任务、记忆、平台授权和未决动作回执，凭据不进入 Git。 [迁移与恢复](specs/foundation/distributed-runtime.md#迁移备份与恢复)
