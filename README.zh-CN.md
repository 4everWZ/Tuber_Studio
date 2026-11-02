# Tuber Studio

<p align="center">
  <a href="https://github.com/4everWZ/Tuber_Studio"><img src="https://img.shields.io/badge/version-v0.1.0-blue.svg?style=flat-square" alt="Version"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.12+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="https://developer.nvidia.com/embedded/jetson-modules"><img src="https://img.shields.io/badge/NVIDIA%20Jetson-L4T%20R36.4-76B900.svg?style=flat-square&logo=nvidia&logoColor=white" alt="Jetson R36.4"></a>
  <a href="https://fastapi.tiangolo.com"><img src="https://img.shields.io/badge/FastAPI-Framework-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://github.com/4everWZ/Tuber_Studio/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Open%20Source-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>简体中文</b>
</p>

---

<p align="center">
  <img src="assets/whale/talking.gif" width="120" alt="大肥鱼 说话状态" />
  &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/whale/cheerful.gif" width="120" alt="大肥鱼 开心状态" />
  &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/whale/sing_open.png" width="120" alt="大肥鱼 歌唱状态" />
</p>
<p align="center">
  <sub>内置示例角色：大肥鱼（Q版鲸鱼娘，形象源自 YunYueSama 开源项目）</sub>
</p>

Tuber Studio 是一个面向 B站评论运营与本地虚拟舞台演出的自部署 AI 角色工作台。支持多模态视频与评论理解、原创角色设定与声音克隆、实验性自主动态发布，以及可在 OBS 中直接使用的虚拟直播播放器。支持在普通电脑或 NVIDIA Jetson Orin 边缘计算设备上运行。

---

## 核心功能

- **多模态评论与私信工作台：** 支持视频抽帧理解与字幕解析，按楼层结构化组织多轮跟帖并分析反馈。默认采用人工审核草稿模式，支持编辑、一键采纳或通过配置的账号发布，亦可设置自动浏览与回复。支持根据议题敏感度将对话引导至私信，并记录粉丝 UID 互动阶段。
- **角色配置与声音克隆：** 支持自定义角色人设、口吻、回复规则与表情包图库。内置大肥鱼预设角色，支持接入 GPT-SoVITS 或 Genie-TTS 参考音频绑定专属音色，具备版本管理能力。
- **自主动态发布（实验性）：** 角色可根据公开记忆与互动沉淀，自主决定发布内容与时机。正文生成关联事实来源追踪，区分背景设定与客观记录，支持发布频控与一键全局熔断。
- **虚拟舞台与音频驱动口型：** 提供基于透明背景的 OBS 浏览器源播放器，支持场景组件拖拽编排。通过提取独立人声音轨分析振幅与共振峰，驱动角色口型开合；内置本地曲库、点歌队列与同步歌词。
- **端侧部署与双机协同：** 适配 NVIDIA Jetson Orin (ARM64 L4T R36.4) 容器环境；支持将 Web 管理与推流放在日常电脑、将模型推理运行在局域网内其他 GPU 主机的双机协同模式。
- **上下文管理与数据存储：** 默认提供 128K 上下文支持，内置按领域的语义压缩策略，长期互动不丢失关键记忆；所有数据均保存在本地 SQLite，保障数据隐私。

---

## 系统架构

