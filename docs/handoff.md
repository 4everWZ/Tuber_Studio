# 项目交接 · 2026-11-23

工作站已于 `2026-11-23` 完成 **M0–M5 全仓分层重构、阶段二双管线演进及阶段四工作站统一切换**（`main` 提交 [`1953df9`](https://github.com/4everWZ/Tuber_Studio/commit/1953df93e8e7e05a6a66f65e4bafeebf399cf4b9) / 标签 `milestone/m5-acceptance`，已合入并推送）。后端按严格单向 DAG 收敛为 `L0 core → L1 infra → L2 persona → L3 {operations, live, agent} → L4 app`，API 全量收敛至 `/api/v1/*` 与 `{code, message, details}` 错误信封，前端升级为顶层 `frontend/`（React 19 + TS + TanStack Query + Vite），构建产物输出至 `src/tuber_studio/web_dist/` 由 FastAPI 与 `WebMount`（`/dafeiyu/`）统一提供。[架构蓝图](design/layered-architecture-refactor.md) · [M0–M5 总验收](../evaluations/results/20261122-layered-refactor-dual-pipeline-acceptance.md) · [阶段四上线验收](../evaluations/results/20261123-stage4-workstation-cutover.md) · [当前计划](plans/current.md#共享推理与-avatar-api)

## 从哪里接手

| 仓库 | 接手基线 |
| --- | --- |
| `<code-root>/omni-avatar-api` | 独立服务仓拥有推理、双工会话与 Avatar 音画。文本前台（`chat_llm`，`enable_thinking=False`）与后台/路由/运营（`Responses`、`operation_llm`）共用 `/llm/v1/*` 下的 9B 文本别名（`llm-default`）；Omni（`/v1/realtime`）专责全双工语音通话，通过共享 `runtime.tasks` 与文本会话联动。 [服务契约](https://github.com/4everWZ/omni-avatar-api/blob/main/docs/api.md) |
| `<code-root>/Tuber_Studio` | 已启用为工作站统一检出与构建根目录（`1953df9`，原 `<state-root>/releases` 共 72 个历史包已整迁至 `<code-root>/dafeiyu-studio-releases-rollback` 留存回滚，失效 `8089` 进程与 `current` 软链已清理）。持久化 SQLite 业务数据挂载 `/data/business`（切换前已留存离线校验快照 `<state-root>/backups/before-1953df9.sqlite3` 并完成 `--network none` 积压归档），凭据/环境/Nginx 本地覆盖与备份分离存放于 `<state-root>/{secrets,backups}`。业务入口为 `deploy/omni/app-compose.yml`（Docker `web-build` 阶段自动编译 `frontend/` 至 `src/tuber_studio/web_dist`）。 [Core 接入](../deploy/omni/README.md) · [部署与启动](guides/deployment.md) |

## 正式入口与后续接手

- **双管线自主运营：** 主动探索（`operations/discovery/`）与消息响应（`operations/inbox/`）通过 `operations/kernel/`（含 `operation_claims` 原子认领与 Inbox 优先租约、`direct_quota_reserve`、`read_gate` 风控退避优先保消息及 `(bvid, cid)` 单飞证据池）实现解耦。正式库已无损迁移至 `operations_pipeline_schema_version = 2` 且默认双关（`discovery_enabled=False`、`inbox_enabled=False`、`state="off"`），待操作者在工作台按需手动开启后，可 24h 单开 Inbox，工作台活动流标注 `discovery`/`inbox`/`manual` 来源徽标。
- **工作台与端到端门禁：** 4 个 HTML 入口与 20 页全部迁移 React 19，通过 `tests/architecture/` 零例外 AST 守卫、ESLint 边界守卫、`export_openapi.py --check` / `check:openapi`、真实 Microsoft Edge 隔离 E2E 及阶段四工作站实机切换复验（`/dafeiyu/`、`static/bundle/*.js`、保留的 `/vision-vantage/` 代理、`/api/v1/*`、`providers/check` 8192/262144 模型卡与临时 9B 对话全通，`account_writes` 增量严格为 0）。
- **已验证上线清单（2026-11-23 完成）：**
  1. 已将历史发布包归档至 `<code-root>/dafeiyu-studio-releases-rollback` 并移除旧 `current` 软链；
  2. 已停止占着 `8089` 端口的旧静态文件进程；
  3. 已在 `--network none` 下完成停机一致性备份、完整性校验及 `telemetry --apply` 积压归档，并经 `v2` 无损升级切换运行 `tuber-studio-core:1953df9`；
  4. 已从工作站 HTTPS `/dafeiyu/` 复验 React 产物、模型卡、`v1` 接口与临时 9B 对话，真实平台写入 `0`。[阶段四上线记录](../evaluations/results/20261123-stage4-workstation-cutover.md) · [IP 入口实测](../evaluations/results/workstation-studio-ip-20261119.md) · [API 准入](specs/foundation/omni-deployment.md#接入验收与切换)

## 验收缺口与业务数据

- 网关变更时复测长文答案与持续混合负载，并核对服务队列和工作台回执。
- 笔电的原库已退役，工作站阶段四切换前生产容器与正式库均未改动；后续上线须经操作者逐项备份与激活并留存原回滚包。空库 P2 归档场已在 r6 结案，独立卷和回执留存。[P2 结案](../evaluations/results/creative-memory-p2-20261119.md) · [正式迁移记录](../evaluations/results/workstation-business-cutover-20261119.md) · [迁移与恢复](specs/foundation/distributed-runtime.md#迁移备份与恢复)
