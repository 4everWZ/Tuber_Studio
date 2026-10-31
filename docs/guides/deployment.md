# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12 与 uv，不向系统／base环境安装依赖：

    uv sync --python 3.12
    uv run python -m tuber_studio --port 8020

打开本地8020，管理令牌从runtime/admin-token读取，不放URL。配置读取runtime/studio.json，不存在时用config/studio.example.json；Provider／API／窗口可在设置页保存并应用，手工编辑配置文件后才需要重启。业务服务启动不代表模型／TTS已经加载或通过容量验证。

离线诊断可用llm.kind=demo、tts.kind=prerecorded并选预录音色；界面必须标明固定台词／预录，不能冒称真实推理、动态配音或128K能力。

## Jetson目标部署

Docker只维护可复用的运行环境：CUDA／原生Torch、llama-server、Python锁定依赖及固定Genie转换／图骨架依赖。应用源码、WebUI、角色素材、初始化清单和配置预设随独立程序包或宿主源码目录交付，以只读挂载运行；模型、音色包、缓存、配置、令牌和数据库保存在独立数据目录。普通程序修改不重建镜像，也不在启动时安装依赖。

目标为Jetson ARM64／L4T R36.4及NVIDIA容器运行时，不是Windows／x86镜像。使用单个tuber-studio容器，后台／受限broker共享业务进程，llama／TTS按需作为固定子进程运行。源码与程序包根仍映射为容器/workspace/tuber；Python环境为/opt/studio-venv，官方GPT-SoVITS依赖为/opt/GPT-SoVITS-v2，固定Genie依赖为/opt/Genie-TTS。不挂Docker.sock，不在宿主安装运行环境，不操作其他项目服务。

| 容器内组件 | 端口 | 职责 |
| --- | --- | --- |
| 后台 | 9020 | 管理API、WebUI、OBS、业务调度，仅发布宿主回环 |
| 受限broker | 9012 | 固定推理入口、进程管理与资源锁 |
| MiniCPM5-2B | 9013 | 原生131072窗口、q4_0 KV，实际容量核查 |
| Genie／GPT-SoVITS | 9011 | 绑定权重与参考的语音合成 |
| 显式实验候选 | 9010／9015 | 独立诊断，不自动替换默认模型 |

管理令牌、OBS凭证和内部推理令牌分权，浏览器不直连模型端口。维护路径为/home/weizheng/code/Tuber_Studio，通过ssh jetson访问；网络地址由SSH配置维护。资源与声音身份要求见[模型与声音](../specs/models-and-audio.md)。

### 首次安装：程序包与环境镜像分开

从[成功的GitHub Actions运行](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)下载jetson-program-<完整commit>产物，内含program.tar.gz、run-ready.sh和program-evidence.json。在新版本专用目录解压，随后由具有Docker权限的普通数据所有者运行：

    tar -xzf program.tar.gz
    bash run-ready.sh --data /path/to/tuber-data

启动器会找到同级program目录；也可以直接运行program/tools/jetson/run-ready.sh或显式传--source PROGRAM_DIR。程序包包含environment-image.txt，指定与该版本依赖匹配的ghcr.io/4everwz/tuber_studio:env-<环境指纹前24位>。不要求宿主Python、uv或运行时pip安装。

**本地有对应镜像就直接复用；缺失才docker pull一次。** 启动器随后将引用解析为固定镜像ID，环境预检、资产初始化和正式容器全部使用该ID与--pull=never。不会每次检查远端更新或重新下载已有镜像。源码目录没有environment-image.txt时使用environment-r36.4别名；可用--image明确选择本地标签或已发布的环境引用。

环境预检对比程序依赖与镜像内/opt/tuber-environment.json；不匹配会在替换旧容器前报错，不自动改依赖、升级已有本地标签或绕过检查。显式环境升级可先docker pull已发布的对应引用，再重运行启动器。首次采用新分离架构需要一个新环境镜像，旧的源码内置r36.4程序镜像不是新架构的已验环境，不能只改标签冒充兼容。

GHCR镜像拉取需要宿主Docker daemon可访问GHCR与其层CDN；私有包须由操作者docker login ghcr.io。TUBER_DOWNLOAD_PROXY只作用于容器内模型资源下载，不配置Docker daemon。公开源码不自动保证新容器包公开，后续包可见性仍需核查。

