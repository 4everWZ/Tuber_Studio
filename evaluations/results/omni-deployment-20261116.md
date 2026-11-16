# Omni 双栈候选验收 · 2026-11-16

部署候选为 vLLM-Omni 0.32.0 和 MiniCPM-o 4.5，checkpoint `fb09628e98b1f55d8d5b863e0eccfcdd11bff499`。两套 Compose 共用 `omni-default`，业务 Core 持有独立数据卷。候选卷未导入现有业务库或平台授权。

| 检查 | 结果 |
| --- | --- |
| 旧推理节点 | 已停止 AGX 上大肥鱼和 Spatial Home Edge 的两个 llama.cpp 容器，两者均实报 `exited`，无 `llama-server` 进程。 |
| AGX 资源 | JetPack 6／L4T R36.4.7，ARM64／SM 8.7，设备实报 62,840 MiB RAM。旧实例停止后一次抽样可用 49,602 MiB。根分区剩余约 1.9 GiB，候选镜像和完整模型快照放在 NVMe 所在文件系统。 |
| 主机 CUDA 12.9 | 原驱动 API 为 12060。通过指定进程的库路径加载官方 Tegra 兼容包，`cuInit=0`，驱动 API 报 12090。用 PTX 8.8 在 SM 8.7 上执行计算，写入并读回 42。系统驱动未更换。 [NVIDIA 兼容说明](https://docs.nvidia.com/cuda/archive/12.9.2/cuda-for-tegra-appnote/index.html#use-the-right-upgrade-package) |
| ARM64 推理前置 | 固定 vLLM CUDA 12.9 容器同样可初始化 GPU。随镜像提供的 PyTorch 2.13.0+cu129 报四个编译目标 `sm_80`、`sm_90`、`sm_100`、`sm_120`。运行 CUDA 张量初始化报 `no kernel image is available for execution on the device`，强制 PTX JIT 后同样报错。抽检 vLLM 主扩展含 SM 8.7，FlashAttention 2 只含 SM 8.0。改在 AGX 重编同版 PyTorch，并给两个注意力阶段启用 Triton。 [PyTorch 构建矩阵](https://github.com/pytorch/pytorch/blob/cf30153c4c131c8164ee7798e5022d810682e2cb/.ci/manywheel/build_env_setup.py) |
| Tegra 运算库 | 同一 CUDA 12.9 容器可分配显存及拷贝数据。通用 cuBLAS 创建句柄返回 3，通用 cuDNN 卷积返回 5003。挂载 NVIDIA Tegra cuBLAS 12.9.2.10 和 cuDNN 9.20.0.48 后，BF16 矩阵乘法正确读回 16，卷积正确读回 3，且 PyTorch 和 vLLM 主扩展可导入。 [Tegra 软件包索引](https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/arm64/Packages) |
| Triton JIT | 从两套 Compose 的实机候选环境启动临时进程，Triton 3.7.1 在 SM 8.7 上编译并执行向量加法，128 个结果全部正确。模型注意力内核待随后验证。 |
| 重编 PyTorch 容器预检 | AGX 导出 PyTorch 2.13.0+cu129 的 SM 8.7 wheel，候选容器实测通过 CUDA 张量、BF16 矩阵乘法、卷积和缩放点积注意力。换用官方 TorchCodec 0.17.0 CPU 解码 wheel 后，vLLM-Omni 和 vLLM 主扩展可导入。 [TorchCodec 官方 wheel 索引](https://download.pytorch.org/whl/cpu/torchcodec/) |
| AGX 两栈传输 | 四项进程网关、MediaMTX、Core、Avatar 媒体桥启动健康。隔离模拟上游验证 Realtime 帧经网关转发 PCM，节目连接进入 Live Mode，此时 Core 正常完成后台 Agent 任务。实际 MediaMTX 节目输出经 ffprobe 识别 H.264 640×360 和 Opus 48 kHz，WebSocket 正常关闭。测试结束移除模拟上游。 |
| AGX 到 Windows OBS | Windows OBS Studio 33.0.1 的临时 Browser Source 通过 SSH 端口转发接入 AGX 的 WHEP。信令返回 HTTP 201，ICE／DTLS 连接并选中 TCP 候选，CEF 实际解码 6 个 H.264 640×360 视频帧并收到 12 个 Opus RTP 包。使用同一套 AGX 节目链路及隔离模拟上游，未开播或录制。 |
| Omni ARM64 镜像与快照 | 用固定 CUDA 12.9 基础镜像构建 Omni 0.32.0 成功，Python 构建检查可导入 CosyVoice2、ONNX Runtime、s3tokenizer 和 torchaudio。固定权重快照落盘并通过清单校验。 |
| MiniCPM-o 真实推理 | Thinker 加载实报 16.83 GiB，固定 4 GiB KV 后三个阶段加载完成。容器启动到健康约六分钟。经网关返回指定 checkpoint，Chat Completions 返回正文及用量，探针耗时 2.94 秒。Realtime 实报 1,000 ms 模型单元，输入 16 kHz PCM 后首段 24 kHz PCM 到达媒体桥的观测耗时为 13.70 秒，包含送入样本的时间。首段 9,600 字节，发出显式打断到引擎确认耗时 13 ms。 |
| 真实节目与后台首轮 | OBS 经 WHEP 解码 H.264 和 Opus。后台结构化 Chat Completions 省略输出模态，被上游送入 Talker。一条缺少语音条件的请求使 Talker 退出，节目流随之结束。首轮 62 个系统内存样本的最低可用值为 9.94 GiB。网关现对省略或设置为 null 的输出模态发送 `["text"]`，保留显式设定。 |
| Chat Completions 多模态输入 | 实际音频样本得到「说话」分类。Core 的视频视觉 Provider 通过网关核验实际模型、checkpoint 和窗口。输入两张图，9.83 秒内按来源帧分别识别红色圆形和蓝色圆形，并给出指向两帧的汇总结论。评论附件入口也识别了红色圆形。Omni 的语法编译不支持 `uniqueItems`，取材程序仍校验帧编号唯一。 |
| AGX 节目编码 | MiniCPM-o 返回的 24 kHz PCM 峰值 25,843。AGX 内部 RTSP 解码为 H.264 640×360 和 Opus，视频 2,486 帧覆盖约 100 秒媒体时间，音频峰值约 −2.1 dB。 |
| 远端 OBS 真实音画 | Windows OBS Browser Source 经认证 WHEP 接收真实语音，选中 UDP ICE 后音频 RMS 约 0.103。主机间走 Tailscale DERP，浏览器统计约 2.46 fps，约 71.46% 音频样本由丢包补偿生成，观测到约 2.33 秒断连。用户确认目前两地无法连接同一局域网，先验收业务功能，远端音质复测留待可用链路。 |
| Thinker 预填峰值 | 保持 12,288-token 模型窗口和 4 GiB Thinker KV，将每批预填上限收紧到 4,096。相近的短时混合探针中，最低可用 RAM 从 6.68 GiB 改善至 11.33 GiB。 |
| 后台结构化调用诊断 | 正常 Core 已发出 `response_format: json_schema`，锁定版 Omni 的 Chat Completions handler 未把约束传给 Thinker，导致模型宣告调用工具后以普通正文结束。候选镜像补转交 vLLM 核心已解析的约束。实机在提示词明确要求普通正文时仍返回 schema 指定的唯一 JSON 值。非法 schema 被解析器拒绝，本版本回 HTTP 500，随后引擎健康。 [Omni 采样参数转交](https://github.com/vllm-project/vllm-omni/blob/5917730a4b02aedb44d374173cdc1b10484ccd52/vllm_omni/entrypoints/openai/serving_chat.py#L1423) · [vLLM 结构化协议](https://github.com/vllm-project/vllm/blob/5df63a72/vllm/entrypoints/openai/chat_completion/protocol.py#L708) |
| AGX 真实共驻短测 | 原生双工节目保持连接 140.8 秒，完成一次语音回复，共 551,040 字节 PCM，首段有声 PCM 从送入样本起约 15.87 秒。同时经正常 Core 入口提交后台任务，19.13 秒内取得 `operation_status` 与 `live_status` 的实际结果并完成，任务结束时网关仍处于 Live Mode 且原生双工连接数为 1。141 个内存样本的最低可用值为 11.22 GiB。直播工具返回原业务平台状态，节目媒体独立运行。 |
| 节目状态与模拟游戏 | 接回媒体桥状态后，Chats 的状态卡实机实报「平台已停，Omni 节目正在发布」。同时，后台任务用 30.19 秒取得运营、直播以及《星露谷物语》模拟局面的三个结构化回执。节目会话保持 115.3 秒并完成一次语音回复，116 个样本的最低可用内存为 10.69 GiB。模型生成的自由文本把实际正在发布的 Omni 节目描述成「静置轮播」，因此这例通过工具执行但未通过总结的事实一致性。原始模型结论和结构化回执保留。 |
| AGX Web 工作台 | Windows Chrome 通过私有 SSH 转发及独立管理令牌打开 AGX 的已构建应用。访问 20 个页面，实测 Chats 活动面板、390 px 窄屏导航、个性化记忆摘要及搜索视图、直播四个内嵌页面。通过流式入口提交一条私聊，真实模型给出切题的 Markdown 回复，独立浏览器会话重新打开后显示同一条回复。活动页把错误的模型文字标为「助手总结」，折叠的直播步骤直接展示平台已停和 Omni 节目正在发布。展开核对了原始回执，返回和关闭正常。未测首字时间，未提交平台操作。受控浏览器把记忆 GET 延迟 16 秒后复现了旧版加载状态，修复后首个有效响应能呈现摘要。 |
| 自动回归 | 共享网关 7 项测试通过。初次业务回归 215 项通过，覆盖 Agent、Chats、Provider、视频视觉、直播媒体和游戏。接入实机视觉、Chats 流式和双状态后，253 项相关 Python 测试及记忆慢链路 Node 测试通过。两套 Compose 连同业务可选 profile 通过配置校验。 |

切换验收继续覆盖中文总结与工具回执的一致性、实拍和来源回放、播放器停止时延及 Avatar、OBS 和后台任务持续共驻。远端 OBS 音质复测等待可用链路。 [切换准入](../../docs/specs/foundation/omni-deployment.md#实机准入与切换)
