# 模型、声音与资源契约

本规范维护 Provider、声音身份／缓存、设备 Profile 和推理封装。条目为验收目标；实验状态不写成已经交付。

## Provider 与声音

| ID | 原优先级 | 要求 |
| --- | --- | --- |
| AUDIO-01 | P0 | MUST 可替换 LLMProvider，初始候选 MiniCPM 1B／2B GGUF＋llama.cpp，业务接口不绑定模型名。 |
| AUDIO-02 | P0 | MUST 校验模型 schema，仅接受 spoken_text、有限 emotion、有限 animation_id、可选 reply_to，非法字段无工具权。 |
| AUDIO-03 | P0 | MUST TTSProvider 与预录路径，优先验证 GPT-SoVITS v2 现成权重，其他候选按设备证据选择。 |
| AUDIO-04 | P1 | MUST voice_profile 绑定模型／权重版本、路径、参考文本／音频和许可；切角色不串声线／缓存。 |
| AUDIO-05 | P0 | MUST 预计算固定参考特征并精简正式推理后端，键含模型、音色版本、参考音频／文字／语言及实现身份，修改失效；SHOULD 缓存固定台词。 |
| AUDIO-06 | P0 | MUST 预制叫声、感谢等音效直通播放，不调 LLM／TTS。 |
| AUDIO-07 | P1 | MUST 音频／字幕／GIF 共用播放事件或时间戳，合成完成不等于播放完成。 |
| AUDIO-08 | P1 | SHOULD 句长上限、标点切分、合成超时／回退、音量归一和防爆音。 |
| AUDIO-09 | P1 | MUST 展示来源、许可和约束，商业模式不默认启用仅非商用／未授权音色。 |

最小语义接口为 LLMProvider.generate(context,constraints) → StructuredReply，TTSProvider.synthesize(text,voice,options) → AudioArtifact/AudioStream。提交草稿／TTS 前校验完整结束、字数、控制字符、情绪／动作枚举及当前源事件引用；原样复述输入不算有效角色回答。输出不能直接成为 shell、文件操作、HTTP 工具、发布或支付。

Think模式通过原生chat template和reasoning_format=deepseek分离推理与正式内容；只解析message.content进入台词／摘要，不将reasoning_content播报、存入对话或当作回复JSON，不靠字符串裁掉思考标签补成功。模板默认正式输出预算1024 tokens，包含模型推理与最终JSON，台词仍受角色字数上限约束；摘要预算独立，未完整stop即失败。延迟须实测，不能从NoThink短句推断Think实时性。

预录限于允许资产目录，字幕对应实际录音，不冒充动态台词。缓存还覆盖 Provider 和参考音频内容，防止同路径换文件串用。合成失败按配置退回预录／静音且显示原因，不伪造成功。动态TTS的全静音结果必须拒绝，即使上游返回HTTP200；缓存命中也检查，静音缓存删除后重新合成，不能算成功播报。音色持久语义含 ID、Provider、版本、权重、参考资产、许可和有效参数；音频、训练数据、权重及下游用途分别核对，原型允许现有内容不授额外商用权。

## Profile 选择与准入

当前授权基线是 2B、真实至少128K和动态TTS；容量与压缩由上下文规范维护。能输出合法 JSON 不等于角色／摘要质量合格，不能未经语义比较自动退成1B。

首版接受明确记录的偶发遗漏，单次漏地点不等于整个模型不可用；该次明确问了地点的回答仍须记为不完整。选型综合实际任务质量、事实错配、延迟和RAM，不把小集合字面分数当自动淘汰器。先比较Qwen 2B等获准候选，若实际表现更差则保留MiniCPM5；不得为修分数删证据、改gold或宣称更短就是更好。来源检索及最多一次补答由角色／记忆规范维护，不改Codex压缩核心。

1. 优先评估 MiniCPM5-2B Q4_K_M 权重＋q4 KV＋128K 与 TTS 共驻。通过同一角色／摘要质量与共享内存门槛后可作为默认共驻；冷／热启动和持续运行仍按相应验收要求分别验证，不能由默认选型推断通过。
2. 2B按官方Think参数做质量准入，temperature=1.0、top_p=0.95、min_p=0.0；既有NoThink只算实验，不作为已通过的质量基线。实验未完成不宣称q4 KV共驻交付。未通过直播准入时保留文本模式并显示失败，不把互斥加载冒称直播共驻。
3. 后续再评估1B质量与共驻收益，同一语义门槛通过才可考虑替换；1B不是默认兜底许可。

当前已确认继续采用MiniCPM5-2B Q4_K_M权重、q4 KV和131072窗口，Qwen保留为比较候选。Qwen3.5-2B已作候选比较：Bartowski社区Q4_K_M量化、q8 KV，官方原生262144，本项目实际配置131072；不附带视觉encoder／mmproj，不由模型名称推断已有图像工作流。固定用例中它的短句更快，但回忆／摘要出现更明显事实错配，不替换默认；参数、Think与模板条件不同，不能当纯模型因果比较。证据见[候选结果](../../evaluations/results/qwen35-2b-single-system-seed42.json)。

