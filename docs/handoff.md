# 项目交接 · 2026-11-15

## 当前状态

- `main` 收录本轮评论直达链接、显示实际回复对象的动态配图，以及从近期私聊提炼角色日常的自主动态。动态模型只接收私聊侧核对过的公开成品。记忆设置变更、账号或角色切换、原对话删除与到期都会影响旧稿交付。 [动态规范](specs/operations/dynamic-operations.md) · [配图样例](../evaluations/results/dynamic-comment-excerpt-20261115.png)
- 本机 9020 已运行评论直达和配图版本，本次交接检查时有一轮自主运营在跑。最新自主私聊取材改动随代码交接，待本轮收尾后按下方命令重启加载。实际平台发布以账号授权和回执为准。
- GitHub Actions 的 CPU 测试与 Jetson 打包并行。两项通过才交付正式程序包和写入 GHCR。上一版已在 GitHub Actions 验过双门禁。 [部署指南](guides/deployment.md) · [上一版 CI](https://github.com/4everWZ/Tuber_Studio/actions/runs/43613680003)

## 下次从这里接

1. 在 9020 重启后，用当前 9B 和隔离账号回放一条普通私聊。查看实际送入动态的成品、草稿和队列状态，再核对清空原对话或关闭记忆后待发稿的变化。平台读写、确认回执、评论链接及图文的线上呈现分开验收。
2. 接大肥鱼现有图片、GIF、声音和台词做一条可预览的 Remotion 短片。渲染先输出本地成品，再接 B 站视频投稿和直播素材交接。渲染、发布与播放按同一份已净化素材留来源和结果。 [Remotion Agent Skills](https://github.com/remotion-dev/skills)
3. 实际直播先查 9B 档与 Genie 配对，确认真实播完回执并测首音。和评论、私信一起跑持续负载时，分别记录平台发现、模型排队和发送等待。游戏实机继续沿现有模拟入口验证状态回传和接管。 [当前计划](plans/current.md) · [游戏接入](plans/game-livestream.md)

## 运行位置

电脑工作台使用 [本机端口 9020](http://127.0.0.1:9020/)，项目根目录下可重新启动。

```powershell
& tools/deployment/run-business.ps1 -Data runtime -Config runtime/studio.json -Background
```

更新程序文件后重启原业务进程，再刷新工作台。工作区静态文件可先于运行中的 Python 路由更新。部署验收从实际端口确认 `GET /api/daily-memory/preferences` 和 `GET /api/characters/deleted` 均返回 200，再打开个性化验证开关。评论筛选验收从活动页选「待审核」，确认请求返回 `comment_filter: pending_review`，列表和计数随之更新。

自主运营重启后保持待命，启动时检查原批次及就绪发送的授权。验证运营首页的积压、最老等待、发送延期以及安全收尾。真实平台观察和受控发送范围见[调度验收](../evaluations/results/operations-scheduler-20261114.md)。

号主逐字对话从 `POST /api/chat/intelligent/stream` 接收 SSE。当前 9B 直连可用。通过节点代理走语言生成的部署需同步更新代理程序，并从使用方验证首字与取消。业务日志记录阶段耗时和 token 用量。

运营使用 AGX 上独立的 llama.cpp CUDA 容器，已核验 `dafeiyu-qwen35-9b:1.0.0` 的 Q4_K_M 权重、Q8_0 投影器、262,144 窗口和 `q4_0` K/V。新安装的运营和直播均默认引用同一 9B，按固定 Hugging Face 发布版本准备资产，默认开启思考。直播使用 Genie 语音和图片／GIF 形象，9B 与 Genie 的共驻和首音待实机复验。 [部署指南](guides/deployment.md) · [可选 Live2D](guides/live/avatar.md)

电脑工作台保存的运营和直播语言模型均已指向现用 9B 端点，重载后从设置接口读回一致。现有 Genie 语音仍由原节点提供。直播档切换和实际播放待核验，改动前的业务配置留在版本目录的 `rollback/studio-before-live-9b-20261113.json`。

Hub 的 9B 模型卡已同步号主对话和故事板取材流程，以及真实运营记录的追问复测。模型资产使用固定 `1.0.0` 修订。 [Hugging Face 模型卡更新](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf/commit/1bdd0b6422d6bb82302fe5e74f7ccae6bf5ed0d3) · [发布卡源稿](model-card.md)

## 数据与恢复

- 11 月 11 日部署检查时，自动运营处于待命，告警计数为 0。角色页只有默认大肥鱼卡，保存覆盖当前卡片。私信与动态共用图库，14 张图已在工作台验证可用。
- 清理移除了 55 条旧未发起私信及 38 条关联归档移交，保留 19 条发送回执、2 条等待任务、原评论和视频进度。清理前备份为版本目录下的 `rollback/studio-before-legacy-outreach-cleanup.sqlite3`。
- 设备探测、训练分片、审核回放和回退配置留在本机 `runtime/releases/dafeiyu-qwen35-9b/1.0.0/`。恢复前停止业务服务，并核对备份之后的写入。工作站保留最终适配器和 GGUF。

## 最近核验

浏览器实测覆盖角色覆盖保存、评论审核、私信历史、动态草稿、共用选图和告警清零。30 个视频审核回放使用真实材料，平台写入为零。随后从业务库只读核对到评论和动态的成功回执，以及发布正文结构残片、生成额度耗尽和自有稿件读取失败。动态图和原评论链接的实际平台显示、真实通知和私信仍待核对。 [视频回放](../evaluations/results/review-agx-mainline-20261111.md) · [后续实跑](../evaluations/results/operations-feedback-20261112.md) · [后续验收](plans/current.md)
