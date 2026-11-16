# AGX Omni 部署设计与准入

目标是在 Jetson AGX Orin 64 GB 上集中提供双工音视频交互、多模态推理和可插拔 Avatar。常驻 Agent、其数据库和外部动作权由 [Agent 底座](agent-runtime.md)与[跨主机规范](distributed-runtime.md)定义。推理、渲染和媒体服务在独立仓库发布。当前准入配置使用 MiniCPM-o 4.5 官方 AWQ。旧 llama.cpp 9B／视觉配置已停止并归档。Genie 是独立语音服务。 [AGX 验收记录](../../../evaluations/results/omni-deployment-20261116.md)

## 可行性判断

集中推理、业务侧执行工具、Avatar 独立渲染、浏览器和 OBS 通过 WebRTC 消费。AGX 的候选配置使用 MiniCPM-o 4.5 官方 AWQ 的同服双接口。Realtime-Venus 原生实现与 Higgs 的体验用于技术对照。正式切换以双工、中文任务、运营质量和 Avatar 共驻实测为准。

| 核查项 | 公开证据及部署影响 |
| --- | --- |
| vLLM-Omni 双接口 | `/v1/realtime?duplex=1` 是双工运行时的 WebSocket 入口，模型须注册 `duplex_plugin` 且以 `session_mode: duplex` 启动。`/v1/chat/completions` 同服可用取决于插件显式声明。WebSocket 握手后读取 `session.created` 事件的 `session.capabilities`，验收原生双工以及语音、视觉帧、中断、续接、输出格式、工具调用和播放确认。 [双工入口](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/full_duplex_api/) · [协议参考](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/realtime_duplex_api/) |
| Realtime-Venus | 上游发布 9B Omni 和 Audio，两者走模型自带的 Transformers 代码。Omni 四片权重约 18.74 GB（17.46 GiB），装载还需视觉、音频、解码工作区和会话状态。核对的 vLLM-Omni 注册表及发布文档没有 `RealtimeVenusOmni` 双工插件或该模型的兼容量化档。要走此组合，先跑通官方原生双工，再实现并测试插件或用独立适配器暴露能力。 [权重清单](https://huggingface.co/inclusionAI/Realtime-Venus/tree/a1d95d3ac61416c8db34736b152241396850a6ed/Realtime-Venus-Omni) · [官方实现](https://github.com/inclusionAI/Realtime-Venus) · [vLLM-Omni 核查版本](https://raw.githubusercontent.com/vllm-project/vllm-omni/0f55c39eb07ccc349d57ae436f99f7a612a2dd16/vllm_omni/model_executor/models/registry.py) |
| MiniCPM-o 4.5 | Chat Completions 和 Realtime 同服，支持文字、图像、音频／视频输入和文字／语音输出。公开双工配方采用约 1 秒的模型单元。实时会话的工具调用按能力协商，必要时由已确认转写交给通用 Agent。 [MiniCPM-o 配方](https://github.com/vllm-project/vllm-omni/blob/main/recipes/OpenBMB/MiniCPM-o-4_5.md) · [模型能力与工具生命周期](https://docs.vllm.ai/projects/vllm-omni/en/latest/design/fullduplex/) |
| Orin 与精度 | AGX Orin 是 ARM64、SM 8.7 Ampere，CPU、GPU、渲染和应用共享 64 GB。实机 R36.4.7 使用 Tegra CUDA 12.9 兼容路径，Thinker 已观察到 AWQ 的 Marlin 内核。Thinker 和 Talker 的逐 token、逐 head 4-bit KV 走 Triton。保留 JetPack 7.2／R39 的独立 SBSA 部署路径。 [NVIDIA Orin](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/) · [固定版 Triton 注意力](https://github.com/vllm-project/vllm/blob/5df63a7203cdb9328d824a27b682e77c7b9928be/vllm/v1/attention/backends/triton_attn.py) · [AGX 实测](../../../evaluations/results/omni-deployment-20261116.md) |
| Higgs 体验参考 | Boson 的 160 ms 是 Higgs 双工模型的处理切片，其面部由模型输出的视频 latent 驱动。项目目标是边听边说、打断、看屏幕、脸随声音动，以及后台继续工作。音频驱动的外部 Avatar 复现这类交互，端到端延迟由实际浏览器和模型测量，160 ms 用来对照而非预填部署参数。 [Boson Higgs Realtime Duplex](https://www.boson.ai/blog/higgs-realtime-duplex) |

AGX 的准入配置锁定 OpenBMB MiniCPM-o 4.5 官方 AWQ。Thinker 的 Chat 窗口从 checkpoint 读取，官方值为 40,960 token。Core 采用认证网关模型卡实报值。Thinker 与 Talker 使用 Triton 的逐 token、逐 head 4-bit KV，双工窗口滚动时按实际缓存打包和尺度重算键的位置编码。权重的 AWQ 作用域按官方张量分布限制在 Thinker 的主语言模型。Talker 与 Code2Wav 使用官方资产，实时会话沿用模型插件自身的滑窗。 [三阶段部署配置](https://github.com/vllm-project/vllm-omni/blob/5917730a4b02aedb44d374173cdc1b10484ccd52/vllm_omni/deploy/minicpmo_4_5.yaml) · [官方 AWQ 权重配置](https://huggingface.co/openbmb/MiniCPM-o-4_5-awq/blob/927faefa0bce96e557945e106f3b3d8b4d39f4b2/config.json)

## 独立仓库与运行拓扑

使用两套独立 Compose，通过显式的共享网络连接。独立部署仓库维护 ARM64 镜像和版本锁、模型及渲染资产清单、硬件探测、运维及性能验收。Tuber Studio 维护常驻 Agent、Chats、SQLite、平台适配、任务审计和工作台。可先使用现有 Provider 独立验证 Agent。

业务 Compose 的 Core、媒体桥和可选 Remotion 准入已实现。媒体桥使用仓库的 GIF 形象和 FFmpeg，通过受限凭据发布到 MediaMTX。节目 WebSocket 连接期间网关采用 Live Mode，断开后恢复手动模式。浏览器麦克风、正式 OBS 节目入口和可信 HTTPS 继续按下述拓扑验收。 [业务部署](../../../deploy/omni/README.md)

| Compose | 服务与职责 |
| --- | --- |
| `infra-compose.yml` | 由独立部署仓库维护，常规服务随 Docker 启动。运行 vLLM-Omni、共享推理网关和 MediaMTX。推理网关负责服务级鉴权、排队、全局并发和限流。模型端口只绑定基础设施私网。正式 HTTPS 入口由部署环境提供。 |
| `app-compose.yml` | 独立发布与重启，运行 Tuber Studio Core、Avatar Engine、按需 Remotion Worker 及后续应用。只经过网关调用推理。应用版本和数据卷各自持有，Tuber Studio Core 是 Agent、SQLite 和平台权限的唯一业务写入者。 |

| 边界 | 目标接法 |
| --- | --- |
| AGX 推理服务 | 内部 `8000/TCP` 是应用网关稳定入口。应用对 Chat Completions 和 Realtime 都使用 `omni-default`，网关映射到实际模型。健康状态及日志报告别名指向的模型与 checkpoint 版本、引擎版本、部署模式、可用协议和能力。上游模型端口只供网关访问。应用按实际能力发现端点及降级状态，服务消费者用独立凭据和资源配额。 |
| AGX 业务容器 | Tuber Studio Core 支持在 AGX 常在线运行。任务、身份、记忆和平台授权只有一个写入者。Remotion Worker 通过任务或文件接口读写获准输入及产物，旁路工具共享推理并各自持有状态。SQLite、平台凭据和待核对回执只由业务容器持有。 |
| 媒体网关 | Caddy 对浏览器和 OBS 提供有可信证书的 HTTPS `443/TCP`，可用 `80/TCP` 跳转。MediaMTX `8889/TCP` 用于内部 WHIP／WHEP 信令，`8189/UDP` 或现场配置提供可达的 ICE 媒体候选，`8554/TCP` 仅用在选定的内部 RTSP 路径。区分终端授权、媒体会话及单场直播的播放权限。 |
| 本地终端 | Laptop 或手机访问同一工作台。浏览器授权后采集麦克风、摄像头或屏幕，上传媒体以及操作所需的 HTTPS／SSE 数据。OBS Browser Source 从受限节目页播放 WebRTC 和字幕，在本地合成游戏与播出。Laptop 承担采集、解码、OBS 合成／推流和可选游戏执行，推理与神经渲染在 AGX。 |

### 首版协议与 Provider 边界

- 实时对话使用 MiniCPM-o 4.5 上游的 `/v1/realtime?duplex=1`。现有桥接入口接受受令牌保护的 Realtime WebSocket，转发 PCM、字幕和打断，并将模型音频送给 GIF Avatar。后续浏览器 WebRTC 采集按协商的 `chunk_period_ms` 切片，公开配方为 1,000 ms。实际播放由播放器确认。双工语音没有原生工具调用时，已确认的用户意图可转交同一 Agent，工具结果回入位置按任务和会话核对。 [双工协议](https://docs.vllm.ai/projects/vllm-omni/en/v0.32.0/serving/realtime_duplex_api/)
- 通用推理首版使用 `/v1/chat/completions`。Provider 将受支持的正文、音频、工具请求与完成状态映射到业务接口，在具备能力时提交工具结果。将来有模型在锁定的目标引擎版本及部署模式中原生支持 `/v1/responses` 时，新增 Provider 适配并验证流式、工具与结果返回、取消和错误处理。应用继续通过 Agent／Provider 业务接口调用，双工沿用 Realtime。 [同服双接口](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/full_duplex_api/) · [Responses API 迁移](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- 节目媒体桥推送同一时钟下的编码音视频，浏览器和 OBS 经 MediaMTX 使用 WHEP 拉取。首版 H.264 无 B 帧加 Opus，透明角色可先由 OBS 色键合成。Caddy 代理 HTTP、SSE 和 WebSocket，ICE 走可达媒体地址。aiortc 可用于验证桥接与时钟，硬编码按目标 JetPack 验证 NVIDIA GStreamer／V4L2。 [MediaMTX 推流](https://mediamtx.org/docs/publish/webrtc) · [WHEP](https://mediamtx.org/docs/read/webrtc) · [浏览器采集权限](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia) · [Jetson GStreamer](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html)

现有文本、视频视觉与语音角色逐项映射到实际能力。把录像采样、实时视觉和说话分开实测。现有语音试听、照稿念和指定音色也要验收，双工会话只覆盖对话语音。上游 MiniCPM-o 4.5 双工配置没有 `/v1/audio/speech`，照稿念继续走现有 Genie 路径，替代方案逐项验收后再切换。点歌、媒体播放、字幕和游戏控制继续由应用完成。 [vLLM-Omni 双工接口边界](https://docs.vllm.ai/projects/vllm-omni/en/v0.32.0/serving/realtime_duplex_api/#start-the-server)

### 内存与网关模式

首轮共驻目标为 Omni 整体 30 GiB、Avatar 6 GiB，预留约 8 GiB 整机可用内存。该机实报总 RAM 61.37 GiB。`gpu_memory_utilization` 按阶段配置。Thinker 预留完整 Chat 窗口与原生双工滑窗所需的 4-bit KV，分批预填控制单次计算工作区。直接以官方 AWQ、完整窗口和 4-bit KV 验收，记录 Thinker、Talker、解码器、KV、工作区和整机最低可用内存。 [AGX 历史实测](../../../evaluations/results/agx-clean-9b-deployment-20261111.md) · [Omni 阶段内存](https://docs.vllm.ai/projects/vllm-omni/en/v0.32.0/configuration/gpu_memory_utilization/)

- **Live Mode** 由直播会话向网关切换并维持。开播前先暂停接收新的重批次，给在途长任务留收尾时间。预留一路直播双工和渲染／编码资源，网关合计所有应用的普通推理并发，先设为 1，混合负载实测通过才放到 2。限制上下文、视觉帧、输出与排队时间，暂停 Remotion 重任务。
- **Batch Mode** 恢复视频、运营和离线素材任务，按实测逐档提高并发。内存余量下降或队列恶化时收紧。网关控制请求准入，已经启动的计算可否让出资源由目标引擎版本实测。必要时分模型或错峰。 [同引擎并发](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/realtime_duplex_api/#start-the-server)

## Avatar 与音画交付

驱动接口消费含会话、播放代次、时间戳及采样率的 `AudioChunk`，返回按时间戳排序的 RGB／RGBA `VideoFrame`，声明尺寸、帧率、Lookahead、Alpha 与取消能力。参考图、脸部定位、抠像和渲染驱动可替换。先用现有图像／GIF 或 Live2D 校对播放链路；神经驱动从以下三条路径比较。 [当前 Avatar 接入](../../guides/live/avatar.md)

| 驱动 | 首测边界 |
| --- | --- |
| Ditto Online | 首个神经驱动基线。官方有在线模式及重叠窗口，验证 arm64 依赖、Orin 上首帧与连续音频、简化 Q 版鲸鱼娘脸部定位。 [Ditto](https://github.com/antgroup/ditto-talkinghead) · [在线模式说明](https://github.com/antgroup/ditto-talkinghead/issues/14) |
| FasterLivePortrait + JoyVASA | 复核音频运动模块的窗口及实时性。ONNX／TensorRT 在目标机按实际版本构建并报告使用的精度，上游 RTX 3090 帧率只作来源条件。 [FasterLivePortrait](https://github.com/warmshao/FasterLivePortrait) · [JoyVASA](https://github.com/jdh-algo/JoyVASA) |
| LivePortrait-AudioDriven | 以 j-LivePortrait-AudioDriven 的 flow matching 运动驱动为候选，先验证离线口型、输出分辨率及权重许可。通过因果流式、首帧和长时稳定验证后可进入在线候选。 [j-LivePortrait-AudioDriven](https://github.com/zerodaily/j-LivePortrait-AudioDriven) |

同机音频先用有界队列或 Unix Socket 传递。需要减少复制时用实测数据决定共享内存／文件描述符传递，记录内存域和复制次数。Avatar、音频和字幕按同一会话时间轴对齐。基线用 H.264 无 B 帧加 Opus。RGB／RGBA 渲染帧先合成普通背景或纯色，OBS 可色键去绿。透明 WebRTC 另验证编解码、接收端及合成，离线透明素材可用 RGBA 图像序列供 Remotion。 [浏览器编码兼容](https://mediamtx.org/docs/features/webrtc-specific-features#codec-support-in-browsers) · [OBS Browser Source](https://obsproject.com/kb/browser-source) · [OBS 色键](https://obsproject.com/kb/chroma-key-filter)

模型输出、发送和实际播放分别计量。用户打断时先清客户端旧音频、字幕和渲染，递增播放代次并确认模型实际支持的截断语义。已播内容按播放器回执进入对话或节目历史，截断后只记录可核实的前缀。录音、画面和私有对话通话与节目媒体使用隔离的会话和凭证。渲染失败时继续交付可用的语音、字幕和状态，复用现有图像场景。晚到的工具结果按任务 ID 回到正确会话，仍可从活动中取得。

## 实机准入与切换

| 阶段 | 通过条件 |
| --- | --- |
| 目标机预检 | 盘点设备 JetPack／L4T、CUDA／TensorRT、GPU 架构、可用内存／NVMe、电源和现有服务占用。验证固定 ARM64 镜像、实际内核以及硬编码。若需 JetPack 迁移，使用可恢复的独立启动环境或恢复镜像，先验证业务数据与旧运行方式可回退。 |
| 推理能力与质量 | 在目标机分别验文字／图像／音频、视频帧的实际输入与文本／音频输出，测工具参数与结果回流、长窗口、双工重叠说话、打断、静默与断线。用相同独立中文样例对比现行 9B 的事实、指代、视频及运营回复、角色表达和工具闭环。主观听辨声音、口型与角色形象。记录实际模型、精度、后端、原生切片和窗口。 |
| 整机混合与媒体 | 一路直播双工加 Avatar 和实际 OBS，同时从多个应用提交通用请求，跟踪网关模式、准入和离线任务暂停。持续至少两小时，记录语音首包和首音、打断到停止、渲染首帧、音画差、帧率与丢帧、队列及任务完成时间，并记录峰值与最低可用内存、交换、温度和时钟。验证 Wi-Fi 和有线下浏览器、OBS、重连及第二设备接续。 |
| 切换与回退 | 在隔离工作台按现有业务入口回放且平台零写入。通过能力、质量和共驻验收后，按文本／视觉、Chat 双工与 Avatar、直播的可观察能力逐项切默认端点。保留旧镜像、权重和配置做回切演练，回切只改 Provider／媒体路由，原任务、记忆、授权和回执继续由同一业务数据库维护。 |

现有 9B 的 32 GB 共驻目标和历史首字、首音数据保留作对照。Omni 准入按目标机实际剩余容量和共同负载设定资源边界。性能报告需写明语言、会话数、窗口、码率、模型和渲染器版本，分别呈现声学与交互质量、后台进展以及是否真正用了原生双工。 [模型准入](models-and-audio.md#profile-选择与准入) · [当前实施计划](../../plans/current.md#独立-omni-部署)
