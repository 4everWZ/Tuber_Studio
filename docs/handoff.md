# 项目交接 · 2026-11-16

下一阶段先接共享推理 API，再完成 Dots 式精细化委派。Chats、视频取材、运营和游戏经 Provider 使用同一服务。双工和 Avatar 通过独立服务的 API 协商，OBS、节目合成和 Remotion 随演出阶段验收。 [当前计划](plans/current.md#共享推理与-avatar-api)

## 从哪里接手

| 仓库 | 接手基线 |
| --- | --- |
| `omni-avatar-api` | 独立服务仓拥有推理、双工会话与 Avatar 音画。Tuber Studio 通过配置使用服务地址、认证、模型别名及实报能力。 [服务契约](https://github.com/4everWZ/omni-avatar-api/blob/main/docs/api.md) |
| `Tuber_Studio` | Core 持有任务、记忆、角色和平台权限，已有持久任务、按工具回执交付、区分排队和执行、动态窗口及来源约束召回。业务入口是 `deploy/omni/app-compose.yml`。 [Core 接入](../deploy/omni/README.md) · [中文 Agent 验收](../evaluations/results/agent-grounded-20261116.md) |

## API 接入的第一项交付

1. **配置服务地址与凭据。** 核对服务健康、模型列表和实报窗口，用 Core 和一个独立调用方验证普通／流式推理、视觉、结构化输出、长文正确性、排队、取消和错误。用独立客户端核对 Realtime 的原生能力。 [API 准入](specs/foundation/omni-deployment.md#接入验收与切换)
2. **按业务入口复测。** 用隔离数据跑现有 Chats、视频、运营和游戏工具，核对来源与回执、任务续跑和 Provider 回退。通用 Avatar 会话服务到位后再按回包关联音画。 [Agent 验收](specs/foundation/agent-runtime.md#验收)

## 常驻 Agent 接手顺序

现有 Agent 可从 Chats 接任务、在活动里继续、改口和取消。能力集中在已登记的工具。下一步先扩展现有任务 API 和业务执行层，让同一套任务能保存并约束对象、时间、数量、筛选、条件、授权及交付。先用不同领域验证精细选择、条件分支和真实回执。指定人、限定私信、视频时码和状态驱动的游戏任务都可作为用例。平台动作继续复用现有领域接口。 [能力缺口与实施入口](plans/current.md#对照-dots-的当前缺口与实施入口) · [任务控制设计](specs/foundation/agent-runtime.md#精细化委派与控制)

首轮用隔离数据核对对象集合、动作授权、未知结果重启核对，以及从另一设备接续任务。随后补齐周期与事件触发、连接器、浏览器接管、文件产物和后台委派。共享推理和 Agent 核心验收先行，OBS 随演出阶段推进。 [核心验收](plans/current.md#常驻-agent-与跨端记忆)

## 验收缺口与业务数据

- 已有候选历史记录中的长文答复和持续共驻失败，接新端点时用同一业务场景复测。 [历史证据](../evaluations/results/omni-deployment-20261116.md)
- 业务快照已有经核验的暂存副本与本机备份，正式写入权尚未交接。推理 API 验收用隔离数据，迁移另按唯一写入者流程执行。 [迁移与恢复](specs/foundation/distributed-runtime.md#迁移备份与恢复)
