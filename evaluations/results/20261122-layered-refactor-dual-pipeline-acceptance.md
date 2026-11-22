# 全仓分层重构与阶段二运营双管线总验收报告（M0–M5）

本文档记录 `refactor/layered-architecture-dual-pipeline` 分支（基于 `main` 提交 `7ad556a`）从 M0 至 M5 闭环的最终代码门禁、自动化测试、OpenAPI 契约、隔离 Microsoft Edge 端到端实测与 `#115` 脱敏检查结果。所有实测均在隔离临时运行目录与受控桩服务中完成；真实平台写入次数为 `0`，工作站生产容器、正式数据库及 `main` 分支（`7ad556a`）全程未触碰。

---

## 一、M0–M5 里程碑演进与基线对照

| 里程碑 | 基线提交 / 标签 | 核心交付与验证落点 |
| --- | --- | --- |
| **M0** | `7ad556a` (`milestone/m0-batch1` / `main`) | Batch 1 双脑路由与隔离回放基线固定，确立 [架构与双管线蓝图](../../docs/design/layered-architecture-refactor.md)。 |
| **M1** | `86fde41` → `3e04f77` (`milestone/m1-l0-l2`) | 完成 `L0 core/`、`L1 infra/`、`L2 persona/` 下沉；B 站凭据剥离至 `infra/platforms/bilibili/account.py`；抽离 `tests/support/` 消除测试互导并建立 `tests/architecture/` AST 守卫。 |
| **M2** | `c485aa3` → `d53a096` (`milestone/m2-operations-dual-pipeline`) | 完成 12 处超千行热点拆解、`operations/kernel/` 五大共享原语（含 `claims.py` `operation_claims` 原子认领与楼级租约、`direct_quota_reserve`）、`discovery/` 与 `inbox/` 双管线解耦及无损增量迁移。 |
| **M3** | `b7d1e3a` → `f263138` (`milestone/m3-live-agent-api-v1`) | 完成 `L4 app/` 模块化装配、198 条 `/api/v1/*` 契约收敛与统一 `{code, message, details}` 错误信封、OpenAPI 导出链及 `#55` 文本（9B `/llm/v1/*`）与语音（Omni `/v1/realtime`）分工。 |
| **M4** | `8b95673` → `d996266` (`milestone/m4-react-frontend`) | 完成 `frontend/`（React 19 + TypeScript + TanStack Query + Vite + Vitest/RTL）4 个 HTML 入口与全量 20 页构建至 `src/tuber_studio/web_dist/`，清除遗留 `src/tuber_studio/web/`。 |
| **M5** | 本阶段结项（拟打标 `milestone/m5-acceptance`） | 补齐 CI 前端与 OpenAPI 门禁、Docker `web-build` 多阶段打包、`run-business.ps1` 入口修复、全套 Edge E2E 与 `#55`/`#115` 回归及文档/交接同步。 |

---

## 二、全仓自动化验证实测结果（最终代码修改后复测）

### 2.1 后端 AST 架构守卫、CI 配置与样式（0 例外 / 0 失败）

- **AST 分层与边界守卫（`uv run --locked pytest tests/architecture/ -v`）：** **91 passed (16.05s)**。
  - `LEGACY_ROOT_PENDING_M2 = frozenset()`（0 项）与 `LEGACY_ROOT_PENDING_M3 = frozenset()`（0 项）：`src/tuber_studio/` 根下仅保留 `__init__.py`、`__main__.py`、`prompts/` 及构建目录 `web_dist/`。
  - `LEGACY_CROSS_ALLOWLIST = frozenset()`（0 项）与 `LEGACY_L3_MODULE_CYCLE_ALLOWLIST = frozenset()`（0 项）：严格执行 `L0 core → L1 infra → L2 persona → L3 {operations, live, agent} → L4 app` 单向 DAG、`operations ↔ live` 零互导、`agent` 仅访问 `facade`、`operations/discovery ↔ operations/inbox` 零直连、无模块/子包循环依赖、测试跨文件零互导、FastAPI 路由仅限于 `app/`。
  - `ALLOWED_CREDENTIAL_MODULES = {"tuber_studio.infra.platforms.bilibili.account"}`：凭据边界 0 越界。
  - `test_ci.py` 与 `test_ci_style.py` 通过：`.github/workflows/ci.yml` 仅使用 SHA 钉死动作、`contents: read` 只读权限，无阻断缺失或 `continue-on-error`。
- **Ruff 静态检查（`uv run --locked ruff check src tests evaluations tools deploy`）：** `All checks passed!`。
- **OpenAPI 契约双向同步：**
  - `uv run --locked python tools/ci/export_openapi.py --check` → `OpenAPI contract OK (198 /api/v1/* paths, ErrorEnvelope present)`。
  - `pnpm --dir frontend run check:openapi` → `OpenAPI TypeScript schema is up to date.`。

