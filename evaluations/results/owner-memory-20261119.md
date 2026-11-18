# 号主记忆跨入口验收 · 2026-11-19

## 同一主帧的三次真实交付

Chromium 在隔离工坊确认设置原话后，文字对话自动将问句标题约定整理为条目 1 的 v2。三次交办均读取 BV1Hy4y1J7xH 的 P2、11–14 秒，分别取得 3 个带 CID 和时点的实际画面。三项标题均绑定主帧视觉锚点「宫廷场景」。独立 x86_64 设备用新登录会话读回每份提纲，正文与原端逐份一致。平台写入 0。[三份完整正文、实际帧与读回](owner-memory-three-editions-20261119.json)

| 编辑时的号主约定 | 成品标题 | 真实检查记录 |
| --- | --- | --- |
| 文字原话，条目 1 v2，问句并以问号结尾 | 这华丽的金色宝座是皇帝的象征吗？ | 1 条格式检查，首稿通过 |
| 语音确认，原条目 v3，陈述句并以句号结尾 | 皇帝宝座的华丽装饰。 | 仅修正第一项标题，重新核对后交付 |
| 另一设备按 v3 忘记，原端检索为空 | 金色宝座与蓝白石像环绕的宫廷场景 | 记忆引用为空 |

语音来自隔离号主获准使用的中文合成发音。独立服务的本地网关以普通应用凭据调用已部署的 Omni，对号主点选的 128,000 个麦克风样本完成一次转认。机器原稿是「好的，以后直播提纲的标题都用陈述句，以句号结尾。」；号主在浏览器预览中去掉误加的「好的，」，指定条目 1 并确认。另一设备同时读到了 v3 的确认稿、机器原稿及已改正标记。该轮原生最终用户转写事件为 0。验收返回 `semantic_status=observed`、`acceptance_passed=true`，关闭时 HTTP 任务与连接均为 0。[转写回包、确认和撤回](owner-memory-three-editions-20261119.json)

旧回放仅按末尾标点计算上述两个状态。重判保留跨端读回、原话、主帧和标点证据。第二版「皇帝宝座的华丽装饰。」整体指称装饰，缺少对主体的断言，未达陈述句要求。三版语义验收记为 `unmet`。原判分保存在 JSON 的 `original_runner_verdict`。

合并回归通过 106 个 pytest 用例，覆盖私有编辑、视频交付、记忆来源、历史召回、通话及 Chromium 设置入口，另有 2 个前端单测通过。Chromium 回归将实际首次角色状态延迟到输入原话之后送达，核对焦点、原文和确认保存。可设置 `TUBER_TEST_CHROMIUM=1` 与 `CHROMIUM_EXECUTABLE` 后在仓库根目录运行：

```sh
uv run --locked pytest -q tests/test_private_editor_contract.py tests/test_owner_memory_entries.py tests/test_private_call.py tests/test_private_task_history.py tests/test_video_delivery.py tests/test_daily_memory_provenance.py tests/test_daily_memory_passive_recall.py tests/test_memory_sources.py
node --test tests/web_personalization.test.cjs tests/web_private_call.test.cjs
```

## 历次对照与回归

Windows 上的 Chromium 156.0.8078.4 登录隔离工坊，在设置页预览并确认了号主提供的原文 “Group related points and separate distinct concerns. Use headings and lists when they improve readability.”。关闭页面后，另一台 ARM64 设备通过仅绑定回环的 SSH 转发访问同一个工坊。该设备首次未登录读取返回 401，独立登录后取得相同原文、设置来源及修订 1。该设备按修订号忘记这条偏好后，原设备检索为空。本次使用新建业务库，平台写入 0。

