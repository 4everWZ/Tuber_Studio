# 阶段四工作站统一切换、备份归档与实机验收报告 · 2026-11-23

本文档记录 `main` 分支合入并推送至 `1953df9`（`milestone/m5-acceptance`）后，在工作站执行 **阶段四正式上线切换（Stage 4 Cutover）** 的全过程、离线备份与积压归档结果、切换后 HTTPS / `/api/v1/*` 验收实录及零平台写入核对。切换全程未开启任何自主运营/自动发送开关，**真实平台写入增加量为 `0`**。

---

## 一、环境收口、统一代码目录与镜像构建

1. **统一源码检出：** 在 `<code-root>/Tuber_Studio`（与 `<code-root>/omni-avatar-api` 同级）检出 `main` 并核对 `HEAD` 为 `1953df93e8e7e05a6a66f65e4bafeebf399cf4b9`，工作区干净。
2. **清理失效本地进程：** 核实宿主机占用 `8089` 端口的旧静态 HTTP 服务进程（指向已移除的旧本地模型目录）并终止，复核 `8089` 端口已释放。
3. **运行配置与 Nginx 代理保留：** 在仓库外 `<state-root>/` 维护生产 `deployment.env`（`0600`）、保留 `/vision-vantage/` 既有同机反向代理规则的 `workstation.nginx.template` 及 `workstation-local.compose.yml` 覆盖文件，`docker compose ... config --quiet` 校验通过，且 `<code-root>/Tuber_Studio` 保持未污染。
4. **镜像构建与断网冒烟：** 以 `deploy/omni/Dockerfile` 构建 `tuber-studio-core:1953df9`（自动执行 Node 24 + `pnpm` React 多入口 `web-build` 阶段、`songs` 组件与 Python 3.12 `uv sync --locked --no-dev`）；在 `--network none` 下通过导入 `tuber_studio.app:create_app`、`tuber_studio.operations.kernel.telemetry:inventory` 与内置 `web_dist/index.html`（`879` 字节）校验。

---

## 二、停机离线备份与历史积压归档（`--network none`）

在 `<state-root>/.core-release.lock` 排他锁保护下执行切换前停机与离线处理：

1. **切换前基线（运行中 `tuber-studio-core:9d19905`）：**
   - `GET /api/automation`：`state = "off"`、`active_rounds = 0`。
   - SQLite 只读核查：`account_writes = 1640`、`uncertain = 0`。
2. **停机、WAL 截断与一致性备份：**
   - 停止旧 Core 后，以 UID/GID `65532:65532` 在 `--network none` 容器内执行 `PRAGMA wal_checkpoint(TRUNCATE)`，确认 `[(0, 0, 0)]` 且 `-wal` 为 `0` 字节。
   - 挂载业务卷为只读（`:ro`）并通过 `sqlite3.Connection.backup()` 生成独立一致性备份 `<state-root>/backups/before-1953df9.sqlite3`（`488,529,920` 字节，权限 `0600`）与 `before-1953df9.studio.json`（`1,270` 字节，`0600`）；验证备份文件 `PRAGMA integrity_check = ok`、`account_writes = 1640`。
3. **断网积压归档（`python -m tuber_studio.operations.kernel.telemetry ... --apply`）：**

| 检查阶段 | `visits` (`retain` / `revisit_parked`) | `expired_reactions` | `private` (`recover` / `check`) | `video_rows` / `video_work` | `uncertain` | `account_writes` |
| --- | --- | --- | --- | --- | --- | --- |
| `--apply` 执行结果 | `329` / `2267` | `657` | `10` / `1` | `4810` / `410` | `0` | `1640` |
| 归档后只读复核 | `327` / `2`（验证间隙新增越过 72h 窗口） | `0` | `10` / `1` | `4810` / `410` | `0` | `1640` |

归档后复核库内 `browsing_events(status='revisit_parked')` 累计 `8,026`、`browsing_intents(status='archived')` 累计 `775`，`PRAGMA integrity_check = ok`。

---

## 三、新版 Core 滚动上线与无写入验收

