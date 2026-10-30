# 模型、声音与资源契约

本规范维护 Provider、声音身份／缓存、设备 Profile 和推理封装；具体设备测量及准入记录由证据文件维护。

非直播评论、可配置的 LLM API 与本地 Mock 直播均可用。本地资源配置为 MiniCPM5-2B／真实131072／q4_0 与 Genie TTS 共驻，已验证动态语音、浏览器实际播放ACK及真实长输入接收后同进程继续合成。资源优化以整机峰值 RAM 为首要指标，音质与生成速度均须保留原正式 compact 声音后端的至少90%。源码模板默认text且关闭直播，ready程序镜像的新安装另用获准声音的live预设，首次下载与准备完成后才加载；真实平台接入与持续运行须独立验收。当前声音与共驻测量见[瑶瑶正式验收](../../evaluations/results/yaoyao-v2-voice-20261030.json)，原引擎优化对照见[Genie资源实验](../../evaluations/results/genie-live-optimization-20261029.json)。外部语音 API 可独立配置，不因此启用本地 TTS 或直播。

初始大肥鱼的目标声线为清亮柔软、略带奶气的普通话少女音，避免明显电音／金属感；已接受的声音绑定为yaoyao_sovits_v2，角色设定和形象继续由whale角色卡维护。原素材和实际合成须分别试听；模型版本、干净参考、来源和运行包身份分别核验。声音替换重新验证质量、速度与整机RAM，旧声线的相对试听通过不转移为新声线准入，也不以强行升调代替声音选择。

## Provider 与声音

| ID | 适用范围 | 要求 |
| --- | --- | --- |
| AUDIO-01 | 当前 | MUST 可替换 LLMProvider，支持本地 GGUF＋llama.cpp 和操作者配置的 OpenAI-compatible API，业务接口不绑定模型名。 |
| AUDIO-02 | 回复生成 | MUST 校验回复 schema，仅接受 spoken_text、有限 emotion、有限 animation_id、可选 reply_to，非法字段无工具权。运营摘要与决定使用独立的受限 schema，不混成播报回复。 |
| AUDIO-03 | 声音接入 | MUST 保留 TTSProvider 与预录路径；外部 API 使用独立声音配置；本地 Genie 推理已接入，新音色、引擎或 Profile 须分别验证。 |
| AUDIO-04 | 声音接入 | MUST voice_profile 绑定 Provider、模型／权重版本、参考资产或 API voice 及许可；切角色不串声线／缓存。 |
| AUDIO-05 | 本地 TTS | MUST 预计算固定参考特征并精简推理后端，键含模型、音色版本、参考音频／文字／语言及实现身份，修改失效；SHOULD 缓存固定台词。 |
| AUDIO-06 | 播放启用时 | MUST 预制叫声、感谢等音效直通播放，不调 LLM／TTS。 |
| AUDIO-07 | 播放启用时 | MUST 音频／字幕／GIF 共用播放事件或时间戳，合成完成不等于播放完成。 |
| AUDIO-08 | 声音接入 | SHOULD 句长上限、标点切分、合成超时／回退、音量归一和防爆音。 |
| AUDIO-09 | 声音接入 | MUST 展示来源、许可和约束，商业模式不默认启用仅非商用／未授权音色。 |
| AUDIO-10 | 演唱接入 | MUST 分别声明并验证说话与演唱能力；演唱绑定当前角色声音身份、后端与版本，缓存隔离，WebUI明确可用性。普通TTS试听／共驻结果不证明可以唱歌。 |

最小语义接口为 LLMProvider.generate(context,constraints) → StructuredReply，TTSProvider.synthesize(text,voice,options) → AudioArtifact/AudioStream。提交草稿／TTS 前校验完整结束、字数、控制字符、情绪／动作枚举及当前源事件引用；原样复述输入不算有效角色回答。输出不能直接成为 shell、文件操作、HTTP 工具、发布或支付。

