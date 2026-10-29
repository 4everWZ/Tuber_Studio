# Tuber Studio

本地角色工作台：逛B站评论区、生成大肥鱼口吻草稿，默认人工审核，可显式开启自动浏览／回复，可复制角色创建OC。MiniCPM5-2B／真实128K与Genie TTS已跑通本地Mock直播、语音播放和100句以上连续合成；初始大肥鱼已采用用户试听接受的瑶瑶v2声线，真实平台直播仍后置。LLM、语音均可配置兼容API。

本地开发：

    uv sync --python 3.12
    uv run python -m tuber_studio

打开[本地工作台](http://127.0.0.1:8020)，管理令牌在runtime/admin-token。Jetson使用单派生Docker镜像／容器；模板仍默认text且关闭直播，已部署设备由Operator显式启用Mock／live，切回text会卸载TTS。新部署须先准备并校验Genie永久音色包，不能直接沿用已部署设备的准入结论。设置页可保存Provider、独立API凭证与上下文窗口。MiniCPM5-2B只支持文本，多模态仅保留本地／API接口，默认关闭。连接／容量失败会显示错误。

[部署与Jetson启动](docs/guides/deployment.md) · [创建OC](docs/guides/characters.md) · [规范与文档导航](docs/README.md) · [当前计划](docs/plans/p0-runtime.md)

声音、共驻、真实长输入及Mock播放的条件与测量见[当前声音与共驻结果](evaluations/results/yaoyao-v2-voice-20261030.json)；持续运行与真实平台接入分别验收。

默认窗口128K（131072 tokens），可显式调小或在真实支持范围内调大，最小4096；当前MiniCPM5-2B原生上限131072，不能靠声明扩窗。兼容API的窗口上限由Operator声明，不能冒称本机实测；实际计数可能额外发送一token探针并计费。[上下文规范](docs/specs/context-and-compaction.md)维护Codex公开本地压缩核心及适配边界。项目环境独立，凭证、数据库、权重、缓存和临时探针不进Git。形象及预录来源见assets/whale/manifest.json，当前音色来源与约束见config/voices.json，代码归属不授予额外素材权利。
