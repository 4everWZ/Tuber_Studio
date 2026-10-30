# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12 与 uv，不向系统／base环境安装依赖：

    uv sync --python 3.12
    uv run python -m tuber_studio --port 8020

打开本地8020，管理令牌从runtime/admin-token读取，不放URL。配置读取runtime/studio.json，不存在时用config/studio.example.json；Provider／API／窗口可在设置页保存并应用，手工编辑配置文件后才需要重启。业务服务启动不代表模型／TTS已经加载或通过容量验证。

离线诊断可用llm.kind=demo、tts.kind=prerecorded并选预录音色；界面必须标明固定台词／预录，不能冒称真实推理、动态配音或128K能力。

## Jetson目标部署

使用单派生镜像、单tuber-studio容器：统一Python／CUDA环境，后台和受限broker共享一个Python进程，llama／TTS按需作为固定子进程启动。正式设备已安装Genie永久音色包，并完成MiniCPM5-2B／真实128K共驻、动态语音和Mock播放验证；新部署仍须自行校验资源与声音身份，不能沿用其他设备的结果。范围由[资源规范](../specs/models-and-audio.md)维护，测量见[当前声音与共驻证据](../../evaluations/results/yaoyao-v2-voice-20261030.json)。

| 容器内组件 | 容器端口 | 职责 |
| --- | --- | --- |
| 管理后台 | 9020 | 认证管理API／WS、WebUI、overlay、SQLite与业务调度；宿主仅发布回环。 |
| 受限broker | 9012 | 固定推理路径与固定子进程runner、全局生成锁、按Profile加载／休眠；内部token认证。 |
| 2B llama子进程 | 9013 | 默认MiniCPM5-2B，默认128K、可在真实native范围调节；别名与实际窗口核查。 |
| GPT-SoVITS／Genie TTS子进程 | 9011 | 动态声音，按绑定权重与参考音频合成。 |
| 可选1B诊断子进程 | 9010 | 仅用于独立质量／资源实验，不替代默认2B。 |
| 可选Qwen 2B候选子进程 | 9015 | 受限入口/llm/qwen35，仅在显式配置该候选时驻留；不自动替换默认。 |

不部署host Docker controller／独立host venv，不挂Docker.sock，也不把宿主docker组权限给业务后台。broker只允许固定进程及本项目模型路径；管理令牌、overlay凭证、runtime/inference-token三者分权。内部认证头为X-Tuber-Inference-Key，不在浏览器、日志或示例中公开其值。外部OBS／浏览器通过后台访问媒体，不直连模型子端口。

维护用项目路径为/home/weizheng/code/Tuber_Studio，使用ssh jetson连接；网络地址由本机SSH配置维护，不在项目规范里固定。源码、工具和配置模板进镜像，工作数据置runtime。ready程序镜像不内置推理权重，首次启动下载到挂载目录并准备音色；精简镜像仍使用预先准备的外部权重。TTS官方源码在镜像/opt/GPT-SoVITS-v2，固定Genie转换／图骨架模板子集在/opt/Genie-TTS，应用环境在/opt/studio-venv；原生CUDA／Torch由基础镜像复用，不在宿主或runtime新建Python环境。仅操作本项目资源，不停止其他项目服务。

模板的LLM入口为http://127.0.0.1:9012/llm/2b，本地TTS入口为http://127.0.0.1:9012/tts。源码模板features.live_enabled=false、资源为text；ready程序镜像使用独立live预设，已有安装则尊重原配置。text下动态试听先手动切live，导航不隐式加载TTS或改变资源模式；停止会清理任务，切text还会卸载TTS。预录与已配置语音API可独立试听，不要求本地共驻。

默认继续MiniCPM5-2B Q4_K_M权重＋q4_0 KV＋131072窗口；Genie／Mock直播与非直播评论可分别使用，真实B站／抖音直播接入仍后置。MiniCPM5-2B没有视觉编码器，本地／API视觉接口默认disabled，不下载新视觉模型或从资源模式名称宣称已能看图。

