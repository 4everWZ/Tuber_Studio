# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12 与 uv，不向系统／base环境安装依赖：

    uv sync --python 3.12
    uv run python -m tuber_studio --port 8020

直接打开 `http://127.0.0.1:8020` 即可使用个人工作台。配置读取runtime/studio.json，不存在时用config/studio.example.json；Provider／API／窗口可在设置页保存并应用，手工编辑配置文件后才需要重启。业务服务启动不代表模型／TTS已经加载或通过容量验证。

远程或代理访问从 `runtime/admin-token` 读取管理令牌，登录后浏览器保留会话。保留 `runtime/studio.sqlite3` 和 `admin-token` 可跨服务重启及代码更新继续使用。远程管理设置可重置令牌和退出登录。若部署显式设置 `TUBER_ADMIN_TOKEN`，本机访问也使用该令牌，轮换在环境中完成。

B 站账号在「设置 → 账号与登录」扫码并在手机上确认。工作站核验账号后保存会话，设置页显示续期状态、上次核验时间和账号资料。退出工坊管理界面保留已绑定的运营账号。换号、续期与手动凭据见[账号登录说明](operations.md)。

离线诊断可用llm.kind=demo、tts.kind=prerecorded并选预录音色；界面必须标明固定台词／预录，不能冒称真实推理、动态配音或128K能力。

## 提交前检查

在准备提交的开发工作树中，用锁定的开发环境运行代码检查入口：

    uv sync --locked --group dev --python 3.12
    uv run --locked python tools/ci/check_style.py

该命令只检查，不改文件；每次都会分别报告 lint 和 format 的结果，任一失败就返回非零退出码。仅看到 `ruff check` 成功不代表格式检查通过。格式问题可运行 `uv run --locked ruff format src tests tools` 修复，检查差异后重新运行上述入口；修改之后再提交，不要把修改前的检查结果当作当前提交已通过。

CI 分别运行锁定项目环境的 Python 测试和可选点歌组件的 Node 测试，不再打包或推送 Jetson 环境、程序包或权重。Lint 和格式检查保留为本地命令。

这些代码检查可在 Windows／Linux 的开发环境独立运行。服务能力和实际设备上的端到端验收见下文。

## 业务部署与服务接入