同题集成来源检索后，两者都答全：MiniCPM使用一次补答约70秒，Qwen无需补答约32秒；原始摘要中的事实错配仍存在，这证明外围证据层能补该例，不证明模型原始记忆已改善。见[MiniCPM结果](../../evaluations/results/grounded-minicpm5-2b-seed42.json)、[Qwen结果](../../evaluations/results/grounded-qwen35-2b-seed42.json)。Qwen已通过一次128K配置下的LLM／TTS共驻和两条非静音动态合成，短程观测不代表长输入或长时稳定；MiniCPM共驻合成仍有明显换页及CUDA分配失败，不能当作稳定直播Profile。见[Qwen资源结果](../../evaluations/results/qwen35-2b-live-resource-4b8cadf.json)。这些测量中其他两个项目服务均已停止，不外推为与其他服务同时运行的结果。

Qwen候选在实际131072窗口、TTS已合成预热且同进程驻留时，约207秒完整接收120000输入tokens并输出4 tokens，未截断；最低可用内存约180MiB，有少量换页，无系统OOM。正常容器重启可恢复live及两进程。见[长输入与恢复结果](../../evaluations/results/qwen35-2b-live-context-f35d061.json)。独立浏览器闭环也已实际completed：短句LLM约4.0秒、动态TTS约5.0秒、非静音播放2.58秒；首轮观察脚本错误取消了生成，broker自动清理并重载LLM，修正观察器后的有效播放使用原TTS及重载LLM，不能拼接为同LLM进程长输入后的连续热态证明。见[播放结果](../../evaluations/results/qwen35-2b-browser-playback-f35d061.json)。两项证明容量接收及一次短句闭环，不证明长文召回或持续稳定，也不转移为MiniCPM的共驻证据。

