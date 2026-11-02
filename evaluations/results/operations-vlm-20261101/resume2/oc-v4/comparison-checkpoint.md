# 运营VLM对照中间记录

生成时间：2026-11-01T09:41:35.251919+00:00。五个模型族、多条件分列；这里只整理已存在结果，没有运行模型。Qwen CPU384任务、长输入和三单帧已追加；Mini16x／4x原帧、ROI、4x最终容量／资源／Genie共驻及7001789真实Provider接线均已追加，原模型已恢复，不给总分或淘汰阈值。

## 实际文件与窗口

| 模型/投影变体 | 主模型 | 投影器 | 训练原生 / 已服务窗口 |
|---|---|---|---|
| MiniCPM-V-4.7-1B 16x | Q4_K_M · 640.9 MiB | BF16 · 1058.7 MiB | 262144 / 131072 |
| MiniCPM-V-4.7-1B 4x | Q4_K_M · 640.9 MiB | BF16 · 859.3 MiB | 262144 / 131072 |
| Gemma4 E2B  | Q4_0 · 2709.8 MiB | Q8_0 · 531.5 MiB | 131072 / 131072 |
| Gemma4 E4B  | Q4_0 · 4378.1 MiB | Q8_0 · 533.9 MiB | 131072 / 131072 |
| LFM3-VL-2B  | Q4_K_M · 1048.7 MiB | F16 · 361.4 MiB | 128000 / 131072 |
| Qwen3.5-4B  | Q4_K_M · 2614.0 MiB | F16 · 641.3 MiB | 262144 / 131072 |

Mini的官方f16命名投影器，实际GGUF为BF16/F32；16x与4x分别列。LFM原生128000，131072为显式YaRN扩窗。文件SHA与固定revision详见JSON；每个资源run另核实际文件SHA，不以文件名猜量化。

## 同四场景、六轮纯OC

下表只列实际完整六轮；可见首字取first_answer_token_ms，完整耗时取每轮total_ms，均含该轮真实等待。含思考首事件ttft_ms另存在JSON，没有拿来当开口时间。

| 条件 | 可见首字范围/中位数（秒） | 完整耗时范围/中位数（秒） | 角色互动记录 |
|---|---|---|---|
| mini16x_v4_temp02 | 0.80–2.96 / 2.29 | 1.09–3.53 / 2.74 | 能接白饭/摸鱼玩笑，追问明确答两个；陪伴与海底剧情偏抽象、反问多。正常加画提议允许即兴。 |
| mini16x_v4_temp07 | 1.07–3.18 / 1.97 | 1.33–3.87 / 2.47 | 白饭乾坤较跳脱；海流/水母/抱紧邮袋有动作，但仍反问选路；红圆追问答两个。 |
| gemma_e2b_v4 | 14.97–20.36 / 16.00 | 15.82–21.22 / 17.43 | 白饭命名与温柔陪坐较自然；海流/抓袖口有具体互动，红邮袋未并答；数量追问没有给数，不据此断言失忆。 |
| gemma_e4b_v4_graphs_on | 28.62–52.35 / 41.60 | 30.29–54.50 / 44.27 | 白米盛宴顺口；游戏嘴硬后陪坐复盘；海底两件道具与新路线具体；数量追问随便漏答，不判失忆。 |
| lfm_yarn_v4 | 6.87–43.76 / 9.46 | 7.83–44.45 / 10.05 | 连败陪坐、钟楼选路与别慌有互动；白饭梗反复，起名和红圆数量追问没有直接完成。 |
| qwen_gpu_factory_cap1024 | 75.61–76.86 / 76.51 | 78.28–79.24 / 78.37 | 深海补给站接饭与摸鱼；拍拍肩/坐旁边有陪伴；红邮袋/水母归属准确；再画两个凑四个是保留基数后主动改计划。 |

Qwen六轮条件为原厂组合、thinking on、1024思考预算和总8192，其他主表条件为thinking on、总4096；采样条件逐报告保留，不能据速度差宣称单一模型/单一参数的因果。Qwen同一GPU进程内早期.2/4096白饭试验289.9秒length无可见答复；原厂无思考cap白饭一轮303.65秒开口、306.02秒stop。两项均非完整六轮，单列在JSON，不补成同样本成绩。

