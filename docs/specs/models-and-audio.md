# 模型、声音与资源契约

本规范维护 Provider、声音身份／缓存、设备 Profile 和推理封装。条目为验收目标；实验状态不写成已经交付。

非直播评论与可配置的 LLM API 接入继续可用；本地声音恢复开发，资源目标为 MiniCPM5-2B／131072 与 Genie TTS 直播共驻。首要降低整机峰值 RAM，音质与生成速度均须保留原正式声音后端的至少90%；该要求不等于已通过共驻或质量验收，默认启用须先满足准入。外部语音 API 可独立配置，不因此启用本地 TTS 或直播。

## Provider 与声音

| ID | 适用范围 | 要求 |
| --- | --- | --- |
| AUDIO-01 | 当前 | MUST 可替换 LLMProvider，支持本地 GGUF＋llama.cpp 和操作者配置的 OpenAI-compatible API，业务接口不绑定模型名。 |
| AUDIO-02 | 当前 | MUST 校验模型 schema，仅接受 spoken_text、有限 emotion、有限 animation_id、可选 reply_to，非法字段无工具权。 |
| AUDIO-03 | 声音接入 | MUST 保留 TTSProvider 与预录路径；外部 API 使用独立声音配置，本地 GPT-SoVITS v2 与其他轻量引擎的准入按后续设备证据决定。 |
| AUDIO-04 | 声音接入 | MUST voice_profile 绑定 Provider、模型／权重版本、参考资产或 API voice 及许可；切角色不串声线／缓存。 |
| AUDIO-05 | 后续本地 TTS | MUST 预计算固定参考特征并精简推理后端，键含模型、音色版本、参考音频／文字／语言及实现身份，修改失效；SHOULD 缓存固定台词。 |
| AUDIO-06 | 播放启用时 | MUST 预制叫声、感谢等音效直通播放，不调 LLM／TTS。 |
| AUDIO-07 | 播放启用时 | MUST 音频／字幕／GIF 共用播放事件或时间戳，合成完成不等于播放完成。 |
| AUDIO-08 | 声音接入 | SHOULD 句长上限、标点切分、合成超时／回退、音量归一和防爆音。 |
| AUDIO-09 | 声音接入 | MUST 展示来源、许可和约束，商业模式不默认启用仅非商用／未授权音色。 |

最小语义接口为 LLMProvider.generate(context,constraints) → StructuredReply，TTSProvider.synthesize(text,voice,options) → AudioArtifact/AudioStream。提交草稿／TTS 前校验完整结束、字数、控制字符、情绪／动作枚举及当前源事件引用；原样复述输入不算有效角色回答。输出不能直接成为 shell、文件操作、HTTP 工具、发布或支付。

本地 MiniCPM 的 Think 模式通过原生 chat template 和 reasoning_format=deepseek 分离推理与正式内容；所有 Provider 只解析 message.content 进入台词／摘要，不将 reasoning_content 播报、存入对话或当作回复 JSON，不靠字符串裁掉思考标签补成功。本地模板默认正式输出预算 1024 tokens，包含模型推理与最终 JSON，台词仍受角色字数上限约束；摘要预算独立，未完整 stop 即失败。延迟须实测，不能从 NoThink 短句推断 Think 实时性。

预录限于允许资产目录，字幕对应实际录音，不冒充动态台词。缓存还覆盖 Provider 和参考音频内容，防止同路径换文件串用。合成失败按配置退回预录／静音且显示原因，不伪造成功。动态TTS的全静音结果必须拒绝，即使上游返回HTTP200；缓存命中也检查，静音缓存删除后重新合成，不能算成功播报。音色持久语义含 ID、Provider、版本、权重、参考资产、许可和有效参数；音频、训练数据、权重及下游用途分别核对，原型允许现有内容不授额外商用权。

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

本地 2B 的质量准入保留官方 Think 参数 temperature=1.0、top_p=0.95、min_p=0.0；既有 NoThink 只算实验，不作为已通过的质量基线。外部 API 采用显式配置，不因接入服务而放宽角色／摘要语义要求。模型、量化或精度的替换须保持同一质量门槛；1B 不是自动兜底许可。直播恢复开发后，再分别验证所选窗口下的 LLM／TTS 共驻、冷／热启动和持续运行；互斥加载不能冒称共驻，未准入时保留文本工作。

