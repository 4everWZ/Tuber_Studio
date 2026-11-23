# 工坊推理服务接入契约

Core 使用一个外部 Omni 网关和按该地址授权的普通应用凭据。模型别名由服务方提供，业务能力按实际调用核验。Core 持有角色、记忆、任务、平台许可和工具回执。服务方负责推理、排队及媒体交付。[独立服务接口](https://github.com/4everWZ/omni-avatar-api/blob/main/docs/api.md)

## 首版协议与 Provider 边界

| 能力 | Tuber Studio 用法 |
| --- | --- |
| 健康与模型发现 | 通过 `GET /healthz` 和 `GET /v1/models` 核对健康与模型，使用配置的别名及模型卡实报的 `max_model_len`。实际模型、版本、窗口和可用模态在设置与诊断可查。 |
| 前台文字与表达 | 号主 Chats、受理承接语与 `DeliveryPacket` 角色化转述使用与后台共用的文本模型 `chat_profile(omni)`（`llm-default` 别名，经 `/llm/v1/chat/completions` 流式运行，`enable_thinking=False` 并保留参数覆写能力），挂在 `OperationTurns` 最高优 `"chat"` 车道（`chat_text_generation()`）且与 Omni 解耦。 |
| 后台任务 | 同网关的 `POST /llm/v1/responses` 承接两类工作：(1) `turn_router` 在 `OperationTurns` 最高优 `"chat"` 车道上以 `parallel_tool_calls=False`、`temperature=0.0`、`store=False` 统一执行 Chats 7 路分流与多轮指代消歧（`route_chat_turn` 原生多轮 `recent_dialogue`，有界思考 `max_output_tokens=640`、`thinking_budget_tokens=160`）以及 `omni.asr.v1` 语音指令分流（`route_voice_utterance` `thinking=False`、`max_output_tokens=256`），并在前台生成前释放车道；(2) 后台长文、带时码的图片输入与多步工具调用，工坊先持久化 `task_id` 再回发前台受理，按 `call_id` 续交实际工具结果。 |
| 实时语音与播报 | Omni（`omni-default`，8,192 窗口）仅负责私有电话与实时音频/播报交互（Realtime 双工听、说、打断）及通过共享 `runtime.tasks`（`delegate`）转交后台，不与文字 Chats 共享原始通话转录历史。 |
| 离线音频转写 | 无官方 CC 字幕的视频通过 `POST /v1/audio/transcriptions` 调用独立 CPU `faster-whisper` ASR Worker（`verbose_json` 带时间戳段落），转成带时码语音转写块供后台按字幕点选故事板／关键帧；网关未启用 ASR 时自动退回原有视觉扫描，不阻塞取材。 |
| 参考朗读 | `POST /v1/audio/speech` 接收指定台词、模型别名与授权参考音频。工坊核对原文和可播放的音频，记录实际播完。[声音与缓存](models-and-audio.md) |
| 运营配图 | `POST /v1/images/jobs` 提交内联的角色参考与场景。按服务回包排队，完成后持久保存校验过的 PNG，渠道通过工件 ID 读取。[配图任务契约](../operations/generated-images.md) |
| 原生双工 | Core 向 `POST /v1/browser-tickets` 换取限定路径的一次性票据。浏览器连接 `/v1/realtime?duplex=1`，按实际协商处理语音、打断和播放确认。应用凭据留在业务端。 |

工坊后台每轮提交业务端保存的必要历史，使用 `store: false` 释放引擎侧历史，工具结果和检查点保存在工坊。服务暂不可用时显示等待或原因，恢复后按工坊检查点续接。Chat、Responses 和 Realtime 各自按其实际协议调用工具。[当前计划](../../plans/current.md#共享推理与-avatar-api)

后台读帧使用 `llm-default` 的 Responses 图片输入。工坊沿原样本分 P、时码和来源保存观察，后台以视频资料和可用字幕辅助理解。后台自动整理记忆使用同一后台模型，按已核验原话写入原有记忆库。前台和后台各自等待推理，网关分别控制实际容量。运营同楼、同会话和提交顺序继续由工坊控制。

后台文本、视频读帧、`route_chat_turn` 路由及 Responses 委派均启用受控思考：一般后台任务思考额度最多为输出上限的四分之一且不超过 1024 tokens；`route_chat_turn` 对话路由（`640 / 160`）、视频取材逐帧观察（`128` tokens）使用各自有界预算，语音分流（`256`）与前台 `llm-default`（`chat_profile`）流式文字接话默认关闭思考（`enable_thinking=False`），保留主输出额度给工具调用或可见交付。触发输出预算上限（`InferenceBudgetExceeded`）时，网关客户端仅记录脱敏预算诊断（`max_output_tokens`、`output_tokens`、`reasoning_tokens`，不落提示词或凭据），并保留既有检查点与用户可读提示。

## 私有电话与直播回执

私有电话只使用 Realtime 语音会话。直播按演出设置使用一只宠物，半身 Avatar 待验。角色资料选择由业务层保存。终端依据时间戳与播放代次呈现音频、字幕及画面，打断时清理旧代次未播内容，渲染不可用时按协商能力继续呈现可用的语音和字幕。

业务历史区分模型生成、媒体传输和客户端确认实际播完。已播台词才凭实际回执加入已说过的历史。节目阶段可把媒体来源接到 OBS，合成、推流和平台直播权限由直播工作区处理。 [播放与回执](../live/live-and-media.md)

<a id="实机准入与切换"></a>

## 接入验收与切换

| 阶段 | 通过条件 |
| --- | --- |
| 共享推理 | 从 Core 与另一获准调用方复用同一服务。核对鉴权、模型卡、计量、普通／流式响应、结构化输出、视觉、真实长文语义、排队、取消、错误和持续可用性。 |
| Agent 与业务 | 在隔离工作台按原入口回放且平台写入为零。验证任务保存、回执、改口、取消、待核对和恢复。更换 Provider 后保持同一任务、记忆和业务授权。 |
| Realtime 与节目 | 直接验证原生输入与输出、插话、静默恢复、关闭与播完回执。OBS 与持续节目共驻随后验收。 |

按验证结果启用能力。网关变更后复核既有任务、授权和声音绑定。业务卷迁移按唯一写入者流程交接。[跨主机验收](distributed-runtime.md#跨主机验收)
