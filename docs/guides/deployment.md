# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12 与 uv，不向系统／base环境安装依赖：

    uv sync --python 3.12
    uv run python -m tuber_studio --port 8020

打开本地8020，管理令牌从runtime/admin-token读取，不放URL。配置读取runtime/studio.json，不存在时用config/studio.example.json；更换Provider／模型Profile后重启。业务服务启动不代表模型／TTS已经加载或通过容量验证。

离线诊断可用llm.kind=demo、tts.kind=prerecorded并选预录音色；界面必须标明固定台词／预录，不能冒称真实推理、动态配音或128K能力。

## Jetson目标部署

最终默认方向是单派生镜像、单tuber-studio容器：统一Python／CUDA环境，内部运行后台、受限broker及按需模型子进程，共享runtime挂载。该封装仍须完成构建、设备启动和恢复验收；下面是部署契约与端口约定，不宣称已有经过验证的一键命令。

| 容器内组件 | 回环端口 | 职责 |
| --- | --- | --- |
| 管理后台 | 9020 | 认证管理API／WS、WebUI、overlay、SQLite与业务调度。 |
| 受限broker | 9012 | 固定推理路径与固定子进程runner、全局生成锁、按Profile加载／休眠；内部token认证。 |
| 2B llama子进程 | 9013 | 默认质量基线的MiniCPM5-2B，真实128K，别名与窗口核查。 |
| GPT-SoVITS子进程 | 9011 | 动态声音，按绑定权重与参考音频合成。 |
| 可选1B诊断子进程 | 9010 | 仅用于独立质量／资源实验，不替代默认2B。 |

不部署host Docker controller／独立host venv，不挂Docker.sock，也不把宿主docker组权限给业务后台。broker只允许固定进程及本项目模型路径；管理令牌、overlay凭证、runtime/inference-token三者分权。内部认证头为X-Tuber-Inference-Key，不在浏览器、日志或示例中公开其值。外部OBS／浏览器通过后台访问媒体，不直连模型子端口。

项目路径为/home/weizheng/code/Tuber_Studio，使用ssh jetson连接；网络地址由本机SSH配置维护，不在项目规范里固定。源码、工具及配置模板在项目树；权重、TTS源码／参考模型、凭证、数据库、缓存和项目环境置runtime。仅操作本项目资源，不停止其他项目服务。

模板的LLM入口为http://127.0.0.1:9012/llm/2b，TTS入口为http://127.0.0.1:9012/tts。当前保守模式为2B／TTS互斥；优先实验2B Q4_K_M权重＋q4 KV＋128K与TTS共驻，只有语义与共享内存验收通过后才改默认。配置共驻不放开生成并发；加载等待和错误显示在资源状态，休眠不改变已验证容量。

镜像可以从已有Jetson CUDA／PyTorch或llama基础镜像派生，最终只留运行必需组件。已有ghcr.io/4everwz/jetson-llama-cpp:r36.4-cu128-u24.04-sm87-d749821是候选基础，并不证明新2B／q4 KV配置兼容。源代码中的runner／入口和镜像构建必须一起验收后再形成正式启动命令。

### 现有分散脚本的边界

tools/jetson/start-llm.sh、tts/deploy.sh、start-studio.sh、inference-start.sh及restart-inference.sh来自分散容器／诊断路径，不能作为最终默认单容器指南。尤其旧start-llm.sh创建1B、旧restart-inference.sh按1B与TTS的启动顺序操作，不用于证明当前2B默认或q4 KV共驻。

TTS准备工具支持下载来源／权重／参考音频配置及离线复用；其独立容器启动／替换步骤仅在相应诊断场景使用，不与最终单容器supervisor混用。最终入口仍需验证统一环境依赖、子进程身份、冷启动、取消／退出清理、重启恢复、只读模型与可写缓存路径。未完成部分在当前工作计划中协调。

## 本地访问Jetson后台

后台准备好后，在Windows保持SSH转发运行：

    powershell -File tools/windows-tunnel.ps1

默认本地8021转发Jetson回环9020；可用-LocalPort改本地端口。打开 [Jetson工作台](http://127.0.0.1:8021)，从Jetson项目runtime/admin-token在本机终端取得管理令牌，不复制到公开配置。无需公开9011／9012／9013，OBS使用同一后台生成的独立overlay链接。

也可在PC运行后台并经受保护转发使用设备broker；此时后台和设备必须共享正确的内部认证配置，界面CPU／RAM统计来自PC，不能冒称Jetson数据。设备另观察tegrastats／vmstat。

## 首次操作

1. 评论页刷新热门视频或填写BV／av／标准链接，读取失败可重试或改导入。
2. 导入CSV／JSON时按需填目标→来源字段映射，先预览规范字段和总数，再导入；修改输入后重新预览。分类可人工修改，重新自动归类不得覆盖人工值。
3. 选择评论生成草稿，再编辑、审核、复制。默认自动浏览／回复关闭；显式配置关注视频、间隔、配额及凭证后才能开启对应策略。无凭证仍可浏览、审核和复制。
4. Mock／语音验收时先打开独立overlay，再注入事件；只有一个播放页拥有播放权。浏览器禁止自动播放时按提示启用，再注入新事件，不重放失败任务。
5. 暂停阻止后续任务；静音／停止取消当前播报。看真实平台回执区分审核、复制、发布和不确定结果。

## Profile验收

按模型／权重、KV精度、窗口、采样／thinking、TTS参数及运行方式记录比较条件。可用项目语义用例执行显式模型检查：

    uv run python tools/evaluate_semantics.py --config runtime/studio.json

它覆盖回声、部分字面事实和摘要遗漏，不证明完整人格、长文召回或长期稳定性。q4 KV须与2B q8 KV／NoThink质量基线使用同一输入；同时测冷／热启动、峰值共享RAM、换页、TTS共驻／交替、长输入及持续运行。不能从旧1B的长输入结果推断新2B Profile已验收，也不能靠增加等待或降到128K以下掩盖瓶颈。
