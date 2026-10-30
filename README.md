# Tuber Studio

本地角色工作台：逛B站评论区、生成大肥鱼口吻草稿，默认人工审核，可显式开启自动浏览／回复，可复制角色创建OC。MiniCPM5-2B／真实128K与Genie TTS已跑通本地Mock直播、语音播放和100句以上连续合成；初始大肥鱼已采用用户试听接受的瑶瑶v2声线，真实平台直播仍后置。LLM、语音均可配置兼容API。

本地开发：

    uv sync --python 3.12
    uv run python -m tuber_studio

打开[本地工作台](http://127.0.0.1:8020)，管理令牌在runtime/admin-token。源码模板默认text且关闭直播；Jetson程序镜像另带MiniCPM5-2B／131072／q4_0与Genie瑶瑶v2的live预设，不内置推理权重。首次启动联网下载固定来源并校验，在持久数据目录一次性准备音色包，然后加载资源；业务保持停止、自动浏览／回复关闭。切回text会卸载TTS。设置页可保存Provider、独立API凭证与上下文窗口。MiniCPM5-2B只支持文本，多模态仅保留本地／API接口，默认关闭。下载、准备、连接或容量失败均须可见，不静默换模型或声音。

Jetson ARM64／L4T R36.4使用GitHub Actions构建的GHCR程序镜像和同次构建附带的独立run-ready.sh，无需项目源码或宿主Python环境。成功发布后执行：

    bash run-ready.sh --data ./tuber-data

从[仓库Actions](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)下载对应构建的`jetson-program-<提交前12位>`产物，解压其中run-ready.sh；脚本默认使用ghcr.io/4everwz/tuber_studio:r36.4，缺少镜像时自动拉取，初始化与服务锁定同一镜像ID。常规发布不要求本机docker build／save／load。首次实际Actions构建和GHCR发布尚待触发与验证，不表示上述镜像已发布；已有标签更新、固定SHA／版本或回退见[部署指南](docs/guides/deployment.md)。

镜像只包含程序、依赖、固定下载清单和小型预设；原生CUDA／Torch使程序镜像仍约14GB量级，不是微型镜像。首次启动须联网，权重、词典和生成的Genie包留在tuber-data；验证通过后重复启动复用，不重新下载安装依赖，也不覆盖已有配置、令牌或数据库。可用TUBER_HF_BASE指定HTTPS镜像源。新数据目录首次下载／准备／冷启动仍待设备验收，见[当前计划](docs/plans/p0-runtime.md)；既有声音结果不替代该验证。本机构建只作开发备用，原精简镜像流程仍保留。

[部署与Jetson启动](docs/guides/deployment.md) · [创建OC](docs/guides/characters.md) · [规范与文档导航](docs/README.md) · [当前计划](docs/plans/p0-runtime.md)

声音、共驻、真实长输入及Mock播放的条件与测量见[当前声音与共驻结果](evaluations/results/yaoyao-v2-voice-20261030.json)；持续运行与真实平台接入分别验收。

默认窗口128K（131072 tokens），可显式调小或在真实支持范围内调大，最小4096；当前MiniCPM5-2B原生上限131072，不能靠声明扩窗。兼容API的窗口上限由Operator声明，不能冒称本机实测；实际计数可能额外发送一token探针并计费。[上下文规范](docs/specs/context-and-compaction.md)维护Codex公开本地压缩核心及适配边界。项目环境独立，凭证、数据库、权重、缓存和临时探针不进Git。形象及预录来源见assets/whale/manifest.json，当前音色来源与约束见config/voices.json，代码归属不授予额外素材权利。
