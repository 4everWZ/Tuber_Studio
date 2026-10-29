# Tuber Studio

本地角色工作台：首版先逛B站评论区、生成大肥鱼口吻草稿，默认人工审核，可显式开启自动浏览／回复，可复制角色创建OC。直播后置，已有Mock／语音／overlay实现与实验保留；LLM、语音均可配置兼容API。

本地开发：

    uv sync --python 3.12
    uv run python -m tuber_studio

打开[本地工作台](http://127.0.0.1:8020)，管理令牌在runtime/admin-token。Jetson使用单派生Docker镜像／容器，首版保持文本模式并卸载TTS；设置页可保存Provider、独立API凭证与上下文窗口。MiniCPM5-2B只支持文本，多模态仅保留本地／API接口，默认关闭。连接／容量失败会显示错误。

[部署与Jetson启动](docs/guides/deployment.md) · [创建OC](docs/guides/characters.md) · [规范与文档导航](docs/README.md) · [当前计划](docs/plans/p0-runtime.md)

默认窗口128K（131072 tokens），可显式调小或在真实支持范围内调大，最小4096；当前MiniCPM5-2B原生上限131072，不能靠声明扩窗。兼容API的窗口上限由Operator声明，不能冒称本机实测；实际计数可能额外发送一token探针并计费。[上下文规范](docs/specs/context-and-compaction.md)维护Codex公开本地压缩核心及适配边界。项目环境独立，凭证、数据库、权重、缓存和临时探针不进Git。第三方角色／音色来源与使用约束见assets/whale/manifest.json，代码归属不授予额外素材权利。
