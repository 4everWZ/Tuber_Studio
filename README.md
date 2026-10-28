# Tuber Studio

本地角色工作台：首版先逛B站评论区、生成大肥鱼口吻草稿，默认人工审核，可显式开启自动浏览／回复；另保留Mock直播、动态语音和OBS overlay，可复制角色创建OC。

本地开发：

    uv sync --python 3.12
    uv run python -m tuber_studio

打开[本地工作台](http://127.0.0.1:8020)，管理令牌在runtime/admin-token。Jetson使用单派生Docker镜像／容器，内部按需加载2B与TTS；镜像与后台已启动验证，Profile质量／资源准入及剩余端到端验收见当前计划。连接／容量失败会显示错误。

[部署与Jetson启动](docs/guides/deployment.md) · [创建OC](docs/guides/characters.md) · [规范与文档导航](docs/README.md) · [当前计划](docs/plans/p0-runtime.md)

真实模型窗口最低128K；[上下文规范](docs/specs/context-and-compaction.md)定义Codex公开本地压缩核心及适配边界。项目环境独立，凭证、数据库、权重、缓存和临时探针不进Git。第三方角色／音色来源与使用约束见assets/whale/manifest.json，代码归属不授予额外素材权利。