运营使用独立的 LLMProvider.generate_json(messages,schema,max_tokens)；本地通过 llama.cpp 的 schema 约束 JSON 输出，兼容 API 使用 JSON 模式与程序 schema 校验。输入和输出仍受真实 token 预算、完整结束和禁止工具调用约束；summary／Codex 压缩原接口与算法不变。摘要及动态决定复用当前模型和全局生成锁，不额外加载 LLM 或 TTS。

本地 MiniCPM 的 Think 模式通过原生 chat template 和 reasoning_format=deepseek 分离推理与正式内容；所有 Provider 只解析 message.content 进入台词／摘要，不将 reasoning_content 播报、存入对话或当作回复 JSON，不靠字符串裁掉思考标签补成功。本地模板默认正式输出预算 1024 tokens，包含模型推理与最终 JSON，台词仍受角色字数上限约束；摘要预算独立，未完整 stop 即失败。延迟须实测，不能从 NoThink 短句推断 Think 实时性。

预录限于允许资产目录，字幕对应实际录音，不冒充动态台词。缓存还覆盖 Provider 和参考音频内容，防止同路径换文件串用。合成失败按配置退回预录／静音且显示原因，不伪造成功。动态TTS的全静音结果必须拒绝，即使上游返回HTTP200；缓存命中也检查，静音缓存删除后重新合成，不能算成功播报。音色持久语义含 ID、Provider、版本、权重、参考资产、许可和有效参数；音频、训练数据、权重及下游用途分别核对，原型允许现有内容不授额外商用权。

### 说话与演唱能力