候选源模型为[Qwen固定版本](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc)，量化来自[Bartowski固定版本](https://huggingface.co/bartowski/Qwen_Qwen3.5-2B-GGUF/tree/7d26695454df6de5fbcce2e58681e62dae06ce43)，不能冒称Qwen官方GGUF。下载按固定revision、大小、SHA256及真实GGUF架构／窗口验证，默认初始化只取所选模型；可显式准备候选而不切默认。

共驻仅表示权重可同时保留，不放开生成并发。默认 active_llm_generations=1、active_tts_jobs=1，需要时串行；直播、评论和试听共享配额，直播高于离线工作。逻辑 subagent 不加载独立驻留模型。

## 工作模式

用户显式选择text（文本／图像资源工作模式）或live（直播），默认text；模式与导航页面独立，查看评论页不会隐式切换。text结束直播生产和待播任务，卸载TTS，保留所选LLM给评论回复等工作；该资源模式名称不表示已实现新的图像能力。live须让所选LLM与TTS均就绪并共驻，仍串行使用GPU。启动／恢复直播先确认live加载成功，失败时不启动直播任务。

切换经过同一资源锁，成功后原子保存inference_broker.mode；加载、取消、断连或保存失败时尽力恢复原模式与原驻留，不能展示假成功。旧exclusive配置只作为text兼容输入。text下不隐式加载动态TTS；实际合成请求明确要求先切live，预录播放不需要TTS进程。前端展示切换等待、实际驻留进程与失败；控制权仅属于认证管理接口，不由观众消息或模型台词切换。

冷切直播时为降低TTS载入瞬间的峰值，先释放LLM、等TTS完整就绪，再加载LLM并确认两者共驻；已热驻留的请求不反复卸载。失败／取消恢复也遵守资源顺序。该内部策略允许切换延迟，不减少真实窗口或张量精度，仍须设备实测，不把策略存在当作共驻成功。

TTS生成器完全关闭后再执行上游已有的缓存清理，让已结束帧的临时张量释放；音频块、驻留权重、推理精度与解码步骤保持原样。冷加载／首句峰值与热态换页分别记录，短程两句成功不等于长时稳定，也不以一次冷启动换页推断整模型不可用。

语音减内存按固定参考特征预计算、精简推理后端、局部INT8的顺序评估。保持当前v2声音、动态中文BERT特征和原采样／解码条件，先验证不改变数值的精简；不能以关闭BERT、换音色或减少LLM真实窗口冒充相同质量下的优化。参考特征缓存绑定音色版本、参考音频／文本／语言、模型权重、前端与实现版本，任何不匹配须拒绝复用，新OC各自建立缓存。

正式v2后端使用固定参考NPZ／JSON缓存，常驻路径跳过HuBERT，仅保留原始特征实际使用的22层BERT backbone；目标中文仍执行同一前端和动态BERT，AR／VITS解码、权重精度与采样不改变。缓存缺失／失效时，由TTS监督进程启动固定argv的短命预计算子进程，确认退出后才加载精简pipeline，不在常驻进程中加载后删除HuBERT。直播冷启动先卸载LLM，完成上述TTS加载和健康检查，再加载所选MiniCPM5／131072；预计算失败、取消或模型异常须失败并按既有模式回滚，不静默换成重型后端或互斥模式。

TTS报告就绪前，须在同进程中完成一条固定短中文、显式种子的完整非静音预热合成并结束清理，让动态中文前端及AR／VITS首次初始化发生在LLM未驻留阶段。预热结果丢弃，不产生台词／播放／对话记录，不改变外部请求的默认随机种子；预热失败不就绪。短句预热不替代更长／混语形状和整机峰值验证。

Operator配置tts.voice_id选择config/voices.json中的一个gpt_sovits音色，缺省取首个；单个TTS进程仅服务该固定参考。不同参考或文字／语言请求明确拒绝，不能串用声音。新OC配置独立voice_id／参考资产并在维护时重载TTS，缓存由相同预计算生命周期建立；不同时加载多个声音模型。运行中已校验输入的配置／文件身份变化必须拒绝旧pipeline并要求重载，完整内容hash在加载时校验，不能每句重复读GB级权重。

优化收益以整机共享RAM为主：相同外部服务状态、模型／KV／窗口和台词下，分别记录冷启动、首句、热态合成的最低MemAvailable及总RAM减可用量、进程RSS、换页与OOM；加载和预计算峰值也要计入，预计算单独标示一次性成本。CUDA张量／reserved和权重文件体积只作解释指标，CPU搬运不等于Jetson整机节省。相同种子下的特征／PCM一致可支持数值保持，量化或其他数值改变仍须试听和稳定性验收，不以文件压缩率推断可共驻。

## 单容器封装与推理边界

目标为从适合 Jetson 的原始 CUDA／PyTorch 镜像派生一个 tuber-studio 镜像／容器，只保留运行所需组件，统一 Python／CUDA 环境，共享项目 runtime 挂载。容器内分离业务后端、受限 broker 和按需 llama／TTS 子进程；不默认部署 host Docker controller／独立 host venv，也不挂载 Docker.sock。

broker 仅执行固定、已验证的模型／TTS 子进程和允许的推理路径，校验路径、模型别名及真实窗口，不接受用户任意命令／路径。业务输入无进程执行权；内部调用使用独立 token，外部仅接触认证后的工作台／overlay。全局生成锁从加载／推理覆盖到完整响应缓冲结束，取消和失败须清理仍工作的子进程。

文本模式先结束非目标资源再加载所选LLM；直播模式仅在准入后保持获准LLM／TTS驻留。UI展示加载等待、活跃／睡眠、错误；已验证上下文容量与驻留状态分开，睡眠不等于低于128K。进程 supervisor、镜像构建、退出清理和设备重启恢复均需实际验收，不能从文件存在推断完成。

Jetson加载使用所固定llama.cpp的原生direct-io，避免读GGUF时额外积累文件缓存。启动前可对本项目runtime/models、runtime/tts/weights与pretrained中的模型文件提示释放干净页缓存；限定项目数据目录、关闭文件描述符，不删除权重或改变张量精度，不全局drop_caches、不修改系统内存策略。缓存提示是资源优化，不能替代真实分配／窗口检查；不据一次成功推断长期无换页。

## 设备与降级

| 形态 | 约束 |
| --- | --- |
| Orin Nano Super 8GB＋外部OBS | CPU／GPU共享8GB，NVMe放权重／媒体；无NVENC，不要求本机编码。 |
| Windows／Linux PC | 可开发Mock、角色、评论和单机功能，性能不直接外推Jetson。 |
| 16GB+ Jetson／其他GPU | 后续更大模型／声音，不改变业务接口。 |

ZRAM／SSD Swap仅兜底，不以持续换页维持低延迟。过载依次暂停离线GPU、合并普通消息并缩短回复、优先合法预录、限制主动话题、采用已授权且质量通过的轻量Profile，保留人工接管；不无限增队列／超时或减少真实128K。

判别实验固定同一批角色短句、中文梗、注入抵抗、跨轮记忆／摘要用例，对比质量、延迟和峰值内存；覆盖冷／热启动、连续100句、TTS交替／共驻。声音用15–30条短句人工试听，记首包延迟、RTF、稳定性和许可。普通短句实时性与长输入吞吐分别报告。

| 验收 ID | 通过条件 |
| --- | --- |
| AC-07 | 真实MiniCPM合法结构，非法情绪／动作被拒绝并可见回退；动态TTS／预录可用。 |
| AC-08 | 切音色不串缓存，商业配置不直接启用开发专用声音。 |

实现依据 [MiniCPM官方llama.cpp部署](https://github.com/OpenBMB/MiniCPM/blob/main/docs/deployment/llama_cpp.md)、[GPT-SoVITS API v2](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)；设备依据 [Orin Nano Super](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/) 与 [Orin Nano软件编码](https://docs.nvidia.com/jetson/archives/r36.4/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html)。接入时核对版本兼容和许可。
