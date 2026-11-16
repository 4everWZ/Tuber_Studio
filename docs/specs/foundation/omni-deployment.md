# Omni 与 Avatar API 接入契约

Tuber Studio 通过配置取得服务地址、凭据和模型别名。推理和 Avatar 作为独立服务交付。HTTP 请求和 Realtime WebSocket 握手使用该服务分配的 Bearer 凭据。业务主机持有用户、任务、共享记忆、角色配置、平台授权和工具回执。 [独立服务接口](https://github.com/4everWZ/Tuber_Studio_Omni_Deploy/blob/main/docs/api.md) · [业务数据归属](distributed-runtime.md)

## 首版协议与 Provider 边界

| 能力 | Tuber Studio 用法 |
| --- | --- |
| 健康与模型发现 | 通过 `GET /healthz` 和 `GET /v1/models` 核对健康与模型，使用配置的别名及模型卡实报的 `max_model_len`。实际模型、版本、窗口和可用模态在设置与诊断可查。 |
| 普通与流式推理 | 通过 `POST /v1/chat/completions` 调用，统一正文、结构化结果、支持的图像或音频、用量以及错误状态。纯文本计量在服务提供 `POST /v1/chat/prompt_tokens` 时沿现有 Provider 使用。模型的工具请求经业务端校验，实际工具回执从原任务回流。 |
| 原生双工 | 连接 `/v1/realtime?duplex=1` WebSocket，按 `session.created` 协商输入模态、切片、语音、字幕、打断、恢复和播放确认。实时通话与后台任务通过业务会话和任务 ID 关联。 |
| Avatar | 服务发布 Avatar 会话能力后，通过 `POST /v1/avatar/sessions` 关联已授权的实时会话，按回包订阅音画。会话权限、资源状态和正常关闭独立核对。当前业务验证使用既有节目桥，通用 Avatar 会话接口待服务端交付。 |

Provider 按实际能力选择协议。后续服务原生支持 `/v1/responses` 时，在 Provider 适配并实测流式输出、工具结果、取消和错误，再让业务使用同一接口。服务不可用时明确显示等待、降级或失败状态，持久任务按原检查点续接。 [当前实施计划](../../plans/current.md#共享推理与-avatar-api) · [上下文与压缩](context-and-compaction.md)

## Avatar 与音画交付

私有 Chats 和直播分别获得会话及播放权限。角色资料选择由业务层保存，Avatar 服务负责实际渲染与媒体交付。终端依据时间戳与播放代次呈现音频、字幕及画面，打断时清理旧代次未播内容，渲染不可用时按协商能力继续呈现可用的语音和字幕。

业务历史区分模型生成、媒体传输和客户端确认实际播完。已播台词才凭实际回执加入已说过的历史。节目阶段可把媒体来源接到 OBS，合成、推流和平台直播权限由直播工作区处理。 [媒体接口](https://github.com/4everWZ/Tuber_Studio_Omni_Deploy/blob/main/docs/api.md#avatar-会话与音画) · [播放与回执](../live/live-and-media.md)

<a id="实机准入与切换"></a>

## 接入验收与切换

| 阶段 | 通过条件 |
| --- | --- |
| 共享推理 | 从 Core 与另一获准调用方复用同一服务。核对鉴权、模型卡、计量、普通／流式响应、结构化输出、视觉、真实长文语义、排队、取消、错误和持续可用性。 |
| Agent 与业务 | 在隔离工作台按原入口回放且平台写入为零。验证任务保存、回执、改口、取消、待核对和恢复。更换 Provider 后保持同一任务、记忆和业务授权。 |
| Realtime、Avatar 与节目 | 直接验证原生输入与输出、插话、静默恢复和关闭。通用 Avatar 到位后验会话隔离、同步及播完回执。OBS 与持续节目共驻随后验收。 |

按实测能力逐项启用。端点回退使用已验的 Provider 配置。业务卷迁移按独立的唯一写入者流程办理。 [跨主机验收](distributed-runtime.md#跨主机验收)
