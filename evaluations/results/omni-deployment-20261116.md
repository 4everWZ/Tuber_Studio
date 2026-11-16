# Omni 双栈候选验收 · 2026-11-16

部署候选为 vLLM-Omni 0.32.0 和 MiniCPM-o 4.5，checkpoint `fb09628e98b1f55d8d5b863e0eccfcdd11bff499`。两套 Compose 共用 `omni-default`，业务 Core 持有独立数据卷。候选卷未导入现有业务库或平台授权。

| 检查 | 结果 |
| --- | --- |
| 旧推理节点 | 已停止 AGX 上大肥鱼和 Spatial Home Edge 的两个 llama.cpp 容器，两者均实报 `exited`，无 `llama-server` 进程。 |
| AGX 资源 | JetPack 6／L4T R36.4.7，ARM64／SM 8.7，设备实报 62,840 MiB RAM。旧实例停止后一次抽样可用 49,602 MiB。根分区剩余约 1.9 GiB，候选镜像和完整模型快照放在 NVMe 所在文件系统。 |
| 主机 CUDA 12.9 | 原驱动 API 为 12060。通过指定进程的库路径加载官方 Tegra 兼容包，`cuInit=0`，驱动 API 报 12090。用 PTX 8.8 在 SM 8.7 上执行计算，写入并读回 42。系统驱动未更换。 [NVIDIA 兼容说明](https://docs.nvidia.com/cuda/archive/12.9.2/cuda-for-tegra-appnote/index.html#use-the-right-upgrade-package) |
| ARM64 推理前置 | 固定 vLLM CUDA 12.9 容器同样可初始化 GPU。PyTorch 2.13.0+cu129 报四个编译目标 `sm_80`、`sm_90`、`sm_100`、`sm_120`。运行 CUDA 张量初始化报 `no kernel image is available for execution on the device`，强制 PTX JIT 后同样报错。抽检 vLLM 主扩展含 SM 8.7，FlashAttention 2 只含 SM 8.0。已开始同版本 PyTorch SM 8.7 源码构建。 [PyTorch 构建矩阵](https://github.com/pytorch/pytorch/blob/cf30153c4c131c8164ee7798e5022d810682e2cb/.ci/manywheel/build_env_setup.py) |
| Tegra 运算库 | 同一 CUDA 12.9 容器可分配显存及拷贝数据。通用 cuBLAS 创建句柄返回 3，通用 cuDNN 卷积返回 5003。挂载 NVIDIA Tegra cuBLAS 12.9.2.10 和 cuDNN 9.20.0.48 后，BF16 矩阵乘法正确读回 16，卷积正确读回 3，且 PyTorch 和 vLLM 主扩展可导入。 [Tegra 软件包索引](https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/arm64/Packages) |
| Triton JIT | 从两套 Compose 的实机候选环境启动临时进程，Triton 3.7.1 在 SM 8.7 上编译并执行向量加法，128 个结果全部正确。模型注意力内核待随后验证。 |
| 重编 PyTorch 容器预检 | AGX 导出 PyTorch 2.13.0+cu129 的 SM 8.7 wheel，候选容器实测通过 CUDA 张量、BF16 矩阵乘法、卷积和缩放点积注意力。换用官方 TorchCodec 0.17.0 CPU 解码 wheel 后，vLLM-Omni 和 vLLM 主扩展可导入。 [TorchCodec 官方 wheel 索引](https://download.pytorch.org/whl/cpu/torchcodec/) |
| AGX 两栈传输 | 四项进程网关、MediaMTX、Core、Avatar 媒体桥启动健康。隔离模拟上游验证 Realtime 帧经网关转发 PCM，节目连接进入 Live Mode，此时 Core 正常完成后台 Agent 任务。实际 MediaMTX 节目输出经 ffprobe 识别 H.264 640×360 和 Opus 48 kHz，WebSocket 正常关闭。测试结束移除模拟上游。 |
| AGX 到 Windows OBS | Windows OBS Studio 33.0.1 的临时 Browser Source 通过 SSH 端口转发接入 AGX 的 WHEP。信令返回 HTTP 201，ICE／DTLS 连接并选中 TCP 候选，CEF 实际解码 6 个 H.264 640×360 视频帧并收到 12 个 Opus RTP 包。使用同一套 AGX 节目链路及隔离模拟上游，未开播或录制。 |
| Omni ARM64 镜像与快照 | 用固定 CUDA 12.9 基础镜像构建 Omni 0.32.0 成功，Python 构建检查可导入 CosyVoice2、ONNX Runtime、s3tokenizer 和 torchaudio。固定权重快照落盘并通过清单校验。 |
| MiniCPM-o 真实推理 | Thinker 加载实报 16.83 GiB，固定 4 GiB KV 后三个阶段加载完成。容器启动到健康约六分钟。经网关返回指定 checkpoint，Chat Completions 返回正文及用量，探针耗时 2.94 秒。Realtime 实报 1,000 ms 模型单元，输入 16 kHz PCM 后首段 24 kHz PCM 到达媒体桥的观测耗时为 13.70 秒，包含送入样本的时间。首段 9,600 字节，发出显式打断到引擎确认耗时 13 ms。 |
| 真实节目与后台首轮 | OBS 经 WHEP 解码 H.264 和 Opus。后台结构化 Chat Completions 省略输出模态，被上游送入 Talker。一条缺少语音条件的请求使 Talker 退出，节目流随之结束。首轮 62 个系统内存样本的最低可用值为 9.94 GiB。网关现对省略或设置为 null 的输出模态发送 `["text"]`，保留显式设定。 |
| 自动回归 | 共享网关 7 项测试通过。业务 215 项测试通过，覆盖 Agent、Chats、Provider、视频视觉、直播媒体和游戏。两套 Compose 连同业务可选 profile 通过配置校验。 |

模型切换还需验证真实图像输入、播放器停止时延，以及 Avatar、OBS 和后台任务的持续混合负载。 [切换准入](../../docs/specs/foundation/omni-deployment.md#实机准入与切换)