镜像以dustynv/pytorch:2.7-r36.4.0-cu128-24.04为基础，从ghcr.io/4everwz/jetson-llama-cpp:r36.4-cu128-u24.04-sm87-d749821阶段只复制llama-server，复用原生Torch和CUDA。锁定依赖安装到项目容器环境，固定只读源码通过同一环境的.pth加载，不在每次源码更新时联网重建editable包；构建时核验模块根仍为/workspace/tuber。运行层不复制开发测试、临时文件、宿主venv或第二套CUDA；带维护用准入／资源评估脚本与固定用例。

### 程序镜像：首次启动下载与准备

目标硬件为Jetson ARM64／L4T R36.4及已配置的Docker／NVIDIA容器运行时，不是Windows／x86镜像。`tuber-studio:ready-r36.4`只含程序、锁定依赖、Genie转换／图骨架模板、固定URL／大小／SHA256清单和小型预设，不内置Mini、原始声音权重、BERT／HuBERT checkpoint或生成的永久音色包。原生CUDA／Torch等基础依赖使本地程序镜像仍约14GB量级；镜像大小、压缩归档大小和首次准备的磁盘峰值须分别核算，不以“未带权重”冒称微型镜像。

维护者从源码构建并交付归档及独立脚本，无需旧runtime、现有权重或已准备的Genie包：

    bash tools/jetson/build-ready-image.sh
    docker save tuber-studio:ready-r36.4 | gzip > tuber-studio-ready-r36.4.tar.gz
    cp tools/jetson/run-ready.sh ./run-ready.sh

构建时安装依赖并包含固定转换工具；`package-ready.py`只读取`config/jetson/downloads.json`和预设，不收集runtime。构建上下文放项目tmp，ready最后一层不联网下载模型；基础镜像构建仍可能需要网络。接收机器不需要源码仓库、uv、宿主Python或运行时pip安装。

接收机器以拥有Docker权限的普通数据所有者执行，数据目录应专用于本应用：

    docker load -i tuber-studio-ready-r36.4.tar.gz
    bash run-ready.sh --data ./tuber-data

首次启动需要访问清单列出的公共HTTPS源。下载包括MiniCPM5-2B Q4_K_M、瑶瑶原始GPT／SoVITS权重与参考原件、中文BERT／HuBERT及NLTK词典；每个文件均核对固定revision、字节数和SHA256。可按网络情况只替换Hugging Face的HTTPS源站，例如：

    TUBER_HF_BASE=https://hf-mirror.com bash run-ready.sh --data ./tuber-data

`TUBER_HF_BASE`必须是HTTPS origin，不带路径、查询参数或凭证；不改变固定路径／revision和校验和，也不替代GitHub等其他下载源。需要代理时可显式设置容器可达的`TUBER_DOWNLOAD_PROXY`，同样仅接受无凭证／路径的HTTP(S) origin，不隐式继承宿主代理环境。镜像站和原公共源均可能不可达；下载有界、进度与失败可见，失败退出，不跳过校验或静默换成其他模型／音色／演示模式。重试同一启动命令会复用已完整验证的文件；不承诺未完成大文件断点续传。

下载后由固定短命bootstrap顺序执行词典准备、权重净化、32kHz参考处理、Genie转换／采样恢复／INT8图准备、22层动态BERT图准备、参考特征预计算和永久bundle安装。此阶段不得有本项目LLM驻留；准备子进程退出、校验完成后，才启动常驻Genie、完成非静音短句预热，再加载Mini131072／q4_0。准备不是训练，也不是运行时安装新依赖；首次准备耗时与内存／磁盘峰值独立计入，不能套用热态合成结果。已验证的生成产物可重启复用，身份不符明确拒绝，不自动覆盖或临时凑包。

脚本支持--image、--data、--name、--port及--replace；默认镜像tuber-studio:ready-r36.4、容器tuber-studio、回环端口9020。初始化容器使用同一镜像／调用者UID和可写数据目录，完成下载与一次性准备后，再以只读根目录及模型／参考／词典只读子挂载运行正式服务；drop ALL capabilities、no-new-privileges、临时目录限额及独立bridge仍生效。不挂Docker.sock，不修改宿主Python环境，不停止其他项目资源。仅发布127.0.0.1:9020，远程访问使用SSH转发。