```mermaid
flowchart TB
    %% 外部交互与输入
    subgraph INGRESS["外部输入与事件源 (Ingress & Events)"]
        direction TB
        IN_BILI["B站视频与评论跟帖"]
        IN_LIVE["直播间弹幕与点歌事件"]
        IN_MEM["角色长期公开记忆"]
    end

    %% 核心智能体决策中枢
    subgraph AGENT["智能体决策与编排中枢 (Agent Orchestration Hub)"]
        direction TB
        VLM["多模态视觉与字幕解析<br/><sub>(MiniCPM-V / Gemma / 视频抽帧)</sub>"]
        PERSONA["角色人设口吻与状态机<br/><sub>(6态情绪 FSM / 动态表情匹配)</sub>"]
        ROUTER["公私信路由 & 审核熔断<br/><sub>(人工待审草稿 / 平台频控)</sub>"]
        MOMENT["自主动态决策引擎<br/><sub>(事实溯源 / 见闻沉淀)</sub>"]
    end

    %% 呈现与输出层
    subgraph OUTPUT["呈现与交互输出 (Presentation & Stage)"]
        direction TB
        OUT_POST["B站动态 / 楼中楼回复 / 私信"]
        subgraph OBS["OBS 实时虚拟舞台"]
            direction TB
            AUDIO["独立人声音轨分析<br/><sub>(振幅共振峰提取)</sub>"]
            STAGE["透明浏览器源<br/><sub>(角色动态口型 / 滚动歌词)</sub>"]
        end
    end

    %% 下层：底层基建与端侧硬件
    subgraph INFRA["底层基建、记忆引擎与端侧硬件 (Infrastructure & Edge Runtime)"]
        direction LR
        subgraph MEM["记忆与上下文"]
            direction TB
            CTX["128K 原生上下文管理"]
            COMPACT["领域语义智能压缩"]
            DB[("本地 SQLite 数据库")]
        end
        subgraph RUNTIME["本地轻量推理"]
            direction TB
            LLM["端侧小模型推理<br/><sub>llama.cpp / MiniCPM5-2B</sub>"]
            TTS["低延迟神经语音<br/><sub>Genie-TTS / GPT-SoVITS</sub>"]
        end
        subgraph HARDWARE["端侧与双机协同"]
            direction TB
            JETSON["NVIDIA Jetson Orin<br/><sub>(ARM64 L4T R36.4)</sub>"]
            DUAL["PC 业务机 + 独立推理机<br/><sub>(局域网双机协同)</sub>"]
        end
    end

    %% 数据流连接
    IN_BILI -->|视频帧 & 评论流| VLM
    IN_MEM -->|事实记忆检索| PERSONA
    IN_LIVE -->|弹幕点歌事件| TTS

    VLM -->|画面语义实况| PERSONA
    PERSONA -->|回复草稿| ROUTER
    PERSONA -->|见闻沉淀| MOMENT
    ROUTER -->|审核采纳/发布| OUT_POST
    MOMENT -->|定时推送| OUT_POST

    TTS -->|独立人声音频| AUDIO
    AUDIO -->|驱动嘴型张合| STAGE

    PERSONA <-->|会话上下文| CTX
    CTX --> COMPACT --> DB
    AGENT <-->|模型调用| LLM
    HARDWARE -.->|硬件支撑| RUNTIME
    HARDWARE -.->|本地运行| MEM

    %% 样式美化
    classDef ingress fill:#0F172A,stroke:#38BDF8,stroke-width:1.5px,color:#F8FAFC;
    classDef agent fill:#1E1B4B,stroke:#818CF8,stroke-width:1.5px,color:#EEF2FF;
    classDef output fill:#4A044E,stroke:#F472B6,stroke-width:1.5px,color:#FAF5FF;
    classDef obs fill:#1E293B,stroke:#FB7185,stroke-width:1.5px,color:#FFF1F2;
    classDef infra fill:#022C22,stroke:#34D399,stroke-width:1.5px,color:#ECFDF5;
    classDef mem fill:#064E3B,stroke:#10B981,stroke-width:1px,color:#D1FAE5;
    classDef runtime fill:#1E293B,stroke:#F59E0B,stroke-width:1px,color:#FEF3C7;
    classDef hardware fill:#0F172A,stroke:#64748B,stroke-width:1px,color:#CBD5E1;

    class IN_BILI,IN_LIVE,IN_MEM ingress;
    class VLM,PERSONA,ROUTER,MOMENT agent;
    class OUT_POST output;
    class AUDIO,STAGE obs;
    class CTX,COMPACT,DB mem;
    class LLM,TTS runtime;
    class JETSON,DUAL hardware;
```