### 2.2 前端工程、Node 组件与全量 Pytest 回归

| 检查项 | 执行命令 | 实测结果 |
| --- | --- | --- |
| TypeScript 类型检查 | `pnpm --dir frontend run typecheck` (`tsc --noEmit`) | 通过（0 错误） |
| ESLint 规范与边界守卫 | `pnpm --dir frontend run lint` (`eslint .`) | 通过（0 错误，含 `shared/features` 边界拦截） |
| Vitest / RTL 单元测试 | `pnpm --dir frontend run test` (`vitest run`) | **29 个测试文件、111 个用例全部通过**（含 `AppShell` 每秒 `/ws/admin` 推送下背景轮询不被饥饿的回归测试） |
| Vite 生产构建 | `pnpm --dir frontend run build` (`vite build`) | 通过（2.36s，236 模块产出 4 个入口：`index.html`、`live.html`、`overlay.html`、`draw-guess-overlay.html` 至 `src/tuber_studio/web_dist/`） |
| 可选点歌 Node 组件 | `npm --prefix components/song_requests test` (`node --test test/*.test.cjs`) | **4 passed, 0 failed, 0 skipped** |
| 全量 Pytest（CI 同口径） | `uv run --locked pytest -q -rs` | **2,796 passed, 13 skipped (988.72s)**；其中 10 项跳过为默认未设 `TUBER_TEST_CHROMIUM` 的浏览器用例（见下节在 Edge 全量执行），其余 3 项为宿主机环境条件跳过（POSIX 权限断言 `tests/core/test_auth_sessions.py:203`、显式工作站测试 `tests/infra/test_responses_provider.py:155`、本地 QA 库 `tests/persona/test_cooperation_memory.py:161`）。 |
| Edge 启用浏览器 Pytest 子集 | `TUBER_TEST_CHROMIUM=1` + `msedge.exe` 运行 `tests/{e2e,app,agent,persona}` 共 9 个浏览器文件（`-k "not offline_memory"`） | **78 passed, 1 deselected (133.86s)**；其中被反选的 `test_offline_memory_receipt_reads_sources_and_withdrawal_from_workstation_db` 已在上一行全量 `pytest` 中独立通过。 |

---

## 三、真实 Microsoft Edge 端到端与 `#55` 分流验证（0 平台写入）

所有浏览器回归均针对 Vite 最新构建产物 `src/tuber_studio/web_dist/`，以真实 `msedge.exe` 在隔离临时运行目录内执行：

1. **阶段二工作台与 `/dafeiyu/` 子路径端到端（`evaluations/operations/stage2_workbench_acceptance.py`）：** 退出码 `0`。
   - **鉴权与一级导航：** `#login-form` / `#token` 正常登录，左栏精确呈现 5 个一级工作区（`chat`、`automation`、`live`、`characters`、`settings`）。
   - **双管线独立控制卡：** `#inbox-pipeline-toggle` 与 `#discovery-pipeline-toggle` 独立启停互不联动（支持关闭主动探索而 24h 单开 Inbox）。
   - **直连配额预留与全局急停：** `direct_quota_reserve` 验证 `0`（`0 · 不预留 / 共享额度`）与 `3`（`已预留 3 条直连额度`）保存与回显；`紧急停止` 触发 `role="alert"` 全局锁定并禁用开关，`恢复急停` 解除。
   - **活动来源徽标与审核草稿：** `OperationsActivityFeed` 同页渲染 `inbox`、`discovery`、`manual` 三类来源徽标；`#comments` 内编辑并保存/审核草稿，验证 `account_write_rows: 0`、`platform_writes: 0`。
   - **模型连接面板（无 Ollama）：** `#settings` `ProvidersPanel` 仅维护单一 Omni/vLLM 网关入口，页面文本 Ollama 引用数 `0`。
   - **子路径 `WebMount`：** `/dafeiyu/` 独立实例精确注入 `<base href="/dafeiyu/">`，加载 `/dafeiyu/static/bundle/*.js` 返回 200，登录后 `http_errors: 0`、`console_errors: 0`、`page_errors: 0`。
2. **`#55` 模型角色分离与前台工具闭环（`evaluations/agent/front_tool_acceptance.py` + `tests/agent/test_omni_chat_loop.py`）：**
   - 在隔离本地模拟网关下通过 Edge 运行 `front_tool_acceptance` 全两阶段（`dialogue`、`call_text`、`discover`、`expression`、`setting`、`continuation`、`restart_seed` 及跨重启 `restart_resume`），`phase1_failed: []`、`phase2_failed: []`、`blocked_requests: 0`、五类 `platform_attempts` 全为 `0`。
   - 网关实录命中 `/llm/v1/responses`: `18`、`/llm/v1/chat/completions`: `13`、`/llm/v1/chat/prompt_tokens`: `12`，**`/v1/chat/completions`: `0`**；其中全部 10 次前台文字对话与日常设置均以 `enable_thinking=False` 路由至 `/llm/v1/chat/completions`（`llm-default`），仅后台 `PrivateDelivery` 使用 `enable_thinking=True`。
   - `tests/agent/test_omni_chat_loop.py`（5/5 通过）同步锁定 `front_ack` 走 `/llm/v1/chat/completions`（`enable_thinking=False`）且 `/v1/chat/completions` 零调用。