点歌的产品模式、队列、歌词和播放确认由[直播规范](live-and-media.md#点歌的两种模式)维护。声音接入须将短句说话与按旋律、节奏演唱分别声明；现有`TTSProvider.synthesize`保持说话语义，不要求预录或普通语音API实现演唱。演唱接入使用独立的能力与输入校验边界，所需歌词、旋律／节拍或其他引导素材由选定后端明确声明，缺失时不能退化成朗读后算成功。

大肥鱼演唱目标沿用当前角色绑定的瑶瑶v2声音身份；其他OC使用自己的声音绑定。演唱配置须记录角色／音色版本、实际后端／模型版本、参考或引导素材、有效参数和许可，不能因引入演唱而自动替换说话声线。若候选后端需要另备声音模型或只能使用其他声线，须先确认，不能冒称已复用原声音。现有Genie／GPT-SoVITS的说话验收不构成演唱能力证明。

演唱缓存另含任务类型、歌曲／编曲与歌词版本、引导素材身份、演唱模型／音色／参考身份和有效参数，不能复用同文本的说话缓存或其他角色的演唱。改变任一输入失效；生成产物仍须检查有效格式、时长、非静音和实际声音身份，缓存命中不跳过检查。

新演唱后端须单独测量准备成本、冷／热生成延迟、整机峰值RAM、与当前LLM／TTS资源的关系，并通过包含旋律／节奏、歌词完整性、声线一致性与异常的试听；已有短句共驻、RTF或百句结果不转移为整曲验收。不预设整曲可实时生成或能在8GB设备共驻；若只能预生成、改用外部服务或调整资源驻留，须先说明实测条件并确认该取舍，WebUI不能静默切云端、换声或卸载当前资源。

### 外部 API 与视觉预留

LLM API 使用操作者指定的 Base URL／model 和 OpenAI-compatible Chat Completions。模型列表可达不等于完成生成或容量验证；API 的 native_context_tokens 仅为操作者声明，必须显示为未经本机验证，不能冒称服务端实测上限。计数使用 max_tokens=1 的 usage 探针，要求有效 prompt_tokens；按归一化消息的 digest 最多缓存 64 项，探针会向服务发送上下文且可能额外计费。该探针可因输出限额结束；正式生成与摘要仍必须完整 stop、满足各自结构与语义校验，失败保留原记录，不简化 Codex 上下文压缩核心、不静默截断历史。

语音 API 使用独立的 external_api voice_profile，通过 audio/speech 请求 WAV；有效格式、有限大小、非静音和缓存身份仍须检查。其声音身份包括 endpoint、model、API voice 和配置作用域，不能串用本地 GPT-SoVITS 缓存，也不能因设置 API 自动替换角色原 voice_id。操作者须显式选择 API 音色。

MiniCPM5-2B 是文本模型。视觉仅保留由操作者配置的 local／remote endpoint 契约，默认 disabled；本地 endpoint 也不表示已安装视觉权重，不下载、替换或从模型名称推断已有视觉 encoder／mmproj。启用时单独配置视觉 Provider、model 与凭证，图片作为有界、类型匹配的内联字节提交，不接受观众指定的 URL／系统文件路径；视觉结果无工具或发布权限。

LLM／TTS／vision 的 API key 独立存放于 runtime/provider-credentials.json，使用 0600 私有权限；管理接口只返回是否已配置，不回显密钥，不把凭证放入模型上下文、公开配置或日志。修改 endpoint 时须重新填写或明确清除对应 key，避免旧凭证发送到新服务；公网 endpoint 使用 HTTPS，禁止跟随重定向泄露凭证。内部 broker token 只用于受控本地 broker，不发送给外部 API。设置修改须经过认证，并在停止直播、无生成作业且自动浏览／回复关闭时原子应用；失败恢复原配置／客户端／本地资源，恢复未确认须可见报错，历史会话保留并按新 Provider 重新计数。

## Profile 选择与准入

默认采用 MiniCPM5-2B Q4_K_M、q4_0 K/V 和 131072 上下文，优先服务非直播评论。操作者可显式缩小或放大配置窗口，最小 4096，实际不得超过所选本地模型的真实 native；默认模型的 native 为 131072，不能靠填写更大的声明突破它。本地从真实 GGUF／运行时核对 native 和实际 n_ctx，API 则区分操作者声明的上限与未验证的远端容量。容量预算和压缩由上下文规范维护，切 Provider 或窗口后重新计数；较小窗口不冒称已接收 128K。能输出合法 JSON 不等于角色／摘要质量合格，不能未经语义比较自动退成 1B。

[MiniCPM5-2B 官方配置](https://huggingface.co/openbmb/MiniCPM5-2B/blob/f97400052a43d642bbc6e9975e2397e3ae6a6b52/config.json)声明 LlamaForCausalLM 和 max_position_embeddings=131072，是文本 CausalLM；多模态接入遵守独立视觉 Provider 契约。

固定 llama.cpp d749821 的 [KV 类型白名单](https://github.com/ggml-org/llama.cpp/blob/d749821db3bd587932d1ed57d43626cd552c9909/common/arg.cpp#L391)没有 2／3 bit 类型；q4_0 与 iq4_nl 的 [块布局](https://github.com/ggml-org/llama.cpp/blob/d749821db3bd587932d1ed57d43626cd552c9909/ggml/src/ggml-common.h#L184)均为每 32 个值 18 字节，即 4.5 bit/value，直接改名不会继续减少 KV 字节。保留 q4_0 默认；显式减小实际窗口可相应减少 KV，但这是容量选择，不是同一 128K 配置下的压缩收益。

首版接受明确记录的偶发遗漏，单次漏地点不等于整个模型不可用；该次明确问了地点的回答仍须记为不完整。选型综合实际任务质量、事实错配、延迟和 RAM，不把小集合字面分数当自动淘汰器。获准候选须与 MiniCPM5 使用可比较的语义用例评估，不得为修分数删证据、改 gold 或宣称更短就是更好；既有 Qwen 比较不要求当前继续下载或切换模型。来源检索及最多一次补答由角色／记忆规范维护，不改 Codex 压缩核心。

本地 2B 的质量准入保留官方 Think 参数 temperature=1.0、top_p=0.95、min_p=0.0；既有 NoThink 只算实验，不作为已通过的质量基线。外部 API 采用显式配置，不因接入服务而放宽角色／摘要语义要求。模型、量化或精度的替换须保持同一质量门槛；1B 不是自动兜底许可。更换 Profile 或窗口须分别验证 LLM／TTS 共驻、冷／热启动和持续运行；互斥加载不能冒称共驻，未准入时保留文本工作。长输入接收只证明容量，不证明长文召回或长期稳定。

Qwen3.5-2B 是可显式准备的历史候选，不替换 MiniCPM5-2B 默认；[候选质量结果](../../evaluations/results/qwen35-2b-single-system-seed42.json)、[来源检索对照](../../evaluations/results/grounded-qwen35-2b-seed42.json)、[资源测量](../../evaluations/results/qwen35-2b-live-resource-4b8cadf.json)、[长输入与恢复](../../evaluations/results/qwen35-2b-live-context-f35d061.json)及[播放测量](../../evaluations/results/qwen35-2b-browser-playback-f35d061.json)保留各自条件，不转移为当前 MiniCPM／Genie 的证据。

历史 compact Profile 的容量测量、共驻分配失败与恢复记录保留在[原链路设备证据](../../evaluations/results/compact-live-runtime-20261029.json)，不覆盖当前 Genie Profile 的准入结论。

来源检索与一次补答的结果见[MiniCPM对照](../../evaluations/results/grounded-minicpm5-2b-seed42.json)及上述Qwen对照；外围证据层补全个例不证明模型原始记忆已改善。

候选源模型为[Qwen固定版本](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc)，量化来自[Bartowski固定版本](https://huggingface.co/bartowski/Qwen_Qwen3.5-2B-GGUF/tree/7d26695454df6de5fbcce2e58681e62dae06ce43)，不能冒称Qwen官方GGUF。下载按固定revision、大小、SHA256及真实GGUF架构／窗口验证，默认初始化只取所选模型；可显式准备候选而不切默认，不附带视觉权重。

共驻仅表示权重可同时保留，不放开生成并发。默认 active_llm_generations=1、active_tts_jobs=1，需要时串行；直播、评论和试听共享配额，直播高于离线工作。逻辑 subagent 不加载独立驻留模型；外部 API 接入不改变业务生成和提交的并发约束。

## 工作模式

源码配置模板默认 text，直播功能关闭；ready程序镜像的新安装使用独立Genie／Mock live预设，已有安装保留Operator配置。评论页和其他导航不会隐式改变资源模式或启用声音。使用本地 LLM 时，text 结束直播生产和待播任务、卸载本地 TTS，保留所选 LLM 给评论回复等工作；选择外部 LLM API 时可释放本地推理资源。视觉须另行显式配置，text 名称不表示默认模型具有图像能力。外部语音 API 不依赖本地 live 共驻，也不自动切换角色声音。

用户显式选择 live，须让所选本地 LLM 与 TTS 均就绪并共驻，仍受统一生成配额控制；启动／恢复直播先确认加载成功，失败时不启动直播任务。正式 Genie Profile 已验证停止、切text卸载TTS及回live，不能据此推断任意重启或故障恢复均已验收。

切换经过同一资源锁，成功后原子保存 inference_broker.mode；加载、取消、断连或保存失败时尽力恢复原模式与原驻留，不能展示假成功。旧 exclusive 配置只作为 text 兼容输入。text 下不隐式加载本地动态 TTS；本地 GPT-SoVITS／Genie 合成请求要求先通过 live 准入，预录播放不需要 TTS 进程。前端展示切换等待、实际驻留进程与失败；控制权仅属于认证管理接口，不由观众消息或模型台词切换。

冷切直播时为降低 TTS 载入瞬间的峰值，先释放 LLM、等 TTS 完整就绪，再加载 LLM 并确认两者共驻；已热驻留的请求不反复卸载。失败／取消恢复也遵守资源顺序。该内部策略允许切换延迟，不擅自减少当前配置窗口或张量精度，仍须设备实测，不把策略存在当作共驻成功。

compact的TTS生成器完全关闭后再执行上游已有的缓存清理，让已结束帧的临时张量释放；音频块、驻留权重、推理精度与解码步骤保持原样。各后端的冷加载／首句峰值与热态换页分别记录，短程成功不等于长时稳定，也不以一次冷启动换页推断整模型不可用。

本地语音减内存保留固定参考特征预计算和推理精简路线，也可独立比较轻量引擎或局部 INT8。数值保持路线保留当前 v2 声音、动态中文 BERT 和原采样／解码；轻量引擎、量化或精度变化须单独标明条件并重新评估质量，不能以关闭 BERT、换音色或缩小 LLM 窗口冒充相同配置／质量下的优化。参考特征缓存绑定音色版本、参考音频／文本／语言、模型权重、前端与实现版本，任何不匹配须拒绝复用，新 OC 各自建立缓存。

compact 对照后端使用固定参考 NPZ／JSON 缓存，常驻路径跳过 HuBERT，仅保留原始特征实际使用的 22 层 BERT backbone；目标中文执行同一前端和动态 BERT，AR／VITS 解码、权重精度与采样不改变。参考缓存缺失／失效时，由 TTS 监督进程启动固定 argv 的短命预计算子进程，确认退出后才加载精简 pipeline，不在常驻进程中加载后删除 HuBERT。Genie 的完整模型包在ready首次初始化或显式维护阶段准备，不在常驻服务中导出；已有OC按所选voice_id验证并复用其合法Genie包，自动首次生成仅支持默认瑶瑶，未准备的其他声线明确报错，不回退默认声线。compact仍走原参考准备流程，不执行Genie初始化。live冷启动先卸载LLM，完成所选TTS加载、预热和健康检查，再按当前窗口加载LLM；失败、取消或模型异常按既有模式回滚，不静默换后端或互斥模式。

TTS报告就绪前，须在同进程中完成一条固定短中文、显式种子的完整非静音预热合成并结束清理，让动态中文前端及AR／VITS首次初始化发生在LLM未驻留阶段。预热结果丢弃，不产生台词／播放／对话记录，不改变外部请求的默认随机种子；预热失败不就绪。短句预热不替代更长／混语形状和整机峰值验证。

compact 对照路径以nn.Module包装同一原生Text2SemanticDecoder，保留config、top_k和完整model.*权重键，严格加载；不导入Lightning及未用于推理的准确率指标。删除限于已核实的训练包装／指标初始化，不改变推理、注意力、采样或声学解码方法；原版与精简导入模式不在同一进程混用。

compact 与原版的同条件PCM对照及历史协议差异见[设备对照](../../evaluations/results/compact-live-runtime-20261029.json)；不改旧gold，不从同序一致保证跨协议／任意重复均bitwise确定。

Genie 早期CPU FP32独立加载筛查保留在[历史初筛](../../evaluations/results/gpt-sovits-inference-floor-20261029.json)，其采样、精度及非共驻条件不等于当前正式后端，也不构成热态内存下限或音质结论。

参考专用的HuBERT、librosa、音频解码和频谱依赖仅在原参考函数中按需导入，已有缓存的常驻路径不主动加载这些组件；音频读取工具仅保留逐字相同的load_audio／clean_path及必要依赖，不由此引入Gradio／Pandas界面和数据表框架。原参考运算与音频解码不改，缓存身份同时覆盖参考处理源文件，不能以绕过参考提取或改特征来省内存。

Operator配置tts.voice_id选择config/voices.json中的一个gpt_sovits音色，缺省取首个；单个TTS进程仅服务该固定参考。不同参考或文字／语言请求明确拒绝，不能串用声音。新OC配置独立voice_id／参考资产并在维护时重载TTS，缓存由相同预计算生命周期建立；不同时加载多个声音模型。运行中已校验输入的配置／文件身份变化必须拒绝旧pipeline并要求重载，完整内容hash在加载时校验，不能每句重复读GB级权重。

优化收益以整机共享RAM为主：相同外部服务状态、模型／KV／窗口和台词下，分别记录冷启动、首句、热态合成的最低MemAvailable及总RAM减可用量、进程RSS、换页与OOM；加载和预计算峰值也要计入，预计算单独标示一次性成本。CUDA张量／reserved和权重文件体积只作解释指标，CPU搬运不等于Jetson整机节省。相同种子下的特征／PCM一致可支持数值保持，量化或其他数值改变仍须试听和稳定性验收，不以文件压缩率推断可共驻。

Genie 优化的速度门槛以原正式compact GPT-SoVITS CUDA后端为基线，另保留Genie未优化条件以解释引擎自身开销；同设备／台词／暖机／调用顺序下比较逐句延迟、首句与RTF。保留90%生成吞吐对应耗时不超过基线的1/0.9；音频时长变化须单独报告。音质至少保留原来90%，须通过配对试听核对读文、音色、自然度与异常；无声检查或单一特征分数不得代替感知质量结论。数值改变候选未完成质量评估不自动提升为默认。固定参考预计算、动态目标中文特征、真实128K与安全边界仍须保持，不能以少算层、关闭BERT或互斥加载满足共驻要求。

### Genie本地后端契约

`tts.kind=gpt_sovits`继续表示既有v2音色格式；受限的`tts.local_backend`只接受`compact`或`genie_onnx_int8`，不是可执行路径。Genie复用原音色ID、权重和固定参考，新OC使用自己的音色与bundle；兼容API／预录保持各自路径，不以外部Provider绕开本地资源锁。旧compact保留作对照，Genie准备或推理失败不静默切回它。

Genie工作参考必须预先准备为单声道32kHz WAV；保留来源原文件，其他采样率在离线阶段显式转换，并将工作副本、准确文本和处理方法纳入来源记录。固定特征须从该工作副本重新计算；更换参考后重建声音包，原版／优化后端的配对比较使用同一工作副本。常驻服务不隐式重采样或忽略格式不符。

Genie常驻路径仅加载CPU ORT：原checkpoint的22层中文BERT有效输出使用离线INT8图（132个常量投影整数MatMul），AR首步／续步各97个常量投影整数MatMul，动态注意力仍为浮点，VITS保持FP32。原tokenizer、文字规范化、word2ph与每条未知中文的动态特征保留；纯英文片段沿用原前端合法的零BERT，逐片段验证规范文本、音素、形状与有限值，含汉字的片段仍须动态非零特征。只导入Torch前端工具，不构造Torch BERT或驻留HuBERT。ORT session共享CPU arena，固定单请求和受控线程；转换／量化不在直播进程执行，准备成本与常驻冷峰分别记录。

固定上游Genie模板的AR正态随机采样与原GPT-SoVITS不等价；本地适配恢复Exp(1)、top_k=5、top_p=1、temperature=1、repetition_penalty=1.35、首步EOS屏蔽，以及罚分后greedy EOS检查。每请求seed控制独立PCG64分段／AR／VITS子流；VITS保留标准正态噪声与原0.5系数，不宣称等于Torch同seed序列或跨后端PCM一致。只支持已实现的固定图参数，其他值明确422；seed、原分段间隔可配置。每段至多500步，无停止标志耗尽、取消、空语义或非法token必须失败，不能把参考语义或半条WAV当完成。

输出恢复原v2后处理：各音频段仅在峰值超过1时归一化，各段尾部都追加`max(fragment_interval,0.01)`秒静音（含最后一段，默认0.3），按文本顺序拼接，再乘32768转int16。测量同时保留原声波时长、分段数、静音总时长、最终音频时长与真实合成耗时；加静音降低RTF不代表计算更快，旧无gap实验不改写为同协议结果。

永久bundle位于`runtime/tts/genie/<voice_id>`，由ready首次初始化或显式维护原子创建，拒绝覆盖和路径链接；常驻服务缺包时要求准备，不从实验目录临时凑模型。manifest绑定原权重、参考、前端／采样／量化实现及每个模型图和external数据的内容身份，冷加载完整校验，每句及健康检查仅查小stat，变化拒绝旧pipeline并要求重建／重载。`/docs`只报告实际backend、voice_id和bundle_identity；工作台先确认该身份，再使用含backend及bundle身份的音频缓存，不串用旧compact或其他OC的声音。

CPU／GPU共享RAM之外，设备的R36.4.7还存在[NVIDIA记录的分配限制](https://forums.developer.nvidia.com/t/intermittent-nvmapmemalloc-error-12-and-cuda-allocator-crash-during-pytorch-inference-on-jetson-orin-nano/349752)：MemAvailable充足不保证大块CUDA分配成功。只可对本项目固定模型目录中的干净文件作缓存建议，不全局drop_caches、不改系统／内核安全参数，不把换页维持或分配错误隐藏成共驻成功。

## 单容器封装与推理边界

目标为从适合 Jetson 的原始 CUDA／PyTorch 镜像派生一个 tuber-studio 镜像／容器，只保留运行所需组件，统一 Python／CUDA 环境，共享项目 runtime 挂载。容器内分离业务后端、受限 broker 和按需 llama／TTS 子进程；不默认部署 host Docker controller／独立 host venv，也不挂载 Docker.sock。

程序交付面向Jetson ARM64／L4T R36.4，默认由GitHub Actions测试并构建ARM64镜像，发布到`ghcr.io/4everwz/tuber_studio:r36.4`，同次构建提供独立run-ready.sh产物。构建SHA／版本标签或digest供固定安装和回退；发布工作流及取得镜像／启动脚本的方法由[部署指南](../guides/deployment.md)维护。本机构建仅作开发备用，不作为操作者常规更新的前置。

程序镜像只包含程序、锁定依赖、固定URL／大小／SHA256清单、小型预设及Genie转换／图骨架模板；不内置推理权重、原始声音模型或生成的永久包。当前发布压缩层约6.04GiB、展开镜像约13.36GiB，仍含原生CUDA／Torch，不按排除权重推断微型体积。统一Dockerfile的program-base／ready阶段使用仅含清单和预设的ready_assets命名上下文；CI构建、发布不得依赖旧runtime、模型资产、设备连接或生产凭证。源码公开、包发布、匿名访问及设备部署分别验收；当前公开CI包与设备部署已有[交付证据](../../evaluations/results/ready-program-image-20261030.json)，不据此忽略操作者Docker daemon对GHCR／CDN的实际可达性。

首次启动将[固定下载清单](../../config/jetson/downloads.json)中的MiniCPM5-2B Q4_K_M、瑶瑶原始GPT／SoVITS与参考、BERT／HuBERT及NLTK下载到持久runtime，逐文件验证固定来源、字节数和SHA256，完整校验后才发布。可使用HTTPS origin形式的TUBER_HF_BASE替换HF源站，不能改变revision、关闭校验或回退到其他声音／模型。初次网络、公共源可用性和转换能力是启动前提；超时、取消或校验失败必须有界报错，不能把部分文件或半个bundle当成就绪。

下载后固定短命bootstrap顺序完成权重净化、参考处理／NPZ、转换、原采样恢复、INT8图准备和bundle安装；整个准备期间不得驻留LLM，结束并释放准备资源后才进入常驻TTS预热及Mini131072／q4_0共驻。初始化与健康检查不得pip安装依赖、创建新运行环境或改变原生Torch。首次准备成本与冷加载、热态合成分别测量；转换不在业务／直播常驻进程中进行。

重启验证并复用已下载及生成产物，不重复下载／转换，也不覆盖已有studio.json、TTS YAML、令牌或数据库。不可变源资产或声音包身份不符时拒绝并保留原文件，不静默修补；原BERT／HuBERT checkpoint、sanitized权重、参考及NPZ保留声音身份依赖，inode和时间戳在冷启动重新建立签名。镜像不携带生产凭证、数据库、日志、普通缓存或实验环境；新数据目录独立生成私有令牌与空业务数据库。

新安装默认features.live_enabled=true及inference_broker.mode=live，业务仍stopped且自动浏览／回复关闭；已有Operator配置优先。模型就绪须观察实际窗口、驻留与bundle身份，HTTP可达不算就绪，健康检查只读，不触发下载、转换或加载。全新空数据卷下载／顺序准备、真实Mini131072／q4_0与Genie合成、断网重启复用及配置／令牌／数据库保留已有[设备验收](../../evaluations/results/ready-program-image-20261030.json)；同一CI镜像已由正式启动包部署到原runtime，复用已接受的瑶瑶包并验证真实回复／音频、配置与令牌保留，业务保持停止／自动关闭。精简镜像及显式维护新声音的流程继续保留；构建、交付与启动命令由[部署指南](../guides/deployment.md)维护。

工作台为本地声音计算缓存内容身份时，只可读取已配置gpt_sovits音色的assets或runtime/tts/ref内有效WAV；拒绝绝对路径、穿越、链接和其他runtime／系统文件。该专用读取不扩展通用素材路由，也不给模型或观众文件读取权限。

broker 仅执行固定、已验证的模型／TTS 子进程和允许的推理路径，校验路径、模型别名及真实窗口，不接受用户任意命令／路径。业务输入无进程执行权；内部调用使用独立 token，外部仅接触认证后的工作台／overlay。全局生成锁从加载／推理覆盖到完整响应缓冲结束，取消和失败须清理仍工作的子进程。

本地文本模式先结束非目标资源再加载所选 LLM；直播模式仅在相应Profile准入后保持获准 LLM／TTS 驻留。UI 展示加载等待、活跃／睡眠、错误，并区分模型 native、配置窗口、服务实际窗口和容量验证来源；睡眠状态不决定可用上下文容量，API 配置声明也不等于本机验证。进程 supervisor、镜像构建、退出清理和设备重启恢复均需实际验收，不能从文件存在推断完成。

Jetson加载使用所固定llama.cpp的原生direct-io，避免读GGUF时额外积累文件缓存。启动前可对本项目runtime/models、runtime/tts/weights与pretrained中的模型文件提示释放干净页缓存；限定项目数据目录、关闭文件描述符，不删除权重或改变张量精度，不全局drop_caches、不修改系统内存策略。缓存提示是资源优化，不能替代真实分配／窗口检查；不据一次成功推断长期无换页。

## 设备与降级

| 形态 | 约束 |
| --- | --- |
| Orin Nano Super 8GB＋外部OBS | CPU／GPU共享8GB，NVMe放权重／媒体；无NVENC，不要求本机编码。 |
| Windows／Linux PC | 可开发Mock、角色、评论和单机功能，性能不直接外推Jetson。 |
| 16GB+ Jetson／其他GPU | 后续更大模型／声音，不改变业务接口。 |

ZRAM／SSD Swap 仅兜底，不以持续换页维持低延迟。过载依次暂停离线 GPU、合并普通消息并缩短回复、优先合法预录、限制主动话题、采用已授权且质量通过的轻量 Profile，保留人工接管；不无限增队列／超时或静默缩小操作者当前配置窗口。操作者可显式选择较小窗口，界面与容量证据须反映实际值。

模型判别实验固定同一批角色短句、中文梗、注入抵抗、跨轮记忆／摘要用例，对比质量、延迟和峰值内存，并按实际 Provider／窗口标注条件。声音验收覆盖冷／热启动、连续100句、TTS交替／共驻、混语及人工试听，记录首包延迟、RTF、稳定性和许可；当前用户接受的三组配对样本不外推到所有读文，完整P1声音验收仍以15–30条短句人工试听。已准入的短程与试听结果不替代持续运行、混合事件负载、真实平台或OBS验收。普通短句实时性与长输入吞吐分别报告。

| 验收 ID | 通过条件 |
| --- | --- |
| AC-07 | 当前本地／API LLM 返回完整合法结构，非法情绪／动作被拒绝并可见失败／回退；动态 TTS／预录遵守声音、非静音与播放契约，不作为非直播评论的前置。 |
| AC-08 | 声音接入时切音色不串缓存，API 音色不自动替换角色本地声线，商业配置不直接启用开发专用声音。 |

实现依据 [MiniCPM官方llama.cpp部署](https://github.com/OpenBMB/MiniCPM/blob/main/docs/deployment/llama_cpp.md)、[GPT-SoVITS API v2](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)；设备依据 [Orin Nano Super](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/) 与 [Orin Nano软件编码](https://docs.nvidia.com/jetson/archives/r36.4/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html)。接入时核对版本兼容和许可。
