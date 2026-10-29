# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12 与 uv，不向系统／base环境安装依赖：

    uv sync --python 3.12
    uv run python -m tuber_studio --port 8020

打开本地8020，管理令牌从runtime/admin-token读取，不放URL。配置读取runtime/studio.json，不存在时用config/studio.example.json；更换Provider／模型Profile后重启。业务服务启动不代表模型／TTS已经加载或通过容量验证。

离线诊断可用llm.kind=demo、tts.kind=prerecorded并选预录音色；界面必须标明固定台词／预录，不能冒称真实推理、动态配音或128K能力。

## Jetson目标部署

使用单派生镜像、单tuber-studio容器：统一Python／CUDA环境，后台和受限broker共享一个Python进程，llama／TTS按需作为固定子进程启动。镜像构建、持久数据初始化和设备后台启动已验证；模型质量、共驻及退出／恢复的剩余验收由当前计划维护。

| 容器内组件 | 容器端口 | 职责 |
| --- | --- | --- |
| 管理后台 | 9020 | 认证管理API／WS、WebUI、overlay、SQLite与业务调度；宿主仅发布回环。 |
| 受限broker | 9012 | 固定推理路径与固定子进程runner、全局生成锁、按Profile加载／休眠；内部token认证。 |
| 2B llama子进程 | 9013 | 默认质量基线的MiniCPM5-2B，真实128K，别名与窗口核查。 |
| GPT-SoVITS子进程 | 9011 | 动态声音，按绑定权重与参考音频合成。 |
| 可选1B诊断子进程 | 9010 | 仅用于独立质量／资源实验，不替代默认2B。 |
| 可选Qwen 2B候选子进程 | 9015 | 受限入口/llm/qwen35，仅在显式配置该候选时驻留；不自动替换默认。 |

不部署host Docker controller／独立host venv，不挂Docker.sock，也不把宿主docker组权限给业务后台。broker只允许固定进程及本项目模型路径；管理令牌、overlay凭证、runtime/inference-token三者分权。内部认证头为X-Tuber-Inference-Key，不在浏览器、日志或示例中公开其值。外部OBS／浏览器通过后台访问媒体，不直连模型子端口。

项目路径为/home/weizheng/code/Tuber_Studio，使用ssh jetson连接；网络地址由本机SSH配置维护，不在项目规范里固定。源码、工具和配置模板进镜像；权重、参考音频、NLTK词典、凭证、数据库和缓存置runtime，容器只挂这一数据目录。TTS官方源码在镜像/opt/GPT-SoVITS-v2，应用环境在/opt/studio-venv；原生CUDA／Torch由基础镜像复用，不在宿主或runtime新建Python环境。仅操作本项目资源，不停止其他项目服务。

模板的LLM入口为http://127.0.0.1:9012/llm/2b，TTS入口为http://127.0.0.1:9012/tts，inference_broker.mode默认text。工作台顶部显式选择「文本/图像」或「直播」；前者停播并卸载TTS，后者须LLM与TTS共驻就绪才成功。开始／恢复直播会先请求直播资源模式，导航页面不改变模式。文本模式的动态TTS合成会要求先切直播；预录播放无需加载TTS。模式成功后持久保存，失败显示实际资源状态并恢复原模式。

优先实验2B Q4_K_M权重＋q4 KV＋128K与TTS共驻，只有语义与共享内存验收通过后才认定该Profile合格。直播模式不放开生成并发；加载等待和错误显示在资源状态，休眠不改变已验证容量。图像仅为资源工作模式的命名，当前不能据此宣称新增图像模型工作流。

镜像以dustynv/pytorch:2.7-r36.4.0-cu128-24.04为基础，从ghcr.io/4everwz/jetson-llama-cpp:r36.4-cu128-u24.04-sm87-d749821阶段只复制llama-server，复用原生Torch和CUDA。锁定依赖安装到项目容器环境，固定只读源码通过同一环境的.pth加载，不在每次源码更新时联网重建editable包；构建时核验模块根仍为/workspace/tuber。运行层不复制开发测试、临时文件、宿主venv或第二套CUDA；带维护用准入／资源评估脚本与固定用例。