### 日常代码更新与回退

程序更新优先解压到新的版本目录，复用原数据目录：

    bash NEW_VERSION/run-ready.sh --source NEW_VERSION/program --data /path/to/tuber-data --replace

只要依赖未变，新版本引用同一个环境镜像，不需要重建或拉取环境。旧版本目录和一个已验证环境保留用于回退；回退时指定原程序目录和匹配镜像，数据目录保持不变。程序／环境回退不会回滚数据库，不推断任意版本之间的数据兼容。

开发者可以在宿主源码目录运行：

    bash tools/jetson/run-studio.sh

该入口复用同一个启动器，将本项目runtime作为数据目录；优先使用本地构建的tuber-studio:environment-r36.4，不存在时采用发布环境，支持--image及--replace等参数。更改同一目录的源码前先停止本项目容器，避免运行中读到混合版本；git更新后重新执行启动器并传--replace。仅重启既有版本可docker stop／docker start，不拉取镜像。

启动器支持--source、--image、--data、--name、--port及--replace。默认容器名tuber-studio、回环9020；替换同名容器必须明确--replace且绑定同一数据目录。只挂载程序所需src、tools、config、assets、third_party、components及项目依赖声明，不暴露宿主.git或.env。程序挂载只读，runtime可写；正式推理资产子目录只读。drop ALL capabilities、no-new-privileges、临时目录限额及独立bridge继续生效。

需要在线点歌时，首次增加`--environment online`；启动器使用程序包中的online-environment-image.txt，拉起包含Node及锁定npm依赖的环境扩展镜像。环境选择保存在数据目录environment-profile，后续仅更新程序时继续沿用online，不能退回未安装组件的基础环境。`--environment base`可明确切回基础环境。两种环境都只在本地缺失时拉取；新程序的环境指纹变化时才选择对应新镜像。普通安装仍默认base，工作台的在线点歌开关仍独立默认关闭。

### 首次模型准备与后续复用

环境镜像不带模型。程序中的固定清单将MiniCPM5-2B Q4_K_M、瑶瑶原始GPT／SoVITS与参考、BERT／HuBERT和NLTK下载到持久目录，逐文件核对来源、大小和SHA256。首次启动需要访问这些公共HTTPS源；可显式使用HTTPS origin形式的TUBER_HF_BASE或容器可达的TUBER_DOWNLOAD_PROXY，不能改变revision或关闭校验。

下载后顺序完成权重净化、参考处理、Genie图准备与永久音色安装；准备期间不加载LLM，子进程结束后才进入Genie预热及Mini131072／q4_0共驻。重复启动验证并复用已下载及生成资源，不重新下载／转换；身份不符时保留原件并报错。环境预检和小型公共启动预设在runtime/.portable-launch内生成，不修改已有Operator配置。初始化日志及portable-status.json显示进度，HTTP可达不等于模型就绪。

新目录独立生成私有admin-token、overlay-token、inference-token和业务数据库；已有配置／TTS YAML、数据库和凭证保留。新安装默认Mock／live资源、业务stopped、自动运营关闭。模型／声音／词典只读挂载、原始声音身份和就绪观察要求仍见资源规范。可检查：

    docker inspect --format '{{.State.Health.Status}}' tuber-studio
    docker logs --tail 80 tuber-studio

此前[程序内置镜像的设备证据](../../evaluations/results/ready-program-image-20261030.json)证明原资源链路的首次下载、断网复用与真实推理。环境分离后的[CI发布与Jetson迁移](../../evaluations/results/reusable-environment-release-20261031.json)已验证独立程序只读挂载、已有资源复用、实际131072模型回复和32kHz真实语音，原配置、令牌及业务记录保留；这次使用既有模型／音色数据，没有重复空卷完整下载。本地回归见[环境交付检查](../../evaluations/results/environment-delivery-20261031.json)。

