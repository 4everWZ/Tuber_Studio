# 大肥鱼直播上下文探针

在工作站复用六对已经冻结的前文交换题。每对保留同一句追问，只改变前文中的分工、结果或要求，期待两条不同的台词。V7 的实际首轮答复来自第七轮存档；中性首答在同一对两侧完全一样，且不给出正确选项。各条件使用同一 llama.cpp、4096 上下文和贪心解码，重点比较 V7 Q4_K_M 与同权重的 F16。原版底模也用同样的短身份和题目做实验控制。[前文交换题](../../cases/grounded-persona-v6-pairs.jsonl) · [中性首答和候选台词](../../cases/grounded-live-probe-interventions.jsonl)

| 条件 | 实际变化 |
| --- | --- |
| 直播原首答与中性首答 | 保持实际直播提示、模型输出结构和观众话不变，只换掉前一轮助手的台词。 |
| 直播重述前文 | 保持中性首答及输出结构，把观众第一句话原样复述在当前问题前。 |
| 直播无语法约束 | 保持中性首答和直播消息原样，只从推理请求移除 JSON schema。 |
| 纯台词与纯台词重述 | 使用自然对话格式和短提示，重述条件再把观众第一句话原样放在当前问题前。 |
| 二选一 | 两个选项是实际可说的台词，同一输入交换 A/B 顺序各做一次。一个变体需要两次都选对，一对题需要两个变体均满足。 |
| BF16 候选打分 | 用同一模型开关 V7 适配器，在 Transformers 上比较两句候选各自的平均条件 token 对数概率。没有选项字母，也没有解码采样。另看前文翻转前后的相对分差。 |

这是同一小批题上的故障探针。二选一和候选打分衡量给定答案的辨别，不作为自然直播质量评分。重新组织输入和关闭 JSON 约束均只在隔离服务上执行。[回放代码](../../../tools/training/probe_grounded_live.py) · [候选打分代码](../../../tools/training/probe_grounded_preference.py)

## 结果

**前文确实进入模型。** 灯光题实际 llama.cpp 提示保留了第一轮观众、首轮助手和当前观众，实测 217 token，和训练用 Hugging Face 模板渲染的文本完全相同。这 12 条中性首答的直播请求只有 216–238 个输入 token。原首答的 Q4 复跑逐字复现 V7 存档的 12 条第二轮答复。[渲染和 token 核对](live-probe-audit.json)

**有些题能辨别答案，自由说话时仍答反。** 「别放掌声」和「保留掌声」两个变体，V7 Q4 在两个选项顺序下都选对；中性首答的直播原答分别是「先放掌声，再给你一句。」和「先放掌声，再给你一句收尾。」。原首答、换中性首答、在当前轮复述前文及改成纯台词，这四类干预均未让 V7 Q4 在自由回复里答全任意一对。个别变体有改善，例如纯台词会在认输题说「你赢了，他认输。」。[V7 Q4 自由输出与选择](live-probe-v7-q4.jsonl)

**JSON 和量化的解释力有限。** 同一直播提示仅去掉 JSON schema，V7 Q4 有 11/12 条解析出的台词逐字相同，剩下一条也说了同样的话，只是原始 JSON 多了一个全角逗号。F16 去掉 schema 后 12/12 条台词相同。换成 F16，中性首答的自由回复仍没有一对两侧都对。选择题随精度及重述条件波动。F16 对重述后的题比 Q4 多答对两对，但原历史条件下少答对一对。[F16 原答](live-probe-v7-f16.jsonl)

| 二选一，两种选项顺序都答对 | 原历史变体 /12 | 两侧都对 /6 | 重述变体 /12 | 两侧都对 /6 |
| --- | ---: | ---: | ---: | ---: |
| 原版 Q4 | 6 | 2 | 4 | 1 |
| V6 Q4 | 6 | 1 | 6 | 1 |
| V7 Q4 | 7 | 2 | 5 | 0 |
| V7 F16 | 6 | 1 | 8 | 2 |

原版 Q4 的原历史条件有 17/24 次生成了合法选项，重述条件有 21/24 次；表里将无效输出记为失败。V7 Q4 的重述条件有 23/24 次合法，其余续训条件均为 24/24。

**候选打分也显示同一句答案容易压过前文。** BF16 下直接给两句台词打分，V7 在原历史里有 5/6 对的相对分差随前文向正确方向移动，但只有 1/6 真正把最高分候选翻转到正确那句。原版也是 5/6 和 1/6。将前文重述到当前轮后，V7 仍只有 1/6 对两侧都对，原版为 2/6。这说明个别条件下能利用前文线索，线索强度仍不足以稳定改口。灯光题中，V7 BF16 在两个前文下都给「我去把灯调暗」较高平均分，分差约为 0.11 和 0.25，第二个前文要求它停投影。[候选对数概率](live-probe-bf16-preference.json) · [原版 Q4](live-probe-base-q4.jsonl) · [V6 Q4](live-probe-v6-q4.jsonl)

V7 的 850 条训练记录中，直播 JSON 目标有 238 条，分别是 116 条第一轮、116 条第二轮和 6 条第三轮。按底模 tokenizer 估算，这 238 条的约 45% 监督 token 落在台词文本，其余是 JSON 协议或结束符。它给出了真实第三轮和台词监督的规模，这个统计本身不能确定哪部分造成了失败。[训练数据核对](live-probe-audit.json)

## 明确说话人的复查

将灯光分工、赌约和报幕三对题的前文改成「大肥鱼」「观众」「队友」等显式称呼，保留同一个追问、中性首答、候选内容和选项顺序。在 V7 Q4 的选择题里，改写前后都只有两个变体稳定答对，没有一对两侧都对。灯光题两个变体仍都偏向「我去把投影停掉」。自由台词局部变化，例如灯光 A 的纯台词由「停。」变成「灯就灭了。」，报幕 B 的直播台词能提到名单还没发。赌约 A 则开始反复说「你认输，我摘下头盔」。显式称呼仍无法稳定绑定动作和说话人。原版 Q4 在这个复查中也没有答全一对。[明确称呼后的题目](../../cases/grounded-live-probe-named.jsonl) · [V7 原答](live-probe-named-v7-q4.jsonl) · [底模原答](live-probe-named-base-q4.jsonl)

现有证据把问题定位在从前文辨认角色、约定和否定条件，到让它们约束最终台词这一段。前文可见，有些候选分数也会随前文移动，但自由台词仍常落回同一套话；另一些例子连给定选项都无法随前文翻转。这次只运行了诊断。

## 复跑

在项目环境里运行以下两条命令。直播探针的端点应使用已导出的 GGUF，模型别名为 `MiniCPM-V-4.7-1B-Dafeiyu-Grounded`。原版权重借用同一个诊断别名，以控制角色提示。替换 `--cases` 即可复跑显式称呼版。

```bash
PYTHONPATH=src python tools/training/probe_grounded_live.py --endpoint "$ENDPOINT" --characters config/characters.json --cases evaluations/cases/grounded-persona-v6-pairs.jsonl --interventions evaluations/cases/grounded-live-probe-interventions.jsonl --history evaluations/results/grounded-persona-v7-20261105/v7-pairs-v7.jsonl --output runtime/live-probe.jsonl
PYTHONPATH=src python tools/training/probe_grounded_preference.py --base "$BASE" --adapter "$ADAPTER" --cases evaluations/cases/grounded-persona-v6-pairs.jsonl --interventions evaluations/cases/grounded-live-probe-interventions.jsonl --output runtime/live-probe-bf16-preference.json
```