大肥鱼工坊的正式入口部署在工作站。Core 在同一进程提供工作台前端、静态文件和业务 API，工作站同时承载数据库、推理与 Avatar。笔电浏览器直接打开工作站的 HTTPS IP。麦克风需要浏览器信任该 IP 对应的服务器证书。[工作站 IP 部署](../../deploy/omni/README.md#工作站-ip-入口)

已有业务卷不会随代码升级自动更换服务。到「设置 → 推理端点」选择兼容 Chat 的协议，填写工作站的 `/v1` 地址、Omni 别名及分配给工坊的凭据，勾选「这是工坊 Omni 服务」可同步切换视频取材并读取实报窗口。在同一表单将后台 Base URL 设为该服务的 `/llm/v1`，填写后台模型别名和应用凭据。不要使用管理密钥；旧地址的凭据不会随表单改址转发。

可选的私有语音需配置浏览器可达的 `TUBER_PRIVATE_REALTIME_ORIGIN`（HTTPS，开发时可用回环 HTTP）和业务主机上的 `TUBER_PRIVATE_REALTIME_TOKEN_FILE`。文件只放专用于私聊的普通应用凭据。没有配置时不展示通话入口；点击开始后才签发短时票据并请求麦克风。直播、Avatar 媒体和真实播完记录分别验收。

电话只签发双工语音票据。直播独立读取 Pets 和 Avatar 演出偏好，当前使用已验收的 pet 素材。[电话与直播选路](private-avatar-call.md)

本地新安装使用回环网关 `http://127.0.0.1:8000`：前台聊天和视频视觉走 `/v1`（`omni-default`），后台任务走 `/llm/v1`（`llm-default`），窗口读取模型卡。语音默认预录，不启动推理节点。工作站在其他主机时，分别配置可达的服务地址和凭据；已有 `studio.json` 不会被示例覆盖。

Tuber Studio 运行业务、工作台和平台工具，通过受认证的 API 接入已部署的 Omni 推理与 Avatar 服务。Core 可以运行在选定的业务主机上，浏览器和 OBS 使用各自可达的业务与媒体入口。工坊不训练模型、不下载或装载推理权重。模型及渲染资产由独立的 `omni-avatar-api` 服务管理，业务数据库、平台凭据和管理会话由 Core 管理。 [数据归属](../specs/foundation/distributed-runtime.md) · [服务接入](../../deploy/omni/README.md)

使用仓库的 Core Compose 时，准备可达的网关及独立的 Core 服务令牌。示例地址为 `http://omni-gateway:8000/v1`，由外部 Docker 网络提供寻址。使用其他兼容地址时，从工作台配置相应 Provider 和凭据。访问远程 Core 时使用管理令牌登录，回环绑定可用 SSH 转发。 [Compose 启动与工作台访问](../../deploy/omni/README.md)

Windows 业务主机也可使用项目环境直接启动。启用在线点歌时，安装 Node.js 22 或更高版本并执行第二条命令。

    uv sync --python 3.12
    npm --prefix components/song_requests ci --ignore-scripts
    & tools/deployment/run-business.ps1 -Data runtime/desktop/business -Background

启动器默认监听 `http://127.0.0.1:9020`，通过 `TUBER_RUNTIME` 传递数据目录。可用 `-Source`、`-Python`、`-Config` 和 `-BindAddress` 指定程序、环境、配置和绑定地址。同一业务目录须由一个服务进程独占。

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

## Provider与上下文设置

在「设置 → 推理与模型」配置文本和视频视觉 Provider，在「角色 → 语音」选择独立音色。填写服务实际提供的 Base URL、模型或声音 ID 与对应凭据。OpenAI-compatible LLM 使用包含版本前缀的地址，例如 `https://omni.example.com/v1`。语音合成通过兼容的 `audio/speech` 返回 WAV。公网入口使用 HTTPS，已授权的内部网络可使用服务提供的内部地址。 [模型、声音与资源规范](../specs/foundation/models-and-audio.md)

先查询实际模型卡与能力，再用当前 Provider 做正常及流式生成、结构化输出和实际取帧。经认证的模型卡窗口覆盖渲染后的输入和输出。通用 API 仅有配置声明时按声明显示。改变 Provider 或窗口后，下次生成重新计数。思考默认开启，可在设置中切换。画面失败且有字幕时按字幕继续，详情显示失败原因。 [Omni 接入能力与验收](../specs/foundation/omni-deployment.md#接入验收与切换)

LLM、后台 Responses、视觉和 TTS 的 Key 存于私有 `runtime/provider-credentials.json`，绑定完整 Base URL。视频视觉可以使用同一 `/v1` 地址的 LLM Key。换地址时须重新填写或清除对应 Key；旧文件若只有 Key 而没有绑定地址，升级后须在确认地址后重新填写，不会把它发送给当前默认服务。语音 API 使用独立的 `external_api` 音色，角色切换和换声线按实际设置生效。兼容服务会收到完成任务所需的上下文，业务与服务侧留存分别管理。

Avatar 会话入口与媒体观看入口分开配置和验收。浏览器或 OBS 使用观看凭据实际解码节目，并验证已发布的 ICE 候选可达。播放回执和业务平台回执分别核对。 [节目接入示例](../../deploy/omni/README.md#gif-avatar-与-obs-媒体)

表情包由共享图库统一管理，图库原件独立于媒体缓存。 [大肥鱼表情包](operations.md#大肥鱼表情包)

## Profile验收

切换服务或修改 Profile 后，使用已授权的隔离数据核对正常 Chats、后台任务恢复、真实视觉来源、窗口与结构化生成。已配置语音或 Avatar 时，分别验证原生双工、朗读、取流与实际播放。记录低负载和持续混合负载的首字、排队、首音及实际完成回执。完成业务、模型和媒体的相应验收后再切换业务写入者。 [接入验收与切换](../specs/foundation/omni-deployment.md#接入验收与切换)
