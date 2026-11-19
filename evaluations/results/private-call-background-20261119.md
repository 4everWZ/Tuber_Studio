# 私有电话背景与 Realtime 协商

2026-11-19 在工作站正式网关空闲时，用正式 Core 的普通应用凭据签发一次性电话票据。探测走同一网关的 Realtime WebSocket，使用原生双工参数。票据与凭据留在站内。

| 检查 | 实际回包或实现 |
| --- | --- |
| 初次协商 | `session.created` 返回 `model_native_duplex`，输入模式为 `append_audio_chunk`，周期 1000 ms，`supports_text_only_turn=false`。 |
| 签名转写 | 显式申请转写的普通通话协商到 `explicit_once`、16 kHz、最多 15 秒、`hmac-sha256-v1`。本次检查了能力声明。此前的合成中文语音回放实收了一次网关转写完成事件，并核对了独立读回的机器原稿和号主确认稿。[语音转写验收](owner-memory-voice-confirmation-20261119.json) |
| 通话中更换背景 | 发送一块静音 PCM 后提交不同的 `session.update.instructions`。首个 `session.updated` 回显原指令；带第二次请求标识的错误为 `instructions_update_unsupported`。 |
| 文本条目 | 提交用户文本收到条目事件，再请求回复得到 `text_only_turn_unsupported`。安装版本的 MiniCPM-o Stage 0 在初始化时编码指令、可选参考音频和初始用户文字，后续音频追加没有把文本条目编进声学上下文的路径。 |
| 工具 | 会话更新可回显工具声明，指定工具的 `response.create` 返回 `unsupported_native_response_options`。本次未观测到原生工具调用与结果回灌。 |

隔离电话票据测试核对了新旧卡字段、普通与临时通话、显式与继承话题、无关背景排除，以及只有角色背景命中时撤回私聊来源的行为。工作站探测验证网关能力，修改后的工坊代码只在本地验收。
