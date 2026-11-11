# 大肥鱼 Qwen3.5-9B：AGX 实机部署，2026-11-11

选定的 `checkpoint-38` 已合并到固定的 `Qwen/Qwen3.5-9B` 基座，导出 Q4_K_M 主权重和同基座 Q8_0 视觉投影器。两件 GGUF 从 [Hugging Face 固定版本](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/tree/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/releases/1.0.0) 下载到 AGX，并校验完整 SHA-256。旧 Hugging Face 文件和可见性保持。Workstation 保留版本化 adapter、最终两件 GGUF 和清单，回收本轮合并及 F16 导出中间件 34.24 GiB。文件校验值与精确运行标识见同名 JSON。

AGX 使用 [n8500x Jetson CUDA 镜像](https://hub.docker.com/r/n8500x/llamacpp-jetson) 的 `b10884` 固定 ARM64 manifest。保留校验过的基础层、依赖层和单份运行层，省掉会重复 `/app` 的权限修改层，导入为本项目单独的 Docker 镜像。实际引擎 commit `434ddbb`，加载 33/33 GPU 层，CUDA 架构 `sm_87`，Flash Attention 开启。单槽实报 32,768 token；CUDA0 的 8 层注意力 KV 为 288 MiB，K 与 V 都是 `q4_0`，各 144 MiB。容器重启策略为 `unless-stopped`。运行窗口的 162 次设备采样中，RAM 总量 61.37 GiB，加载前可用 29.69 GiB，最低可用 22.18 GiB，未触发 OOM。模型文件只读挂载；这次部署的服务使用 llama.cpp。

项目 `LlamaCppProvider` 真实结构化调用保留了 8,192 的运营输出预算：53 个输入 token、8 个输出 token，3.25 秒，`stop` 完成，返回系统校验词“蓝鲸”，没有控制标记泄露。运行模板与 GGUF 模板 SHA 相同，声明支持 system 角色，EOS 是 `<|im_end|>`。`VisionService` 读取 BV1dQnd6qEjj / CID 43091691821 的真实 40 秒画面，2.55 秒返回“底部文字是黄色，左侧有导航栏。”，与像素一致。该次检验覆盖真实接口、图像输入、32K 分配和 Q4 注意力缓存；没有用单帧结果代替整段视频理解或自动发布的验收。

电脑运营档已保存该端点，并通过项目现有联合三帧取材与来源绑定。资源探测使用三张边界图片，实际耗时 44.53 秒，9 次采样最低可用内存 22,958 MiB，换页零增长。独立视觉准入绑定同一模型、投影器和实际 `/props`。
