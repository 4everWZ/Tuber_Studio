# 私聊中的实时 Avatar

在对话页打开「更多对话操作 → 私有语音通话」。默认仅语音。两套形象均已验收、当前角色匹配，并且服务实报两者均已就绪时，可在开始通话前选择「常规原稿」或「Q 版原稿」。

## 登记本站验收结果

沿用普通私聊服务的 `TUBER_PRIVATE_REALTIME_ORIGIN`、`TUBER_PRIVATE_REALTIME_TOKEN_FILE`。以同一普通应用身份登记两份原稿，分别核对接收端的形象、实际语音口型与停顿。通过后，在 Studio 的运行目录下写入 `private-call-avatars.json`。下例的 ID 为占位值；将每项的 `accepted` 按实际验收结果填写。原稿、真实 ID、凭据和验收配置留在本站。

```json
{
  "character_id": "whale",
  "origin": "http://127.0.0.1:8080",
  "profiles": {
    "original": {"avatar_id": "example-regular", "driver": "inochi2d", "accepted": false},
    "q": {"avatar_id": "example-q", "driver": "inochi2d", "accepted": false}
  }
}
```

打开选择器和绑定通话时，Studio 分别查询服务登记的两个 ID，核对 ID、驱动及就绪状态。Inochi2D 使用部署方公布的 rig ID；Ditto 使用登记后返回的形象 ID，旧配置省略驱动时按 Ditto 读取。改稿后换成新形象 ID 并重新验收。服务地址、角色、授权或本站验收变化会使原选择凭据失效。浏览器须能连通网关 WebSocket 及 WHEP 应答中的 ICE 地址。

2026-11-18 的两份母版短片均未观察到闭唇，两份跨机录像也均未达到 25 fps。这次实测不构成启用两套配置的依据。[原稿口型与 WHEP 实测](../../evaluations/results/ditto-master-mouth-20261118.md)

## 通话与回执

浏览器使用短时票据连接实时语音。选择形象后，Studio 以网关返回的 `source_session_id` 绑定同一次 realtime 会话，代理 SDP 协商与订阅释放，再交给浏览器仅限本次 Avatar 事件路径的一次性票据。普通应用 token 留在 Studio 服务端。

WHEP 播放器接收音频和视频。Avatar 回执使用订阅起点加播放器实际播放时间；实时语音回执再映射到当前代次、回复、音频分段及连续听到的前缀。静音和暂停期间停止回执。「打断回答」发出取消与清空，浏览器关闭旧接收器，按新代次重新订阅。媒体故障时后续回复回退到原生语音。挂断、关闭对话框或离开工作区会释放订阅和会话。

填写话题并引用号主偏好时也能选择原画。通话先订阅所引用记忆的版本。号主在另一端更正或删除记忆后，Studio 通知播放器并释放绑定的形象会话。浏览器取消旧回复、停止音视频轨道、清空缓冲并关闭旧 Realtime。角色和授权一致时，以当前偏好、新票据和同一形象续开，保留静音选择。无法核对记忆或权限时结束通话。

## 隔离验收

在 Studio 项目环境执行 `python tests/browser/run_private_call.py --whep`。默认从相邻的 `omni-avatar-api` 项目环境启动真实 Gateway、Avatar 服务与 aiortc，Chromium 通过工坊入口依次选择两套形象。测试使用临时业务目录、模拟语音引擎和替身渲染器，检查媒体解码、两种回执、静音、打断换代、挂断和媒体故障回退。两套生产原稿的视觉质量须独立验收。

要核对真实 rig 渲染与记忆撤回，可执行 `python tests/browser/run_private_call.py --rig-dir runtime/releases/dafeiyu-avatar/master-rigs`。[两套原画与跨端撤回验收](../../evaluations/results/private-rig-memory-20261119.md)