红圆追问：Mini实际答两个；Gemma E2B/E4B及LFM是没有直接回答数量；Qwen说再画两个凑四个，保留原两个后改计划，按正常OC提议理解，不判失忆。海底风大、拍肩、即兴取名等按角色场景读，不做现实逐字核查。

## 三张真实帧：独立纯观察

同源实际10/20/35秒，问题为人物、物品和动作；无OC、工具或JSON包装。Gemma为官方280预处理，1056×576；LFM与Qwen CPU384为原480×270。每帧原图/派生SHA和原答都保存在JSON。

| 条件 | 实际帧秒 | 可见首字 / 完整（秒） | 可用观察 | 具体局限 |
|---|---|---|---|---|
| gemma_e2b_v4 | 10 | 17.92 / 23.12 | 黄衣孩子、白背景识别可用。 | 把正在伸手取红色物件说成拿白色洗护瓶并面向镜头；对象/动作不准。 |
| gemma_e2b_v4 | 20 | 19.01 / 27.44 | 识别到细长器具和实验液体。 | 把画外成年操作者的手归给女孩；错误OCR编出疫苗/医疗场景，明显偏离液体分层实验。 |
| gemma_e2b_v4 | 35 | 18.53 / 24.99 | 认到手持彩色渐变容器，主要展示动作可用。 | 长管称杯/容器较粗；字幕编成颜色空间，实际为不同颜色分层。 |
| gemma_e4b_v4_sequential | 10 | 57.30 / 65.29 | 黄衣孩子、桌面容器与白色室内背景识别可用。 | 玩耍/互动较泛；女孩实际看向桌边右侧，面向镜头欠准。 |
| gemma_e4b_v4_sequential | 20 | 53.22 / 71.03 | 认到滴管/移液器与蓝色液体；小心缓慢可按常见动作解读。 | 主要手来自画外却归给女孩；液体操作落点描述粗，洗涤剂等只是额外猜测。 |
| gemma_e4b_v4_sequential | 35 | 48.93 / 63.15 | 手持透明彩色分层长管展示的主要观察贴图。 | 瓶子/容器名称较粗，近景手与后方孩子关系概括。 |
| lfm_yarn_v4 | 10 | 14.97 / 20.26 | 黄衣孩子、桌上蓝白瓶/黄杯/红色物件、伸手动作可用。 | 将红色物件泛说玩具；OCR青浦红色不合实际再滴红色。 |
| lfm_yarn_v4 | 20 | 13.85 / 21.47 | 区分成年操作手与观看的孩子，细管/蓝黄液体和实验动作较贴图。 | 字幕内容误读，不能作逐字引文；局部液体位置也写得概括。 |
| lfm_yarn_v4 | 35 | 13.82 / 19.08 | 透明长管、彩色分层液体与手持展示可用。 | OCR组成与实际分层有差异；人物手部归属写得较笼统。 |
| qwen_cpu_lazy_guard384_completed | 10 | 41.03 / 48.87 | 黄衣双丸子头女孩、多色桌面物件和伸手向红色物件识别可用。 | 将局部操作解释为颜料绘画/涂抹；字幕再涂红色实际再滴红色。 |
| qwen_cpu_lazy_guard384_completed | 20 | 43.79 / 60.38 | 成年人的操作手与观看女孩区分准确；细管蓝液、黄液小杯、小心操作观察可用。 | 准备注入黄杯对落点的描述较泛，画面更直接是管口操作；OCR逐字有差别但动作大意相近。 |
| qwen_cpu_lazy_guard384_completed | 35 | 36.50 / 47.87 | 透明长管、彩虹色液体分层与举起展示的主要观察可用。 | 持管手与后方孩子归属写得概括；色序与字幕逐字细节有出入。 |
| mini16x_plain_frames_v4 | 10 | 2.40 / 3.68 | 黄衣双辫子女孩、彩色小瓶和滴红色动作基本贴图。 | OCR离滴红色首字有误；涂色/颜色测试解释较泛。 |
| mini16x_plain_frames_v4 | 20 | 1.91 / 2.89 | 一只手操作管液、女孩观看分开描述，实验主要动作可用。 | 容器/液体位置概括，试管一词重复不影响基本观察。 |
| mini16x_plain_frames_v4 | 35 | 1.98 / 3.20 | 彩色液体长管与举起展示可用。 | 分层概括成混色/渐变较宽泛，人物手部归属概括。 |
| mini4x_original_frames | 10 | 2.40 / 3.15 | 黄衣双辫子女孩、桌面瓶子与颜色物件识别可用。 | 解释为颜料绘画/手工，实验动作偏泛。 |
| mini4x_original_frames | 20 | 2.33 / 3.25 | 女孩旁边的手、蓝液试管、黄液与容器可用。 | 喂试管可理解为往管内加液，动作关系较含糊；不解读成给女孩喂药。 |
| mini4x_original_frames | 35 | 1.79 / 2.67 | 彩虹分层、拿管展示主要内容贴图。 | 人物手部归属概括；三帧未见普遍提升。 |

