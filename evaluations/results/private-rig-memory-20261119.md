# 原画通话与跨端记忆撤回

2026-11-19，Studio、Gateway、Avatar 服务和 Chromium 在回环端口运行。临时注册表绑定常规与 Q 版各自的母版 INP。音视频经实际 Inochi2D、aiortc 和浏览器播放器传输，麦克风和语音引擎提供受控音频。两套原画各自验了播放、静音、打断换代和挂断，共建立四次成功订阅。采样时常规版回复已播 384 ms，形象时间线 2531 ms；Q 版分别为 370 ms 和 2235 ms。单独注入 WHEP 503 后，下一条语音收到原生播放回执。

随后让 Q 版引用号主已确认的偏好，在另一客户端先更正，再删除。三次 Realtime 握手分别含原偏好、仅含更正后的偏好、两者均无。更正时读到取消和清空事件、旧浏览器轨道结束；两次变化后旧服务会话均关闭、订阅数归零，新来源均能播放并回执。生产地址、注册 ID、实际推理以及工作站帧率和人眼口型质量仍需现场验收。

复跑命令为 `uv run --locked python tests/browser/run_private_call.py --rig-dir runtime/releases/dafeiyu-avatar/master-rigs`。
