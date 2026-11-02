# 字幕输入紧凑化与 E2B 决策协议实测（2026-11-02）

本例将完整字幕材料呈现为 14 个可追溯内容块：格式包为 **1862 tokens**，完整实际请求为 **3065 tokens**；追加已采纳的 A 版机器任务释义后，完整请求为 **3293 tokens**。192 段原文及顺序全部保留，没有摘要、截断或删除拟声词。

A 保留现有决策 schema，本例的控制字段已表达一致意图。B 使用独立轻结构，少 141 个输入 tokens，完成耗时仍与 A 相近；本轮保留 A，B 不进入产品协议。原始小决定、usage、逐项计量与来源 SHA 见[脱敏快照](operations-vlm-20261101/shared-persona/subtitle-input-e2b-20261102.json)。这批测量不并入[此前同角色卡八素材对照](operations-vlm-20261101-shared-persona.md)。

## 样本与运行条件

- 视频：[零基础deepseek养鱼教程 · BV161nZ6JE6b](https://www.bilibili.com/video/BV161nZ6JE6b)。192 段字幕，分段正文合计 2006 个字符，加 191 个连接换行后，计量文本为 2197 个字符。字符数均按 Python `len()`。
- 使用同一份大肥鱼角色源文件与原料索引，SHA256 分别为 `51d9a6d85a7d6b10a0bf5617c3a3102c2885f2e174a82100775186a00f0af5d1`、`d2a85ca804f8467d21f9148ed5949be9961b5340af108ab09a30f6a1ba758df6`。A/B 的角色 system 与来源 user 消息逐字相同。
- Gemma 4 E2B IT，模型 Q4_0、投影器 Q8_0，二者 GPU；实际窗口 131072、单 slot、Q4_0 KV、batch256／ubatch64、CUDA graphs on。引擎固定 `0ec4027a2a0183de87c007069adddc06e665f7de`，模型／投影器／二进制 SHA 另存快照。
- 本轮三次生成均为 temperature=.2、top_p=.95、top_k=40、min_p=0、presence_penalty=0、repeat_penalty=1、seed42，总输出额度4096，思考开启，无单独思考额度上限，`cache_prompt=false`，实际 cached_tokens 均为0。请求参数覆盖服务默认采样，不能用 `/props` 的默认 T1／k64 代替本次参数。
- 仅原文计量和内容分类，没有图片请求、最终评论生成或平台发送。三次生成的平台写入均为0。

## 同一原生 tokenizer 的分层计量

文本层通过 `/tokenize`、`add_special=false` 探测；完整请求经过真实 Native Provider 消息归并及 `/apply-template`，再以 `add_special=true` 计数。MockTransport 预检已核对实际 Native messages 与 counted_messages、模板请求体逐字一致。JSON schema 在本次 Native 路径中作为 grammar 传输，没有重复写入 prompt。

| 探测对象 | 字符数 | tokens | 范围 |
| --- | ---: | ---: | --- |
| 字幕本体 | 2197 | 1308 | 完整192段正文，以换行连接 |
| 旧详细 segment JSON 数组 | 23439 | 16792 | 重建的数组本身，含重复键与长ID |
| `01327ff` 短ID JSON 格式包 | 14244 | 8486 | 重建的格式包，仍含逐段 JSON 开销 |
| 紧凑文本格式包 | 3516 | 1862 | 来源信息及14个带短编号／时码的内容块 |
| 紧凑版完整 Native 请求 | 依实际模板 | 3065 | 角色、机器任务、来源材料与模板 |
| A 版完整 Native 请求 | 依实际模板 | 3293 | 同上，机器任务追加中文枚举与组合释义 |
| B 版完整 Native 请求 | 依实际模板 | 3152 | 同角色／材料，独立机器任务与轻结构 |

**1500–2000 tokens 是本教程案例的优化目标，不施加到所有视频。** 当前字幕格式包为1862，保留完整角色与任务后的A请求为3293，仍高于2000；因此这里只能说字幕包装进入该区间，不能认定完整请求已达标。23439字符仅指旧segment数组，不是旧完整请求的字符数。旧数组、短ID包与本轮文本包的计数均由同一驻留E2B原生tokenizer实测，与完整messages分别报告。

14 个内容块保留原192段的后台绑定；模型最多选择8个合并块，后台再展开对应的所有原段、CID、轨道／版本与精确时码。格式压缩不改变实际材料和角色源文件。完整第三方字幕留在本地原料中，本报告和快照只保存计数、散列与小决定。

## 生成结果与耗时

首字指可见 JSON 的首字；完成耗时包含该次 SSE 的思考与可见输出，不包含外网读取、计数探针或整轮调度。每个本轮条件仅一次生成，均 `finish_reason=stop`。

| 条件 | 实际输入／输出 tokens | 可见首字 | 完成耗时 | 可见小决定 |
| --- | ---: | ---: | ---: | --- |
| 旧历史教程请求 | 18504／724 | 54.779秒 | 61.668秒 | 原 timing 未保存可见 JSON 原文 |
| 紧凑输入，尚无 A 释义 | 3065／713 | 28.192秒 | 31.768秒 | like、worth=true、provided，却选 unsafe_interaction；refs=e2/e4/e5 |
| A：原 schema＋任务释义 | 3293／828 | 32.503秒 | 35.259秒 | like、worth=true、provided、specific_detail；refs=e2/e4 |
| B：独立轻结构 | 3152／837 | 28.675秒 | 35.291秒 | prepare_reply；refs=e1/e4/e14，附自然 reason |

旧历史请求来自同一教程的实际分类 POST，原请求 SHA 为 `65b3f874d7bfca61be7ce2bfae42fd0f1b6955fd85569481c5b53daf77b3261e`。此前 **324.498 秒是8条不同视频分类生成的累计值**，本教程单次为61.668秒。[原性能归因快照](operations-vlm-20261101/shared-persona/performance-attribution.json)保留了这两个统计层次。

旧完整请求没有成功重建并匹配原 SHA；当前版本还调整了机器任务和运营角色投影，后者移除直播长度偏好。因此旧18504与本轮3065／3293是历史对比，不能将耗时变化全部归因于唯一一个输入改动。计数探针的独立毫秒耗时已保存在快照，未加入上表生成耗时。

## 判断与采用范围

紧凑版首答在当时的 schema 下结构合法，但 `like + worth=true + provided + unsafe_interaction` 意图冲突，不能计作语义通过。A 仅为机器任务补上理由与组合的中文释义，保持角色、完整材料和 schema 不变，本例随后给出了相互一致的准备回复选择。

B 的可见 reason 能识别教程、插件命令、账号设置和权限配置，并说明准备轻松的角色回应。这是内部解释字段，不是最终评论，不能拿来评价公开台词的自然度。B 按独立协议结构通过；原 ContentDecision parser 不接受 B 的事实单列保留。B 比 A 少141个输入 tokens，但本例完成时长35.291秒与35.259秒近乎相同，没有性能理由为此更换产品接口。

本轮采用 A，并在产品的模型校验阶段处理该具体字段冲突：交由共用生成续接反馈给 Agent，允许重选参与、暂不参与或补读意图，不强行替换为某个理由或台词。A 在这个样本上有效，其他素材上的兴趣判断、回复质量与视觉理解仍需分别验证。

## 复核材料

[脱敏快照](operations-vlm-20261101/shared-persona/subtitle-input-e2b-20261102.json)保存三次可见 JSON 原文、usage、首字／完成时间、实际采样、计数层、原始文件 SHA、输入及请求 SHA。基础输入 SHA 为 `2fe584bc8f117b97a729d641d4aa1feb85ac686d006dfd37d1e6de7cbe79f1b3`；A 为 `a30773d9052c0cccd18fba48605a796d8ce2fab086aff21baae2b4e17efd6e77`，B 为 `448e054ba2fbc150aa4111ed840842c1c343a7b5a7ecd753b025387ee88f0c51`。

本轮本地原始材料位于 `tmp/operations-browse-20261101/device/subtitle-measurement-e2b-20261102/`。快照不导出思考正文、凭证、请求头、进程命令／环境、整份来源字幕或图片；思考只保留字符计数与散列。