以下保留既有候选与设备实验身份，不把历史配置固定成所有后续窗口的下限。Qwen3.5-2B 使用 Bartowski 社区 Q4_K_M 量化、q8 KV，官方原生 262144，比较时本项目实际配置 131072；不附带视觉 encoder／mmproj，不由模型名称推断已有图像工作流。固定用例中它的短句更快，但回忆／摘要出现更明显事实错配，不替换 MiniCPM5-2B 默认；参数、Think 与模板条件不同，不能当纯模型因果比较。证据见[候选结果](../../evaluations/results/qwen35-2b-single-system-seed42.json)。

MiniCPM 自身曾在文本模式、TTS 不驻留时通过精确 120000 输入 tokens／4 输出、131072 窗口及未截断核验，约 661 秒；这只验证该配置的输入容量，不验证召回质量或其他配置。正式精简 TTS 已完成过冷预热，但历史设备的 live 冷加载仍在 Mini KV 分配失败，live 长输入和两小时／100句未开始，不能宣称稳定共驻；失败与恢复记录见[正式链路设备证据](../../evaluations/results/compact-live-runtime-20261029.json)。延期保留这些失败和未完成边界，不把直播当作本次非直播交付门槛。

同题集成来源检索后，两者都答全：MiniCPM使用一次补答约70秒，Qwen无需补答约32秒；原始摘要中的事实错配仍存在，这证明外围证据层能补该例，不证明模型原始记忆已改善。见[MiniCPM结果](../../evaluations/results/grounded-minicpm5-2b-seed42.json)、[Qwen结果](../../evaluations/results/grounded-qwen35-2b-seed42.json)。Qwen已通过一次128K配置下的LLM／TTS共驻和两条非静音动态合成，短程观测不代表长输入或长时稳定；MiniCPM共驻合成仍有明显换页及CUDA分配失败，不能当作稳定直播Profile。见[Qwen资源结果](../../evaluations/results/qwen35-2b-live-resource-4b8cadf.json)。这些测量中其他两个项目服务均已停止，不外推为与其他服务同时运行的结果。

Qwen候选在实际131072窗口、TTS已合成预热且同进程驻留时，约207秒完整接收120000输入tokens并输出4 tokens，未截断；最低可用内存约180MiB，有少量换页，无系统OOM。正常容器重启可恢复live及两进程。见[长输入与恢复结果](../../evaluations/results/qwen35-2b-live-context-f35d061.json)。独立浏览器闭环也已实际completed：短句LLM约4.0秒、动态TTS约5.0秒、非静音播放2.58秒；首轮观察脚本错误取消了生成，broker自动清理并重载LLM，修正观察器后的有效播放使用原TTS及重载LLM，不能拼接为同LLM进程长输入后的连续热态证明。见[播放结果](../../evaluations/results/qwen35-2b-browser-playback-f35d061.json)。两项证明容量接收及一次短句闭环，不证明长文召回或持续稳定，也不转移为MiniCPM的共驻证据。

