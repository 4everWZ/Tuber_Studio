# 私聊中的实时 Avatar

在工作站 HTTPS 对话页打开「更多对话操作 → 私有语音通话」。默认仅语音。两套形象均已验收、当前角色匹配，并且服务实报两者均已就绪时，可在开始通话前选择「常规版」或「Q 版」。造型采用号主选定的两张 OC 成图。原始设计图用于核对生物结构、人物比例、鳍耳和发饰，原稿绑定保留用于回归。

## 登记本站验收结果

沿用普通私聊服务的 `TUBER_PRIVATE_REALTIME_ORIGIN`、`TUBER_PRIVATE_REALTIME_TOKEN_FILE`。由独立媒体服务加载两个原画 INP 文件，在它的私有登记表中各分配一个形象 ID。工坊使用自己的普通应用凭据查询这两个 ID。返回 404 表示该服务没有登记此形象，503 表示需要检查服务侧素材或驱动。两套分别完成接收端形象、中文口型、打断及混合负载验收后，在 Studio 的运行目录下写入 `private-call-avatars.json`。下例中的 ID 为占位值。原稿、真实 ID、凭据和验收配置留在本站。

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

两份母版在 Ditto 上经过逐图校准仍有静音闭唇问题。工坊现有两套原画的连续二维绑定，均已用真实 Omni 语音在浏览器通话。正式启用还要核对目标工作站媒体到号主浏览器的持续通话表现，以及共享网关两条形象记录的就绪状态。[Ditto 原稿诊断](../../evaluations/results/ditto-master-mouth-20261118.md) · [工作站原画实测](../../evaluations/results/inochi-whep-master-rigs-20261119.md) · [真实通话](../../evaluations/results/private-rig-omni-voice-20261119.md)

## 通话与回执

浏览器使用短时票据连接实时语音。选择形象后，Studio 以网关返回的 `source_session_id` 绑定同一次 realtime 会话，代理 SDP 协商与订阅释放，再交给浏览器仅限本次 Avatar 事件路径的一次性票据。普通应用 token 留在 Studio 服务端。

WHEP 播放器接收音频和视频。Avatar 回执使用订阅起点加播放器实际播放时间；实时语音回执再映射到当前代次、回复、音频分段及连续听到的前缀。静音和暂停期间停止回执。「打断回答」发出取消与清空，浏览器关闭旧接收器，按新代次重新订阅。媒体故障时后续回复回退到原生语音。挂断、关闭对话框或离开工作区会释放订阅和会话。

填写话题并引用号主偏好时也能选择原画。通话先订阅所引用记忆的版本。号主在另一端更正或删除记忆后，Studio 通知播放器并释放绑定的形象会话。浏览器取消旧回复、停止音视频轨道、清空缓冲并关闭旧 Realtime。角色和授权一致时，以当前偏好、新票据和同一形象续开，保留静音选择。无法核对记忆或权限时结束通话。

## 隔离验收

在 Studio 项目环境执行 `python tests/browser/run_private_call.py --whep`。默认从相邻的 `omni-avatar-api` 项目环境启动真实 Gateway、Avatar 服务与 aiortc，Chromium 通过工坊入口依次选择两套形象。测试使用临时业务目录、模拟语音引擎和替身渲染器，检查媒体解码、两种回执、静音、打断换代、挂断和媒体故障回退。两套生产原稿的视觉质量须独立验收。

要核对真实 rig 渲染与记忆撤回，可执行 `python tests/browser/run_private_call.py --rig-dir runtime/releases/dafeiyu-avatar/master-rigs`。[两套原画与跨端撤回验收](../../evaluations/results/private-rig-memory-20261119.md)

要验证真实 Omni 语音，配置 `OMNI_GATEWAY_URL` 和普通应用的 `OMNI_CORE_TOKEN_FILE` 或 `OMNI_STUDIO_TOKEN`，执行 `uv run --locked python -m evaluations.private_rig_call`。命令使用同一个临时工坊依次呼叫两套原画，默认把仓库的 `assets/whale/sample1.wav` 接入 Chromium 麦克风。可用 `--wav` 指定获准发送的语音。运行环境需要 FFmpeg、Chromium 和相邻独立服务的项目环境。每套形象输出一行实测结果；任一项未达标会以非零状态退出。[真实语音与原画播放读回](../../evaluations/results/private-rig-omni-voice-20261119.md)
