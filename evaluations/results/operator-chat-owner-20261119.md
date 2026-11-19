# 角色私聊的绑定号主与运营账号

2026-11-19，正式 Core 的业务 SQLite 以只读方式核对。两轮均为大肥鱼（`whale` 第 3 版），`submission.intent` 为空。该角色卡的 `bilibili_owner` 与运营账号缓存 `bilibili_account_last_profile.profile` 的 UID、昵称各不相同。角色卡已有绑定。

| 正式回执 | 用户原话 | 保存的路由和回答 |
| --- | --- | --- |
| `operator_chat_turns.rowid=26`，06:42:31 UTC | 小鱼，你的主人是谁呀 | `result.memory_intent=chat`，回答寄居在 Jetson |
| `operator_chat_turns.rowid=27`，06:42:42 UTC | 我说你的主人呀 | `result.memory_intent=clarify`，回答“你指的是哪一件事？” |

Jetson 的文字来源可在运营账号简介的 `profile.bio` 查到。当前角色卡里没有 Jetson。旧入口将运营账号简介放在 provider 的系统身份块，将绑定人的资料放在较早的 user 消息，还会把没有平台 UID 的私聊标成 `speaker_is_bilibili_owner=false`。第 25 轮曾提及主人的 B 站账号，带有 `organized_memory`，按部署代码排除在普通聊天前文之外。第 24 轮的闲聊可引用，第 26 轮的问答也符合第 27 轮的引用条件。两轮都没有成果卡；核查时正式库的交办表为空。第二轮文案与 `clarify` 未提供具体问题时的后端默认回复一致。正式回执没有保存当时完整的网关请求，无法仅凭这些字段确认模型选择措辞的原因。

私聊入口现在把当前角色卡的绑定人作为系统提供的参与者资料，和运营账号简介分别标明归属。只有带 B 站发言 UID 的事件才比较发言人与绑定人的身份。未绑定的角色传空值。聊天路由按角色资料与前文承接可直接回答的追问，交办缺少执行对象或要求时仍可澄清。

## 工作站普通网关回放

变更前后各建一个隔离库，使用同一份已存角色卡、运营账号公开资料和符合条件的第 24 轮问答。使用隔离库自己的空平台凭据重建资料缓存；未导入正式登录态或私聊记忆。连续向网页使用的 `POST /api/chat/intelligent/stream` 发上述原句，只传消息和新请求 ID，由后端决定路由。使用工作站普通网关 `omni-default`、模型卡窗口、`structural_tag` 和 4096 输出预算。每轮记录发往聊天补全接口的消息体、SSE 及隔离库回执。

| 版本 | 第一轮 | 第二轮 |
| --- | --- | --- |
| 修正前 | `chat`，回答住在海底小屋 | `chat`，再次回答住在海底小屋 |
| 修正后 | `chat`，回答绑定号主昵称及 UID | `chat`，再次答出相同昵称及 UID |

四轮各发生一次聊天补全。修正前的实发请求同时包含账号简介和用户消息中的绑定人；重现了将身份问题答成住所，未复现第二轮的澄清路由。修正后的两个请求均在系统消息中带入卡片绑定人，仍带有含 Jetson 的账号简介。第二轮实发消息包含首轮完整问答。独立对照角色卡，两个回答的 UID 均逐字一致，昵称均使用简体，卡片登记字形是繁体。

本地私有证据目录为 `runtime/private-artifacts/operator-owner-turns-20261119/`。`formal.json` 保存选取的正式字段，`before/`、`after/` 各自的 `turn-26.json`、`turn-27.json` 保存实发消息、回复和路由。提交中仅保留脱敏结论。

`uv run --locked pytest -q tests/test_character_oc_types.py tests/test_intelligent_ui_chat.py` 通过 66 项。角色测试覆盖更名、解绑、平台 UID 匹配以及私聊无平台 UID。平台写入为零，未部署。
