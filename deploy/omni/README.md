# Tuber Studio Omni 业务部署

应用和基础设施分别启动。应用加入基础设施创建的外部 Docker 网络 `tuber-omni`，以 `omni-gateway:8000` 访问推理，以 `mediamtx` 访问媒体服务器。网关上的 `omni-default` 指向现场核验的模型。默认容器端口 9020 只发布到宿主机回环地址，同网络代理可访问 `http://tuber-core:9020`。

## Core

在仓库根目录执行。先从基础设施管理员取得分配给 Core 的网关服务令牌，并将它存入 `deploy/omni/secrets/gateway-core.token`。`secrets/` 用宿主目录权限限制访问，容器内 UID/GID 65532 需要可读各自的令牌文件。可用 `OMNI_CORE_TOKEN_FILE` 指向授权的其他文件。

```sh
docker compose -f deploy/omni/app-compose.yml config --quiet
docker compose -f deploy/omni/app-compose.yml up -d --build --wait tuber-core
curl --fail http://127.0.0.1:9020/healthz
```

初次创建数据卷时，Core 用 OpenAI 兼容 Chat Completions 接入 `http://omni-gateway:8000/v1`，上下文采用首测配置的 12,288 tokens，可按服务实报修改。该端点默认用 JSON Schema 约束 Agent 的结构化回复，其他已验证服务可在 Provider 配置中设置 `llm.structured_output`。视频取帧与评论附件也使用同一网关，取材时核对实报模型、checkpoint 和窗口。`OMNI_VISION_CHECKPOINT` 用于初次建卷时锁定视觉权重版本。直播摄像头继续按单独的实机准入执行。基础语音沿用预录回退，合成语音沿用独立 Genie 或已验收 TTS 配置。Core 健康检查覆盖应用和数据库启动。推理能力由网关及实际请求核对。

`core-data` 是 Core 独占的持久卷，保存、备份和迁移步骤沿用 [部署指南](../../docs/guides/deployment.md)。复用已有业务数据时须保有 `studio.json` 及权威写入者记录，`TUBER_CORE_HOSTNAME` 需和迁移时登记的目标一致。初次建卷可用 `TUBER_CORE_MODE=demo` 运行离线演示，或用 `TUBER_CORE_MODE=existing` 要求已有配置。切换已存在的配置通过工作台及部署准入完成。

## 从电脑访问 AGX 工作台

AGX 上的 Core 直接提供完整工作台、静态素材、管理 API、SSE 和 WebSocket。电脑只用浏览器访问，任务和 SQLite 由 AGX 上的这个 Core 实例持有。候选卷的管理令牌、聊天与平台登录各自从该卷读取。

默认保留 AGX 端口在回环地址。在电脑上运行以下命令，将 `agx` 换成实际 SSH 目标，然后打开 [工作台](http://127.0.0.1:8021/)。

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8021:127.0.0.1:9020 agx
```

如果 Core 使用了自定义 `TUBER_CORE_PORT`，用实际端口替换上面最后一个 `9020`。SSH 转发经过 Docker 网桥时，工作台会要求登录。在 AGX 上用启动该 Core 的同一 Docker 上下文和 Compose 项目读取当前管理令牌。

```sh
docker compose -f deploy/omni/app-compose.yml exec -T tuber-core cat /data/admin-token
```

在转发后的工作台输入令牌。浏览器验收可从对话进入活动，打开自主运营、直播工作台和个性化。登录和后续 API 请求走同一个转发地址。

需要受信任的 HTTPS 入口时，同网络的反向代理可转发到 `http://tuber-core:9020`，保留原始 Host 并支持 SSE 和 WebSocket。`TUBER_CORE_TRUSTED_PROXIES` 填该代理的地址或子网。直接绑定宿主机的访问范围由 `TUBER_CORE_BIND` 控制。

## GIF Avatar 与 OBS 媒体

启用前分别配置 `secrets/gateway-media.token`、`secrets/media-access.token` 和 `secrets/media-publish.password`。网关把媒体桥的独立令牌登记在 `OMNI_GATEWAY_TOKENS` 的 `media` 字段，节目双工连接建立时进入 Live Mode，断开后恢复网关的手动模式。另两个令牌分别用于访问媒体桥与 MediaMTX 发布。发布用户为 `tuber-publisher`，内部地址为 `rtsp://mediamtx:8554/program`。

```sh
docker compose -f deploy/omni/app-compose.yml --profile media up -d --build --wait
```

媒体桥通过 `omni-media:8080` 提供服务，同网络也可按 `tuber-avatar` 寻址。宿主机可经 `127.0.0.1:9080` 探测健康状态。渲染读取仓库已有的鲸鱼娘 GIF。神经驱动作为待独立验收的可选渲染器。OBS 和浏览器从现场媒体入口获取 `/program/whep`，用独立的节目观看凭据 `tuber-viewer`。媒体入口须对终端发布可达的 ICE 候选。端口和编码边界见 [Omni 部署契约](../../docs/specs/foundation/omni-deployment.md)。

## 按需 Remotion

配置 `TUBER_REMOTION_IMAGE` 为已导入的合规 worker 镜像、`TUBER_REMOTION_INPUT_DIR` 为任务输入目录、`secrets/gateway-remotion.token` 为独立服务令牌。worker 从只读的 `/input` 取任务数据，在独立的 `/output` 卷写入产物。

```sh
docker compose -f deploy/omni/app-compose.yml --profile remotion up -d remotion-worker
```

每次启动前，准入任务通过 `GET /api/mode` 确认 `batch`。外部 worker 需在领取每个渲染任务前检查同一模式，按其任务协议处理切入 `live` 后的在途任务。仓库当前提供 Compose 接入和启动准入，Remotion 渲染程序及运行期暂停行为由外部 worker 提供。