新数据目录独立生成私有admin-token、overlay-token、inference-token和空白业务数据库；镜像不携带生产令牌、API凭证、Cookie、数据库、日志或已生成音频。令牌保存在数据目录，不放URL。已有studio.json、TTS YAML、数据库和凭证保留，预设只填缺失文件；已有不可变下载或生成资产身份不符时拒绝并保留原件，不静默替换。原BERT／HuBERT checkpoint虽不常驻推理，仍参与完整声音身份校验，下载后不能随意删去。公共声音素材与权重的许可分别适用，程序打包不授予额外声音使用权。

新安装默认启用Mock／live资源，业务会话仍为stopped，自动浏览／回复关闭，不自动注入事件、播放或发布。已有配置选择text或外部Provider时按其实际配置运行，不强切live。启动观察核对实际模型、窗口、驻留集合、Genie声线及bundle身份；健康检查只读，不重跑下载、转换或加载。可查看：

    docker inspect --format '{{.State.Health.Status}}' tuber-studio
    docker logs --tail 80 tuber-studio

初始化下载／准备阶段通过启动终端、容器日志及数据目录的portable-status.json报告进度和失败；此时后台可能尚未启动。资源加载阶段后台可达也不代表模型就绪，失败须显示资源错误。新空数据目录的首次下载、转换、冷启动和重启复用尚待设备验收，见[当前计划](../plans/p0-runtime.md)；原设备声音、128K及100句结果不替代交付流程验收。

重复启动使用同一数据目录，已验证资产复用。替换现有同名容器须显式传--replace，且只允许绑定该数据目录的本项目容器；需要重新准备音色时必须先受控停止该项目LLM／TTS，不能边运行旧模型边转换。日常停止／重启使用docker stop／docker start，不删除数据目录。直接docker run同样使用镜像内portable入口及固定/workspace/tuber/runtime卷；独立脚本提供完整的权限、初始化和挂载参数。

### 保留的精简镜像流程

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

第4／5项适用于已启用的Mock／直播设备；源码模板仍默认关闭，ready程序镜像的新安装另有live预设。评论审核／复制不需要播放器。

评论以楼ID隔离上下文，筛选或选择单楼后再看占用／压缩；同批可并发调度4楼，GPU仍串行。审核时可展开来源与完整性检查：它只对有证据的明确问题提示缺项并最多补答一次，日常闲聊不会被要求背诵旧事实。缺项warning不表示已发布或模型整体不可用。

设置页可保存对话留存天数（默认30天）或选择不自动清理，策略保存在SQLite；旧event_retention_days不再控制会话清理。手动清理先预览，再勾选确认；可指定UTC截至日期，按该日结束的排他时间边界提交。原始评论数据集／草稿／发布回执独立保留，清理不是全库删除。楼上下文失效后，下次生成会按本地原文重建，也可点“重建上下文”；已有非空窗口不覆盖。无原文时重新读取或导入，不从已清理摘要／旧草稿复活故事。Demo显示未真实prefill。

