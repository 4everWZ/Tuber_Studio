# 项目交接 · 2026-11-19

工作站已承载工坊前端、业务 Core、正式数据库、Omni 和 Avatar。号主从笔电浏览器直接访问工作站 IP。HTTP 会转向 HTTPS，私有通话在同一入口换取短时网关票据。两张获选 OC 成图和两套原稿已独立登记。OBS、节目合成和 Remotion 随演出阶段验收。[IP 入口实测](../evaluations/results/workstation-studio-ip-20261119.md) · [当前计划](plans/current.md#共享推理与-avatar-api)

## 从哪里接手

| 仓库 | 接手基线 |
| --- | --- |
| `omni-avatar-api` | 独立服务仓拥有推理、双工会话与 Avatar 音画。Tuber Studio 通过配置使用服务地址、认证、模型别名及实报能力。 [服务契约](https://github.com/4everWZ/omni-avatar-api/blob/main/docs/api.md) |
| `Tuber_Studio` | Core 持有任务、记忆、角色和平台权限，已有持久任务、按工具回执交付、区分排队和执行、动态窗口及来源约束召回。业务入口是 `deploy/omni/app-compose.yml`。 [Core 接入](../deploy/omni/README.md) · [中文 Agent 验收](../evaluations/results/agent-grounded-20261116.md) |

## 正式入口与后续接手

工作站正式入口已验证 Edge 登录、角色参考图设置、Pets、纯语音电话和三渠道运营页面。周期跟进、指定来信和评论楼跟踪、私有文件产物、公开网页阅读与接管均有现成入口。接手时从当前实现扩展授权来源、任务条件和连接器。[工作站实测](../evaluations/results/workstation-studio-ip-20261119.md) · [三渠道验收](../evaluations/results/operations-images-three-channels.md) · [实施待办](plans/current.md#实施待办)

模型直播 Avatar 已由号主暂停，既有绑定保留。替换推理服务时，按普通调用方实报模型与窗口，分别核对文本、工具、视觉和原生双工会话。[API 准入](specs/foundation/omni-deployment.md#接入验收与切换)

## 验收缺口与业务数据

- 网关变更时复测长文答案与持续混合负载，并核对服务队列和工作台回执。
- 笔电的原库已退役，正式写入权已交给工作站 Core。原库和迁移快照保留。后续回退须核对切换后新增的任务及回执。空库 P2 归档场已在 r6 结案，独立卷和回执留存。[P2 结案](../evaluations/results/creative-memory-p2-20261119.md) · [正式迁移记录](../evaluations/results/workstation-business-cutover-20261119.md) · [迁移与恢复](specs/foundation/distributed-runtime.md#迁移备份与恢复)