3. **既有浏览器场景（全部经真实 `msedge.exe` 复验通过）：**
   - `tests/browser/omni_settings_acceptance.py` 与 `tests/browser/live_handoff_check.py`：**2 passed (17.67s)**。
   - `tests/browser/owner_activity_fixture.py`：`ledgerAttempts: 1`, `externalConnections: 0`, `externalBrowserRequests: 0`。
   - `tests/browser/oc_capture_offline.py`、`tests/browser/dynamic_sharing_acceptance.py`、`tests/browser/operation_images_acceptance.py`、`tests/browser/private_browser_acceptance.py`：全部退出 `0`。
   - `tests/browser/run_private_call.py` 全三套场景（`private-call`、`private-whep`、`character-display`）：全部退出 `0`（复验过程中拦截并修复了 `frontend/src/shell/AppShell.tsx` 中因 `useEffect` 依赖 `status` 被 `/ws/admin` 每秒推送重置而饥饿 `characterDisplayStore.refresh()` 的真实缺陷）。
   - `tests/browser/chat-home.playwright.js`：输出 `PASS: domain outages, history/composer, expressions, hide/reload/restore, activity, settings and mobile reduced motion`。

---

## 四、CI、Docker、启动脚本与 `#115` 脱敏核查

- **CI（`.github/workflows/ci.yml`）：** 新增 `frontend` 作业（Node 24 + `pnpm@10.28.1`，执行 `--frozen-lockfile --ignore-scripts`、`typecheck`、`lint`、`test`、`build`），并在 `checks` 作业接入 `export_openapi.py --check`、`check:openapi`、`pnpm build`、`ruff check` 与 `pytest`。
- **Docker 与 Compose（`deploy/omni/{Dockerfile,Dockerfile.dockerignore,README.md,app-compose.yml}`）：**
  - `Dockerfile` 新增基于 `node:24-bookworm-slim` 的 `web-build` 阶段（`corepack prepare pnpm@10.28.1 --activate`、`pnpm install --frozen-lockfile --ignore-scripts && pnpm build`）并将产物拷贝至 `core` 镜像 `/opt/tuber/src/tuber_studio/web_dist`；`Dockerfile.dockerignore` 放行 `frontend/`（排除 `frontend/node_modules`、`frontend/dist` 与宿主机 `src/tuber_studio/web_dist`）。
  - `app-compose.yml config --quiet` 检查退出 `0`（注：当前 Windows 验证宿主机未安装 Docker 守护进程，未在本地执行 `docker build`，容器构建在工作站阶段四与 CI 中执行）。
- **Windows 启动脚本（`tools/deployment/run-business.ps1`）：** 修复入口检查路径为 `src/tuber_studio/app/__init__.py`，并在缺失 `src/tuber_studio/web_dist/index.html` 时自动使用 `pnpm` 安装并构建前端或给出清晰失败提示；PowerShell AST 语法解析器检查 `0` 错误。
- **隐私脱敏扫描（`tools/check_release_privacy.py`）：**
  - `--ref 7ad556a`（`main` 基线）与 `--ref HEAD` 均仅检出原基线相同的 `4 finding(s)`（`validation.py:10` 与 `test_providers.py` 合成尾网校验用例），**`0` 新增违规**。
  - 全工作树扫描 `frontend/src/`、`docs/`、`deploy/` 与 `evaluations/results/` 共 `384` 个文本/代码文件：**`0 findings`**。

---

## 五、阶段四上线准入说明

M0–M5 代码重构、阶段二双管线、React 工作台、CI/Docker 打包、Microsoft Edge 端到端回归及 `#55`/`#115` 规范同步已全部闭环，**准入用户审批后的阶段四工作站部署切换**：

- 工作站从统一检出 `<code-root>/Tuber_Studio`（与 `<code-root>/omni-avatar-api` 同级）执行构建，业务 SQLite 挂载 `/data/business`，密钥/环境与备份保持在 `<state-root>/{secrets,backups}`。
- 切换前由操作者按[交接文档](../../docs/handoff.md)与[部署指南](../../docs/guides/deployment.md)留存回滚归档包、停止占用旧本地路径或未使用端口的静态文件进程，并执行 `migrate_business.py`（`staged → active`）与正式入口复验。
