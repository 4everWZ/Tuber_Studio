# 部署与启动

## 项目环境

本地开发使用项目专用 Python 3.12 与 uv，不向系统／base环境安装依赖：

    uv sync --python 3.12
    uv run python -m tuber_studio --port 8020

直接打开 `http://127.0.0.1:8020` 即可使用个人工作台。配置读取runtime/studio.json，不存在时用config/studio.example.json；Provider／API／窗口可在设置页保存并应用，手工编辑配置文件后才需要重启。业务服务启动不代表模型／TTS已经加载或通过容量验证。

远程或代理访问从 `runtime/admin-token` 读取管理令牌，登录后浏览器保留会话。保留 `runtime/studio.sqlite3` 和 `admin-token` 可跨服务重启及代码更新继续使用。远程管理设置可重置令牌和退出登录。若部署显式设置 `TUBER_ADMIN_TOKEN`，本机访问也使用该令牌，轮换在环境中完成。

B 站凭证在「设置 → 账号与权限」保存SESSDATA和CSRF／bili_jct，留空不覆盖；显式清除才移除。顶部验证实际UID、头像、硬币和粉丝数，未知统计显示“—”；自动状态读取只查本地缓存，验证及刷新才只读访问平台。同一凭据的上次成功资料与核验时间持久保存，重启或暂时查询失败仍可查看，超过5分钟标注待验证；明确失效、换账号或清除凭据才移除旧资料。退出管理界面不清除B站凭证。

离线诊断可用llm.kind=demo、tts.kind=prerecorded并选预录音色；界面必须标明固定台词／预录，不能冒称真实推理、动态配音或128K能力。

## 提交前检查

在准备提交的开发工作树中，用锁定的开发环境运行代码检查入口：

    uv sync --locked --group dev --python 3.12
    uv run --locked python tools/ci/check_style.py

该命令只检查，不改文件；每次都会分别报告 lint 和 format 的结果，任一失败就返回非零退出码。仅看到 `ruff check` 成功不代表格式检查通过。格式问题可运行 `uv run --locked ruff format src tests tools` 修复，检查差异后重新运行上述入口；修改之后再提交，不要把修改前的检查结果当作当前提交已通过。

CI 分开运行 Python CPU 测试、独立程序包校验和可选组件的 Node 测试。CPU 测试与打包校验均通过后才交付正式程序包。失败的运行仍可分别查看 CPU 测试报告和打包日志，打包校验成功时也可查看候选包的审计记录。Lint 和格式检查保留为本地命令，可用 `--format-advisory` 将格式差异作为提示。

代码检查不替代功能测试。运行受改动影响的 pytest 测试，并以对应 PR 提交的 Linux CPU CI 结果确认完整套件与打包结果。若本地因 NumPy、ONNX 或 PyYAML 等可选依赖缺失而跳过测试，应如实报告；不要因此修改 Jetson 的生产依赖。

这些代码检查不需要模型、GPU、Docker 或设备连接，可在 Windows／Linux 开发环境运行。分支／PR CI 默认不执行 ARM64 环境镜像阶段；该阶段由主分支、发布标签或手动运行触发。CPU CI 成功不代表 Jetson 实机推理或 ARM64 镜像构建已经验证。

## 电脑业务主机与独立推理节点

默认部署由电脑运行工作台、平台工具、点歌和 OBS，局域网内 Jetson 提供受认证的推理。业务数据、平台凭据和登录会话保存在电脑，模型、音色模型及推理缓存留在 Jetson。数据归属与恢复见[跨主机规范](../specs/foundation/distributed-runtime.md)。

电脑使用项目 Python 环境。在线点歌按需安装 Node.js 22 或更高版本，并执行下列第二条命令。

    uv sync --python 3.12
    npm --prefix components/song_requests ci --ignore-scripts

迁移采用`tools/deployment/migrate_business.py`。先停止旧业务服务，再依次`inventory`、`snapshot`、`verify`，传输备份并核对其SHA，最后`restore`到新目录。恢复目录初始为staged，先在隔离副本演练，核对原数据、引用文件及未知发送状态；切换时将旧目录标记retired，再在电脑显式激活正式目录。命令参数可用各子命令`--help`查询。不要通过复制单个SQLite文件迁移正在使用WAL的数据。