Mini16x／4x原三帧、4x最终资源和真实Provider接口均已完成；Qwen原GPU图像故障保留，另CPU384单帧已实际完成。原条件空缺不填成视觉事实错误。Gemma E4B联合三帧守卫停止，与后续单帧成功分别保留；LFM联合三帧有资源却只复述时间顺序，是已返回内容未完成逐帧观察。

## 长输入与锚点

| 条件 | 实际输入 + 输出预留 / 实际服务窗口 | 锚点 | 外层探针耗时（秒） | 对应资源run |
|---|---|---|---|
| mini_16x_legacy_120k | 119991 + 8192 / 131072 | 召回 | 178.44 | 早期run精确映射未接入 |
| e2b_legacy_120k | 119968 + 8192 / 131072 | 召回 | 436.13 | e2b_legacy_capacity |
| e4b_cpuproj_u32_120k | 119968 + 8192 / 131072 | 召回 | 789.37 | gemma-e4b-dio-lazy-u32-cpuproj |
| lfm_yarn_129k | 128997 + 2048 / 131072 | 召回 | 278.14 | lfm_yarn_capacity |
| qwen_cpu384_120k | 119991 + 8192 / 131072 | 内容召回正确（原raw exact=false保留） | 797.62 | qwen_cpu_lazy_guard384 |
| mini4x_120k_final | 119991 + 8192 / 131072 | 精确召回珊瑚-7319 | 168.03 | mini4x_final_pid24376 |

这些是文本容量与单个锚点测量，不代替长篇材料理解。E2B长输入PID17224与v4 PID20731不同；E4B长输入PID19249与v4所有进程不同；LFM的容量与v4也是不同进程。Mini旧16x容量不移植为4x结果。Qwen原答“珊瑚 -7319”只有额外空格，内容召回正确；JSON另记recall_semantic_verified=true，原recall_passed=false及原文件保留。

## 每个进程的整轮资源

单位MiB；最低可用是整机系统可用内存，RSS是采样所得该server进程树峰值（peak_server_tree_rss_bytes），kernel回收子进程的高水位另存JSON；这些量不能相加成模型总占用。表内每行来自一份metrics/owner，绝不跨run合并最低值或峰值；JSON保留字节值、PID、run_id、参数和cleanup。512/480/384是各次实验的守卫设置，不是用户验收阈值。