设备容器使用独立bridge网络，只有宿主127.0.0.1:9020映射后台；容器内后台监听0.0.0.0:9020，broker／LLM／TTS仍只监听容器回环，不发布到宿主。非root、只读根目录、drop ALL capabilities、no-new-privileges及默认seccomp由固定启动工具设置；runtime持久写入，权重／参考素材／词典只读覆盖，临时目录限额。不提供任意宿主挂载或命令执行接口，不能通过模型台词执行系统指令。没有Landlock／AppArmor或每子进程独立文件沙箱的保证，边界详见[权限规范](../specs/tuber-studio.md#系统与权限边界)。

## Provider与上下文设置

设置页分别选择本地llama、兼容LLM API、预录／保留的GPT-SoVITS／兼容语音API。API Base URL包含服务自己的版本前缀，例如https://服务域名/v1；公网用HTTPS，本地端点可用HTTP。LLM兼容Chat Completions、max_tokens、JSON对象输出及usage.prompt_tokens；语音兼容audio/speech并返回WAV。模型ID与voice ID由服务提供，不把任意API都当同一模型。

Key只写入runtime/provider-credentials.json（设备0600），设置响应／配置JSON不返回其值。空输入保留已有Key，单独勾选清除才删除；更换API地址时必须重新填写或清除对应Key，避免把旧凭证发到新服务。内部broker token不发给外部服务。语音API使用独立external_api音色；已有角色不会自动换声线，需显式选择对应音色。接口测试替身通过不代表真实账号、声音许可或服务商质量已验证。

窗口默认131072，可显式调整为4096起的整数。当前Mini真实native131072；本地保存会受控重载并核对实际窗口，失败或取消恢复原资源／配置。外部窗口由Operator依据服务能力声明，不能标作本机verified；计数通过一token请求的实际usage，可能额外计费，同输入缓存计数摘要。检查连接只查健康／模型列表，不生成文本。Provider切换不删除现有楼历史，下次生成按新Provider重新计数／压缩；小窗口的保留预算按比例适配，原评论数据集仍保留。

兼容API会收到任务上下文，本地留存清理不控制服务商留存。默认Mini为文本模型，视觉高级配置仅保留本地／外部兼容端点；默认disabled，未配置时明确拒绝图片请求。认证的/api/vision/describe仅接收prompt、image_base64、mime_type，不接受系统路径或URL，不把输出自动写成历史事实。当前管理API总请求上限2.1MB，图片签名／MIME检查不代表本地视觉模型已部署。

## Profile验收

按模型／权重、KV精度、窗口、采样／thinking、TTS参数及运行方式记录比较条件。可用项目语义用例执行显式模型检查：

    uv run python tools/evaluate_semantics.py --config runtime/studio.json

只测集成的原句检索／一次补答路径可加--grounded；该用例使用隔离的内存数据库，真实执行摘要与模型回复，不修改工作台对话、调用TTS或发布。容器内可在空闲时用docker exec tuber-studio python tools/evaluate_semantics.py --grounded。两种检查均按题记录，不凭单次遗漏自动淘汰模型；原模型条件、框架输入与外围证据层分别报告。

Linux设备的真实长输入接收探针为docker exec tuber-studio python tools/evaluate_context.py --tokens 120000 --deadline 900。它验证真实alias／窗口、精确输入token数、未截断和实际处理计数，输出小型JSON并守护可用内存／换页／OOM；不打印全部填充文本。合成填充接收成功不证明长文召回、人格或长期稳定性，取消后先检查实际资源状态再继续业务。

正式TTS入口为tools/jetson/tts/server.py，固定监听容器127.0.0.1:9011，只提供健康检查及WAV合成。runtime/studio.json的tts.voice_id选择配置的音色，缺省取首个gpt_sovits；该进程只使用一个固定参考。切新OC时配置对应voice_id／参考音频／文字／语言并重载TTS，不能请求任意路径或同时加载多个声音模型。

参考缓存位于runtime/tts/reference_features/<voice_id>，由经过身份及张量校验的NPZ／JSON组成。compact路径缺失／失效时，直播冷加载先卸载LLM，在短命进程中重新预计算，退出后才加载不含HuBERT的精简后端；ready程序镜像在首次初始化阶段准备Genie完整模型包，新增声音的手动维护方式见下文，常驻服务不临时导出。两个后端均在同进程完成固定短句预热及清理，再报告健康并加载MiniCPM5／131072。预热音频丢弃、不记为播放或对话，不改变对外默认随机种子；静音／异常不就绪。运行中素材或配置变化会拒绝旧pipeline并报错，broker退出失效子进程，后续请求按既定加载顺序处理；失效的Genie包须重建。serve.py仅保留原版兼容／对照用途，不作为生产HTTP入口。

历史compact语音内存比较使用tools/evaluate_tts_memory.py，与compact.py共用实现并保留原版对照；不将其结果混作Genie测量。维护工具在同一镜像和原生Torch环境内运行，必须先停止业务、清空待执行任务并关闭评论自动策略。输出指定在项目runtime内，包含音频和各阶段整机采样；保护阈值沿用可用160MiB、25秒换页64MiB和有界超时。

--load-order isolated比较单独TTS的原实现与候选（默认--variant both），原实现会生成固定参考特征；--load-order tts-first --variant compact --exercise-llm在候选驻留后加载MiniCPM，并于三句语音间执行两次四token请求。两者都要求维护监督器已受控卸载本项目LLM，并在finally恢复；工具不自行停止其他进程或更改生产模式。--load-order llm-first要求MiniCPM已驻留，是独立的冷加载压力条件，不与TTS先加载的结果混称。同一进程要完成该轮所有语音；单独一轮没有PCM比较时输出null，不将缺少比较写成质量失败。

与正式预热／线程协议一致的PCM数值对照使用tools/evaluate_tts_parity.py --voice whale_sovits_v2 --output runtime/evaluations/tts-parity-<新目录>。外层维护监督器先受控卸载Mini并在finally恢复；工具自身只检查业务停止、自动关闭、GPUworker均已退出，不控制broker或改原缓存。原版／精简版各用新进程与单个工作线程执行生产预热、三句各两遍，记录完整参数、源文件／权重身份和整机守护。同序六组配对、自身重复及可选--legacy-dir旧WAV分别报告；旧协议hash不作为本轮质量门槛，形状／文件不完整不得判一致，数值一致不代表任意文本确定性或人工音质已通过。

正式链路的持续运行入口为tools/evaluate_live.py --output /workspace/tuber/runtime/tmp/live-evaluation，默认两小时且至少100次语音请求：先无额外节拍地完成100次，之后每轮完成后间隔60秒，每第五轮用真实角色／MiniCPM结构回复作为合成文字；其余轮次使用evaluations/speech-cases.json。需先在工作台切到直播资源模式，保持业务停止及自动策略关闭。工具不改模式、不写业务数据库、不发布、不伪造播放回执；记录非静音WAV、无效LLM回复、同进程连续性与整机守护，单纯保活不计作完成合成。当前瑶瑶声线已完成三组相对原版试听、浏览器Mock播放ACK与恰好100句共驻；该工具独立验证持续推理，不替代真实平台、混合事件调度或OBS验收。持续运行的完成状态由[当前计划](../plans/p0-runtime.md)及[准入证据](../../evaluations/results/genie-live-optimization-20261029.json)维护。

轻量／量化对照由tools/evaluate_tts_candidates.py执行，inspect只读包元数据／源码，不导入Torch或初始化CUDA。候选离线包放runtime/evaluations/tts-candidates，用tools/jetson/tts/install-candidates.py --bundle <该目录>安装到它自己的venv；只安装已校验的本地wheel，复用原生Torch／Audio／Vision／TorchAO／Transformers，不升级正式环境。缺少依赖或离线资产时明确失败，不在计时过程中下载。

候选诊断与正式测量分进程：先验证实际算子／INT8底层存储，再用同配置诊断凭证执行无profiler的合成测量。INT8权重存储配合浮点矩阵乘法不标成INT8计算；Lite须开启BERT，原24层前端和新SDK的差异作为候选条件保留。--speech-cases evaluations/speech-cases.json可生成25句对照音频，未实际完成的条数不计通过。tts-first／isolated均由维护监督器受控卸载本项目LLM并在finally恢复，不与正式模型或别的实验抢GPU；加载、参考缓存、预热及Mini共驻开销都计入整机峰值。

Genie历史独立实验使用tools/evaluate_genie_tts.py及单独环境，旧[初筛](../../evaluations/results/gpt-sovits-inference-floor-20261029.json)不等于正式服务或质量准入；当前正式后端的采样、后处理、试听及共驻验收见[优化证据](../../evaluations/results/genie-live-optimization-20261029.json)。外层维护owner卸载Mini并在finally恢复，原MemoryWatch阈值不变；目标中文每次动态执行原前端及22层BERT，不能预计算测试台词后冒称下限。legacy采样／无gap实验与正式后端分开报告。

Genie正式依赖作为原镜像的小层安装进同一`/opt/studio-venv`，锁定CPU ORT1.22.1与少量依赖，保留原CUDA Torch／Audio／Vision、Transformers和NumPy版本及路径；不另起TTS容器或第二套Torch。镜像只携带固定Genie v2转换／模板子集及MIT许可，不装公共Genie服务器、播放器或额外语言资源。

ready程序镜像的默认瑶瑶包由首次bootstrap准备，无须操作者手工执行以下转换。新增声音或精简镜像的维护路径仍须先取得并验证原v2权重／固定参考，再在服务外显式准备：用`genie_sampling.patch_graph`先恢复AR采样，再由`tools/jetson/tts/genie_quantize.py --models <恢复后FP32目录> --output <新AR目录> --execute`量化两个AR图；`tools/jetson/tts/genie_bert.py --root /workspace/tuber --output <新BERT目录> --execute`从原checkpoint准备一次22层动态BERT。上述维护输出放项目`runtime/evaluations`，全程必须有单一维护owner和内存守护，不能与常驻Mini同时导出。首次转换及准备峰值单列，不从直播峰值中消失。

新参考先保留来源原件，再准备单声道32kHz WAV工作副本并配置准确文字；例如48kHz原声须在离线阶段显式重采样，不能等Genie加载时临时处理。对工作副本运行`python tools/jetson/tts/server.py --prepare-reference --voice-id <候选音色ID>`，此后再安装声音包。新GPT／SoVITS权重须重新转换和量化；若BERT checkpoint／config／tokenizer完全相同，可复用经过receipt及内容身份校验的BERT包，避免重复导出。独立候选使用自己的配置和runtime，不把候选追加进仍驻留的生产voices.json；整份文件的身份守卫会使旧worker失效。

安装命令为`python tools/jetson/tts/genie_bundle.py --root /workspace/tuber --voice <voice_id> --models <原转换且已展开FP32的目录> --ar-bundle <恢复采样后的INT8目录> --bert-bundle <BERT目录> --execute`。它核对原checkpoint逐tensor内容、采样／量化来源链和全部运行产物，原子创建`runtime/tts/genie/<voice_id>`，不覆盖已有包。正式容器将该目录只读挂载；安装须停止工作台后，用同一镜像／UID、只读根目录及受限权限运行一次无网络维护容器，仅将本项目runtime作为写边界，外层保留内存守护并在结束后恢复工作台。新OC按自己的voice与参考重复安装；缺包／身份失效会要求显式准备，常驻服务不临时导出、不读取实验目录或静默换回compact。重建先停止相关资源，保留现有包供回退，由Operator显式更换，安装器不自动删除旧包。

初始whale角色默认声音为`yaoyao_sovits_v2`；本地runner仍须配置`tts.voice_id=yaoyao_sovits_v2`并具有相应模型包。ready首次初始化提供该预设并准备包，精简镜像或新增声音由操作者显式准备。省略voice_id仍兼容选择第一个旧本地声线，不会自动推断或替换权重。旧voice、预录素材与历史角色版本保留，恢复旧声音须同步恢复原YAML／voice_id／bundle及角色版本。

精简镜像或新增声音的手动部署须先准备并验证永久音色包，再配置`tts.local_backend=genie_onnx_int8`、选定`tts.voice_id`并显式启用`features.live_enabled`；ready新安装由预设及首次初始化完成这些步骤。引擎选择属启动配置，手改后重启。保持MiniCPM5-2B／131072／q4_0，手动切live时先完成Genie冷载和完整暖机，再加载Mini。前端只读显示引擎，text下动态试听提示先手动切live，不隐式加载；仅允许所选固定音色。`/docs`实际backend／voice／bundle身份和小stat检查通过后，工作台才能复用相应音频缓存。真实平台和评论自动策略不因启用Mock而开启。

语义检查覆盖回声、部分字面事实和摘要遗漏，不证明完整人格、长文召回或长期稳定性。比较须记录真实模型、采样、Think、模板适配和证据层条件，不能把旧2B NoThink实验当官方质量基线。正式Genie Profile已完成声音冷／热启动、共享RAM、128K共驻与真实长输入接收验证；持续运行独立验收。新Profile或窗口仍须分别测量，不从旧结果外推，也不把低窗口结果冒称默认128K。
