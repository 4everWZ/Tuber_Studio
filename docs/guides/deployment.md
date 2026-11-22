# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12（`uv`）与独立前端工程 `frontend/`（Node 24 + `pnpm@10.28.1` + React 19 + Vite），不向系统／base 环境安装依赖：

    uv sync --python 3.12
    pnpm --dir frontend install --frozen-lockfile --ignore-scripts
    pnpm --dir frontend run build
    uv run python -m tuber_studio --port 8020

`pnpm --dir frontend run build` 将前端多入口产物构建到 `src/tuber_studio/web_dist/`（`.gitignore` 排除，不入库）。工作台默认在 `http://127.0.0.1:8020`（或经 `TUBER_WEB_PREFIX=/dafeiyu` 配置子路径挂载 `<base href="/dafeiyu/">`）。配置读取业务目录的 `studio.json`，新装以 `config/studio.example.json` 为模板。运行中的正式实例通过 `/api/v1/system/settings/*` 保存更改。

远程或代理访问从 `runtime/admin-token` 读取管理令牌，登录后浏览器保留会话。保留 `runtime/studio.sqlite3` 和 `admin-token` 可跨服务重启及代码更新继续使用。远程管理设置可重置令牌和退出登录。若部署显式设置 `TUBER_ADMIN_TOKEN`，本机访问也使用该令牌，轮换在环境中完成。

B 站账号在「设置 → 账号与登录」扫码并在手机上确认。工作站核验账号后保存会话，设置页显示续期状态、上次核验时间和账号资料。退出工坊管理界面保留已绑定的运营账号。换号、续期与手动凭据见[账号登录说明](operations.md)。

固定试听可离线播放登记的原句；验证动态朗读需向网关送读新句并实听。

## 提交前检查

在准备提交的开发工作树中，用锁定的项目环境分别运行前端、OpenAPI 契约、后端、AST 架构守卫与脱敏检查：

    uv sync --locked --group dev --python 3.12
    pnpm --dir frontend install --frozen-lockfile --ignore-scripts
    pnpm --dir frontend run typecheck && pnpm --dir frontend run lint && pnpm --dir frontend run check:openapi && pnpm --dir frontend run test && pnpm --dir frontend run build
    uv run --locked python tools/ci/export_openapi.py --check
    uv run --locked python tools/ci/check_style.py
    uv run --locked pytest -q
    uv run --locked python tools/check_release_privacy.py

`tools/ci/check_style.py` 分别报告 Ruff lint 和格式检查结果；可用 `uv run --locked ruff format src tests evaluations tools deploy` 修正格式。`pytest` 自动执行 `tests/architecture/` 零例外 AST 分层与边界守卫，以及 `/api/v1/*` 契约与双管线回归测试。可选点歌组件使用 `npm --prefix components/song_requests test`（`node --test test/*.test.cjs`）。

GitHub Actions 工作流（`.github/workflows/ci.yml`）按 `song-component`、`frontend` 与 `checks`（含 OpenAPI 同步校验、`ruff check` 与 `pytest`）运行上述阻断检查。所有文档、脚本与验收记录必须通过 `tools/check_release_privacy.py`，禁止写入真实私网地址、主机名、个人 OS 路径、真实账号 UID 或 Docker 卷哈希。

## 业务部署与服务接入