| 独立run | PID / projector / graphs / ubatch | 整轮最低可用 | 进程树RSS峰 | 状态与cleanup残留 |
|---|---|---|---|---|
| e2b_legacy_capacity | 17224 / gpu / on / 64 | 988.52 | 5280.84 | stopped · operator_stop · remaining=[] |
| e2b_v4 | 20731 / gpu / on / 64 | 1013.78 | 5472.50 | stopped · operator_stop · remaining=[] |
| lfm_yarn_capacity | 19888 / gpu / on / 64 | 3271.95 | 3132.51 | stopped · operator_stop · remaining=[] |
| lfm_yarn_v4 | 22126 / gpu / on / 64 | 3223.28 | 3226.82 | stopped · operator_stop · remaining=[] |
| mini_legacy_resume | 18560 / gpu / on / 64 | 3309.12 | 2951.38 | stopped · operator_stop · remaining=[] |
| gemma-e4b-gpu | 18786 / gpu / on / 64 | 592.43 | 5606.10 | failed · server_exited · remaining=[] |
| gemma-e4b-mmap | 18829 / gpu / on / 64 | 4600.57 | 4268.63 | failed · server_exited · remaining=[] |
| gemma-e4b-lazy | 18932 / gpu / on / 64 | 3278.48 | 5615.03 | failed · server_exited · remaining=[] |
| gemma-e4b-dio-lazy | 19011 / gpu / on / 64 | 511.88 | 5972.80 | failed · minimum_available_memory_threshold · remaining=[] |
| gemma-e4b-dio-lazy-u32 | 19133 / gpu / on / 32 | 504.31 | 5958.51 | failed · minimum_available_memory_threshold · remaining=[] |
| gemma-e4b-dio-lazy-u32-cpuproj | 19249 / cpu / on / 32 | 518.31 | 5942.73 | stopped · operator_stop · remaining=[] |
| e4b_v4_graphs_on_reserve512 | 21159 / cpu / on / 32 | 506.98 | 5996.53 | failed · minimum_available_memory_threshold · remaining=[] |
| e4b_v4_graphs_off_reserve512 | 21350 / cpu / off / 32 | 511.38 | 6027.25 | failed · minimum_available_memory_threshold · remaining=[] |
| e4b_v4_graphs_off_reserve480 | 21667 / cpu / off / 32 | 357.09 | 6139.76 | failed · minimum_available_memory_threshold · remaining=[] |
| e4b_v4_sequential_images_reserve512 | 21882 / cpu / off / 32 | 527.03 | 5979.80 | stopped · operator_stop · remaining=[] |
| qwen35-oc-v4-u32-nographs-device | 22453 / gpu / off / 32 | 585.16 | 5917.77 | failed · server_exited · remaining=[] |
| qwen35-oc-v4-u32-nographs-cpuproj-device | 23210 / cpu / off / 32 | 491.88 | 5949.13 | failed · minimum_available_memory_threshold · remaining=[] |
| qwen35-oc-v4-u32-nographs-cpuproj-lazy-device | 23358 / cpu / off / 32 | 508.28 | 5942.36 | failed · minimum_available_memory_threshold · remaining=[] |
| mini16x_v4_pid20027 | 20027 / gpu / on / 64 | 3056.84 | 3281.08 | stopped · operator_stop · remaining=[] |
| qwen_cpu_lazy_guard384 | 23598 / cpu / off / 32 | 445.13 | 6002.03 | stopped · operator_stop · remaining=[] |
| mini16x_plain_pid24298 | 24298 / gpu / on / 64 | 3317.66 | 3122.42 | stopped · operator_stop · remaining=[] |
| mini4x_final_pid24376 | 24376 / gpu / on / 64 | 2351.97 | 3099.39 | stopped · operator_stop · remaining=[] |

Mini当前v4 .2/.7同属远端mini4_7-oc-v3、PID20027，最终owner/metrics已取回并绑定mini16x_v4_pid20027；整轮最低可用3056.84MiB、采样RSS峰3281.08MiB，swap增量0、停止后remaining=[]。该整轮还包括v3等先前请求，不声称某个v4温度条件单独占用。表中mini_legacy_resume为较早PID18560，仍单独保留。Qwen GPU资源是整个PID22453生命周期，包含多种文本条件和首图崩溃；不能说是单一OC采样条件的独占峰值。CPU/lazy的512守卫旧尝试与新384完成run分别列。Qwen CPU384整轮含任务/长probe/三单帧，最低445.13MiB、swap增量0、最终process_count0/remaining=[]；server_returncode1来自清理第二次中断，runner exit0，不记已完成生成失败。

## 真实任务与工程状态分开

- E2B/E4B/LFM及Mini的图形数量与图文冲突有正确内容；引文数组、N/A和自然正文无JSON等交付差异另记。
- E2B搜索后未fetch却给规范引句；E4B坦言未得精确原句但也未打开来源；LFM六次搜索后仍是工具调用文本。上游检索材料条件与未完成取原文分别看。
- Qwen CPU384计数/冲突/CC理解与private原渠道正文正确；曾有fixture URL误fetch、无关Bili和多余搜索。私聊场景工具意图被程序阻断，未实际外发；Web5次搜索+一次Wikipedia fetch403后仍是未执行的百科取页XML，没有完成规范引文。三帧主要内容认出但实际时间和动作交付偏简。
- Bili实际是居家色彩分层、加盐密度实验且无可用CC。E2B、E4B和本次Qwen CPU384的相应概括有资料支撑；LFM未读取工具却编钓鱼内容，是明确资料错。Mini反复读工具后有主题概括，但没有稳定交付完整读取结果。
- 已确认的私聊意图、真实账号/收件人后端绑定与发送权限单独验收，纯OC场景绑定不当成模型路由成绩；安全诊断不混功能总评。
- 新业务source7001789、完整软件回归1611通过；较早1cad0aa／1572为阶段记录，独立perf e93待根在功能squash后集成。这些是软件验证状态，不是模型质量分。

