# Tuber Studio

[English](README.md) · 简体中文

Tuber Studio 是一个面向 B 站社区运营和直播演出的自部署 AI 角色工作台。你可以取材、以角色口吻生成回应、审核草稿并核对平台回执。项目内置 Q 版鲸鱼娘「大肥鱼」，也支持自定义角色、形象和音色。

![大肥鱼说话动画](assets/whale/talking.gif)

## 主要功能

- **评论工作台**读取或导入 B 站评论，按楼查看对话、生成角色回复、编辑和审核草稿。授权提交后可查看平台回执。
- **社区运营**统一启动视频、评论、私信和动态。观看读取可用字幕和真实抽样画面，私信邀请由程序按原评论与 UID 确认。各渠道分别设置审核和发送权限。
- **角色卡**供运营、私聊与直播共用。可以新建、编辑和激活角色，保存覆盖当前卡片。私信与动态共用表情图库。
- **直播工作台**支持拖拽场景、OBS 浏览器源、语音、点歌、歌词与口型。可以先用 Mock 事件验证本地播放流程。

## 快速启动

安装 [uv](https://docs.astral.sh/uv/)，使用 Python 3.12。在仓库根目录运行以下命令。

```sh
uv sync --python 3.12
uv run python -m tuber_studio --port 8020
```

直接打开本地工作台无需管理令牌。远程访问时按部署指南配置认证。在「设置 → 推理与模型」填写 Provider 地址和凭据。「自主运营」内可切换活动、评论处理、私信、动态和运营设置。直播与角色工作室各有独立工作区。 [本地工作台](http://127.0.0.1:8020) · [部署指南](docs/guides/deployment.md)

在「角色 → 语音」设置试听和直播台词的语音来源。离线体验可选择演示文本与预录音频。

开始运营时，在「设置 → 账号与权限」添加并验证 B 站账号，在「自主运营 → 运营设置」选择视频、评论、私信、动态及各自的审核／发送权限，再开始运营。各渠道共用一个启停入口，活动按日汇总；手动读取／导入评论、生成和审核在同一工作台完成。设置与恢复方法见[社区运营指南](docs/guides/operations.md)。

## 部署方式

Tuber Studio 运行工作台与常驻 Agent，持有平台凭据、业务数据、任务和记忆。推理及 Avatar 媒体接入使用受认证的 API，按服务提供的地址、凭据、模型别名和实报能力配置。 [业务部署](docs/guides/deployment.md#业务部署与服务接入) · [API 接入契约](docs/specs/foundation/omni-deployment.md)

## 项目进度

| 方向 | 当前状态 |
| --- | --- |
| 社区运营 | 已完成 30 个真实视频的审核回放。真实私信来信与持续运营仍待验收。 |
| 直播 | 默认使用图片／GIF。OBS 已实测可选 Live2D 的对白口型、静音与 GIF 回退，真实平台、首音和混合负载仍待验收。 |
| 游戏 | 星露谷和植物大战僵尸已有离线模拟。实机接入在直播之后推进。 |

实测证据和后续验收见[文档索引](docs/README.md)。

## 开发与贡献

`src/tuber_studio/` 包含 FastAPI 后端和浏览器工作台。在线点歌是 `components/song_requests/` 下的可选组件。使用项目锁定环境进行开发和测试。

```sh
uv sync --locked --group dev --python 3.12
uv run --locked python tools/ci/check_style.py
uv run --locked pytest tests/test_comments.py
```

按改动范围运行对应流程的测试。使用指南、技术规范和游戏计划见[文档索引](docs/README.md)，微调与评估见 [9B 训练指南](tools/training/README.md)。

## 致谢

随附第三方材料的条款见 [Genie-TTS 许可证](third_party/Genie-TTS/LICENSE)和 [Codex 声明](third_party/openai-codex/NOTICE)。

内置大肥鱼形象源于 YunYueSama 的作品。来源、许可与修改见[形象说明](third_party/whale-avatar/UPSTREAM.md)、[素材清单](assets/whale/manifest.json)和[音色配置](config/voices.json)。