候选源模型为[Qwen固定版本](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc)，量化来自[Bartowski固定版本](https://huggingface.co/bartowski/Qwen_Qwen3.5-2B-GGUF/tree/7d26695454df6de5fbcce2e58681e62dae06ce43)，不能冒称Qwen官方GGUF。下载按固定revision、大小、SHA256及真实GGUF架构／窗口验证，默认初始化只取所选模型；可显式准备候选而不切默认。

共驻仅表示权重可同时保留，不放开生成并发。默认 active_llm_generations=1、active_tts_jobs=1，需要时串行；恢复直播时，直播、评论和试听共享配额，直播高于离线工作。逻辑 subagent 不加载独立驻留模型；外部 API 接入不改变业务生成和提交的并发约束。

## 工作模式

配置模板默认 text，直播功能关闭；完成资源准入后可显式启用Genie／Mock直播，评论页和其他导航不会隐式改变资源模式或启用声音。使用本地 LLM 时，text 结束直播生产和待播任务、卸载本地 TTS，保留所选 LLM 给评论回复等工作；选择外部 LLM API 时可释放本地推理资源。视觉须另行显式配置，text 名称不表示默认模型具有图像能力。外部语音 API 不依赖本地 live 共驻，也不自动切换角色声音。

以下 live 与本地 GPT-SoVITS／Genie 规则用于恢复声音／直播的准入和现有实现维护，不表示已经通过验收。用户显式选择 live，须让所选本地 LLM 与 TTS 均就绪并共驻，仍受统一生成配额控制；启动／恢复直播先确认加载成功，失败时不启动直播任务。

切换经过同一资源锁，成功后原子保存 inference_broker.mode；加载、取消、断连或保存失败时尽力恢复原模式与原驻留，不能展示假成功。旧 exclusive 配置只作为 text 兼容输入。text 下不隐式加载本地动态 TTS；后续本地 GPT-SoVITS 合成请求要求先通过 live 准入，预录播放不需要 TTS 进程。前端展示切换等待、实际驻留进程与失败；控制权仅属于认证管理接口，不由观众消息或模型台词切换。

冷切直播时为降低 TTS 载入瞬间的峰值，先释放 LLM、等 TTS 完整就绪，再加载 LLM 并确认两者共驻；已热驻留的请求不反复卸载。失败／取消恢复也遵守资源顺序。该内部策略允许切换延迟，不擅自减少当前配置窗口或张量精度，仍须设备实测，不把策略存在当作共驻成功。

TTS生成器完全关闭后再执行上游已有的缓存清理，让已结束帧的临时张量释放；音频块、驻留权重、推理精度与解码步骤保持原样。冷加载／首句峰值与热态换页分别记录，短程两句成功不等于长时稳定，也不以一次冷启动换页推断整模型不可用。

本地语音减内存保留固定参考特征预计算和推理精简路线，也可独立比较轻量引擎或局部 INT8。数值保持路线保留当前 v2 声音、动态中文 BERT 和原采样／解码；轻量引擎、量化或精度变化须单独标明条件并重新评估质量，不能以关闭 BERT、换音色或缩小 LLM 窗口冒充相同配置／质量下的优化。参考特征缓存绑定音色版本、参考音频／文本／语言、模型权重、前端与实现版本，任何不匹配须拒绝复用，新 OC 各自建立缓存。

保留的正式本地 v2 后端使用固定参考 NPZ／JSON 缓存，常驻路径跳过 HuBERT，仅保留原始特征实际使用的 22 层 BERT backbone；目标中文仍执行同一前端和动态 BERT，AR／VITS 解码、权重精度与采样不改变。缓存缺失／失效时，由 TTS 监督进程启动固定 argv 的短命预计算子进程，确认退出后才加载精简 pipeline，不在常驻进程中加载后删除 HuBERT。后续直播冷启动先卸载 LLM，完成上述 TTS 加载和健康检查，再按当前配置窗口加载所选 LLM（默认 MiniCPM5-2B／131072）；预计算失败、取消或模型异常须失败并按既有模式回滚，不静默换成重型后端或互斥模式。

TTS报告就绪前，须在同进程中完成一条固定短中文、显式种子的完整非静音预热合成并结束清理，让动态中文前端及AR／VITS首次初始化发生在LLM未驻留阶段。预热结果丢弃，不产生台词／播放／对话记录，不改变外部请求的默认随机种子；预热失败不就绪。短句预热不替代更长／混语形状和整机峰值验证。

正式精简路径以nn.Module包装同一原生Text2SemanticDecoder，保留config、top_k和完整model.*权重键，严格加载而不忽略缺失／额外权重；不导入Lightning及未用于推理的准确率指标。删除仅限已核实的训练包装／训练指标初始化，不改变任何推理、注意力、采样或声学解码方法。旧版对照保持原包装，两个导入模式不得在同一进程混用；必须以真实权重、固定种子PCM及整机资源测量验证等价与收益。

既有正式精简实现与原版在相同暖机、单工作线程和调用顺序下，三句各两遍共 6 组 PCM 逐样本一致。原版自身重复也出现过不同 hash，且旧评估未暖机、线程条件不同；保留这些差异，不改旧 golden 或由此保证跨协议／任意重复都 bitwise 确定。具体条件、逐样本差和资源数据见[同条件设备对照](../../evaluations/results/compact-live-runtime-20261029.json)。

独立 Genie-TTS v2 CPU 筛查保留原声线／固定参考、原中文前端及每次动态计算的 22 层 FP32 BERT；原始加载与合法 external FP32／mmap／initializer 加载各完成 6/6 非静音合成。整机采样冷峰值从 5.972 降至 4.931 GiB，约 17.4%；这不是热态内存下限、LLM 共驻或音质一致的结论，也不是与本地 compact CUDA FP16 的匹配比较。Genie 自带采样、CPU 精度和未统一的随机协议须保留为差异；不替换默认。详见[独立内存筛查](../../evaluations/results/gpt-sovits-inference-floor-20261029.json)，原参考提取的一次性历史成本未在该筛查重跑，未测阶段保持未验证。

参考专用的HuBERT、librosa、音频解码和频谱依赖仅在原参考函数中按需导入，已有缓存的常驻路径不主动加载这些组件；音频读取工具仅保留逐字相同的load_audio／clean_path及必要依赖，不由此引入Gradio／Pandas界面和数据表框架。原参考运算与音频解码不改，缓存身份同时覆盖参考处理源文件，不能以绕过参考提取或改特征来省内存。

Operator配置tts.voice_id选择config/voices.json中的一个gpt_sovits音色，缺省取首个；单个TTS进程仅服务该固定参考。不同参考或文字／语言请求明确拒绝，不能串用声音。新OC配置独立voice_id／参考资产并在维护时重载TTS，缓存由相同预计算生命周期建立；不同时加载多个声音模型。运行中已校验输入的配置／文件身份变化必须拒绝旧pipeline并要求重载，完整内容hash在加载时校验，不能每句重复读GB级权重。

优化收益以整机共享RAM为主：相同外部服务状态、模型／KV／窗口和台词下，分别记录冷启动、首句、热态合成的最低MemAvailable及总RAM减可用量、进程RSS、换页与OOM；加载和预计算峰值也要计入，预计算单独标示一次性成本。CUDA张量／reserved和权重文件体积只作解释指标，CPU搬运不等于Jetson整机节省。相同种子下的特征／PCM一致可支持数值保持，量化或其他数值改变仍须试听和稳定性验收，不以文件压缩率推断可共驻。

Genie 优化的速度门槛以原正式compact GPT-SoVITS CUDA后端为基线，另保留Genie未优化条件以解释引擎自身开销；同设备／台词／暖机／调用顺序下比较逐句延迟、首句与RTF。保留90%生成吞吐对应耗时不超过基线的1/0.9；音频时长变化须单独报告。音质至少保留原来90%，须通过配对试听核对读文、音色、自然度与异常；无声检查或单一特征分数不得代替感知质量结论。数值改变候选未完成质量评估不自动提升为默认。固定参考预计算、动态目标中文特征、真实128K与安全边界仍须保持，不能以少算层、关闭BERT或互斥加载满足共驻要求。

### Genie本地后端契约

`tts.kind=gpt_sovits`继续表示既有v2音色格式；受限的`tts.local_backend`只接受`compact`或`genie_onnx_int8`，不是可执行路径。Genie复用原音色ID、权重和固定参考，新OC使用自己的音色与bundle；兼容API／预录保持各自路径，不以外部Provider绕开本地资源锁。旧compact保留作对照，Genie准备或推理失败不静默切回它。

Genie常驻路径仅加载CPU ORT：原checkpoint的22层中文BERT有效输出使用离线INT8图（132个常量投影整数MatMul），AR首步／续步各97个常量投影整数MatMul，动态注意力仍为浮点，VITS保持FP32。原tokenizer、文字规范化、word2ph与每条未知中文的动态特征保留；纯英文片段沿用原前端合法的零BERT，逐片段验证规范文本、音素、形状与有限值，含汉字的片段仍须动态非零特征。只导入Torch前端工具，不构造Torch BERT或驻留HuBERT。ORT session共享CPU arena，固定单请求和受控线程；转换／量化不在直播进程执行，准备成本与常驻冷峰分别记录。

固定上游Genie模板的AR正态随机采样与原GPT-SoVITS不等价；本地适配恢复Exp(1)、top_k=5、top_p=1、temperature=1、repetition_penalty=1.35、首步EOS屏蔽，以及罚分后greedy EOS检查。每请求seed控制独立PCG64分段／AR／VITS子流；VITS保留标准正态噪声与原0.5系数，不宣称等于Torch同seed序列或跨后端PCM一致。只支持已实现的固定图参数，其他值明确422；seed、原分段间隔可配置。每段至多500步，无停止标志耗尽、取消、空语义或非法token必须失败，不能把参考语义或半条WAV当完成。

输出恢复原v2后处理：各音频段仅在峰值超过1时归一化，各段尾部都追加`max(fragment_interval,0.01)`秒静音（含最后一段，默认0.3），按文本顺序拼接，再乘32768转int16。测量同时保留原声波时长、分段数、静音总时长、最终音频时长与真实合成耗时；加静音降低RTF不代表计算更快，旧无gap实验不改写为同协议结果。

永久bundle位于`runtime/tts/genie/<voice_id>`，首次离线安装原子创建，拒绝覆盖和路径链接；缺失时要求显式准备，不从实验目录临时凑模型。manifest绑定原权重、参考、前端／采样／量化实现及每个模型图和external数据的内容身份，冷加载完整校验，每句及健康检查仅查小stat，变化拒绝旧pipeline并要求重建／重载。`/docs`只报告实际backend、voice_id和bundle_identity；工作台先确认该身份，再使用含backend及bundle身份的音频缓存，不串用旧compact或其他OC的声音。

CPU／GPU共享RAM之外，设备的R36.4.7还存在[NVIDIA记录的分配限制](https://forums.developer.nvidia.com/t/intermittent-nvmapmemalloc-error-12-and-cuda-allocator-crash-during-pytorch-inference-on-jetson-orin-nano/349752)：MemAvailable充足不保证大块CUDA分配成功。只可对本项目固定模型目录中的干净文件作缓存建议，不全局drop_caches、不改系统／内核安全参数，不把换页维持或分配错误隐藏成共驻成功。

## 单容器封装与推理边界

目标为从适合 Jetson 的原始 CUDA／PyTorch 镜像派生一个 tuber-studio 镜像／容器，只保留运行所需组件，统一 Python／CUDA 环境，共享项目 runtime 挂载。容器内分离业务后端、受限 broker 和按需 llama／TTS 子进程；不默认部署 host Docker controller／独立 host venv，也不挂载 Docker.sock。

broker 仅执行固定、已验证的模型／TTS 子进程和允许的推理路径，校验路径、模型别名及真实窗口，不接受用户任意命令／路径。业务输入无进程执行权；内部调用使用独立 token，外部仅接触认证后的工作台／overlay。全局生成锁从加载／推理覆盖到完整响应缓冲结束，取消和失败须清理仍工作的子进程。

本地文本模式先结束非目标资源再加载所选 LLM；后续直播模式仅在准入后保持获准 LLM／TTS 驻留。UI 展示加载等待、活跃／睡眠、错误，并区分模型 native、配置窗口、服务实际窗口和容量验证来源；睡眠状态不决定可用上下文容量，API 配置声明也不等于本机验证。进程 supervisor、镜像构建、退出清理和设备重启恢复均需实际验收，不能从文件存在推断完成。

Jetson加载使用所固定llama.cpp的原生direct-io，避免读GGUF时额外积累文件缓存。启动前可对本项目runtime/models、runtime/tts/weights与pretrained中的模型文件提示释放干净页缓存；限定项目数据目录、关闭文件描述符，不删除权重或改变张量精度，不全局drop_caches、不修改系统内存策略。缓存提示是资源优化，不能替代真实分配／窗口检查；不据一次成功推断长期无换页。

## 设备与降级

| 形态 | 约束 |
| --- | --- |
| Orin Nano Super 8GB＋外部OBS | CPU／GPU共享8GB，NVMe放权重／媒体；无NVENC，不要求本机编码。 |
| Windows／Linux PC | 可开发Mock、角色、评论和单机功能，性能不直接外推Jetson。 |
| 16GB+ Jetson／其他GPU | 后续更大模型／声音，不改变业务接口。 |

ZRAM／SSD Swap 仅兜底，不以持续换页维持低延迟。过载依次暂停离线 GPU、合并普通消息并缩短回复、优先合法预录、限制主动话题、采用已授权且质量通过的轻量 Profile，保留人工接管；不无限增队列／超时或静默缩小操作者当前配置窗口。操作者可显式选择较小窗口，界面与容量证据须反映实际值。

模型判别实验固定同一批角色短句、中文梗、注入抵抗、跨轮记忆／摘要用例，对比质量、延迟和峰值内存，并按实际 Provider／窗口标注条件。后续声音／直播验收再覆盖冷／热启动、连续 100 句、TTS 交替／共驻，以 15–30 条短句人工试听并记录首包延迟、RTF、稳定性和许可。普通短句实时性与长输入吞吐分别报告；声音延期不减弱当前文本角色、来源和摘要校验。

| 验收 ID | 通过条件 |
| --- | --- |
| AC-07 | 当前本地／API LLM 返回完整合法结构，非法情绪／动作被拒绝并可见失败／回退；后续声音恢复时再验收动态 TTS／预录，不作为非直播评论的前置。 |
| AC-08 | 声音接入时切音色不串缓存，API 音色不自动替换角色本地声线，商业配置不直接启用开发专用声音。 |

实现依据 [MiniCPM官方llama.cpp部署](https://github.com/OpenBMB/MiniCPM/blob/main/docs/deployment/llama_cpp.md)、[GPT-SoVITS API v2](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)；设备依据 [Orin Nano Super](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/) 与 [Orin Nano软件编码](https://docs.nvidia.com/jetson/archives/r36.4/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html)。接入时核对版本兼容和许可。
