# 号主记忆跨入口验收 · 2026-11-19

Windows 上的 Chromium 156.0.8078.4 登录隔离工坊，在设置页预览并确认了号主提供的原文 “Group related points and separate distinct concerns. Use headings and lists when they improve readability.”。关闭页面后，另一台 ARM64 设备通过仅绑定回环的 SSH 转发访问同一个工坊。该设备首次未登录读取返回 401，独立登录后取得相同原文、设置来源及修订 1。该设备按修订号忘记这条偏好后，原设备检索为空。本次使用新建业务库，平台写入 0。

| 验收项 | 取得的证据 |
| --- | --- |
| 精确旧来源 | 工坊实际 HTTP 搜索入口。在同一偏好有两条号主原话且已有八条模糊命中时，精确命中的旧来源排第一，同时返回旧引语与当前笔记。 |
| 私有视频提纲 | 隔离传输经过任务 API、匿名分 P 取帧、视觉读取、Responses 取材和最终 Omni 编辑入口。偏好只随私有编辑请求传递。第一版使用问句标题，打开历史成果、忘记、改口再取材后，第二版使用陈述标题。在途删除阻止了第一版落库。此项使用可控的推理与视频传输。 |
| 最终用户转写 | Chromium 协议夹具覆盖已提交的用户音频、最终转写、点击确认和跨端读回。此轮没有取得真实服务的最终转写。 |
| 真实 Omni 自动长期偏好与前后两份提纲 | 当前验收进程没有配置普通应用令牌和网关地址，此项未取得真实模型证据。 |

运行独立设备验收需可用的 SSH 客户端别名及对端 Python 3。设置普通网关连接后，`--live` 使用相同 BV、P2 和时间区间各创建一项任务，并输出前后两份实际成果以便检查文字。语音部分使用私聊已配置的短时票据服务，`--voice-wav` 指定获准发送的语音输入。

```sh
uv run --locked python -m evaluations.owner_memory_device --device <ssh-alias> --live --voice-wav <speech.wav>
uv run --locked pytest -q tests/test_owner_memory_entries.py
```
