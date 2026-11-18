# 直播交接后实际播完

2026-11-19，在隔离业务目录启动 Studio 与 Chromium 演出页，两次交接同一段 4.2 秒的样例语音。交接经任务审批，台词和选用音频固定。浏览器从普通播放器入口播放，记录原生媒体事件。关闭 Studio 后通过只读 SQLite 连接，按交接请求 ID 联查任务、节目作业和「说过」记录。

| 操作 | 浏览器读回 | 业务库读回 |
|---|---|---|
| 播放到底 | 收到可信的 `playing`、`ended`，结束位置 4.2/4.2 秒 | 作业和交接步骤完成，有且只有一条相符的台词 |
| 播放中跳过 | 收到可信的 `playing`，未收到 `ended` | 作业及步骤取消，无已播台词 |

受控平台适配器发送数与数据库平台写入数均为零。此项验证演出页实际播放和结算。另行核对节目状态时，真实媒体桥隔离实例的 GET 返回 `idle`；任务和聊天中对 `publishing` 的展示、异常状态与断线处理使用模拟响应。此次没有在平台开播。

复跑使用 `uv run --locked pytest -qs tests/browser/live_handoff_check.py` 和 `uv run --locked pytest -q tests/test_omni_media.py tests/test_agent_tasks.py -k "idle_from_actual_bridge or live_status"`。
