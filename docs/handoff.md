# 部署交接 · 2026-11-11

## 运行位置

电脑工作台使用 [本机端口 9020](http://127.0.0.1:9020/)，项目根目录下可重新启动。

```powershell
& tools/deployment/run-business.ps1 -Data runtime -Config runtime/studio.json -Background
```

运营使用 AGX 上独立的 llama.cpp CUDA 容器。模型别名为 `dafeiyu-qwen35-9b:1.0.0`，Q4_K_M 权重和 Q8_0 投影器配套使用，实际窗口为 262,144，K/V 均为 `q4_0`，思考开启。直播使用原有 MiniCPM5-2B＋Genie 节点，默认形象为图片／GIF。 [部署指南](guides/deployment.md) · [可选 Live2D](guides/live/avatar.md)

## 数据与恢复

- 上次检查时自动运营处于待命，告警计数为 0。角色页只有默认大肥鱼卡，保存覆盖当前卡片。私信与动态共用图库，14 张图已在工作台验证可用。
- 清理移除了 55 条旧未发起私信及 38 条关联归档移交，保留 19 条发送回执、2 条等待任务、原评论和视频进度。清理前备份为版本目录下的 `rollback/studio-before-legacy-outreach-cleanup.sqlite3`。
- 设备探测、训练分片、审核回放和回退配置留在本机 `runtime/releases/dafeiyu-qwen35-9b/1.0.0/`。恢复前停止业务服务，并核对备份之后的写入。工作站保留最终适配器和 GGUF。

## 最近核验

浏览器实测覆盖角色覆盖保存、评论审核、私信历史、动态草稿、共用选图和告警清零。更新后的 9B 实跑公开评论和邀请私信两条隔离生成，公开评论样例仍有语义偏差。30 个视频审核回放使用真实材料，平台写入为零。动态原评论图和链接已在工作台核对，真实发布仍待操作。 [视频回放](../evaluations/results/review-agx-mainline-20261111.md) · [后续验收](plans/current.md)
