# Tuber Studio

本地角色工作台：先逛B站评论区，生成大肥鱼口吻草稿，默认人工审核；支持显式自动浏览／回复。附Mock直播、动态语音和OBS overlay，可复制角色建立新OC。

```powershell
uv sync --python 3.12
uv run python -m tuber_studio
```

打开 [本地工作台](http://127.0.0.1:8020)，管理令牌在 `runtime/admin-token`。推理服务需要另行启动；连接失败会显示错误。

[部署与Jetson启动](docs/guides/deployment.md) · [创建OC](docs/guides/characters.md) · [规范与文档导航](docs/README.md)

真实模型窗口最低128K；Codex公开本地压缩算法及适配边界见[上下文规范](docs/specs/context-and-compaction.md)。代码使用项目独立环境，权重、凭证、数据库、音频缓存不进Git。第三方角色素材与音色的来源、署名和使用限制见 `assets/whale/manifest.json`，代码归属不授予第三方素材的额外权利。
