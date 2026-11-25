# 当前工作与待验收项

## 实施待办

| 工作 | 从现有能力继续 | 完成条件 |
| --- | --- | --- |
| 长期目标的触发与主动调研 | 已有一次预约、日／周跟进、指定 UID 来信和指定评论楼入库提醒。扩展其他获准事件来源，以及由条件触发的私有调研。 | 能从活动页查到触发来源、读取范围和私有交付；重复事件只处理一次，撤权后停止后续动作。[已有跟踪入口](../guides/goal-event-tracking.md) · [任务契约](../specs/foundation/agent-runtime.md#常驻任务与恢复) |
| 连接器与受限工作区 | 已有公开 HTTPS 阅读页接管、私有附件、授权计算和可编辑下载的成果。继续接入按来源授权的连接器，扩展受限工作区可读写的文件格式。 | 从普通任务选择获准输入，取得可读回的来源与文件；缩小授权、接管和取消后，后续步骤遵守新范围。[浏览器边界](../guides/private-browser.md) · [委派与控制](../specs/foundation/agent-runtime.md#精细化委派与控制) |
| 通用任务与跨任务召回 | 从已有多步工具调用、独立读取委派和可溯源偏好出发，扩展自然语言任务的条件组合、跨任务主题归并与按来源召回。 | 用不同领域的普通交办核对选中的对象、条件未满足时的处理、改口后的交付和撤回后的引用。[任务控制](../specs/foundation/agent-runtime.md#精细化委派与控制) · [已有记忆实测](../../evaluations/results/owner-memory-20261119.md) |
| 视频投稿、节目制作与游戏实机 | 视频取材、运营动态与直播素材交接已有入口。Wave 2 Remotion 自动制片插件已升级至 **V2.1 浅海手账版**（`remotion-studio/` 浅海手账杂谈桌、`AquaBadge` 15 态 2D 动图演出小窗、33 套 Aqua 动图/CC0 表情包去重联动、B 站花字高亮、轻量 BGM/转场混音、默认 `0.45s` 句间与 `0.75s` 切镜停顿，`agent/skills/remotion_{video,assets}.py`、`app/routes/video_production.py` 及前端 `#videos`）并完成实模（`llm-default` + `omni_speech`）1080p MP4 出片、ASR 回读及浏览器端验收；同时将 `MediaCache` 扩展为 6 类统一媒体/资产缓存与清理入口（`video`/`music`/`speech`/`browsing`/`temporary`/`assets` 收口至 `#cache-panel`）；星露谷从单地块推进到完整游戏日，植物大战僵尸实机等待可启动的授权安装与版本。 | 工作站复测授权单支 `biliup` 投稿 Stage 3 实测；游戏的动作读回、接管和恢复按各自实机阶段验收。[Wave 2 视频管线](../design/video-production-pipeline.md) · [直播规范](../specs/live/live-and-media.md) · [游戏实施计划](game-livestream.md) · [已有实机记录](../../evaluations/results/stardew-device-20261119.md) |

## 常驻 Agent 与跨端记忆

**Personal Always-On Agent (`feat/personal-agent-v2`) 已完成后端内核、`Aqua Studio` 界面与主动脉冲／制片集成（工作站实机切换待操作者复验）：**

- **动态鲸鱼表情包图库与三渠道融合：** 完成 `MemeCatalog` 扩展（`usage_scope="all_channels" | "interaction_only"`、`meme_dhash.py` 64-bit 静态与动图 dHash 感知去重及拒绝黑名单）、`MediaCache` 7 类缓存目录（新增 `meme_inbox` 独立 `512 MiB` / `200` 张上限与 `0` 不限语义）、`MemeHarvester`（B 站评论粗筛、UP 主二创空间/专栏/文集与 GitHub/Manifest 远程订阅同步、`classify_meme_image` 两阶段视觉筛选及待审箱审核队列）、前端表情包管理与待审箱弹窗，以及评论/私信/动态三渠道与 Remotion 制片的 `usage_scope` 严格隔离。
- **两层扁平架构与窄工具集 Profile 对齐：** 在既有 `turn_router` + `TaskRuntime` 上落地最大派发深度 `≤ 1` 防套娃守卫（`ACTIVE_TASK_EXECUTION` + 后台禁派 `delegate_task`）、4 组显式 `TaskProfile` 窄工具集（`meme_harvest` / `video_production` / `media_inspection` / `general_research`）、图库前台只读快查（`instant_read(action="meme_gallery_status")`）与后台异步同步（`sync_meme_sources` 返回精简统计 + `evidence_id`）双向分流，以及 `assistant_history()` 原始 JSON 隔离保护。
- **80/20 鲸鱼娘人设与 B 站短评节奏（`Aqua`）：** `persona/self_model.py` 与 `config/characters.json`（保留 `id="whale"`、`name="Aqua"`，同步更新 `persona/characters/presets.py` 4 组哈希）回调为 80% 鲸鱼娘 OC（呆毛/藏蓝鲸尾/白饭/摸鱼/轻傲娇）+ 20% 本地 Jetson 底色，精简过饱和防御词并增加 3 条关键词触发 `lore`，配合 `browsing_turns.py` 引导 1–2 句自然 B 站短评。
- **B 站评论真实可见性闭环：** `infra/platforms/bilibili/browsing.py` 补齐主评 `sort=0`、楼内末页定位、无 Cookie 匿名双视角复查与非阻塞锁外延迟复查（`public_visible` / `self_only` / `shadow_deleted` / `pending_recheck` / `check_failed`），连续 2 次非公开触发 30 分钟发评冷却与风控告警，并在 `operations/kernel/activity.py` 与 `OperationsActivityFeed.tsx` / `Stage2PipelineCards.tsx` 透出真实状态。
- **双层严格隔离记忆：** Layer 1 `UnifiedMemoryStore`（`memories` 表，默认软上限 `100`）按 `scope="owner"`（仅号主）与 `scope="public"`（仅公开渠道）严格隔离，禁用启动时自动迁移遗留表并在设置弹窗提供 `purge-migrated` 一键清理；Layer 2 `persona/memory/recall.py` 经 `recall_history` / `recall_memory` 按需检索经历与原文表。
- **`ContextNode Base -> Branches` 通用树：** `persona/context/tree.py` 以不可变 `Base` 节点承载人设、Scoped Memory 与观看视频基础材料（含分章摘要／时码片段），各对话、视频主评、评论楼（保留 `[人工接管 · ...]` `user` 轮）与私信分支从 `Base` 分叉并输出标准 OpenAI Chat / `Responses` 多轮工具消息。
- **统一常驻心跳与主动脉冲：** `agent/heartbeat/`（`AgentEventBus` + `HeartbeatService`）将后台健康检查、热点与好奇心联网调研去重、按冷却与日配额收敛为 `Aqua` 第一人称脉冲简报，经 `agent/chat/heartbeat_feed.py` 持久化并由 `GET /api/v1/agent/chat/events` 推送；`agent/chat/sse_events.py` 提供 `memory_updated`、`tool_activity`、`stage` 与 `calm_constraint_message` 平静状态提示。
- **OpenAI 风格 `Aqua Studio` 界面与全双工分工：** `frontend/` 切换为可折叠侧栏 `Aqua Studio` 壳层（`对话与脉冲`、`渠道活动`、`直播台`、`角色与声音`、`设置`），聊天气泡支持点击「已更新记忆」胶囊打开全局 `ManageMemoriesModal`（`N / 100` 容量条、`mig:*` 清理）及折叠「思考与工具」胶囊；精简 `PersonalizationPage`；前台文本（`chat_llm` → `/llm/v1/chat/completions`，`enable_thinking=False`）与 `OperationTurns` 共用 9B，全双工私有语音专走 Omni `/v1/realtime` 并共享 `runtime.tasks`。[连续对话实测](../../evaluations/results/operator-chat-continuity-20261120.md) · [记忆与成品实测](../../evaluations/results/owner-memory-20261119.md) · [分层与阶段二总验收](../../evaluations/results/20261122-layered-refactor-dual-pipeline-acceptance.md)

## 运营主线

全仓已迁移至 `L0–L4` 单向分层架构、React 19 顶层 `frontend/` 与 `/api/v1/*` 契约，并已完成工作站阶段四切换（`1953df9`、`tuber-studio-core:1953df9`）。`feat/personal-agent-v2` 已将运营多渠道生成迁移至 **原生 OpenAI `Responses` 多轮工具调用与纯文本直出**（`operations/kernel/{tools,session,browsing_turns}.py` 与 `channels/{comments,direct_messages,dynamics}/turns.py`，开放 `search_web`、`inspect_video_frames`、`read_video_segment`、`save_memory`、`recall_history`、`like_video`、`skip_reply`、`queue_dm_handoff` 8 路工具，`store=False`、`thinking=True`），彻底退役 `[ref:N]` / `[eN|...]` / `evidence_ids` 提示词引用仪式并改由 Python 侧记录来源归因与防漏校验（`detect_internal_protocol_leak`）；评论审核台新增 `VideoContextTreeCard`（观看视频 `Base -> 楼分支` 树形视图，含人工接管轮次与异常草稿 `errored_only` 批量拒绝）。阶段二运营双管线——主动探索（Discovery）与消息响应（Inbox）共用 `operations/kernel/`，新链路的真实平台送达、来信及私有语音通话待工作站更新上线后由操作者在工作台按需开启实机验收。[部署与三渠道验收](../../evaluations/results/operations-images-three-channels.md) · [分层与阶段二总验收](../../evaluations/results/20261122-layered-refactor-dual-pipeline-acceptance.md) · [阶段四上线验收](../../evaluations/results/20261123-stage4-workstation-cutover.md) · [调度](../../evaluations/results/operations-scheduler-20261114.md)

日常看帧已接同一投稿、对应分 P 的元数据及邻近带时码文字。逐帧、联合与汇总请求由隔离传输核对。普通网关单次实读答对场景大意和两帧时码，漏答标题与简介，并把画内烧录字误报为公开字幕，整项未通过。[消息对照](../../tests/agent/test_video_frame_text.py) · [普通画面实读](../../evaluations/results/p2-ordinary-frame-reading-20261119.md)

Wave 1 加固分支已完成：全渠道批量拒绝与 12014/509 风控退避透传、评论楼层与私信角色隔离/反迎合、统一可编辑 `owner`/`public` 记忆库与号主接管上下文、B 站无 CC 音轨 CPU `faster-whisper` ASR 回退（按字幕时码联动选帧）、`whale` 傲娇/拒称「大肥鱼」人设更新以及动态双轨自动补链。

## 共享推理与 Avatar API

M0–M5 代码重构、React 前端构建、Docker 多阶段打包（`deploy/omni/Dockerfile` `web-build`）与工作站阶段四正式切换（`<code-root>/Tuber_Studio` 构建、`<state-root>/backups/before-1953df9.sqlite3` 离线备份及 `/data/business` 激活运行）已全部完成并复验通过。后台文本使用 `/llm/v1/responses` 与 9B 对话，私有电话使用 Omni 原生双工语音；服务替换时按实报能力复验工具往返、长文语义、排队和取消。[阶段四上线验收](../../evaluations/results/20261123-stage4-workstation-cutover.md) · [迁移回执](../../evaluations/results/workstation-business-cutover-20261119.md) · [正式入口](../../evaluations/results/workstation-studio-ip-20261119.md) · [服务准入](../specs/foundation/omni-deployment.md#接入验收与切换)

## 直播、点歌与部署验收

直播交接已有播放器实际播完回执。真实 B 站／抖音开播、礼物与断线恢复、实际节目中的歌曲人声／歌词／音量仍待有授权和节目条件时验证。两小时混合负载、直播首音 p50／p95、后台并发和思考额度属于设备与服务测量；短时回放不能给出持续负载结论。演唱另需可用模型，咪咕另需可用凭证。[播放器回执](../../evaluations/results/live-handoff-ended-20261119.md) · [点歌实测](../../evaluations/results/online-song-requests-20261031.json) · [直播验收条件](../specs/live/live-and-media.md#验收)

## 已结束或暂停的工作

- **P2 归档场在 r6 结案。** 号主决定停止。只读核库为 `failed`、0 份 edition、四类平台写入均为 0。该场的三版成品、同场语音更正和撤回对成品的影响均未验成；保留 r1–r6 回执、原业务卷和归档片段，未派发 r7。[结案记录](../../evaluations/results/creative-memory-p2-20261119.md)