以 `docker compose up -d --no-deps --no-build --wait` 启动 `tuber-core` 并重建 `studio-edge`（`dafeiyu-studio-reading-worker-1` 保持健康运行不中断）：

1. **容器与日志状态：**
   - `dafeiyu-studio-tuber-core-1`：`tuber-studio-core:1953df9`，状态 `healthy`，`org.opencontainers.image.revision=1953df93e8e7e05a6a66f65e4bafeebf399cf4b9`，仍挂载 `<business-volume>` 至 `/data`（`TUBER_RUNTIME=/data/business`）。
   - Core 启动与验收期间日志无任何异常或回溯栈。
2. **HTTPS 入口与静态构建产物核验（`https://<workstation-ip>`）：**
   - `GET /dafeiyu/healthz` → `200`（`{"status":"ok"}`）。
   - `GET /dafeiyu/` → `200`，HTML 包含 `<base href="/dafeiyu/">`，主入口 `static/bundle/index-9eT3319Z.js`、样式 `static/bundle/LiveSceneCanvas-CHpy9BCB.css` 及 `static/studio-path.js` 均返回 `200`。
   - `GET /` → `404`（符合生产 IP 根路径屏蔽规则）；`GET /vision-vantage/` → `302`（同机原有反向代理不受影响）。
3. **`/api/v1/*` 契约、数据库 `v2` 无损升级与网关/对话复核（不触发平台写）：**
   - `GET /api/v1/system/settings`：`omni.base_url = "http://omni-gateway:8000/v1"`、`omni.model = "omni-default"`、`omni.responses_model = "llm-default"`、`provider_credentials.omni = true`。
   - `POST /api/v1/system/providers/check`：`{"foreground": {"available": true, "context_tokens": 8192}, "background": {"available": true, "context_tokens": 262144}}`。
   - `GET /api/v1/operations/automation`、`GET /api/v1/operations/discovery`、`GET /api/v1/operations/inbox`：总开关 `enabled = false`、`state = "off"`、`direct_quota_reserve = 2`，`discovery_enabled = false`、`inbox_enabled = false`、`mentions_enabled = false`、`replies_enabled = false`、`mention_reply_enabled = false`，已保留登录账号绑定、活跃角色 `whale`（`v3`）及入站视图（`100` 条事件、`2` 条游标、`11` 条来信记录）。
   - `GET /api/v1/persona/characters`（`2` 个角色卡）与 `GET /api/v1/agent/tasks`（`0`）正常返回 `200`。
   - **数据库 v2 结构迁移核验：** `kv.operations_pipeline_schema_version = 2`、已创建 `operation_claims` 表及 `browsing_rounds.source_pipeline` 列。
   - **临时 9B 文字对话复验（`POST /api/v1/agent/chat`，`temporary: true`）：** HTTP `200`（耗时 `4.236s`，Core 日志记录 `turn_router route=chat action=chat route_ms=2967 input_tokens=1233 output_tokens=187 reasoning_tokens=159`），返回完整角色回复且不污染永久会话。
   - **写账本终审：** 全部验收完成后只读核查 SQLite，`account_writes` 仍为 **`1640`**（新增为 `0`）。

---

## 四、旧发布目录归档与目录终态验证

上线验收全部通过后，将 `<state-root>/releases/`（含 `9d19905` 在内的 72 个历史解包版本）整体迁至 `<code-root>/dafeiyu-studio-releases-rollback` 留作回滚归档，并移除失效的 `<state-root>/current` 软链接。迁移后再次复测 HTTPS `/dafeiyu/healthz`（`200`）、`/dafeiyu/`（`200`）、`/vision-vantage/`（`302`）与 `/`（`404`），确认现网容器对旧目录已零依赖。

---

## 五、上线后待操作项（由号主自主启用）

- 自主运营双管线按升级安全策略默认均处于关闭（`off`）；如需恢复运行，由操作者在工作台 `/dafeiyu/` 手动按需开启 `Inbox`（消息响应）或 `Discovery`（主动探索），并可在实网条件就绪时单独核验真实 B 站回执、来信及私有语音通话。
