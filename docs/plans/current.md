# 当前工作与待验收项

## 实施待办

| 工作 | 从现有能力继续 | 完成条件 |
| --- | --- | --- |
| 长期目标的触发与主动调研 | 已有一次预约、日／周跟进、指定 UID 来信和指定评论楼入库提醒。扩展其他获准事件来源，以及由条件触发的私有调研。 | 能从活动页查到触发来源、读取范围和私有交付；重复事件只处理一次，撤权后停止后续动作。[已有跟踪入口](../guides/goal-event-tracking.md) · [任务契约](../specs/foundation/agent-runtime.md#常驻任务与恢复) |
| 连接器与受限工作区 | 已有公开 HTTPS 阅读页接管、私有附件、授权计算和可编辑下载的成果。继续接入按来源授权的连接器，扩展受限工作区可读写的文件格式。 | 从普通任务选择获准输入，取得可读回的来源与文件；缩小授权、接管和取消后，后续步骤遵守新范围。[浏览器边界](../guides/private-browser.md) · [委派与控制](../specs/foundation/agent-runtime.md#精细化委派与控制) |
| 通用任务与跨任务召回 | 从已有多步工具调用、独立读取委派和可溯源偏好出发，扩展自然语言任务的条件组合、跨任务主题归并与按来源召回。 | 用不同领域的普通交办核对选中的对象、条件未满足时的处理、改口后的交付和撤回后的引用。[任务控制](../specs/foundation/agent-runtime.md#精细化委派与控制) · [已有记忆实测](../../evaluations/results/owner-memory-20261119.md) |
| 视频投稿、节目制作与游戏实机 | 视频取材、运营动态与直播素材交接已有入口。后续接创作素材到视频投稿、OBS 节目制作的流程；星露谷从单地块推进到完整游戏日，植物大战僵尸实机等待可启动的授权安装与版本。 | 投稿绑定获准作品与平台对象，节目以实际播放结算；游戏的动作读回、接管和恢复按各自实机阶段验收。[直播规范](../specs/live/live-and-media.md) · [游戏实施计划](game-livestream.md) · [已有实机记录](../../evaluations/results/stardew-device-20261119.md) |

## 常驻 Agent 与跨端记忆

Chats 支持交办、改口、取消和查看工具回执。已完成全仓分层重构与双脑职责落位：文本前台（`chat_llm` → `/llm/v1/chat/completions`，`enable_thinking=False`）与后台路由/长任务共用 9B 文本服务且 `OperationTurns` 优先抢占前台对话；全双工私有语音通话专走 Omni `/v1/realtime` 并共享同一 `runtime.tasks` 生命周期（断挂后可从文本 Chats 连续接管而不同步语音原文）。M5 已通过真实 Microsoft Edge 隔离复验，工作站隔离回放与活动页见[连续对话实测](../../evaluations/results/operator-chat-continuity-20261120.md) · [记忆与成品实测](../../evaluations/results/owner-memory-20261119.md) · [分层与阶段二总验收](../../evaluations/results/20261122-layered-refactor-dual-pipeline-acceptance.md)。

## 运营主线

全仓已迁移至 `L0–L4` 单向分层架构、React 19 顶层 `frontend/` 与 `/api/v1/*` 契约，并落地阶段二运营双管线：主动探索（Discovery）与消息响应（Inbox）共用 `operations/kernel/`（`operation_claims` 原子认领与楼级 Inbox 优先租约、`direct_quota_reserve` 预留保护、412/429 风控退避保消息与 `(bvid, cid)` 单飞证据池）。工作台具备独立双控制卡、全局急停/解除、活动流 `discovery`/`inbox`/`manual` 来源徽标与 `/dafeiyu/` 子路径挂载，零平台写入的 Edge E2E 与架构 AST 守卫已全量闭环；新链路的真实平台送达、来信及工作站阶段四切换等待用户审批后单独执行。[部署与三渠道验收](../../evaluations/results/operations-images-three-channels.md) · [分层与阶段二总验收](../../evaluations/results/20261122-layered-refactor-dual-pipeline-acceptance.md) · [调度](../../evaluations/results/operations-scheduler-20261114.md)

日常看帧已接同一投稿、对应分 P 的元数据及邻近带时码文字。逐帧、联合与汇总请求由隔离传输核对。普通网关单次实读答对场景大意和两帧时码，漏答标题与简介，并把画内烧录字误报为公开字幕，整项未通过。[消息对照](../../tests/agent/test_video_frame_text.py) · [普通画面实读](../../evaluations/results/p2-ordinary-frame-reading-20261119.md)

## 共享推理与 Avatar API

M0–M5 代码重构、React 前端构建、Docker 多阶段打包（`deploy/omni/Dockerfile` `web-build`）、CI 检查、脱敏文档规范与隔离 Edge 验收已全部就绪，待用户审批后按[交接清单](../handoff.md)执行阶段四（`<code-root>/Tuber_Studio` 构建与 `/data/business`、`staged → active` 业务卷切换）。后台文本使用 `/llm/v1/responses` 与 9B 对话，私有电话使用 Omni 原生双工语音；服务替换时按实报能力复验工具往返、长文语义、排队和取消。[迁移回执](../../evaluations/results/workstation-business-cutover-20261119.md) · [正式入口](../../evaluations/results/workstation-studio-ip-20261119.md) · [服务准入](../specs/foundation/omni-deployment.md#接入验收与切换)

## 直播、点歌与部署验收

直播交接已有播放器实际播完回执。真实 B 站／抖音开播、礼物与断线恢复、实际节目中的歌曲人声／歌词／音量仍待有授权和节目条件时验证。两小时混合负载、直播首音 p50／p95、后台并发和思考额度属于设备与服务测量；短时回放不能给出持续负载结论。演唱另需可用模型，咪咕另需可用凭证。[播放器回执](../../evaluations/results/live-handoff-ended-20261119.md) · [点歌实测](../../evaluations/results/online-song-requests-20261031.json) · [直播验收条件](../specs/live/live-and-media.md#验收)

## 已结束或暂停的工作

- **P2 归档场在 r6 结案。** 号主决定停止。只读核库为 `failed`、0 份 edition、四类平台写入均为 0。该场的三版成品、同场语音更正和撤回对成品的影响均未验成；保留 r1–r6 回执、原业务卷和归档片段，未派发 r7。[结案记录](../../evaluations/results/creative-memory-p2-20261119.md)
