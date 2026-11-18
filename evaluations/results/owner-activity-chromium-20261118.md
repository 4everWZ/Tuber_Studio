# Chromium 双会话续办与限定发布

2026-11-18 在工坊工作树（HEAD `7d91a05`）运行通过，退出码 0。Chromium 156.0.8078.4，视口 1280 × 800。两个独立 browser context 在登录页分别输入同一临时号主令牌，各自取得不同的 HttpOnly 会话。

| 页面操作 | 核对结果 |
| --- | --- |
| 会话 A 从对话快捷操作交办“先核对动态草稿 #1 的原评论出处，等我决定是否发布。” | 工坊读取了草稿的原评论回执。任务进入等待号主决定，页面没有发布按钮。关闭 A 的页面和整个 context。 |
| 会话 B 登录，查看对话历史，从活动列表打开原任务并补充要求 | 找回相同的对话轮次和任务 ID `c4507488-8050-4d1f-9fa2-6c3b21ee0791`。新要求为“只发布动态草稿 #1，保留原评论出处，完成后给我回执。”任务修订号从 r1 变为 r2。 |
| B 核对审批预览后点“确认公开发布这条动态” | 审批对象为 **B站 UID 100 的公开动态 · 草稿 #1 · 大肥鱼 v1 · 含配图**。来源链接正确，配图在 Chromium 中解码成功。页面正文与假平台收到的文本逐字相同，含空行和评论锚点。批准的唯一一步属于 r2 和草稿 #1。 |
| B 在活动中编辑私有成果、保存、关闭活动 | 焦点回到活动按钮。打开活动前和返回后，首条可见消息都是“素材札记 06”，消息 ID 相同，顶部偏移均为 −25.21875 px，聊天滚动位置均为 714 px。 |
| B 刷新页面，从历史任务卡重新打开 | 任务仍为 r2，成果为 v2，编辑新增的开场备忘可见。 |

核对的完整发布正文如下。

```text
今晚开场分享海底厨房的来信。

原评论：https://www.bilibili.com/video/BV1d8pU6xEZX#reply42
```

批准前，两个草稿都待审，假平台请求和业务发送账本均为 0。批准后，图文发布客户端向隔离传输中的 `platform.example.test` 发起一次图片上传和一次动态创建。业务账本记录一次发送，草稿 #1 保存测试回执 `99001`，对照草稿 #2 仍待审且没有回执。浏览器外部请求和工坊出站连接计数均为 0，**真实平台写入 0**。

## 复跑

从仓库根目录运行。需要项目锁定的 Python 环境、Node、可由 Node 解析的 Playwright 和 Chromium。夹具支持本项目安装的 Playwright，以及 Windows 上全局 Playwright CLI 自带的包。使用 Playwright 配套浏览器时运行第一条命令即可。

```powershell
uv run --locked python tests/browser/owner_activity_fixture.py
```

本次使用本机已有的 Chromium 1248 构建，通过环境变量指定可执行文件。

```powershell
$env:CHROMIUM_EXECUTABLE = (Get-ChildItem "$env:LOCALAPPDATA/ms-playwright/chromium-1248" -Recurse -Filter chrome.exe | Select-Object -First 1).FullName
uv run --locked python tests/browser/owner_activity_fixture.py
```

[工坊夹具](../../tests/browser/owner_activity_fixture.py) 在回环随机端口启动实际应用，使用临时业务库、合成草稿及合成账号。模型的工具选择固定，任务调度、来源核验、审批、发布请求构造、回执和成果保存走应用实现。平台响应由内存 HTTP transport 提供。[浏览器脚本](../../tests/browser/owner-activity.cjs) 通过可见控件完成操作，只读观察接口用于核对传输与落库。每次运行输出当次的任务 ID、位置、请求和回执，并清理临时库。本项测量的是 B 打开和关闭活动时的阅读位置；模型语义与真实平台送达需分别验收。