大肥鱼工坊的正式入口部署在工作站。Core（`src/tuber_studio/app/__init__.py`）在同一进程提供打包后的 React 前端（`src/tuber_studio/web_dist/`）、静态资源与 `/api/v1/*` 业务 API，工作站同时承载数据库、推理与 Avatar。笔电浏览器打开工作站 HTTPS 地址下的 `/dafeiyu/`。麦克风需要浏览器信任该 IP 对应的服务器证书。[工作站 IP 部署](../../deploy/omni/README.md#工作站-ip-入口)

工作站生产构造遵循统一代码目录与运行时隔离约定：

- **统一代码与 Compose 构建根目录：** 直接在统一 Git 检出目录 `<code-root>/Tuber_Studio`（与 `<code-root>/omni-avatar-api` 同级）执行 `docker compose build/up`，不再向 `<state-root>/releases/<sha>` 解压旧式版本包；`deploy/omni/Dockerfile` 的 `web-build` 阶段（`node:24-bookworm-slim` + `pnpm@10.28.1`）自动在容器内构建 `src/tuber_studio/web_dist` 并拷贝入 `core` 镜像。
- **状态、密钥与备份分离：** 业务 SQLite 与运行状态持久化挂载在 `/data/business`；网关令牌、环境配置与正式备份目录保留在 `<state-root>/{secrets,backups}`，严格脱离代码树与 Git 仓库。
- **切换前清理清单：** 正式上线切换前，在 `<code-root>/` 保留好回滚归档包再退役旧解包发布目录，并提前停止占用失效本地路径与未使用端口的旧静态文件进程。

在「设置 → 共用 Omni API」填写网关地址、图文与语音模型别名、后台 Responses 别名和普通应用凭据，点击「保存 Omni 设置」。新装示例的 `omni.base_url` 为 `http://127.0.0.1:8000/v1`。使用工作站时填 Core 可达的网关地址。前台和后台可使用服务分配的不同别名；后台 Responses、视频取帧、参考朗读和配图共用这项连接。[能力路由及验收](../specs/foundation/omni-deployment.md)

浏览器从受信任的 HTTPS 入口访问电话，拨号时由 Core 换取限定 Realtime 的短时票据。直播演出偏好在工作台保存。[电话与直播演出](private-avatar-call.md)

Compose 的网络、网关根地址和凭据文件见 [Core 部署](../../deploy/omni/README.md#core)。账号、任务、平台授权、角色选择与播放回执保存在业务卷。已有安装保留所选声音身份，包括固定试听和已选的 `external_api`。

Windows 业务主机也可使用项目环境直接启动。启用在线点歌时，安装 Node.js 22 或更高版本并执行第二条命令。

    uv sync --python 3.12
    npm --prefix components/song_requests ci --ignore-scripts
    & tools/deployment/run-business.ps1 -Data runtime/desktop/business -Background

启动器验证 `src/tuber_studio/app/__init__.py`、业务数据目录与 `src/tuber_studio/web_dist/index.html`（若缺少构建产物且环境存在 `pnpm`，会自动安装依赖并执行 `pnpm build`），默认监听 `http://127.0.0.1:9020`，通过 `TUBER_RUNTIME` 传递数据目录。可用 `-Source`、`-Python`、`-Config` 和 `-BindAddress` 指定程序、环境、配置和绑定地址。同一业务目录须由一个服务进程独占。

### 迁移业务数据

迁移采用 `tools/deployment/migrate_business.py`。停止原业务写入者后依次运行 `inventory`、`snapshot` 和 `verify`。经受控通道传输快照，核对传输完整性，再使用隔离恢复环境运行 `restore`。Compose 的正式业务卷挂载到 `/data/business`，Core 用户 UID/GID 65532 须有读写权限。核对账号与登录文件、SQLite、业务引用和未决提交。 [业务卷 Compose 覆盖](../../deploy/omni/app-business-data.compose.yml)

恢复副本初始为 `staged`。以隔离数据验收当前服务后，确认原写入者停止并标为 `retired`，在选定 Core 主机把业务卷标为 `active`。传给迁移工具的 `--writer-host` 须匹配部署的 `TUBER_CORE_HOSTNAME`。激活后从该工作台核对账号与任务状态，待核对提交沿原回执处理。回退时先核对切换后的新增业务数据，再交回原实例的写入权。 [唯一写入者与恢复](../specs/foundation/distributed-runtime.md#迁移备份与恢复)

## 首次操作

1. 评论页刷新热门视频或填写BV／av／标准链接，读取失败可重试或改导入。
2. 导入 CSV／JSON 时按需映射字段，也可预览规范化结果。导入后直接选择该视频的未处理评论。手工分类会保留。
3. 选择评论生成草稿，再编辑、保存、拒绝、发布或复制。启动自主运营前，在运营设置保存来源、渠道权限与额度，并在账号页验证凭证。导入后的审核和复制可离线完成。
4. Mock／语音验收时先打开独立overlay，再注入事件；只有一个播放页拥有播放权。浏览器禁止自动播放时按提示启用，再注入新事件，不重放失败任务。
5. 暂停阻止后续任务；静音／停止取消当前播报。看真实平台回执区分审核、复制、发布和不确定结果。

第 4、5 项适用于已启用的 Mock 或直播工作台。源码模板默认关闭直播功能。

评论按楼 ID 隔离上下文，选择单楼可查看占用及压缩状态。同批可调度 4 楼，实际推理并发受服务配额约束。审核时可展开原评论、引用和缺项诊断。发现缺项时可查看原稿并修订。

设置页可保存对话留存天数（默认30天）或选择不自动清理，策略保存在SQLite；旧event_retention_days不再控制会话清理。手动清理先预览，再勾选确认；可指定UTC截至日期，按该日结束的排他时间边界提交。原始评论数据集／草稿／发布回执独立保留，清理不是全库删除。楼上下文失效后，下次生成会按本地原文重建，也可点“重建上下文”；已有非空窗口不覆盖。无原文时重新读取或导入，不从已清理摘要／旧草稿复活故事。Demo显示未真实prefill。

## 连接验证

在「设置 → 连接」点击「检查已保存的连接」，通过统一的单个 Omni/vLLM 网关入口维护地址、模型别名与凭据（无 Ollama 连接），分别查看前台对话模型与后台文本模型的模型卡读取结果。文本对话前台（`chat_llm` → `/llm/v1/chat/completions`，`enable_thinking=False`）与后台路由／任务／运营（`/llm/v1/responses` 与 `/llm/v1/chat/completions`）共用网关上的 `responses_model`（`llm-default`），全双工私有语音通话专走 Omni `/v1/realtime`。用普通应用凭据读取网关模型卡与窗口，再验证正常与流式对话、后台工具回执和真实视频帧。改变网关或窗口后重新计数。思考选项保留在设置中；画面读取失败时在任务详情查看原因，有字幕可继续取材。凭据保存在业务端，换地址时重新绑定。[API 接入验收](../specs/foundation/omni-deployment.md#接入验收与切换)

参考朗读从工作台输入新句并试听。直播另用获播放权的独立播放器验证播完回执。启用节目媒体时，从实际浏览器或 OBS 验证解码和可达性。[节目接入](../../deploy/omni/README.md#gif-avatar-与-obs-媒体)

表情包由共享图库统一管理，图库原件独立于媒体缓存。 [大肥鱼表情包](operations.md#大肥鱼表情包)

## 切换验收

在隔离业务卷核对 Chats、后台任务恢复、取材来源、结构化生成与启用的音画能力。记录低负载和持续混合负载下的排队、首字、首音与完成回执。正式持久配置通过当前 Core 的设置 API 保存。需要离线迁移时停服取得业务卷独占锁，再交接写入权。[接入验收与切换](../specs/foundation/omni-deployment.md#接入验收与切换)