当前设备程序目录为/home/weizheng/code/Tuber_Studio/releases/2b452cae235a435f3e4a242a026e35cfaecddd33/program，current指向该目录；数据仍在原项目runtime。已安装online环境扩展ghcr.io/4everwz/tuber_studio:online-env-be6441607ae064398b0ee271，镜像摘要为sha256:4b3d0deb9bd9608e801f7c202748e69a84d55ef8cc096d54b3f5053894c7e4ee；其基础仍为env-ff99292f7ae1efb88a7a367d。在线点歌已启用，按Operator选择仅用于本机／OBS技术测试，直播／商业用途未确认。真实搜索、波点FLAC、浏览器播放器、歌词和普通重启后保留组件均见[设备点歌验收](../../evaluations/results/online-song-component-device-20261031.json)；咪咕仍未配置凭证，OBS本体未计为已验。

### 环境镜像何时更新

CI照常执行Python／Node检查并交付独立程序包。环境标识只依赖pyproject.toml、uv.lock、Dockerfile、TTS依赖清单及固定Genie依赖；应用、WebUI、角色／素材、初始化和运维脚本变动不改变标识。CI先查询env-<指纹>，已发布则跳过构建；只有新环境未发布时才构建ARM64镜像。默认分支的environment-r36.4别名仅在目标环境变更时更新。

CI另外发布可选online-env-<指纹>环境：在已发布的基础环境上只增加Node和npm依赖，不重建CUDA／Python底座。指纹由基础环境、组件Dockerfile及package.json／package-lock.json决定；helper、UI和其他程序代码更新复用同一镜像。发布前沿用组件协议测试，目标机器安装后仍需真实搜索、取流与播放器验收。

CI镜像构建需要可读的基础CUDA／llama包及GHCR发布权限；使用限定packages:write和短命GITHUB_TOKEN，不将凭证分发给操作者。默认原生ubuntu-24.04-arm runner，真正构建前才执行磁盘预检。发布、匿名访问、设备启动与模型质量分别验收。

开发备用构建：

    bash tools/jetson/build-ready-image.sh
    bash tools/jetson/run-ready.sh --image tuber-studio:environment-r36.4 --data ./tuber-data

build-image.sh是同一环境构建的兼容入口；Dockerfile保留program-base／ready目标名称，但二者都不包含应用源码。构建上下文不读取runtime，不要求模型或生产凭证。普通更新不在设备重新构建；需要离线搬运时可对环境镜像使用docker save／load，同时携带对应程序包，模型资源仍须已经准备或能够下载。

## 存储目录与维护

模型、数据库、评论数据集、配置和凭证保存在持久数据目录中，随容器更新继续复用。`models/`、`tts/`下的模型原件、参考特征与永久Genie包，以及`nltk_data/`共同构成离线启动所需资源；常驻程序使用镜像内的`/opt/studio-venv`。

历史实验保留结果、来源回执和试听音频。重复的ONNX转换文件、传输分片及退役Python环境可回收，需要重现实验时使用保留的模型原件和维护工具重新生成。Docker清理按本项目的具体容器、镜像与缓存ID执行，保留当前镜像和一个已验证的回退版本。

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

第4／5项适用于已启用的Mock／直播设备；源码模板仍默认关闭，独立程序启动器的新安装另有live预设。评论审核／复制不需要播放器。

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

参考缓存位于runtime/tts/reference_features/<voice_id>，由经过身份及张量校验的NPZ／JSON组成。compact路径缺失／失效时，直播冷加载先卸载LLM，在短命进程中重新预计算，退出后才加载不含HuBERT的精简后端；独立程序启动器在首次初始化阶段准备Genie完整模型包，新增声音的手动维护方式见下文，常驻服务不临时导出。两个后端均在同进程完成固定短句预热及清理，再报告健康并加载MiniCPM5／131072。预热音频丢弃、不记为播放或对话，不改变对外默认随机种子；静音／异常不就绪。运行中素材或配置变化会拒绝旧pipeline并报错，broker退出失效子进程，后续请求按既定加载顺序处理；失效的Genie包须重建。serve.py仅保留原版兼容／对照用途，不作为生产HTTP入口。

历史compact语音内存比较使用tools/evaluate_tts_memory.py，与compact.py共用实现并保留原版对照；不将其结果混作Genie测量。维护工具在同一镜像和原生Torch环境内运行，必须先停止业务、清空待执行任务并关闭评论自动策略。输出指定在项目runtime内，包含音频和各阶段整机采样；保护阈值沿用可用160MiB、25秒换页64MiB和有界超时。