| 验收项 | 取得的证据 |
| --- | --- |
| 精确旧来源 | 工坊实际 HTTP 搜索入口。在同一偏好有两条号主原话且已有八条模糊命中时，精确命中的旧来源排第一，同时返回旧引语与当前笔记。 |
| 私有视频提纲 | 隔离传输经过任务 API、匿名分 P 取帧、视觉读取、Responses 取材和最终 Omni 编辑入口。偏好只随私有编辑请求传递。第一版使用问句标题，打开历史成果、忘记、改口再取材后，第二版使用陈述标题。在途删除阻止了第一版落库。此项使用可控的推理与视频传输。 |
| 最终用户转写 | Chromium 协议夹具覆盖已提交的用户音频、最终转写、点击确认和跨端读回。更正场景在同一条目上产生新版本及新原话，另一客户端核对了当前版本，旧确认返回 412。真实服务最终转写单独记录。 |
| 真实 Omni 初次回放 | 已确认自动整理和 3 条 P2 取材。第一版引用了号主的结论先行偏好，实际先写画面再给建议，语义未通过。撤回时旧修订触发冲突。保留了[原始提纲与判读](owner-memory-live-before-20261119.json)。 |
| 真实 Omni 问句标题对照 | 号主确认设置来源后，通过文字对话给出“每个标题都用问句，以问号结尾”。另一台 x86_64 设备读到自动整理的 v2。工坊对同一 BV 的 P2、11–14 秒完成两次私有取材。第一份标题为「宝座前站着的人是谁？」；对端按 v2 忘记后，同一交办的标题为「古宫宝座」，引用为空。两份成品均从另一设备读回。平台写入 0。[实际正文与取材回执摘要](owner-memory-live-titles-20261119.json) |
| 部署中的原生双工 | Chromium 发送 22 秒 PCM，服务确认一个用户音频条目，返回了助手语音及助手字幕，最终用户转写事件为 0。该轮没有触发语音更正。输入为号主授权 QA 句子的中文合成发音。 |
| 独立设备的语音更正 | 普通应用凭据连接真实 Omni，本机运行独立服务网关。真实 Chromium 在号主选择的 8 秒录音上取得「好的，以后直播提纲的标题都用陈述句，以句号结尾。」。预览按授权测试原句改正后，另一台 x86_64 设备读回原条目 v2，同时核实了含“好的”的机器原稿与不含该词的号主确认稿。忘记 v2 后原端检索为空，服务退出时剩余 HTTP 任务与连接均为 0。[机器输出、确认稿和跨端读回](owner-memory-voice-confirmation-20261119.json) |
| 真实短片标题失败对照 | 同一画面的三份成品依次引用问句偏好 v2、语音确认的陈述句偏好 v3、零条偏好，标题均为「宫廷场景」。该轮语音原稿准确，独立设备读回 v3；两项有效偏好均未落实到标题。后续在成品交付前加入写作约定核对。[三版标题、来源修订与采样时点](owner-memory-clip-title-failure-20261119.json) |
| 应用绑定约定后的真实复验（`c046fad`） | 三份实际标题仍为「宫廷场景」。问句和陈述句两版的检查记录都选中了条目 1，却生成 0 条格式检查，语义审稿也未要求改写。跨端来源和撤回事务通过，成品验收返回 false，退出时剩余 HTTP 任务和连接均为 0。对照文件的 `after_application_bound_review` 保存三份正文和同轮的审核数据。[逐版读回](owner-memory-clip-title-failure-20261119.json) |
| 字面约定交付拦截（`a3c287d`） | 同一 P2 读取到 3 个带时点的真实画面。第一版修稿后仍未满足约定 #1 的 `items.0.title`，任务失败且没有可交付成果。这轮未进入语音或忘记阶段，关闭时没有遗留 HTTP 任务或连接。[失败消息与实际帧](owner-memory-clip-title-failure-20261119.json) |

运行独立设备验收需可用的 SSH 客户端别名及对端 Python 3。设置普通网关连接后，完整回放在文字约定、语音更正、忘记偏好三个阶段分别向同一 BV、P2 和时间区间交办任务。普通 token 可为原生 Realtime 签发一次性票据。结构对照使用号主授权的隔离 QA 问句标题偏好，并以合成语音朗读改为陈述句的要求，`--voice-wav` 可指定获准发送的语音。每次回放在本地 tmp 下输出独立 result.json，记录转写事件、号主确认的原话、各阶段实际交付及跨端读回。

```sh
uv run --locked python -m evaluations.owner_memory_device --device <ssh-alias> --live --voice-wav <speech.wav>
uv run --locked python -m evaluations.owner_memory_device --device <ssh-alias> --live --gateway-checkout ../omni-avatar-api
uv run --locked python -m evaluations.owner_memory_device --device <ssh-alias> --live --gateway-checkout ../omni-avatar-api --voice-only
uv run --locked pytest -q tests/test_owner_memory_entries.py
```

`--gateway-checkout` 只对私聊使用本地签票网关和远端普通应用连接，用于在部署前验证显式转认。录音要经过产品的开始、结束与确认操作；报告分别记录原生 final 数量、一次转认回包、所选条目的版本，以及独立设备读回的原话。