### 构建、初始化与替换

在Jetson项目目录执行：

    bash tools/jetson/build-image.sh
    bash tools/jetson/run-studio.sh

已有本项目容器时，第二条改为bash tools/jetson/run-studio.sh --replace。工具先用最终镜像、当前UID初始化／检查数据，再验证归属并停止旧项目GPU容器、替换工作台；同名但挂载不属于本项目时拒绝操作。旧分散部署入口已退役，不与单容器supervisor混用。配置缺失才从模板创建，已有配置／TTS YAML属于Operator，不覆盖；从旧版本迁移时须将llm、tts入口改为上述broker地址并补inference_broker，保留其他设置、凭证和数据库。

初始化直接复用已验证的权重与词典；缺少时下载官方资源，网络失败有界退出。可用最终镜像执行tools/jetson/initialize-data.py --offline验证本地前提；它会检查真实cmudict、pos_tag与g2p_en消费者，避免启动时再交互下载。runtime须由运行UID可写，词典数据自身禁止其他账号写入。运行时无需安装依赖。

停止／启动使用docker stop tuber-studio和docker start tuber-studio；更新源码、角色素材或音色模板须重建镜像并--replace，runtime保留。服务启动与模型可用分开：即使资源加载失败，后台仍显示错误；检查runtime/inference-llm2b.log等资源日志，不把HTTP可达当作质量／128K验收。

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

评论以楼ID隔离上下文，筛选或选择单楼后再看占用／压缩；同批可并发调度4楼，GPU仍串行。审核时可展开来源与完整性检查：它只对有证据的明确问题提示缺项并最多补答一次，日常闲聊不会被要求背诵旧事实。缺项warning不表示已发布或模型整体不可用。

设置页可保存对话留存天数（默认30天）或选择不自动清理，策略保存在SQLite；旧event_retention_days不再控制会话清理。手动清理先预览，再勾选确认；可指定UTC截至日期，按该日结束的排他时间边界提交。原始评论数据集／草稿／发布回执独立保留，清理不是全库删除。楼上下文失效后，下次生成会按本地原文重建，也可点“重建上下文”；已有非空窗口不覆盖。无原文时重新读取或导入，不从已清理摘要／旧草稿复活故事。Demo显示未真实prefill。

