# 项目交接 · 2026-11-16

## 当前状态

- `main` 收录本轮评论直达链接、显示实际回复对象的动态配图，以及从近期私聊提炼角色日常的自主动态。动态模型只接收私聊侧核对过的公开成品。记忆设置变更、账号或角色切换、原对话删除与到期都会影响旧稿交付。 [动态规范](specs/operations/dynamic-operations.md) · [配图样例](../evaluations/results/dynamic-comment-excerpt-20261115.png)
- 本机 9020 已加载原生聊天正文流和基于公开来源的记忆复用。压测前正常收尾，验收后按原权限恢复自主运营。评论直达、配图和私聊侧脱敏取材继续由现有入口提供。实际平台发布以账号授权和回执为准。 [复验](../evaluations/results/assistant-memory-20261115.md)
- GitHub Actions 的 CPU 测试与 Jetson 打包并行。两项通过才交付正式程序包和写入 GHCR。上一版已在 GitHub Actions 验过双门禁。 [部署指南](guides/deployment.md) · [上一版 CI](https://github.com/4everWZ/Tuber_Studio/actions/runs/43613680003)

## 下次从这里接

按[当前计划](plans/current.md#常驻-agent-与跨端记忆)，先用通用助手跑通流式对话、多步工具、持久任务及跨设备恢复，再接大肥鱼的角色、记忆、号主主页及领域能力。

独立 Omni 基础设施 Compose 和业务 Compose 已在 AGX 运行候选实例。Core 自持数据卷并直接提供完整 Web 工作台，可经私有 SSH 转发登录。CUDA 12.9 Tegra 库与重编的 PyTorch 2.13.0 SM 8.7 wheel 通过容器预检，MiniCPM-o 三个阶段加载后实测返回 Chat Completions 正文和 Realtime PCM。GIF Avatar 的节目已到达远端 OBS。当前主机间走中继，用户确认先推进业务功能，远端 OBS 音质留待复测。 [部署验收](../evaluations/results/omni-deployment-20261116.md) · [AGX Omni 部署](specs/foundation/omni-deployment.md) · [业务 Compose](../deploy/omni/README.md)

1. 当前 9B 已在隔离工作台复验普通私聊以及动态端收到的脱敏成品。后续用授权账号核对图文动态在线呈现和真实平台回执，并持续观察关闭记忆、清理原对话时待发稿的状态。 [复验](../evaluations/results/assistant-memory-20261115.md)
2. 接大肥鱼现有图片、GIF、声音和台词做一条可预览的 Remotion 短片。渲染先输出本地成品，再接 B 站视频投稿和直播素材交接。渲染、发布与播放按同一份已净化素材留来源和结果。 [Remotion Agent Skills](https://github.com/remotion-dev/skills)
3. 实际直播先查 9B 档与 Genie 配对，确认真实播完回执并测首音。和评论、私信一起跑持续负载时，分别记录平台发现、模型排队和发送等待。游戏实机继续沿现有模拟入口验证状态回传和接管。 [当前计划](plans/current.md) · [游戏接入](plans/game-livestream.md)

## 运行位置

电脑工作台使用 [本机端口 9020](http://127.0.0.1:9020/)，项目根目录下可重新启动。

```powershell
& tools/deployment/run-business.ps1 -Data runtime -Config runtime/studio.json -Background
```

更新程序文件后重启原业务进程，再刷新工作台。工作区静态文件可先于运行中的 Python 路由更新。部署验收从实际端口确认 `GET /api/daily-memory/preferences` 和 `GET /api/characters/deleted` 均返回 200，再打开个性化验证开关。评论筛选验收从活动页打开「评论」，核对默认「回复」及两个类型标签。切换「仅读」或「全部」时，主评和楼内回复的新发计数保持一致。

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
