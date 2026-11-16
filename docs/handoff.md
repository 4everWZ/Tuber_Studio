# 项目交接 · 2026-11-16

## 当前状态

- `main` 收录本轮评论直达链接、显示实际回复对象的动态配图，以及从近期私聊提炼角色日常的自主动态。动态模型只接收私聊侧核对过的公开成品。记忆设置变更、账号或角色切换、原对话删除与到期都会影响旧稿交付。 [动态规范](specs/operations/dynamic-operations.md) · [配图样例](../evaluations/results/dynamic-comment-excerpt-20261115.png)
- 本机 9020 已加载原生聊天正文流和基于公开来源的记忆复用。压测前正常收尾，验收后按原权限恢复自主运营。评论直达、配图和私聊侧脱敏取材继续由现有入口提供。实际平台发布以账号授权和回执为准。 [复验](../evaluations/results/assistant-memory-20261115.md)
- GitHub Actions 的 CPU 测试与 Jetson 打包并行。两项通过才交付正式程序包和写入 GHCR。上一版已在 GitHub Actions 验过双门禁。 [部署指南](guides/deployment.md) · [上一版 CI](https://github.com/4everWZ/Tuber_Studio/actions/runs/43613680003)

## 下次从这里接

按[当前计划](plans/current.md#常驻-agent-与跨端记忆)，先用通用助手跑通流式对话、多步工具、持久任务及跨设备恢复，再接大肥鱼的角色、记忆、号主主页及领域能力。

两套独立 Compose 已落在 AGX，候选 Core 自持数据卷并直接提供完整 Web 工作台。当前准入候选是 MiniCPM-o 4.5 官方 AWQ、完整 40,960 Chat 窗口及 4-bit KV。Core 从网关模型卡取窗口。前一档已打通 Chat、Realtime PCM、GIF Avatar 和远端 OBS。远端音质复测按用户要求移后。正式 Windows 业务快照已在 AGX 恢复为只供核验的暂存卷，13,260 个文件通过校验，SQLite 完整性检查通过。通过新配置实机准入后再转交唯一写入权。 [部署验收](../evaluations/results/omni-deployment-20261116.md) · [AGX Omni 部署](specs/foundation/omni-deployment.md) · [业务 Compose](../deploy/omni/README.md)

最新实测中，保留 4,096 生成额度的长 Chats 仍以 `length` 结束。后台任务已验证跨越旧的 120 秒排队边界后按回执交付。同一原生节目持续 75 分 11.5 秒后关闭，引擎在晚发内存压力下停止响应，容器重启未取得退出事件。恢复后先验证同一配置，再查明匿名内存增长，并完成持续运行和长文质量验收。 [终止记录](../evaluations/results/omni-deployment-20261116.md#单会话共驻终止记录)

1. 当前 9B 已在隔离工作台复验普通私聊以及动态端收到的脱敏成品。后续用授权账号核对图文动态在线呈现和真实平台回执，并持续观察关闭记忆、清理原对话时待发稿的状态。 [复验](../evaluations/results/assistant-memory-20261115.md)
2. 接大肥鱼现有图片、GIF、声音和台词做一条可预览的 Remotion 短片。渲染先输出本地成品，再接 B 站视频投稿和直播素材交接。渲染、发布与播放按同一份已净化素材留来源和结果。 [Remotion Agent Skills](https://github.com/remotion-dev/skills)
3. 直播准入读取 Realtime 会话实报能力、播放器播完回执和首音。和评论、私信共驻时分别记录平台发现、推理排队和发送等待。游戏实机沿现有模拟入口验证状态回传和接管。 [当前计划](plans/current.md) · [游戏接入](plans/game-livestream.md)

## 运行位置

AGX 候选工作台经 SSH 转发访问。两套 Compose 的入口、端口和登录操作见[业务部署](../deploy/omni/README.md)。Windows 原业务资料保留用于授权交接和回退核对。

应用更新通过独立业务 Compose 构建和发布。部署验收从实际端口查询 `GET /api/daily-memory/preferences` 与 `GET /api/characters/deleted`，再打开个性化验证开关。评论筛选从活动页打开「评论」，核对默认「回复」及类型标签。切换「仅读」或「全部」时，主评和楼内回复的新发计数保持一致。

自主运营重启后保持待命，启动时检查原批次及就绪发送的授权。验证运营首页的积压、最老等待、发送延期以及安全收尾。真实平台观察和受控发送范围见[调度验收](../evaluations/results/operations-scheduler-20261114.md)。

号主逐字对话从 `POST /api/chat/intelligent/stream` 接收 SSE。当前 9B 的原生正文流已通过节点代理实测。业务日志记录本地上下文、模型能力检查、调度等待、首字、输出规模和 token 用量。

AGX 上大肥鱼与 Spatial Home Edge 的旧 llama.cpp CUDA 容器均已停止。保留容器配置和资产。大肥鱼的 9B 历史基线为 `dafeiyu-qwen35-9b:1.0.0`，曾用 Q4_K_M 权重、Q8_0 投影器、262,144 窗口和 `q4_0` K/V。业务语言模型仍指向已停的端点，切换等待 Omni 实机验收。已有 Genie 语音端点继续提供照稿念和试听所需服务。 [部署指南](guides/deployment.md) · [可选 Live2D](guides/live/avatar.md)

电脑工作台的运营和直播 9B 配置保留在原配置与版本目录中。候选容器使用隔离数据卷，切换时按唯一写入者的迁移顺序处理现有业务数据。 [迁移与回退](guides/deployment.md)

Hub 的 9B 模型卡已同步号主对话和故事板取材流程，以及真实运营记录的追问复测。模型资产使用固定 `1.0.0` 修订。 [Hugging Face 模型卡更新](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/commit/1bdd0b6422d6bb82302fe5e74f7ccae6bf5ed0d3) · [发布卡源稿](model-card.md)

## 数据与恢复

- 11 月 11 日部署检查时，自动运营处于待命，告警计数为 0。角色页只有默认大肥鱼卡，保存覆盖当前卡片。私信与动态共用图库，14 张图已在工作台验证可用。
- 清理移除了 55 条旧未发起私信及 38 条关联归档移交，保留 19 条发送回执、2 条等待任务、原评论和视频进度。清理前备份为版本目录下的 `rollback/studio-before-legacy-outreach-cleanup.sqlite3`。
- 设备探测、训练分片、审核回放和回退配置留在本机 `runtime/releases/dafeiyu-qwen35-9b/1.0.0/`。恢复前停止业务服务，并核对备份之后的写入。工作站保留最终适配器和 GGUF。

## 最近核验

浏览器实测覆盖角色覆盖保存、评论审核、私信历史、动态草稿、共用选图和告警清零。30 个视频审核回放使用真实材料，平台写入为零。随后从业务库只读核对到评论和动态的成功回执，以及发布正文结构残片、生成额度耗尽和自有稿件读取失败。动态图和原评论链接的实际平台显示、真实通知和私信仍待核对。 [视频回放](../evaluations/results/review-agx-mainline-20261111.md) · [后续实跑](../evaluations/results/operations-feedback-20261112.md) · [后续验收](plans/current.md)
