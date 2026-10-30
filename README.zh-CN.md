# Tuber Studio

[English](README.md) · [简体中文](README.zh-CN.md)

可自部署的 AI 角色工作台，用于 B站评论互动与本地语音演出。创建原创角色，以角色口吻生成回复，并通过浏览器播放器呈现对话。

## 功能

- **评论工作台：** 浏览视频或导入评论，按楼组织对话、分类反馈并生成回复。默认人工审核，支持编辑、复制、通过配置的账号发布，也可选择启用自动浏览与回复。
- **角色配置：** 复制并自定义人格、说话风格、规则、形象与音色绑定，支持版本管理。
- **本地模型与 API：** MiniCPM5-2B 默认使用可调的 128K 上下文，支持 Genie 语音、预录音频及 OpenAI 兼容的 LLM／TTS 服务。
- **上下文与记忆：** 按楼隔离对话、基于来源检索事实、自动压缩上下文，并可设置数据留存策略。
- **语音互动：** 文本／直播资源模式、短句试听、Mock 事件，以及呈现音频、字幕和角色表情的浏览器播放器。

B站、抖音真实直播适配器在后续计划中。

## Jetson 快速启动

需要运行 **L4T R36.4** 的 ARM64 Jetson、已配置 NVIDIA 容器运行时的 Docker、Bash 和 curl。使用具有 Docker 权限的普通用户运行。镜像拉取需要访问 GHCR／CDN，首次模型下载也需要联网。

```bash
curl -fsSL https://raw.githubusercontent.com/4everWZ/Tuber_Studio/main/tools/jetson/run-ready.sh -o run-ready.sh
bash run-ready.sh --data ./tuber-data
```

启动器会拉取 `ghcr.io/4everwz/tuber_studio:r36.4`，首次启动时下载模型并准备默认音色。模型、设置、凭证和数据库保存在 `tuber-data`，后续启动继续复用。

打开 [http://127.0.0.1:9020](http://127.0.0.1:9020)，使用 `tuber-data/admin-token` 中的管理员令牌登录，请妥善保管该文件。从其他电脑访问的方法见[部署指南](docs/guides/deployment.md)。

从评论工作台开始：读取或导入评论，选择一楼对话，生成草稿后审核。自动浏览与回复默认关闭。体验语音互动时，使用直播资源模式，打开播放器、启动 Mock 会话，再注入一条弹幕。

## 本地开发

使用 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --python 3.12
uv run python -m tuber_studio
```

打开 [http://127.0.0.1:8020](http://127.0.0.1:8020)，管理员令牌在 `runtime/admin-token`。在「设置」中配置模型与语音服务端点。源码配置默认以文本模式启动。

## 文档

- [部署、更新与网络配置](docs/guides/deployment.md)
- [创建原创角色](docs/guides/characters.md)
- [文档索引](docs/README.md) · [上下文与压缩](docs/specs/context-and-compaction.md)
- [CI 与 ARM64 镜像构建](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)

## 致谢

项目使用 MiniCPM、llama.cpp、GPT-SoVITS 和 Genie-TTS。第三方条款见 [Genie-TTS 许可证](third_party/Genie-TTS/LICENSE)、[Codex 许可证](third_party/openai-codex/LICENSE)和 [Codex 声明](third_party/openai-codex/NOTICE)。

角色形象与音色来源及相应使用条款记录在[素材清单](assets/whale/manifest.json)和[音色配置](config/voices.json)中。
