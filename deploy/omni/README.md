# Tuber Studio Omni 业务部署

Tuber Studio 消费已部署的 Omni 推理和 Avatar 服务。业务启动需要服务地址、模型别名及对应凭据。可选节目另需 Avatar 会话和观看入口。引擎、渲染器和媒体服务由各自部署方维护。 [独立推理部署仓库](https://github.com/4everWZ/omni-avatar-api)

本仓库的示例 Compose 加入名为 `tuber-omni` 的外部 Docker 网络，可用 `TUBER_OMNI_NETWORK` 指定已创建的网络。示例使用 `omni-gateway:8000` 和默认别名 `omni-default`。Core 的 9020 默认发布到宿主机回环地址，同网络代理可访问 `http://tuber-core:9020`。Core 共用一组 Omni 网关地址与凭据。示例注入的 Core 网关令牌仅用于预设网关地址。 [配置入口](../../docs/guides/deployment.md#连接验证)

## Core

在仓库根目录执行。先从基础设施管理员取得分配给 Core 的网关服务令牌，并将它存入 `deploy/omni/secrets/gateway-core.token`。`secrets/` 用宿主目录权限限制访问，容器内 UID/GID 65532 需要可读各自的令牌文件。可用 `OMNI_CORE_TOKEN_FILE` 指向授权的其他文件。

同一网关地址可在工作台保存新的普通应用凭据。保存值优先，清除后恢复该地址的 Compose 凭据。换成其他地址时须为新地址单独授权。

设置 API 的 `PUT /api/settings/providers` 接受 `omni.base_url`、`omni.model` 和 `omni.responses_model`。后台并发和输出额度分别使用 `omni.responses_options.parallel_reads` 和 `max_output_tokens`。填写 `credentials.omni` 可更换凭据，发送 `clear_credentials: ["omni"]` 可清除保存值。`GET /api/settings` 通过 `provider_credentials.omni` 报告当前是否持有可用凭据。

```sh
docker compose -f deploy/omni/app-compose.yml config --quiet
docker compose -f deploy/omni/app-compose.yml up -d --build --wait tuber-core
curl --fail http://127.0.0.1:9020/healthz
```

通过 `OMNI_GATEWAY_URL` 指定服务根地址（不带 `/v1`）。Omni 前台使用 `OMNI_MODEL_ALIAS`，后台使用 `OMNI_LLM_MODEL_ALIAS`。工作站与 Core 在不同机器时使用 Core 可达的工作站地址；不需要在 Core 安装引擎。用应用凭据验证 Chat、模型卡实报窗口、真实图文输入和后台 `/llm/v1/responses` 函数结果回传。只得到健康响应不代表 Agent 已接通。参考音色朗读使用同一 Omni 连接。 [能力与验收](../../docs/specs/foundation/omni-deployment.md#接入验收与切换)

`core-data` 是 Core 独占的持久卷，保存、备份和迁移步骤沿用 [部署指南](../../docs/guides/deployment.md)。复用已有业务数据时须保有 `studio.json` 及权威写入者记录，`TUBER_CORE_HOSTNAME` 需和迁移时登记的目标一致。初次建卷读取部署的 Omni 连接。`TUBER_CORE_MODE=existing` 要求卷内已有配置。切换已存在的配置通过工作台及部署准入完成。

接收现有正式业务时，将已核验快照还原到独立 Docker 卷的 `/data/business`。同用 `-f deploy/omni/app-business-data.compose.yml` 接入 `TUBER_CORE_BUSINESS_VOLUME` 指定的卷。恢复和正式 Core 使用相同容器内路径，数据库的取材引用在恢复时重新绑定。正式数据按迁移工具的 `staged → active` 交接。在 staged 卷运行 `python /opt/tuber/core_entrypoint.py --stage-omni` 可在独占写锁下准备网关配置。角色音色选择按稳定 ID 迁移，凭据仅可在绑定的网关地址复用。任务及平台连接由正式 Core 激活后管理。

## 从电脑访问业务工作台

### 工作站 IP 入口

将 Core、TLS 代理、业务卷和独立推理服务部署到同一台工作站。笔电只需要浏览器。将 `TUBER_WORKSTATION_IP` 设为浏览器可达的工作站 IPv4，另以 `TUBER_WORKSTATION_CERT` 和 `TUBER_WORKSTATION_KEY` 指定含该 IP SAN 的服务器证书和私钥路径。浏览器须信任证书。工作站保管私钥和普通应用凭据。

```sh
docker compose -f deploy/omni/app-compose.yml -f deploy/omni/workstation.compose.yml up -d --build --wait tuber-core studio-edge
```

已有业务卷的部署再叠加 `app-business-data.compose.yml`。正式工坊同时启用可接管的网页阅读，在该文件后追加 `-f deploy/omni/reading-worker.compose.yml`，Compose 会先启动独立阅读 worker。[阅读进程部署](reading-worker/README.md)

当号主允许使用已有视频原件取材时，可以在相应工坊追加 `-f deploy/omni/private-source.compose.yml`。`TUBER_PRIVATE_SOURCE_DIR` 填工作站上含 `manifest.json` 和原始片段的目录，`TUBER_PRIVATE_SOURCE_URL` 填清单对应的原投稿分 P 完整网址。Core 的 UID 65532 须能读取该目录。只有 Core 获得只读挂载。任务工具在对应分 P 的取材授权内使用原件，来源回执注明归档。

在浏览器地址栏输入工作站 IP，HTTP 自动转向 HTTPS。TLS 代理服务工作台、静态文件、SSE 和私有通话 WebSocket。工作站覆盖文件将电话签票代理到同一网关，复用 Core 的普通应用凭据；浏览器在当前 HTTPS 入口持短时票据连接。WHEP 音视频遵循协商得到的 ICE 路径。验证实际收帧、冻结和音画同步时使用号主浏览器所在网络。

Core 提供工作台、静态素材、管理 API、SSE 和 WebSocket。浏览器连接实际业务主机，任务和 SQLite 由该 Core 实例持有。使用独立候选卷时，管理令牌、聊天和平台登录按该卷读取。

访问回环绑定的远程 Core 时，在终端将下方的 `core-host` 换成实际业务主机的 SSH 目标，然后打开工作台。 [转发后的工作台](http://127.0.0.1:8021/)

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8021:127.0.0.1:9020 core-host
```

如果 Core 使用了自定义 `TUBER_CORE_PORT`，用实际端口替换上面最后一个 `9020`。SSH 转发经过 Docker 网桥时，工作台会要求登录。在业务主机使用启动该 Core 的同一 Docker 上下文与 Compose 项目读取当前管理令牌。

```sh
docker compose -f deploy/omni/app-compose.yml exec -T tuber-core cat /data/admin-token
```

正式迁入卷的对应路径是 `/data/business/admin-token`。在与正式 Core 同一个 Compose 项目下读取。

在转发后的工作台输入令牌。浏览器验收可从对话进入活动，打开自主运营、直播工作台和个性化。登录和后续 API 请求走同一个转发地址。

需要受信任的 HTTPS 入口时，同网络的反向代理可转发到 `http://tuber-core:9020`，保留原始 Host 并支持 SSE 和 WebSocket。`TUBER_CORE_TRUSTED_PROXIES` 填该代理的地址或子网。直接绑定宿主机的访问范围由 `TUBER_CORE_BIND` 控制。

## GIF Avatar 与 OBS 媒体

使用节目桥时，向服务方取得会话地址、节目观看地址和各自的访问凭据。桥的 `GET /healthz` 报告节目发布状态，受认证的 WebSocket `/session` 协商原生双工，OBS 与浏览器从媒体入口获取 WHEP。确认观看端可访问服务发布的 ICE 候选。私有电话使用独立网关的原生双工语音票据。节目状态、音画解码与播完回执分别验收。[电话与直播选路](../../docs/guides/private-avatar-call.md) · [直播交接播完实测](../../evaluations/results/live-handoff-ended-20261119.md)

本仓库另带可选 GIF 渲染和媒体桥。运行示例需已可访问的网关及媒体发布入口。将网关服务令牌、桥接访问令牌和发布密码分别置于 `secrets/gateway-media.token`、`secrets/media-access.token` 和 `secrets/media-publish.password`。示例发布地址为 `rtsp://mediamtx:8554/program`，发布用户为 `tuber-publisher`。网关授予媒体令牌的节目准入权限使节目连接期间进入 Live Mode，关闭后回到网关的手动模式。

```sh
docker compose -f deploy/omni/app-compose.yml --profile media up -d --build --wait
```

示例媒体桥地址为 `omni-media:8080`，同网络也可通过 `tuber-avatar` 访问。示例宿主机健康检查地址为 `http://127.0.0.1:9080/healthz`。渲染读取仓库已有的鲸鱼娘 GIF。OBS 和浏览器独立认证，示例播放路径为 `/program/whep`，观看用户为 `tuber-viewer`。

需要在对话和 Agent 工具里查看原生节目时，设置 `TUBER_NATIVE_PROGRAM_STATUS_URL` 为 Core 可达的完整 HTTP(S) 状态地址。本示例启用媒体桥时可填 `http://omni-media:8080/healthz`。其他部署使用其媒体服务提供的只读端点；留空仅显示工坊直播状态。旧布尔开关不再启用查询。Core 匿名 GET 该地址，读取 `process: ready` 及 `native_program.state`，未取到兼容状态时显示暂不可查。推理网关的健康状态和平台开播回执各自独立。

## 按需 Remotion

配置 `TUBER_REMOTION_IMAGE` 为已导入的合规 worker 镜像、`TUBER_REMOTION_INPUT_DIR` 为任务输入目录、`secrets/gateway-remotion.token` 为独立服务令牌。worker 从只读的 `/input` 取任务数据，在独立的 `/output` 卷写入产物。

```sh
docker compose -f deploy/omni/app-compose.yml --profile remotion up -d remotion-worker
```

每次启动前，准入任务通过 `GET /api/mode` 确认 `batch`。外部 worker 需在领取每个渲染任务前检查同一模式，按其任务协议处理切入 `live` 后的在途任务。仓库当前提供 Compose 接入和启动准入，Remotion 渲染程序及运行期暂停行为由外部 worker 提供。
