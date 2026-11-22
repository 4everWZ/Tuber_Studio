# 项目交接 · 2026-11-22

工作站业务架构已完成 **M0–M5 全仓分层重构与阶段二双管线演进**（`refactor/layered-architecture-dual-pipeline`，待用户审批后切换阶段四工作站正式上线）。后端按严格单向 DAG 收敛为 `L0 core → L1 infra → L2 persona → L3 {operations, live, agent} → L4 app`，API 全量收敛至 `/api/v1/*` 与 `{code, message, details}` 错误信封，前端升级为顶层 `frontend/`（React 19 + TS + TanStack Query + Vite），构建产物输出至 `src/tuber_studio/web_dist/` 由 FastAPI 与 `WebMount`（`/dafeiyu/`）统一提供。[架构蓝图](design/layered-architecture-refactor.md) · [M0–M5 总验收](../evaluations/results/20261122-layered-refactor-dual-pipeline-acceptance.md) · [当前计划](plans/current.md#共享推理与-avatar-api)

## 从哪里接手

| 仓库 | 接手基线 |
| --- | --- |
| `<code-root>/omni-avatar-api` | 独立服务仓拥有推理、双工会话与 Avatar 音画。文本前台（`chat_llm`，`enable_thinking=False`）与后台/路由/运营（`Responses`、`operation_llm`）共用 `/llm/v1/*` 下的 9B 文本别名（`llm-default`）；Omni（`/v1/realtime`）专责全双工语音通话，通过共享 `runtime.tasks` 与文本会话联动。 [服务契约](https://github.com/4everWZ/omni-avatar-api/blob/main/docs/api.md) |
| `<code-root>/Tuber_Studio` | 工作站统一检出与构建根目录（替代旧 `<state-root>/releases/<sha>` 解包目录）。持久化 SQLite 业务数据挂载 `/data/business`，凭据/环境/备份分离存放于 `<state-root>/{secrets,backups}`。业务入口为 `deploy/omni/app-compose.yml`（Docker `web-build` 阶段自动编译 `frontend/` 至 `src/tuber_studio/web_dist`）。 [Core 接入](../deploy/omni/README.md) · [部署与启动](guides/deployment.md) |

## 正式入口与后续接手

- **双管线自主运营：** 主动探索（`operations/discovery/`）与消息响应（`operations/inbox/`）通过 `operations/kernel/`（含 `operation_claims` 原子认领与 Inbox 优先租约、`direct_quota_reserve`、`read_gate` 风控退避优先保消息及 `(bvid, cid)` 单飞证据池）实现解耦，升级后默认双关，关闭探索可 24h 单开 Inbox，工作台活动流标注 `discovery`/`inbox`/`manual` 来源徽标。
- **工作台与端到端门禁：** 4 个 HTML 入口与 20 页全部迁移 React 19，通过 `tests/architecture/` 零例外 AST 守卫、ESLint 边界守卫、`export_openapi.py --check` / `check:openapi` 与真实 Microsoft Edge E2E（含 `/dafeiyu/` 子路径挂载、Stage 2 双控制卡与 `#55` 9B 对话回放）验收。
- **切换工作站（阶段四）前检查清单：**
  1. 在工作站代码区 `<code-root>/` 保留好历史发布包与数据库离线备份快照后再清理旧解包目录；
  2. 停止占用废弃本地运行路径或未使用端口的旧静态文件进程；
  3. 切换前通过 `tools/deployment/migrate_business.py` 走 `inventory → snapshot → verify → restore`（`/data/business`、`staged → active`）并核验 `tools/check_release_privacy.py` 零违规；
  4. 按单端口/HTTPS `/dafeiyu/` 入口复验真实模型卡、流式对话、私有通话与三渠道写入边界。[IP 入口实测](../evaluations/results/workstation-studio-ip-20261119.md) · [API 准入](specs/foundation/omni-deployment.md#接入验收与切换)

## 验收缺口与业务数据

- 网关变更时复测长文答案与持续混合负载，并核对服务队列和工作台回执。
- 笔电的原库已退役，工作站阶段四切换前生产容器与正式库均未改动；后续上线须经操作者逐项备份与激活并留存原回滚包。空库 P2 归档场已在 r6 结案，独立卷和回执留存。[P2 结案](../evaluations/results/creative-memory-p2-20261119.md) · [正式迁移记录](../evaluations/results/workstation-business-cutover-20261119.md) · [迁移与恢复](specs/foundation/distributed-runtime.md#迁移备份与恢复)