--load-order isolated比较单独TTS的原实现与候选（默认--variant both），原实现会生成固定参考特征；--load-order tts-first --variant compact --exercise-llm在候选驻留后加载MiniCPM，并于三句语音间执行两次四token请求。两者都要求维护监督器已受控卸载本项目LLM，并在finally恢复；工具不自行停止其他进程或更改生产模式。--load-order llm-first要求MiniCPM已驻留，是独立的冷加载压力条件，不与TTS先加载的结果混称。同一进程要完成该轮所有语音；单独一轮没有PCM比较时输出null，不将缺少比较写成质量失败。

与正式预热／线程协议一致的PCM数值对照使用tools/evaluate_tts_parity.py --voice whale_sovits_v2 --output runtime/evaluations/tts-parity-<新目录>。外层维护监督器先受控卸载Mini并在finally恢复；工具自身只检查业务停止、自动关闭、GPUworker均已退出，不控制broker或改原缓存。原版／精简版各用新进程与单个工作线程执行生产预热、三句各两遍，记录完整参数、源文件／权重身份和整机守护。同序六组配对、自身重复及可选--legacy-dir旧WAV分别报告；旧协议hash不作为本轮质量门槛，形状／文件不完整不得判一致，数值一致不代表任意文本确定性或人工音质已通过。

正式链路的持续运行入口为tools/evaluate_live.py --output /workspace/tuber/runtime/tmp/live-evaluation，默认两小时且至少100次语音请求：先无额外节拍地完成100次，之后每轮完成后间隔60秒，每第五轮用真实角色／MiniCPM结构回复作为合成文字；其余轮次使用evaluations/speech-cases.json。需先在工作台切到直播资源模式，保持业务停止及自动策略关闭。工具不改模式、不写业务数据库、不发布、不伪造播放回执；记录非静音WAV、无效LLM回复、同进程连续性与整机守护，单纯保活不计作完成合成。当前瑶瑶声线已完成三组相对原版试听、浏览器Mock播放ACK与恰好100句共驻；该工具独立验证持续推理，不替代真实平台、混合事件调度或OBS验收。持续运行的完成状态由[当前计划](../plans/p0-runtime.md)及[准入证据](../../evaluations/results/genie-live-optimization-20261029.json)维护。

轻量／量化对照由tools/evaluate_tts_candidates.py执行，inspect只读包元数据／源码，不导入Torch或初始化CUDA。候选离线包放runtime/evaluations/tts-candidates，用tools/jetson/tts/install-candidates.py --bundle <该目录>安装到它自己的venv；只安装已校验的本地wheel，复用原生Torch／Audio／Vision／TorchAO／Transformers，不升级正式环境。缺少依赖或离线资产时明确失败，不在计时过程中下载。

候选诊断与正式测量分进程：先验证实际算子／INT8底层存储，再用同配置诊断凭证执行无profiler的合成测量。INT8权重存储配合浮点矩阵乘法不标成INT8计算；Lite须开启BERT，原24层前端和新SDK的差异作为候选条件保留。--speech-cases evaluations/speech-cases.json可生成25句对照音频，未实际完成的条数不计通过。tts-first／isolated均由维护监督器受控卸载本项目LLM并在finally恢复，不与正式模型或别的实验抢GPU；加载、参考缓存、预热及Mini共驻开销都计入整机峰值。

Genie历史独立实验使用tools/evaluate_genie_tts.py及单独环境，旧[初筛](../../evaluations/results/gpt-sovits-inference-floor-20261029.json)不等于正式服务或质量准入；当前正式后端的采样、后处理、试听及共驻验收见[优化证据](../../evaluations/results/genie-live-optimization-20261029.json)。外层维护owner卸载Mini并在finally恢复，原MemoryWatch阈值不变；目标中文每次动态执行原前端及22层BERT，不能预计算测试台词后冒称下限。legacy采样／无gap实验与正式后端分开报告。

