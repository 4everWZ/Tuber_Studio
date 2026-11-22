# 项目文档

## 使用指南

- [业务部署、服务连接与数据恢复](guides/deployment.md)
- [对话、视频、评论、私信与动态](guides/operations.md)
- [长期目标、来信与评论跟踪](guides/goal-event-tracking.md) · [交办并接管公开网页阅读](guides/private-browser.md)
- [私有语音电话与直播演出](guides/private-avatar-call.md) · [在工作台交办选定回复](guides/selected-replies.md)
- [角色卡、音色与形象](guides/characters.md)
- [角色历史原始语料](../data/whale-persona/README.md) · [大肥鱼形象署名](../third_party/whale-avatar/UPSTREAM.md) · [录音来源与许可](../config/voices.json)
- 直播 · [工作台与点歌](guides/live/workbench.md) · [可选 Live2D](guides/live/avatar.md)

## 产品与技术规范

- [产品与系统契约](specs/tuber-studio.md)
- [记忆规范](specs/foundation/memory.md)
- 运营 · [自主浏览与调度](specs/operations/autonomous-browsing.md) · [评论](specs/operations/comment-operations.md) · [私信](specs/operations/private-messages.md) · [动态](specs/operations/dynamic-operations.md) · [任务续跑与回执](specs/operations/operation-continuations.md) · [共享表情包](specs/operations/memes.md) · [后台生成配图](specs/operations/generated-images.md)
- 运行底座 · [常驻 Agent](specs/foundation/agent-runtime.md) · [角色与运行时数据](specs/foundation/runtime-and-characters.md) · [关系与跨端记忆](specs/foundation/memory.md) · [上下文与压缩](specs/foundation/context-and-compaction.md) · [模型、声音与资源](specs/foundation/models-and-audio.md) · [Omni 与 Avatar API 接入](specs/foundation/omni-deployment.md) · [跨主机运行与数据](specs/foundation/distributed-runtime.md)
- 直播与游戏 · [事件、场景、音乐与播放](specs/live/live-and-media.md) · [模拟与实机接入](specs/live/game-livestream.md)

## 计划与交接

- [全仓分层重构与运营双管线架构蓝图](design/layered-architecture-refactor.md)
- [当前工作与待验收项](plans/current.md)
- [游戏模拟和实机计划](plans/game-livestream.md)
- [当前部署交接](handoff.md)

## 实测记录

- [工作站正式 IP 入口](../evaluations/results/workstation-studio-ip-20261119.md) · [业务库迁移](../evaluations/results/workstation-business-cutover-20261119.md) · [工作站至桌面的媒体传输](../evaluations/results/workstation-whep-ip-20261119.md)
- [获选 Q 与 mature 成图绑定](../evaluations/results/approved-oc-rigs-20261119.md) · [原稿工作站实测](../evaluations/results/inochi-whep-master-rigs-20261119.md) · [真实 Omni 私有通话回归](../evaluations/results/private-rig-omni-voice-20261119.md)
- [三渠道配图与正式入口](../evaluations/results/operations-images-three-channels.md) · [后台生成与参考图](../evaluations/results/operations-artwork-20261119.md)
- [跨端记忆](../evaluations/results/owner-memory-20261119.md) · [按当时的选材理由回看原稿](../evaluations/results/private-task-history-20261119.md) · [P2 归档场 r6 结案](../evaluations/results/creative-memory-p2-20261119.md)
- [工作站参考朗读与播放器回执](../evaluations/results/omni-reference-reading-20261119.md)
- [OBS 口型与 GIF 回退](../evaluations/results/live2d-community-obs-20261111.json)

## 历史归档

按原项目阶段查实现、复现步骤和当时的评估。

- 设备与语音 · [Jetson 本地语音](https://github.com/4everWZ/Tuber_Studio/tree/archive/jetson-local-speech) · [分布式 Gemma](https://github.com/4everWZ/Tuber_Studio/tree/archive/distributed-gemma)
- 角色与训练 · [MiniCPM 角色实验](https://github.com/4everWZ/Tuber_Studio/tree/archive/minicpm-grounded-oc) · [大肥鱼动漫角色训练](https://github.com/4everWZ/Tuber_Studio/tree/archive/dafeiyu-anime-training)
- 主线演进 · [Omni 接入前的 9B 版本](https://github.com/4everWZ/Tuber_Studio/tree/archive/pre-omni-9b) · [创作 Agent 改版前](https://github.com/4everWZ/Tuber_Studio/tree/archive/pre-creative-agent)