设备容器使用独立bridge网络，只有宿主127.0.0.1:9020映射后台；容器内后台监听0.0.0.0:9020，broker／LLM／TTS仍只监听容器回环，不发布到宿主。非root、只读根目录、drop ALL capabilities、no-new-privileges及默认seccomp由固定启动工具设置；runtime持久写入，权重／参考素材／词典只读覆盖，临时目录限额。不提供任意宿主挂载或命令执行接口，不能通过模型台词执行系统指令。没有Landlock／AppArmor或每子进程独立文件沙箱的保证，边界详见[权限规范](../specs/tuber-studio.md#系统与权限边界)。

## Profile验收

按模型／权重、KV精度、窗口、采样／thinking、TTS参数及运行方式记录比较条件。可用项目语义用例执行显式模型检查：

    uv run python tools/evaluate_semantics.py --config runtime/studio.json

只测集成的原句检索／一次补答路径可加--grounded；该用例使用隔离的内存数据库，真实执行摘要与模型回复，不修改工作台对话、调用TTS或发布。容器内可在空闲时用docker exec tuber-studio python tools/evaluate_semantics.py --grounded。两种检查均按题记录，不凭单次遗漏自动淘汰模型；原模型条件、框架输入与外围证据层分别报告。

Linux设备的真实长输入接收探针为docker exec tuber-studio python tools/evaluate_context.py --tokens 120000 --deadline 900。它验证真实alias／窗口、精确输入token数、未截断和实际处理计数，输出小型JSON并守护可用内存／换页／OOM；不打印全部填充文本。合成填充接收成功不证明长文召回、人格或长期稳定性，取消后先检查实际资源状态再继续业务。

正式TTS入口为tools/jetson/tts/server.py，固定监听容器127.0.0.1:9011，只提供健康检查及WAV合成。runtime/studio.json的tts.voice_id选择配置的音色，缺省取首个gpt_sovits；该进程只使用一个固定参考。切新OC时配置对应voice_id／参考音频／文字／语言并重载TTS，不能请求任意路径或同时加载多个声音模型。

参考缓存位于runtime/tts/reference_features/<voice_id>，由经过身份及张量校验的NPZ／JSON组成。缺失／失效时，直播冷加载先卸载LLM，在短命进程中重新预计算；进程退出后加载不含HuBERT的精简后端，完成同进程固定短句预热及清理，再报告健康并加载MiniCPM5／131072。预热音频丢弃、不记为播放或对话，不改变对外默认随机种子；静音／异常不就绪。运行中素材或配置变化会拒绝旧pipeline并报错，broker退出失效子进程，后续请求按同一顺序重载。原始权重、动态中文BERT特征、采样与AR／VITS解码保持；serve.py仅保留原版兼容／对照用途，不再作为生产HTTP入口。

语音内存比较使用tools/evaluate_tts_memory.py；它与正式后端共用compact.py，也保留原实现作对照。所有入口都在同一镜像和原生Torch环境内。必须先停止业务、清空待执行任务并关闭评论自动策略。输出指定在项目runtime内，包含音频和各阶段整机采样；保护阈值沿用可用160MiB、25秒换页64MiB和有界超时。

--load-order isolated比较单独TTS的原实现与候选（默认--variant both），原实现会生成固定参考特征；--load-order tts-first --variant compact --exercise-llm在候选驻留后加载MiniCPM，并于三句语音间执行两次四token请求。两者都要求维护监督器已受控卸载本项目LLM，并在finally恢复；工具不自行停止其他进程或更改生产模式。--load-order llm-first要求MiniCPM已驻留，是独立的冷加载压力条件，不与TTS先加载的结果混称。同一进程要完成该轮所有语音；单独一轮没有PCM比较时输出null，不将缺少比较写成质量失败。

正式链路的持续运行入口为tools/evaluate_live.py --output /workspace/tuber/runtime/tmp/live-evaluation，默认两小时且至少100次语音请求：先无额外节拍地完成100次，之后每轮完成后间隔60秒，每第五轮用真实角色／MiniCPM结构回复作为合成文字；其余轮次使用evaluations/speech-cases.json。需先在工作台切到直播资源模式，保持业务停止及自动策略关闭。工具不改模式、不写业务数据库、不发布、不伪造播放回执；记录非静音WAV、无效LLM回复、同进程连续性与整机守护，单纯保活不计作完成合成。人声听感、真实平台到达率和播放闭环仍需相应验收。

轻量／量化对照由tools/evaluate_tts_candidates.py执行，inspect只读包元数据／源码，不导入Torch或初始化CUDA。候选离线包放runtime/evaluations/tts-candidates，用tools/jetson/tts/install-candidates.py --bundle <该目录>安装到它自己的venv；只安装已校验的本地wheel，复用原生Torch／Audio／Vision／TorchAO／Transformers，不升级正式环境。缺少依赖或离线资产时明确失败，不在计时过程中下载。

候选诊断与正式测量分进程：先验证实际算子／INT8底层存储，再用同配置诊断凭证执行无profiler的合成测量。INT8权重存储配合浮点矩阵乘法不标成INT8计算；Lite须开启BERT，原24层前端和新SDK的差异作为候选条件保留。--speech-cases evaluations/speech-cases.json可生成25句对照音频，未实际完成的条数不计通过。tts-first／isolated均由维护监督器受控卸载本项目LLM并在finally恢复，不与正式模型或别的实验抢GPU；加载、参考缓存、预热及Mini共驻开销都计入整机峰值。

它覆盖回声、部分字面事实和摘要遗漏，不证明完整人格、长文召回或长期稳定性。比较须记录真实模型、采样、Think、模板适配和证据层条件，不能把旧2B NoThink实验当官方质量基线。另测冷／热启动、峰值共享RAM、换页、TTS共驻／交替、长输入及持续运行；不能从旧1B容量推断新2B Profile已验收，也不能靠增加等待或降到128K以下掩盖瓶颈。
