# 大肥鱼刷视频修复与通用问答复测

工作站使用同一个 llama.cpp 构建和 Q4_K_M，固定 seed 42、温度 0。刷视频保留 V6 最佳视频 LoRA，这轮没有新增训练步数。原版对照用同一个短身份和结构化回复格式，便于单独比较权重。

## 刷视频

| 条件 | 完整返回 /8 | 现象 |
| --- | ---: | --- |
| 旧 V6 运营输入 | 2 | 6 条在 360 秒内未完整返回 |
| 同输入加 DRY | 7 | 1 条写到 8192 token；另有正文夹带内部标题 |
| 证据在前、较严格的简短指令 | 8 | 5 条暂缓 |
| 证据在前、允许回应有依据的部分 | 8 | 3 条暂缓，两条仍有可答内容 |
| 精简成顶层评论专用提示 | 8 | 多条在关键字幕上漏答或错答 |

8 个真帧加字幕用例沿用隔离运营回放的输入与原始来源。DRY 只在顶层视频留言的原生解码生效，参数为 multiplier 0.8、allowed length 5、penalty last n 512，保留 8192 token 请求额度。生产同时补了取材和引用两个环节。根据内容判断引用的字幕选择分 P 和时码。生成正文时可看到选定材料以及同一分 P、时码重叠的另一种材料，最后只保存正文实际选择的原始引用。双 P 端到端回归验证第二 P 的字幕在 60–70 秒时，取帧请求发往第二 P 的 65 秒，正文能看到这两条材料，草稿只保存模型引用的那条。此回归用受控适配器验证生产链路，真实帧回放单独验证解码。

在另一个冻结的 40 题口径下，当天两组 V6 都得到事实 23/32、事实与引用 21/32，其中图文题 10/16，缺证据时谨慎处理 6/8。两组只改变 DRY；不同于 8 个运营用例，这组用了既有事实问答提示和 384 token 额度。与更早一轮相比，金帽这题两组都带上了画面引用，变化未归因于 DRY。余额题依然凭空作答；实测只覆盖工作站，尚无 Jetson 上的 p95 和共驻结果。

- 8 条运营原答。[旧 V6](../grounded-preference-20261105/preference-ops-native-v6.jsonl) · [DRY](repair-video-dry.jsonl) · [严格提示](repair-video-grounded.jsonl) · [放宽提示](repair-video-open.jsonl) · [顶层评论提示](repair-video-focused.jsonl)
- 40 题原答。[同轮基线](repair-video-base40.jsonl) · [DRY](repair-video-dry40.jsonl)

## 通用和直播

除了原 36 道跨领域短答，补了 12 道独立确认题。原版、完整 V6、50% 与 75% LoRA 合并系数均走同一个直播 JSON 回复结构。另外试了简化观众消息、精简身份与答题指令的组合。精简直答组用「观众：」加原话代替事件 JSON，省略空关系与 lore，保留原生输出语法。系统指令为「你是大肥鱼，在和观众聊天。问事实就先说答案，说明依据或条件。拿不准就明说。其他时候自然接话。」两个缩减 LoRA 均从同一 BF16 底模和 V6 adapter 重新合并量化，无优化器更新。

| 条件 | 原 36 题完整正确 | 12 题确认完整正确 |
| --- | ---: | ---: |
| 原版加同一短身份 | 10 | 5 |
| 完整 V6 | 7 | 4 |
| 完整 V6，精简直答输入 | 10 | 6 |

精简直答修正了来回 4 公里、冷玻璃水汽凝结、真空里的声音等题，也丢了原来答对的比例尺题。问船上加货后是浮高还是沉低，仍答「会浮高一点」。同一格式在 8 个未见多轮场景丢了白旗分工和等一长声的暗号。常规直播保留原有输入。精简直答保留为工作站对照。50% 与 75% 合并没有同时带回稳定的常识、CS 和当前场景理解，未替换 V6。

- [36 题](../../cases/grounded-general-transfer.jsonl) · [12 题](../../cases/grounded-general-confirmation.jsonl) · [直答原答](repair-general-transfer-natural.jsonl) · [直答确认原答](repair-general-confirmation-natural.jsonl)
- [原版确认原答](repair-general-confirmation-base.jsonl) · [V6 确认原答](repair-general-confirmation-v6.jsonl) · [V6 多轮](../grounded-preference-20261105/preference-heldout-v6.jsonl) · [精简直答多轮](repair-live-next-heldout-natural.jsonl)
