# 私有视频交办的双 Chromium 验收

从仓库根目录运行。默认素材为 `BV1Hy4y1J7xH` 的 P2；`live` 和 `receipt` 均接受 `--bvid` 与 `--part`。两种入口都需要项目的锁定 Python 环境和 PATH 上的 ffprobe。

## 只读复核已有运行

```powershell
uv run --locked python -m evaluations.creative_browser receipt tmp/p2-live-chromium-c970c0c89a
```

位置参数是同一次运行的私有目录，应含 `browser.json`、`r1.json`、`r2.json`、`studio.sqlite3` 和 `browsing/private-clips/`。命令只读数据库、回执和 MP4，无需联网或凭据。输出每个实际 edition 的两项标题、理由、来源、分 P 时间链接、CID、窗口、帧数和对应媒体路径。示例旧回执保存了回跳成功标记；新版还记录回跳前后聊天行的 ID、偏移和滚动位置。候选提示、恢复副本、任务控制改口均不能充当全新普通交办。

## 发起一次新运行

工作站可用时，在进程环境中提供 `OMNI_GATEWAY_URL` 和应用级 `OMNI_STUDIO_TOKEN`。可用 `OMNI_MODEL_ALIAS`、`OMNI_LLM_MODEL_ALIAS` 指定模型别名。本机需要 ffmpeg 及 Chromium。首次安装项目配套浏览器可运行 `uv run --locked python -m playwright install chromium`，也可用 `--chromium` 指向现有可执行文件。

```powershell
uv run --locked python -m evaluations.creative_browser live --bvid BV1Hy4y1J7xH --part 2
```

命令在 `tmp/creative-browser-*/` 启动新的回环工坊，使用独立数据库、浏览器配置和匿名平台读取。后台只开放视频、网页和网页检索工具。服务端 HTTP 写请求仅放行配置网关的 Chat、token 计数及 Responses 推理端点。运行队列繁忙时可先执行上面的只读复核。

1. 设备 A 在普通聊天框请求两个不剧透片段和原出处，不提示候选窗口。在任务仍在推进时闲聊，读取已完成回复，关闭 Chromium 进程。
2. 设备 B 启动新的 Chromium，通过历史消息打开原任务。读到首版后，在聊天框说“保留第一段，第二段换成适合开场的”。每版按该任务的实际修订号和 edition 保存快照。
3. 刷新页面，从当前任务展开“历史成果”，点击首版。比对这次下钻返回的完整 edition 和原首版，从历史正文打开原视频、核对 BV、分 P 和秒数，再关闭原片和活动面板，检查聊天阅读位置。
4. 只读核对聊天路由、真实成果表、每项投稿署名、检索后取得的外部正文和短片回执，用 ffprobe 解码计帧并核对时长。第一项须保留，第二项须换到与原版两项均不重叠的窗口，平台写入尝试须为零。

成功返回 0，并在运行目录生成 `workflow.json`。失败时返回非零，可查看 `browser.json` 的最后阶段和已取得的版本快照。单次聊天等待默认 240 秒，每版交付等待默认 900 秒，可用 `--turn-timeout` 和 `--edition-timeout` 调整。

`workflow: passed` 仅表示这条交互及成果关联链路通过。只交付一项时仍保留实际正文，双段替换用例记为未通过。标题是否写实、整段是否剧透、出处是否支持主张和开场理由是否成立，需要独立内容审核。已有的 `independent-field-objections.json` 会随复核结果列出。