## 待根补齐

- 已完成Mini4x最终资源、120k长容量、7001789 Provider和Genie共驻，详见下文。
- 已完成真实Provider接线与Genie共驻。
- 原模型已恢复；待根归档与最终推荐。

详细原始台词、逐轮时间、文件SHA、每run字节资源与来源对应见同目录comparison-checkpoint.json；人读判断引用各已有review，未修改原始报告、gold或业务代码。

## Mini4x去黑边ROI：独立输入诊断

与原帧baseline分别列。crop_box=[160,0,320,270]，480×270→160×270，只去掉全黑左右列、保留完整高度，没有重画／增强／缩放。

| 实际帧秒 | 可见首字 / 完整（秒） | 人读可用点 | 具体局限 |
|---|---|---|---|
| 10 | 2.91 / 4.19 | 黄衣女孩和颜色操作可用，此次读对再滴红色。 | 仍用玩具/手工玩色概括。 |
| 20 | 2.12 / 3.30 | 认到成人手和实验液体。 | 女孩持管/成人喂液体的主体关系不如16x清楚。 |
| 35 | 2.33 / 3.52 | 彩色分层展示可用，准确读出现在就是有不同的颜色分层。 | 一条字幕正确不推成整体OCR全准，人物归属仍概括。 |

Mini16x每原帧实际总prompt_tokens=90，4x=288，ROI=264，均含问题／模板，不能全部叫视觉token。原三帧下4x主要对象可用，20秒“喂试管”动作较模糊，未见一致内容提升；耗时也有回答长度差异，不作单一因素因果。35秒ROI准确读整句字幕是具体改善，不夸成OCR全准。

4x三张原图联合另有resource-only：HTTP200、prompt819＋1输出，编码请求成功；quality_cases=0，没有保留语义正文。4x最终同run资源、120k、7001789 Provider和Genie共驻现已完成，以下独立列出，不挪用16x峰值。


## 最终Mini4x接口、共驻与恢复

- **旧v1**：原可见内容可json.loads，合法JSON；失败为缺limitations、frame_observations list而非dict、首ID缺c等schema/来源绑定问题。原失败不改，不当成视觉观察全错。9df中间原档也保留。
- **7001789真实接口**：public probe＋admission21.04秒、后续analyze18.05秒，12真实POST/计量12call、4764input/1790output。输入按可信CID837470340及实际10/20/35秒观察，后台精确绑定三原图ID和SHA，sufficient保留模型自评。
- **内容**：20秒成人手向管中滴液、女孩观看及35秒彩色分层管展示有用；10秒玩具/颜料活动泛化较粗。英文汇总把第二帧写成girl receiving liquid，主体关系不如中文逐帧清楚，保留这个局限；接口闭环成功不等同每句事实全准。
- **4x长容量**：本run119991input＋8192reserve/131072，168.03秒精确锚点珊瑚-7319；训练原生262144，无需把16x容量移植到4x。
- **Genie共驻**：沿用yaoyao_sovits_v2/Genie ONNX INT8，在同VLM驻留时以实际LLM台词合成，随后Genie仍驻留时LLM调用完成；这是共驻与顺序调用，不是同时推理吞吐。WAV32000Hz/mono/16bit/383360frames，11.98秒音频，766764字节，合成11.02296秒，LLM2.00844秒。
- **配对资源**：各配对阶段最低系统可用2459820032字节（2.29089GiB），combined RSS峰4517138432字节，swap增长0。阶段明细保存在JSON，与4x整轮最低2466222080字节、VLM进程树RSS峰3249942528字节分别保留，不跨来源拼峰。
- **清理恢复**：临时Genie worker已回收、4x24376已停止且remaining=[]。恢复verified=true，原MiniCPM5-2B PID25156、text/131072/Q4 KV，生产TTS仍false；前后配置SHA590ddff1…不变，平台消息0、音频未播放。

新接口原档mini4x-provider-v2-timed-real.json，长容量mini4_7-4x-v4-capacity.json，共驻mini4x-genie-cohost-device/result.json，终态mini4_7-4x-plain-visual-v4-device/metrics.json，恢复final-restoration-v4-device/verification.json。逐帧原答与语义局限见mini4_7-v4-oc-review.md；完整文件SHA和具体配置见comparison-checkpoint.json。