Genie正式依赖作为原镜像的小层安装进同一`/opt/studio-venv`，锁定CPU ORT1.22.1与少量依赖，保留原CUDA Torch／Audio／Vision、Transformers和NumPy版本及路径；不另起TTS容器或第二套Torch。镜像只携带固定Genie v2转换／模板子集及MIT许可，不装公共Genie服务器、播放器或额外语言资源。

独立程序启动器的默认瑶瑶包由首次bootstrap准备，无须操作者手工执行以下转换。新增声音或精简镜像的维护路径仍须先取得并验证原v2权重／固定参考，再在服务外显式准备：用`genie_sampling.patch_graph`先恢复AR采样，再由`tools/jetson/tts/genie_quantize.py --models <恢复后FP32目录> --output <新AR目录> --execute`量化两个AR图；`tools/jetson/tts/genie_bert.py --root /workspace/tuber --output <新BERT目录> --execute`从原checkpoint准备一次22层动态BERT。上述维护输出放项目`runtime/evaluations`，全程必须有单一维护owner和内存守护，不能与常驻Mini同时导出。首次转换及准备峰值单列，不从直播峰值中消失。

新参考先保留来源原件，再准备单声道32kHz WAV工作副本并配置准确文字；例如48kHz原声须在离线阶段显式重采样，不能等Genie加载时临时处理。对工作副本运行`python tools/jetson/tts/server.py --prepare-reference --voice-id <候选音色ID>`，此后再安装声音包。新GPT／SoVITS权重须重新转换和量化；若BERT checkpoint／config／tokenizer完全相同，可复用经过receipt及内容身份校验的BERT包，避免重复导出。独立候选使用自己的配置和runtime，不把候选追加进仍驻留的生产voices.json；整份文件的身份守卫会使旧worker失效。

安装命令为`python tools/jetson/tts/genie_bundle.py --root /workspace/tuber --voice <voice_id> --models <原转换且已展开FP32的目录> --ar-bundle <恢复采样后的INT8目录> --bert-bundle <BERT目录> --execute`。它核对原checkpoint逐tensor内容、采样／量化来源链和全部运行产物，原子创建`runtime/tts/genie/<voice_id>`，不覆盖已有包。正式容器将该目录只读挂载；安装须停止工作台后，用同一镜像／UID、只读根目录及受限权限运行一次无网络维护容器，仅将本项目runtime作为写边界，外层保留内存守护并在结束后恢复工作台。新OC按自己的voice与参考重复安装；缺包／身份失效会要求显式准备，常驻服务不临时导出、不读取实验目录或静默换回compact。重建先停止相关资源，保留现有包供回退，由Operator显式更换，安装器不自动删除旧包。

初始whale角色默认声音为`yaoyao_sovits_v2`；本地runner仍须配置`tts.voice_id=yaoyao_sovits_v2`并具有相应模型包。ready首次初始化提供该预设并准备包，精简镜像或新增声音由操作者显式准备。省略voice_id仍兼容选择第一个旧本地声线，不会自动推断或替换权重。旧voice、预录素材与历史角色版本保留，恢复旧声音须同步恢复原YAML／voice_id／bundle及角色版本。

精简镜像或新增声音的手动部署须先准备并验证永久音色包，再配置`tts.local_backend=genie_onnx_int8`、选定`tts.voice_id`并显式启用`features.live_enabled`；ready新安装由预设及首次初始化完成这些步骤。引擎选择属启动配置，手改后重启。保持MiniCPM5-2B／131072／q4_0，手动切live时先完成Genie冷载和完整暖机，再加载Mini。前端只读显示引擎，text下动态试听提示先手动切live，不隐式加载；仅允许所选固定音色。`/docs`实际backend／voice／bundle身份和小stat检查通过后，工作台才能复用相应音频缓存。真实平台和评论自动策略不因启用Mock而开启。

语义检查覆盖回声、部分字面事实和摘要遗漏，不证明完整人格、长文召回或长期稳定性。比较须记录真实模型、采样、Think、模板适配和证据层条件，不能把旧2B NoThink实验当官方质量基线。正式Genie Profile已完成声音冷／热启动、共享RAM、128K共驻与真实长输入接收验证；持续运行独立验收。新Profile或窗口仍须分别测量，不从旧结果外推，也不把低窗口结果冒称默认128K。
