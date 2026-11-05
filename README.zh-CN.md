# Tuber Studio

[English](README.md) · 简体中文

Tuber Studio 是一个面向 B 站社区运营和直播演出的自部署 AI 角色工作台。你可以取材、以角色口吻生成回应、审核草稿并核对平台回执。项目内置 Q 版鲸鱼娘「大肥鱼」，也支持自定义角色、形象和音色。

![大肥鱼说话动画](assets/whale/talking.gif)

## 主要功能

- **评论工作台**读取或导入 B 站评论，按楼查看对话、生成角色回复、编辑和审核草稿。授权提交后可查看平台回执。
- **社区运营**支持自主浏览、字幕取材和经验证的画面取材。私信、动态和公开互动各有授权设置及审核流程。
- **两种角色来源**。自有大肥鱼微调模型使用简短身份提示，通用本地模型、VLM 和兼容 API 使用角色卡。任务材料和对话历史独立于角色提示。
- **直播工作台**支持拖拽场景、OBS 浏览器源、语音、点歌、歌词与口型。可以先用 Mock 事件验证本地播放流程。

## 快速启动

安装 [uv](https://docs.astral.sh/uv/)，使用 Python 3.12。在仓库根目录运行以下命令。

```sh
uv sync --python 3.12
uv run python -m tuber_studio --port 8020
```

打开[本地工作台](http://127.0.0.1:8020)，使用 `runtime/admin-token` 中的令牌登录。在「推理服务与设置」配对 Jetson，或展开自定义服务设置接入其他模型。离线体验可选择演示文本与预录音频。

开始运营时，在设置中添加并验证 B 站账号，从指定视频或已导入的评论生成草稿。审核通过后按授权提交，再核对回执。自动化权限、只读预览和紧停见[社区运营指南](docs/guides/operations.md)。

## 部署方式

默认由电脑承载工作台、业务数据库、平台凭据、点歌和 OBS，局域网内的 Jetson 提供推理并保管模型及推理缓存。配对使用节点实际地址和独立凭据。安装和迁移步骤见[电脑与 Jetson 部署](docs/guides/deployment.md#电脑业务主机与独立推理节点)。

也可以在 Jetson 上运行完整工作台。全本地启动使用程序包或源码目录，环境镜像和持久数据独立管理。见[全本地 Jetson 部署](docs/guides/deployment.md#全本地jetson可选部署)。

## 项目进度

| 方向 | 当前状态 |
| --- | --- |
| 社区运营 | 当前优先交付。已有草稿工作流，真实账号权限、模型判断和获准写入各有独立验收条件。 |
| 直播 | 已有本地工作台、Mock 事件、语音和播放。OBS 本体、真实平台适配和持续混合负载仍在验收计划中。 |
| 游戏 | 星露谷和植物大战僵尸已有离线模拟。实机接入在直播之后推进。 |

实现差距、实测证据和通过条件见[统一路线图](docs/plans/current.md)。

## 开发与贡献

`src/tuber_studio/` 包含 FastAPI 后端和浏览器工作台，部署入口在 `tools/deployment/` 与 `tools/jetson/`，在线点歌是 `components/song_requests/` 下的可选组件。使用项目锁定环境进行开发和测试。

```sh
uv sync --locked --group dev --python 3.12
uv run --locked python tools/ci/check_style.py
uv run --locked pytest tests/test_comments.py
```

按改动范围运行对应流程的测试。使用指南、技术规范和游戏计划见[文档索引](docs/README.md)。

## 致谢

项目使用 MiniCPM、llama.cpp、GPT-SoVITS 和 Genie-TTS。第三方条款见 [Genie-TTS 许可证](third_party/Genie-TTS/LICENSE)和 [Codex 声明](third_party/openai-codex/NOTICE)。

内置大肥鱼形象源于 YunYueSama 的作品。来源、许可与修改见[形象说明](third_party/whale-avatar/UPSTREAM.md)、[素材清单](assets/whale/manifest.json)和[音色配置](config/voices.json)。