电脑启动示例：

    & tools/deployment/run-business.ps1 -Data runtime/desktop/business -Background

默认地址为[电脑工作台](http://127.0.0.1:9020)，数据目录通过`TUBER_RUNTIME`传给子进程；`-Source`、`-Python`、`-Config`可显式选择程序与环境，`-BindAddress`可选电脑的局域网地址。启动器不安装依赖或拉取镜像。同一数据目录的第二个业务进程会在数据库初始化前被拒绝。

在「设置 → 推理与模型」填写 Jetson 节点地址与配对凭据。使用节点根地址，同一节点已有凭据时可以留空，更换节点时提供新凭据。支持局域网或 Tailscale HTTP 地址，公网端点使用 HTTPS。配对后顶栏显示实际连接和模式，高级诊断提供节点资源详情。

远端节点管理连接及 Tailscale 推理连接遇到建连失败时，按 0.5、1.5、3 秒退避，最多重试三次。状态、分词和模板读取在收到响应头之前遇到断流或网关错误也可重试。生成或合成请求发出后若连接中断，由原任务显示失败并可按原入口重试。流式输出中断时保留原任务的完成判定。

「角色 → 语音」默认跟随 Jetson。试听本地声音前，先用顶栏切到直播模式。也可在语音页保存兼容 API 的服务地址、模型、Voice ID 与可选密钥，用于试听和直播台词。真实合成通过 `audio/speech` 返回 WAV，点歌继续使用现有播放链路。

推理宿主的可复用Docker启动器为：

    python3 PROGRAM/tools/deployment/run_inference.py --source PROGRAM --data NODE_DATA --assets MODEL_DATA --image ENV_IMAGE --host NODE_LAN_IP --replace

`NODE_DATA` 必须独立于程序和模型目录，业务数据库和平台凭据留在业务主机。`MODEL_DATA` 只挂载 models／engines／tts／nltk_data 子目录。默认 9B 的主权重和投影器一起放在 `MODEL_DATA/models`。可在 Jetson 的项目环境运行 `python tools/jetson/initialize-data.py --candidate dafeiyu9b`，将验证后的两个文件从项目 `runtime/models` 装入模型目录。镜像存在时固定其 ID，缺失时拉取。`--dry-run` 检查并打印装配。替换前核对数据目录并保留旧容器，启动失败恢复旧容器。Docker 健康检查访问推理节点。 [Hugging Face 固定版本](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/tree/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/releases/1.0.0)

容器中的纯推理入口为：

    python tools/jetson/inference-node.py --root /workspace/tuber --host 0.0.0.0 --port 9012

节点配置位于自身 `runtime/inference-node.json`，保存模型路径、模式、窗口和资源设置。节点凭据来自独立的 `inference-token`。所选引擎使用完整的 `build/bin` 共享库目录。模型、引擎、Genie 包、词典及配套投影器只读挂载，日志和推理缓存写入节点 runtime。容器只发布受认证的节点端口。

AGX 上独立的 llama.cpp CUDA 容器承接当前运营和直播档的语言模型请求，Genie 由配对节点提供。装载检查见[推理与上下文设置](#provider与上下文设置)。

维护前保存节点配置和容器。回退时使用对应程序、配置和已安装权重，并核对 `/llm/props` 的别名与窗口。更换 Genie 音色时，节点的 `tts.voice_id`、永久音色包与 `tts_infer.yaml` 中的 checkpoint 路径需要对应同一音色。切换后用浏览器试听。

旧分模型路径已退役。仓外脚本将 `/llm/2b/props` 换为 `GET /llm/props`，读取前先用 `GET /health` 确认已选模型。将 `POST /llm/2b/warmup` 换为 `POST /llm/warmup`，请求体使用节点当前模型，默认值为 `{"model":"dafeiyu-qwen35-9b:1.0.0"}`。生成同样在请求体指定模型，继续用 `/tts` 合成语音。工作台读取与已配置节点匹配的旧 LLM 地址时会归一化，下次保存设置时写回。双机升级时先更新 Jetson，再更新业务主机。

OBS使用电脑后端生成的播放器链接，音频直接从电脑媒体目录读取，不经Jetson转发。电脑后端关闭后节点不继续浏览、执行Agent或发送平台消息。代码更新先停对应服务、换程序版本，再用原数据目录和环境启动；回退先核对切换后的新增数据，再变更权威标记，不能直接启动旧副本覆盖进度。

## 全本地Jetson可选部署

Docker 环境提供 CUDA／原生 Torch、llama-server、锁定的 Python 依赖和 Genie 转换依赖。应用源码、WebUI、角色素材、初始化清单和配置预设随程序包或宿主源码目录交付，以只读挂载运行。模型、音色包、缓存、配置、令牌和数据库保存在独立数据目录。独立启动器默认加载 9B＋Genie 直播档。应用更新沿用环境镜像。

目标为Jetson ARM64／L4T R36.4及NVIDIA容器运行时，不是Windows／x86镜像。使用单个tuber-studio容器，后台／受限broker共享业务进程，llama／TTS按需作为固定子进程运行。源码与程序包根仍映射为容器/workspace/tuber；Python环境为/opt/studio-venv，官方GPT-SoVITS依赖为/opt/GPT-SoVITS-v2，固定Genie依赖为/opt/Genie-TTS。不挂Docker.sock，不在宿主安装运行环境，不操作其他项目服务。

| 容器内组件 | 端口 | 职责 |
| --- | --- | --- |
| 后台 | 9020 | 管理API、WebUI、OBS、业务调度 |
| 受限broker | 9012 | 固定推理入口、进程管理与资源锁 |
| 大肥鱼 Qwen3.5-9B | 9018 | 默认模型，原生 262144 窗口、q4_0 KV，Q8_0 投影器 |
| Genie／GPT-SoVITS | 9011 | 绑定权重与参考的语音合成 |

管理令牌、OBS 凭证和内部推理令牌各有作用域，浏览器经业务服务访问模型。资源与声音身份要求见[模型与声音](../specs/foundation/models-and-audio.md)。

### 首次安装：程序包与环境镜像分开

从[成功的 GitHub Actions 运行](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)下载正式 `jetson-program-<完整 commit>`，内含 `program.tar.gz`、`run-ready.sh` 和 `program-evidence.json`。`jetson-program-candidate-<完整 commit>` 用于检查打包内容和审计记录。在新版本专用目录解压正式程序包，随后由具有 Docker 权限的普通数据所有者运行：

    tar -xzf program.tar.gz
    bash run-ready.sh --data /path/to/tuber-data --host 127.0.0.1

启动器会找到同级program目录；也可以直接运行program/tools/jetson/run-ready.sh或显式传--source PROGRAM_DIR。程序包包含environment-image.txt，指定与该版本依赖匹配的ghcr.io/4everwz/tuber_studio:env-<环境指纹前24位>。不要求宿主Python、uv或运行时pip安装。

**本地有对应镜像就直接复用；缺失才docker pull一次。** 启动器随后将引用解析为固定镜像ID，环境预检、资产初始化和正式容器全部使用该ID与--pull=never。不会每次检查远端更新或重新下载已有镜像。源码目录没有environment-image.txt时使用environment-r36.4别名；可用--image明确选择本地标签或已发布的环境引用。

环境预检对比程序依赖与镜像内/opt/tuber-environment.json；不匹配会在替换旧容器前报错，不自动改依赖、升级已有本地标签或绕过检查。显式环境升级可先docker pull已发布的对应引用，再重运行启动器。首次采用新分离架构需要一个新环境镜像，旧的源码内置r36.4程序镜像不是新架构的已验环境，不能只改标签冒充兼容。

GHCR镜像拉取需要宿主Docker daemon可访问GHCR与其层CDN；私有包须由操作者docker login ghcr.io。TUBER_DOWNLOAD_PROXY只作用于容器内模型资源下载，不配置Docker daemon。公开源码不自动保证新容器包公开，后续包可见性仍需核查。

### 日常代码更新与回退

程序更新优先解压到新的版本目录，复用原数据目录：

    bash NEW_VERSION/run-ready.sh --source NEW_VERSION/program --data /path/to/tuber-data --host 127.0.0.1 --replace

只要依赖未变，新版本引用同一个环境镜像，不需要重建或拉取环境。旧版本目录和一个已验证环境保留用于回退；回退时指定原程序目录和匹配镜像，数据目录保持不变。程序／环境回退不会回滚数据库，不推断任意版本之间的数据兼容。

开发者可以在宿主源码目录运行：

    bash tools/jetson/run-ready.sh --source "$PWD" --data "$PWD/runtime" --host 127.0.0.1

此命令将本项目的 runtime 作为数据目录。可用 `--image` 指定本地构建的环境。更改同一目录的源码前先停止本项目容器，避免运行中读到混合版本。更新后重新执行启动器并传 `--replace`。仅重启既有版本可使用 `docker stop` 和 `docker start`。

启动器默认容器名为 `tuber-studio`。管理端口 9020 和受认证的 broker 端口 9012 默认绑定宿主回环。需要远程访问管理时使用下文 SSH 转发。可通过 `--port`／`--broker-port` 设置宿主端口。替换同名容器须传 `--replace` 并使用同一数据目录。程序与正式推理资产只读挂载，runtime 可写。容器继续使用独立 bridge、drop ALL capabilities、no-new-privileges 和有界临时目录。

需要在线点歌时，首次增加`--environment online`；启动器使用程序包中的online-environment-image.txt，拉起包含Node及锁定npm依赖的环境扩展镜像。环境选择保存在数据目录environment-profile，后续仅更新程序时继续沿用online，不能退回未安装组件的基础环境。`--environment base`可明确切回基础环境。两种环境都只在本地缺失时拉取；新程序的环境指纹变化时才选择对应新镜像。普通安装仍默认base，工作台的在线点歌开关仍独立默认关闭。

### 首次模型准备与后续复用

环境镜像从持久目录加载模型。portable 初始化按固定清单下载并校验 9B 主权重和投影器、瑶瑶原始 GPT／SoVITS 与参考、BERT／HuBERT 和 NLTK。`run-ready.sh` 使用同一初始化链，也可在已装配的容器根目录运行 `python -m tuber_studio.portable --initialize-only`。新安装默认 9B＋Genie，已有配置保留显式选择。首次准备访问公共 HTTPS 源，支持 HTTPS origin 形式的 `TUBER_HF_BASE` 和容器可达的 `TUBER_DOWNLOAD_PROXY`。 [固定下载清单](../../config/jetson/downloads.json)

已有节点需要恢复旧视觉模型时可显式运行 `python tools/jetson/initialize-data.py --candidate dafeiyu-dora`。使用该发布的权重、投影器和匹配引擎。新安装只准备 9B 模型文件。

下载后依次完成语音权重净化、参考处理、Genie 图准备与音色安装。准备子进程结束后先完成语音预热，再启动配置的语言模型。重复启动验证并复用已下载及生成资源，身份不符时保留原件并报错。环境预检和公共启动预设位于 `runtime/.portable-launch`。初始化进度见日志和 `portable-status.json`，模型就绪以运行实报为准。

新目录独立生成私有admin-token、overlay-token、inference-token和业务数据库；已有配置／TTS YAML、数据库和凭证保留。新安装默认Mock／live资源、业务stopped、自动运营关闭。模型／声音／词典只读挂载、原始声音身份和就绪观察要求仍见资源规范。可检查：

    docker inspect --format '{{.State.Health.Status}}' tuber-studio
    docker logs --tail 80 tuber-studio

单独准备鲸鱼 compact 音色时，运行 `python tools/jetson/initialize-data.py --compact-voice-only`。已有依赖可用 `--offline` 离线准备，使用 `--check-only` 只检查现有资源。瑶瑶 Genie 由程序包首次初始化准备，两套音色模板均位于 `config/jetson/`。

此前[程序内置镜像的设备证据](../../evaluations/results/ready-program-image-20261030.json)证明原资源链路的首次下载、断网复用与真实推理。环境分离后的[CI发布与Jetson迁移](../../evaluations/results/reusable-environment-release-20261031.json)已验证独立程序只读挂载、已有资源复用、实际131072模型回复和32kHz真实语音，原配置、令牌及业务记录保留；这次使用既有模型／音色数据，没有重复空卷完整下载。本地回归见[环境交付检查](../../evaluations/results/environment-delivery-20261031.json)。

在线点歌的全本地设备验收覆盖真实搜索、波点 FLAC、浏览器播放、歌词和重启后复用组件。该次按本机技术测试用途运行。 [设备点歌验收](../../evaluations/results/online-song-component-device-20261031.json)

### 环境镜像何时更新

CPU 测试和程序包校验均通过后，符合发布条件的运行才会发布 Jetson 基础镜像或更新 GHCR 别名。环境标识只依赖 `pyproject.toml`、`uv.lock`、`Dockerfile`、TTS 依赖清单及固定 Genie 依赖，应用、WebUI、角色／素材、初始化和运维脚本变动不改变标识。CI 先查询 `env-<指纹>`，只有新环境尚未发布时才构建 ARM64 镜像。默认分支的 `environment-r36.4` 别名仅在目标环境变更时更新。

CI另外发布可选online-env-<指纹>环境：在已发布的基础环境上只增加Node和npm依赖，不重建CUDA／Python底座。指纹由基础环境、组件Dockerfile及package.json／package-lock.json决定；helper、UI和其他程序代码更新复用同一镜像。组件协议测试通过后发布扩展环境，默认分支的environment-online-r36.4别名也会在复用已发布环境时对齐。目标机器安装后仍需真实搜索、取流与播放器验收。

CI镜像构建需要可读的基础CUDA／llama包及GHCR发布权限；使用限定packages:write和短命GITHUB_TOKEN，不将凭证分发给操作者。默认原生ubuntu-24.04-arm runner，真正构建前才执行磁盘预检。发布、匿名访问、设备启动与模型质量分别验收。

开发备用构建：

    bash tools/jetson/build-ready-image.sh
    bash tools/jetson/run-ready.sh --image tuber-studio:environment-r36.4 --data ./tuber-data --host 127.0.0.1

Dockerfile 的 program-base／ready 目标用于构建环境。构建上下文使用公开依赖，普通程序更新复用原环境。需要离线搬运时，可用 `docker save` 和 `docker load` 传输环境镜像，再携带对应程序包和已准备的模型资源。

## 存储目录与维护

双机部署分别保留电脑业务目录与Jetson推理目录；全本地模式将模型、数据库、评论数据集、配置和凭证保存在同一持久目录中。更新程序继续复用对应目录。`models/`、`tts/`下的模型原件、参考特征与永久Genie包，以及`nltk_data/`共同构成离线启动所需资源；常驻程序使用镜像内的`/opt/studio-venv`。

历史实验保留结果、来源回执和试听音频。重复的ONNX转换文件、传输分片及退役Python环境可回收，需要重现实验时使用保留的模型原件和维护工具重新生成。Docker清理按本项目的具体容器、镜像与缓存ID执行，保留当前镜像和一个已验证的回退版本。

## 全本地模式的SSH访问

后台准备好后，在Windows保持SSH转发运行：

    powershell -File tools/windows-tunnel.ps1

默认本地8021转发Jetson回环9020；可用 `-LocalPort` 改本地端口。打开 [Jetson工作台](http://127.0.0.1:8021)。Docker 桥接部署由容器看到桥接连接，登录时从 Jetson 数据目录的 `admin-token` 读取令牌。OBS使用同一后台生成的独立overlay链接。

电脑业务模式按上文直接访问电脑后端，不需要把整套Jetson后台转发回来；设置页分别显示业务主机与推理节点资源。

## 首次操作

1. 评论页刷新热门视频或填写BV／av／标准链接，读取失败可重试或改导入。
2. 导入 CSV／JSON 时按需映射字段，也可预览规范化结果。导入后直接选择该视频的未处理评论。手工分类会保留。
3. 选择评论生成草稿，再编辑、保存、拒绝、发布或复制。启动自主运营前，在运营设置保存来源、渠道权限与额度，并在账号页验证凭证。导入后的审核和复制可离线完成。
4. Mock／语音验收时先打开独立overlay，再注入事件；只有一个播放页拥有播放权。浏览器禁止自动播放时按提示启用，再注入新事件，不重放失败任务。
5. 暂停阻止后续任务；静音／停止取消当前播报。看真实平台回执区分审核、复制、发布和不确定结果。

第4／5项适用于已启用的Mock／直播设备；源码模板仍默认关闭，独立程序启动器的新安装另有live预设。评论审核／复制不需要播放器。

评论以楼ID隔离上下文，筛选或选择单楼后再看占用／压缩；同批可并发调度4楼，GPU仍串行。审核时可展开原评论、引用和缺项诊断。发现缺项时可查看原稿并自行修订。

设置页可保存对话留存天数（默认30天）或选择不自动清理，策略保存在SQLite；旧event_retention_days不再控制会话清理。手动清理先预览，再勾选确认；可指定UTC截至日期，按该日结束的排他时间边界提交。原始评论数据集／草稿／发布回执独立保留，清理不是全库删除。楼上下文失效后，下次生成会按本地原文重建，也可点“重建上下文”；已有非空窗口不覆盖。无原文时重新读取或导入，不从已清理摘要／旧草稿复活故事。Demo显示未真实prefill。

全本地容器按上述命令将后台和受认证 broker 映射到宿主回环，原生 LLM／TTS 端口留在容器内。进程权限、容器挂载与隔离边界见[权限规范](../specs/tuber-studio.md#系统与权限边界)。

## Provider与上下文设置

AGX 运营档在独立的 llama.cpp CUDA 容器中运行 `dafeiyu-qwen35-9b:1.0.0`。装载同版 Q4_K_M 权重和 Q8_0 投影器，实际窗口为 262,144，K/V 均为 `q4_0`。在「推理与模型」保存文本及视频视觉配置，模型名按服务实际报告填写。思考默认开启，可在设置中切换。 [初次装配实测](../../evaluations/results/agx-clean-9b-deployment-20261111.md) · [当前窗口的真实视频回放](../../evaluations/results/review-agx-mainline-20261111.md)

观看时直接向当前视觉端点请求真实画面。有字幕而画面失败时，按字幕继续，详情会显示取帧原因。设备压测用于检查模型、投影器、图像体积、可用内存与换页，可独立运行。

设置页分别选择本地llama、兼容LLM API、预录／保留的GPT-SoVITS／兼容语音API。API Base URL包含服务自己的版本前缀，例如https://服务域名/v1；公网用HTTPS，本地端点可用HTTP。LLM兼容Chat Completions、max_tokens、JSON对象输出及usage.prompt_tokens；语音兼容audio/speech并返回WAV。模型ID与voice ID由服务提供，不把任意API都当同一模型。

Jetson 节点先配对，LLM Base URL 使用 `http://<Jetson地址>:9012/llm`，模型名以实际节点报告为准。「检查连接」读取已配对节点状态，「高级诊断与维护」显示设备内存及缓存信息。自定义服务的模型列表和地址检查使用表单地址及对应凭证。`/api/providers/local-models`只列实际本地模型目录与状态；保存LLM配置不会安装权重。资产准备统一使用节点／portable初始化与固定下载清单，不再提供业务端独立大肥鱼下载器。接口及旧分路见[推理接口规范](../specs/foundation/models-and-audio.md#jetson-推理接口)。

Key只写入runtime/provider-credentials.json（设备0600），设置响应／配置JSON不返回其值。空输入保留已有Key，单独勾选清除才删除；更换API地址时必须重新填写或清除对应Key，避免把旧凭证发到新服务。内部broker token不发给外部服务。语音API使用独立external_api音色；已有角色不会自动换声线，需显式选择对应音色。接口测试替身通过不代表真实账号、声音许可或服务商质量已验证。

未单独填写时，窗口沿用所选模型的原生上限，当前 9B 默认 262,144。可显式设置从 4,096 到模型原生上限的窗口。本地保存后核对服务实际窗口，失败或取消时恢复原配置。外部窗口由操作者依据服务能力声明。Provider 切换后，下次生成按新 Provider 重新计数和压缩。

兼容 API 会收到任务上下文，本地留存和服务商留存各自管理。9B 图像推理需启用视觉配置并装载配套投影器，关闭视觉时运营按可用字幕取材。认证的 `/api/vision/describe` 接收 `prompt`、`image_base64`、`mime_type`，观察结果按原任务的证据边界使用。普通管理 API 请求上限 2.1 MB，表情上传支持 8 MiB 原图及对应的 base64 封装，歌曲走受限流式上传。

表情包由[共享图库](operations.md#大肥鱼表情包)统一管理，图库原件独立于媒体缓存。

## Profile验收

按模型、K/V 精度、窗口、思考、TTS 参数及运行方式记录比较条件。已驻留的文本模型可用下列命令检查回执支持的压缩和现行评论正文生成。

    uv run --locked python tools/evaluate_semantics.py --config runtime/studio.json --seed 42

用例在隔离库里写入合成回执，核对未发草稿排除及压缩后原句可找回，输出正文与词面诊断。容器内可直接运行 `python tools/evaluate_semantics.py`。

Linux设备的真实长输入接收探针为docker exec tuber-studio python tools/evaluate_context.py --tokens 120000 --deadline 900。它验证真实alias／窗口、精确输入token数、未截断和实际处理计数，输出小型JSON并守护可用内存／换页／OOM；不打印全部填充文本。合成填充接收成功不证明长文召回、人格或长期稳定性，取消后先检查实际资源状态再继续业务。

正式TTS入口为tools/jetson/tts/server.py，固定监听容器127.0.0.1:9011，只提供健康检查及WAV合成。runtime/studio.json的tts.voice_id选择配置的音色，缺省取首个gpt_sovits；该进程只使用一个固定参考。切新OC时配置对应voice_id／参考音频／文字／语言并重载TTS，不能请求任意路径或同时加载多个声音模型。

参考缓存位于 `runtime/tts/reference_features/<voice_id>`，使用经过身份及张量校验的 NPZ／JSON。compact 缓存缺失或失效时，直播冷加载先卸载 LLM，由短命进程重新计算。独立启动器在初始化阶段准备 Genie 音色包。两个后端均在正式进程完成短句预热后报告就绪，再加载当前配置的语言模型。预热音频随即丢弃，静音或异常视为启动失败。运行中音色或配置变化时停止失效子进程，Genie 音色包失效时须重建。

历史compact语音内存比较使用tools/evaluate_tts_memory.py，与compact.py共用实现并保留原版对照；不将其结果混作Genie测量。维护工具在同一镜像和原生Torch环境内运行，必须先停止业务、清空待执行任务并关闭评论自动策略。输出指定在项目runtime内，包含音频和各阶段整机采样；保护阈值沿用可用160MiB、25秒换页64MiB和有界超时。

`--load-order isolated` 比较独立 TTS 的原实现和候选。`--load-order tts-first --variant compact --exercise-llm` 在 TTS 驻留后加载已配置模型，在短句间执行两次小输出请求。`--load-order llm-first` 在所选模型已驻留时测量声音冷启动。维护时按所选顺序管理本项目模型并在结束后恢复。每轮记录真实资源和 WAV，PCM 对照只在具备配对输出时报告。

与正式预热／线程协议一致的 PCM 对照使用 `tools/evaluate_tts_parity.py --voice whale_sovits_v2 --output runtime/evaluations/tts-parity-<新目录>`。先停止业务、关闭自动运营、受控卸载当前 LLM，并在结束后恢复。原版和精简版各用新进程及单工作线程执行预热，每句合成两次。结果记录参数、权重身份、同序配对、各自重复及可选旧 WAV 对照。文件和样本形状齐备后才判断本轮 PCM 一致性。

正式链路的持续运行入口为 `tools/evaluate_live.py --output runtime/tmp/live-evaluation`，每轮使用新目录。在推理节点运行，先从工作台切到已配置的节点直播档，保持直播场次与自主运营停止。工具核对该档的真实模型、窗口、K/V、TTS 和进程身份。默认先完成 100 次语音请求，再每轮间隔 60 秒，持续至少两小时。每第五轮通过当前直播 Provider 生成结构化台词，其余轮次使用 `evaluations/speech-cases.json`。记录非静音 WAV、无效模型回复、进程连续性与整机内存。播放器首音和平台事件分别验收。 [当前计划](../plans/current.md#直播点歌与部署验收)

轻量／量化对照由tools/evaluate_tts_candidates.py执行，inspect只读包元数据／源码，不导入Torch或初始化CUDA。候选离线包放runtime/evaluations/tts-candidates，用tools/jetson/tts/install-candidates.py --bundle <该目录>安装到它自己的venv；只安装已校验的本地wheel，复用原生Torch／Audio／Vision／TorchAO／Transformers，不升级正式环境。缺少依赖或离线资产时明确失败，不在计时过程中下载。

候选诊断与正式测量分进程。先核验实际算子和权重存储，再用相同配置测量合成。使用 `--speech-cases evaluations/speech-cases.json` 生成对照音频，记录实际完成条数。`tts-first` 和 `isolated` 条件由维护过程受控卸载并恢复当前 LLM，加载、参考缓存、预热及实测共驻开销计入整机峰值。

`tools/evaluate_genie_tts.py` 在维护窗口诊断独立 Genie 合成，按实际采样、语音前端、后处理和内存条件记录。正式共驻使用 `tools/evaluate_live.py`。 [声音优化记录](../../evaluations/results/genie-live-optimization-20261029.json)

Genie正式依赖作为原镜像的小层安装进同一`/opt/studio-venv`，锁定CPU ORT1.22.1与少量依赖，保留原CUDA Torch／Audio／Vision、Transformers和NumPy版本及路径；不另起TTS容器或第二套Torch。镜像只携带固定Genie v2转换／模板子集及MIT许可，不装公共Genie服务器、播放器或额外语言资源。

独立启动器首次准备默认瑶瑶包。新增声音时先验证原 v2 权重与固定参考，再在服务外使用 `genie_sampling.patch_graph` 恢复 AR 采样。用 `tools/jetson/tts/genie_quantize.py --models <FP32目录> --output <新AR目录> --execute` 量化两个 AR 图，随后用 `tools/jetson/tts/genie_bert.py --root /workspace/tuber --output <新BERT目录> --execute` 从原检查点准备动态 BERT。维护输出放项目 `runtime/evaluations`，使用单一维护进程和内存守护，导出时释放当前 LLM。首次准备和正式共驻的峰值分别记录。

新参考先保留来源原件，再准备单声道32kHz WAV工作副本并配置准确文字；例如48kHz原声须在离线阶段显式重采样，不能等Genie加载时临时处理。对工作副本运行`python tools/jetson/tts/server.py --prepare-reference --voice-id <候选音色ID>`，此后再安装声音包。新GPT／SoVITS权重须重新转换和量化；若BERT checkpoint／config／tokenizer完全相同，可复用经过receipt及内容身份校验的BERT包，避免重复导出。独立候选使用自己的配置和runtime，不把候选追加进仍驻留的生产voices.json；整份文件的身份守卫会使旧worker失效。

安装命令为`python tools/jetson/tts/genie_bundle.py --root /workspace/tuber --voice <voice_id> --models <原转换且已展开FP32的目录> --ar-bundle <恢复采样后的INT8目录> --bert-bundle <BERT目录> --execute`。它核对原checkpoint逐tensor内容、采样／量化来源链和全部运行产物，原子创建`runtime/tts/genie/<voice_id>`，不覆盖已有包。正式容器将该目录只读挂载；安装须停止工作台后，用同一镜像／UID、只读根目录及受限权限运行一次无网络维护容器，仅将本项目runtime作为写边界，外层保留内存守护并在结束后恢复工作台。新OC按自己的voice与参考重复安装；缺包／身份失效会要求显式准备，常驻服务不临时导出、不读取实验目录或静默换回compact。重建先停止相关资源，保留现有包供回退，由Operator显式更换，安装器不自动删除旧包。

初始whale角色默认声音为`yaoyao_sovits_v2`；本地runner仍须配置`tts.voice_id=yaoyao_sovits_v2`并具有相应模型包。ready首次初始化提供该预设并准备包，精简镜像或新增声音由操作者显式准备。省略voice_id仍兼容选择第一个旧本地声线，不会自动推断或替换权重。旧voice、预录素材与历史角色版本保留，恢复旧声音须同步恢复原YAML／voice_id／bundle及角色版本。

精简镜像或新增声音的手动部署须先准备并验证永久音色包，再配置 `tts.local_backend=genie_onnx_int8`、选定 `tts.voice_id` 并启用 `features.live_enabled`。ready 新安装由预设及首次初始化完成这些步骤。引擎选择属启动配置，修改后重启。手动切 live 时先完成 Genie 冷载和暖机，再加载所选语言模型。默认 9B 使用 262,144 和 q4_0。前端显示实际引擎，text 模式的试听提示先切 live。工作台核对 `/docs` 中的 backend、voice 和 bundle 身份后复用音频缓存。

语义检查记录模型、采样、思考、模板、来源与逐题结果。现有 Genie 档已验证冷／热启动、128K 共驻和真实长输入接收。切换运行档或窗口后，按实际配置复测资源与持续运行。
