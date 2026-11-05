# 大肥鱼多轮更正最后一轮对照

在 workstation 的隔离 llama.cpp 上复用同一 V6 Q4_K_M 和角色身份。两组均保留模型自己的上轮回答和现有直播回复验证，固定温度 0、seed 42，单次输出额度 180 token。试验组从第二轮起要求先写一条内部事实备注再写台词，仍只生成一次。备注只用于该次解码和评测记录。两组均完成 32 个场景。

六对新场景交换更正后的分工、归属、署名或规则，保留同一句最终追问。按关键事实看，普通 V6 命中 2/12，内部备注组命中 3/12。前者两侧都答对的场景是 0/6，后者是 1/6。备注组对两侧声音剪辑都答对，例如「脚步声留着，叮声删掉，字幕按原。」换场牌题中，它从答对蓝牌上台的一侧变成答对蓝牌退场的一侧。署名和道具归属仍会说反或复述提问。单例小幅改善不足以说明能稳定跟着更正改口。

8 个原多轮场景仍会复读或漏答，猫名和黑屏文案两组均未按要求写出来。12 道独立通用确认题前面统一加了一轮随口闲聊，备注组改善了来回路程和冷玻璃凝结，其他题仍会答非所问。末轮输出中位数从 39 增至 50.5 token，包含内部字段。保留这组回放作为证据，直播生产继续使用原有 V6 回复协议。视频、评论、私信和动态的模型输入和权重沿用上一轮。

- [六对场景与判据](../../cases/grounded-live-revision.jsonl) · [原答](final-result-grounded-live-revision-base.jsonl) · [内部备注原答](final-result-grounded-live-revision-note.jsonl)
- [既有多轮原答](final-result-grounded-live-next-heldout-base.jsonl) · [内部备注对照](final-result-grounded-live-next-heldout-note.jsonl)
- [带闲聊的通用问答原答](final-result-final-revision-qa-base.jsonl) · [内部备注对照](final-result-final-revision-qa-note.jsonl)

使用 [直播回放脚本](../../../tools/training/replay_grounded_live.py) 的 `--revision-note` 运行备注组，原组省略该参数。通用回放的原答附有完整两轮输入，可直接作为脚本的用例文件。