---

## 快速上手

### 方式一：NVIDIA Jetson 设备 (推荐 ARM64 L4T R36.4)

要求安装 Docker 与 NVIDIA Container Runtime。使用具有 Docker 权限的用户执行：

```bash
curl -fsSL https://raw.githubusercontent.com/4everWZ/Tuber_Studio/main/tools/jetson/run-ready.sh -o run-ready.sh
bash run-ready.sh --data ./tuber-data
```

脚本会拉取 `ghcr.io/4everwz/tuber_studio:r36.4` 容器镜像，首次启动会自动下载所需模型并准备默认音色。  
启动后访问 [http://127.0.0.1:9020](http://127.0.0.1:9020)，管理员令牌保存在 `tuber-data/admin-token`。

---

### 方式二：本地开发运行 (Windows / Linux / macOS)

环境要求：**Python 3.12** 与 **[uv](https://docs.astral.sh/uv/)**。

```bash
# 1. 安装依赖
uv sync --python 3.12

# 2. 启动服务
uv run python -m tuber_studio
```

启动后访问 [http://127.0.0.1:8020](http://127.0.0.1:8020)，管理员令牌在 `runtime/admin-token`。可在「设置」中配置本地 llama.cpp、TTS 或兼容 OpenAI 协议的模型端点。

---

### 方式三：双机协同部署

当推理模型开销较大时，可将 Web 界面与 OBS 播放器运行在日常使用的电脑上，而将推理服务部署在局域网内的独立主机或 Jetson 上。具体网络与端口配置详见 [部署指南](docs/guides/deployment.md)。

---

## 角色状态说明

内置角色「大肥鱼」提供 6 种预设动画状态：
- **待机 (Idle)**：基础呼吸律动与眨眼
- **说话 (Talking)**：语音播报与回复互动时的口型动作
- **开心 (Cheerful)**：收到互动好评或触发正面情绪
- **思考 (Confused)**：复杂推理与渠道判断
- **生气 (Angry)**：遇到不友好评论或被调侃时的情绪反馈
- **感谢 (Gift)**：收到礼物或打赏动效

如需创建自定义角色（OC），可在系统界面的「角色配置」中自定义人设规则、替换贴图与绑定音色，详见 [原创角色指南](docs/guides/characters.md)。

---

## 文档索引

- [部署、更新与网络配置指南](docs/guides/deployment.md)
- [创建与定制原创角色 (OC)](docs/guides/characters.md)
- [评论抓取、审核与自主动态运营](docs/guides/operations.md)
- [直播工作台与 OBS 播放器使用指南](docs/guides/live.md)
- [上下文管理与语义压缩规范](docs/specs/context-and-compaction.md)
- [CI 与 ARM64 镜像构建](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)

---

## 路线图 (Roadmap)

- [ ] 抖音、B站真实直播间弹幕双向实时适配器
- [ ] 可拔插歌唱推理模型接口接入（Singing Voice Conversion）
- [ ] 游戏画面实时捕获与解说互动
- [ ] 多角色同台互动机制

---

## 致谢与开源协议

本项目由 MiniCPM, llama.cpp, GPT-SoVITS 与 Genie-TTS 等开源项目赋能构建。第三方开源条款见 [Genie-TTS 许可证](third_party/Genie-TTS/LICENSE)、[Codex 许可证](third_party/openai-codex/LICENSE) 和 [Codex 声明](third_party/openai-codex/NOTICE)。

Q 版形象原作作者为 [YunYueSama](https://github.com/YunYueSama/codex-deepseek-pet)，本项目将图集转换为六种透明 GIF 状态，详见 [素材条款与修改说明](third_party/whale-avatar/UPSTREAM.md)。其他形象和音色来源见 [素材清单](assets/whale/manifest.json) 和 [音色配置](config/voices.json)。
