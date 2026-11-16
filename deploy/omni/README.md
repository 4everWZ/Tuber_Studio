# Tuber Studio Omni 业务部署

Tuber Studio 消费已部署的 Omni 推理和 Avatar 服务。业务启动需要服务地址、模型别名及对应凭据。可选节目另需 Avatar 会话和观看入口。引擎、渲染器和媒体服务由各自部署方维护。 [独立推理部署仓库](https://github.com/4everWZ/Tuber_Studio_Omni_Deploy)

本仓库的示例 Compose 加入名为 `tuber-omni` 的外部 Docker 网络，可用 `TUBER_OMNI_NETWORK` 指定已创建的网络。示例使用 `omni-gateway:8000` 和默认别名 `omni-default`。Core 的 9020 默认发布到宿主机回环地址，同网络代理可访问 `http://tuber-core:9020`。接入其他服务地址时，在工作台分别配置受支持的 Provider 和对应凭据。示例注入的 Core 网关令牌仅用于预设网关地址。 [配置入口](../../docs/guides/deployment.md#provider与上下文设置)

## Core

在仓库根目录执行。先从基础设施管理员取得分配给 Core 的网关服务令牌，并将它存入 `deploy/omni/secrets/gateway-core.token`。`secrets/` 用宿主目录权限限制访问，容器内 UID/GID 65532 需要可读各自的令牌文件。可用 `OMNI_CORE_TOKEN_FILE` 指向授权的其他文件。

```sh
docker compose -f deploy/omni/app-compose.yml config --quiet
docker compose -f deploy/omni/app-compose.yml up -d --build --wait tuber-core
curl --fail http://127.0.0.1:9020/healthz
```

Core 通过受认证的 Chat Completions 接入网关。先核对网关 `GET /healthz` 和 `GET /v1/models` 的服务、别名、模型与实报 `max_model_len`，再用 `POST /v1/chat/prompt_tokens` 和实际生成检查计数、普通回复、SSE 及结构化结果。窗口包含已渲染输入和生成输出，Chats 思考默认开启并可调整。示例配置使用 JSON Schema 的紧凑结构约束，继续由 Core 校验完整输出。视频取帧与评论附件使用经验证的视觉能力，`OMNI_VISION_CHECKPOINT` 可为示例网关指定应核对的版本。朗读合成使用独立的语音 Provider。Core 的 `/healthz` 检查应用与数据库启动。 [能力与验收](../../docs/specs/foundation/omni-deployment.md#接入验收与切换)

`core-data` 是 Core 独占的持久卷，保存、备份和迁移步骤沿用 [部署指南](../../docs/guides/deployment.md)。复用已有业务数据时须保有 `studio.json` 及权威写入者记录，`TUBER_CORE_HOSTNAME` 需和迁移时登记的目标一致。初次建卷可用 `TUBER_CORE_MODE=demo` 运行离线演示，或用 `TUBER_CORE_MODE=existing` 要求已有配置。切换已存在的配置通过工作台及部署准入完成。

接收现有正式业务时，将已核验快照还原到独立 Docker 卷的 `/data/business`。同用 `-f deploy/omni/app-business-data.compose.yml` 接入 `TUBER_CORE_BUSINESS_VOLUME` 指定的卷。恢复和正式 Core 使用相同容器内路径，数据库的取材引用在恢复时重新绑定。正式数据按迁移工具的 `staged → active` 交接。在 staged 卷运行 `python /opt/tuber/core_entrypoint.py --stage-omni` 可准备模型配置并退出旧推理模式档。保留平台账号和现有 TTS 地址，原通过推理节点配对提供的 TTS 凭据绑定到同一地址。任务及平台连接由正式 Core 激活后管理。

## 从电脑访问业务工作台

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

使用已部署的 Avatar 服务时，向服务方取得会话地址、节目观看地址和各自的访问凭据。先核对节目健康状态，再建立新的授权会话。现行节目桥通过 `GET /healthz` 读取节目发布状态，通过受认证的 WebSocket `/session` 协商原生双工，OBS 与浏览器从媒体入口获取 WHEP。确认观看端可访问服务发布的 ICE 候选。通用 Avatar 会话能力由服务实际提供后接入。节目发布、真实音画解码与播放器播完回执分别验收。 [Avatar 与音画交付](../../docs/specs/foundation/omni-deployment.md#avatar-与音画交付)

本仓库另带可选 GIF 渲染和媒体桥。运行示例需已可访问的网关及媒体发布入口。将网关服务令牌、桥接访问令牌和发布密码分别置于 `secrets/gateway-media.token`、`secrets/media-access.token` 和 `secrets/media-publish.password`。示例发布地址为 `rtsp://mediamtx:8554/program`，发布用户为 `tuber-publisher`。网关授予媒体令牌的节目准入权限使节目连接期间进入 Live Mode，关闭后回到网关的手动模式。

```sh
docker compose -f deploy/omni/app-compose.yml --profile media up -d --build --wait
```

示例媒体桥地址为 `omni-media:8080`，同网络也可通过 `tuber-avatar` 访问。示例宿主机健康检查地址为 `http://127.0.0.1:9080/healthz`。渲染读取仓库已有的鲸鱼娘 GIF。OBS 和浏览器独立认证，示例播放路径为 `/program/whep`，观看用户为 `tuber-viewer`。

现行 Core 通过 Compose 网络的 `http://omni-media:8080/healthz` 查看节目状态，并在 Chats 和 Agent 工具里另列平台直播状态。替换媒体服务时，需为该状态接口提供相同网络地址及兼容响应，或用 `TUBER_NATIVE_PROGRAM_STATUS=0` 关闭查询。媒体状态未取到时显示暂不可查。

## 按需 Remotion

配置 `TUBER_REMOTION_IMAGE` 为已导入的合规 worker 镜像、`TUBER_REMOTION_INPUT_DIR` 为任务输入目录、`secrets/gateway-remotion.token` 为独立服务令牌。worker 从只读的 `/input` 取任务数据，在独立的 `/output` 卷写入产物。

```sh
docker compose -f deploy/omni/app-compose.yml --profile remotion up -d remotion-worker
```

每次启动前，准入任务通过 `GET /api/mode` 确认 `batch`。外部 worker 需在领取每个渲染任务前检查同一模式，按其任务协议处理切入 `live` 后的在途任务。仓库当前提供 Compose 接入和启动准入，Remotion 渲染程序及运行期暂停行为由外部 worker 提供。
