# Tuber Studio 当前契约

本规范维护用户授权的 P0 目标；输入基线为 `tmp/GIFTuber_Studio_SRS_v0.1.md`（原文不改写）。P1 真实直播和 P2 评论运营仍按输入分期，不因 P0 原型而视为已实现。

## P0 边界

- 单 Python 服务、SQLite、独立 OBS `/overlay`；本地或 Jetson 推理，无付费 API 前置条件。
- 初始角色为 DeepSeek Q 版「大肥鱼／鲸鱼娘」，明确虚构 AI 同人角色、非官方账号。原型允许使用已有角色内容；资产来源与待确认许可可见，可替换，商业配置不得默认启用未授权音色。
- 多角色、不可变配置版本、复制、激活、预览及回滚。固定人格、Lore、用户关系、场次摘要、最近已播报对话和任务输入分别保存／组装。
- Mock 注入 chat/gift/song/disconnect；平台、房间、事件 ID、用户 ID、时间及元数据规范化。持久去重、TTL、过滤、有限队列。未知观众不按昵称合并。
- 默认一个 LLM/TTS 生成任务串行执行；MiniCPM llama.cpp 与 GPT-SoVITS HTTP Provider 可替换，预录 WAV 可绕过生成。离线演示 Provider 必须明确标识，不能冒称真实模型或声音。
- 模型输出只接受 `spoken_text`、有限 `emotion`、有限 `animation_id` 和可选 `reply_to`。额外字段、越界标签、长度或控制字符失败时显示降级原因，不执行工具。
- 角色／会话切换、跳过、静音、停止取消当前任务并通知播放器停止；暂停只阻止后续任务。清空队列不会隐含完成任何播报。
- 单一 overlay 播放者收到一次性播放指令，需先回传 started、再回传 ended，任务才能 completed 并写入最近对话。无播放器、播放失败、超时、断线、取消和服务重启均不能宣称完成，也不重播遗留任务。
- 管理 API 和管理 WS 认证；默认回环监听。overlay 凭证只授予播放／确认和媒体读取权限，不能管理。弹幕与模型输出均是数据。凭证、数据库、权重及缓存存于忽略的 runtime，不写公开配置。
- WebUI 含总览／直播控制／角色／语音／素材／设置；可看队列、失败原因、LLM/TTS 耗时、真实进程资源、连接及播放状态。`/overlay` 透明或场景背景，角色、字幕、音频共用播放 ID，未知素材回退。

## Provider 与状态边界

`LLMProvider.generate(messages, character, event_id)` 返回受校验回复及 token 数；真实模型通过其 `/tokenize` 执行上下文预算。`TTSProvider.synthesize(text, voice)` 返回本地 WAV；缓存身份覆盖 Provider、权重／音色版本、文字及推理参数。预录路径必须解析到资产目录以内。

事件结束状态含 duplicate/filtered/expired/dropped/cancelled；任务主要状态为 queued → generating → synthesizing → ready_to_play → playing → completed，支路 failed/cancelled/expired。启动恢复把遗留活动状态取消，保留已完成记录。SQLite 是事件、角色版本和播放记录的事实来源。

## 验收边界

P0 验证角色复制／回滚与切换隔离；同名不同 ID；持久去重／TTL／有界队列；非法模型 JSON；取消与过期播放确认；预录直通；真实 MiniCPM 及语音短句；浏览器 started/ended 到数据库 completed 的闭环。设备性能只报告实际测量，不从接口测试推断。OBS 软件自身适配、P1 的 2 小时稳定性和真实平台授权连接需单独验收。
