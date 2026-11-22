# Batch 1 双脑全双工重构与 P2 视觉隔离实机验收 · 2026-11-22

使用本地核心代码对接工作站外部 Omni 网关（通过回环隔离传输，仅访问推理网关，真实平台写入为零；未触及正式库、工作站部署容器与阶段二／四代码）完成 Batch 1 全部四项实机验收。

## 隔离环境与模型配置

- **网关访问与隔离：** 仅通过回环端口访问外部 Omni 网关，HTTP 传输层阻断非网关外部请求（`blocked_requests = 0`），数据库发送与发布表写计数全部为 `0`。
- **模型规格：**
  - **前台脑（`omni-default`）：** `8,192` 上下文窗口；仅注入角色卡、瘦身自然对话（移除原始工具回执 JSON 拼接与 `WorkLifecycle oneOf` 工具目录）、极简活跃任务摘要与就绪记忆；流式文本生成关闭思考（`enable_thinking=False`）。
  - **后台路由脑（`llm-default` / `/llm/v1/responses`）：** `262,144` 上下文窗口；`OperationTurns` 最高优 `"chat"` 车道，采用确定性采样与串行工具调用（`temperature=0.0, parallel_tool_calls=False, store=False`）；文字路由 `route_chat_turn` 使用有界思考（`max_output_tokens=640, thinking_budget_tokens=160`）+ 原生多轮 `recent_dialogue` + 7 路平坦路由工具；语音分类 `route_voice_utterance` 使用轻量零思考（`thinking=False, max_output_tokens=256`）。

## 总览结果

| 验收项 | 入口 / 用例 | 实测结果 | 关键指标 / 证据 |
| --- | --- | --- | --- |
| Check A | `evaluations/omni_chat_loop.py --entry chat` | 通过 | `task_revision=2`，`local_reads=["operation_status"]`，`platform_writes=0`；首轮 `delegate_task`（`4,469 ms` / `3,281` in / `159` rtok），次轮 `steer_task`（`6,063 ms` / `4,388` in / `159` rtok） |
| Check B | 39 项全领域留出与双脑场景回放 | **39 / 39 全部通过** | 0 次 `503`、0 次错路；前台输入 `2,051–2,409 / 8,192` tokens（较 `4,762` 基线下降 **49.4%–56.9%**）；后台路由 `2,688–4,859 ms` |
| Check C（重启前） | `evaluations/front_tool_acceptance.py` 15 项（Microsoft Edge） | **15 / 15 全部通过** | `failed=[]`，`blocked_requests=0`，`platform_attempts` 全 `0`；前台调用 14 次 `2,067–2,206 / 8,192` tokens |
| Check C（重启后） | `--cases restart_resume`（Microsoft Edge） | **通过** | 同目录重启 Core 后恢复暂停的预约任务：`restored=true, same_due=true, revision=1`，`failed=[]` |
| Check D | 阶段三 P2 日常视频读帧（`llm-default`） | **4 / 4 全部通过** | `sufficient=true`，`elapsed_ms=17,187`（无 CID 泄漏，严格区分空公开字幕轨与画内烧录字）；详见 [P2 读帧报告](p2-ordinary-frame-reading-20261119.md#2026-11-22-视觉链路复验通过) |

## Check B 分组明细（39 项全过，JSON / SSE 交替）

| 分组 | 用例数 | 覆盖要点与实测表现 |
| --- | --- | --- |
| `screenshot` | 4 | 问钟点、邀请分享、问日期、追问年月日全部准确识别（`clock_fields` 与零工具闲聊），确定性钟点读数准确。 |
| `transfer` | 7 | 观看统计 → 视频列表 (`workshop_lookup`) → 歧义追问 `clarify` → 换题追问星期 (`chat` + `local_weekday`) 与连续三轮闲聊／设想／否定操作，换题不被上轮未答 `clarification` 锁死。 |
| `objects` | 5 | 已发动态列表与编号 (`workshop_lookup(dynamic)`) → 假想闲聊 → 记忆回查 (`recall_memory` 命中 `comment:BV1ab411c7mD:12:12`）→ 来源追踪 → 跨话题问日期 (`local_date`)。 |
| `operating` | 4 | 自主运营状态 → 假想暂停 → 打招呼 → 多轮对话后追问画布尺寸，准确命中 `instant_read(daily_settings, scene)` 并读回 `1920x1080`。 |
| `holdout` | 8 | 直播状态、防改画布玩笑、安抚闲聊、工作日历年月日、观看账本、已看视频原链、待发动态送审 (`prepare_publication`)、否定词「先别再接新活」守住闲聊边界。 |
| `outside` | 4 | 运营登录账号、小说设定闲聊、联网新闻查询、奇幻故事改编闲聊（0 次 `503`）。 |
| `bug1_pub` | 1 | 前文已绑定视频与观看历史对象时，「准备把待发动态发出去」仍直接进入 `prepare_publication(dynamics)`，不再被误裁成视频检索。 |
| `bug2_ambig` / `bug2_bv` | 2 | 同类两部视频绑定时，单数「那条」触发 `clarify` 列出两项；明确给出 `BV1ab411c7mE` 后直达对应记录。 |
| `voice_unified` | 3 | `private_call_work.classify` 走统一后台 `route_voice_utterance`：`conversation` (`828 ms`)、`delegate` (`1,234 ms`)、`revise` 绑定任务 ID (`2,407 ms`)。 |
| `nonblocking` | 1 | 后台任务阻塞期间前台并发闲聊即刻完成、不被阻塞；后台完成后通过事件流回推原会话（`status="completed"`）。 |

### Check B 计量实测汇总

- **前台脑（`omni-default`，23 次调用）：** 输入 `2,051–2,409` prompt tokens（占 `8,192` 窗口 **25.0%–29.4%**，较旧单脑首轮 `4,762` 下降 **49.4%–56.9%**），单次生成耗时 `875–2,828 ms`；零空正文故障，零英文元注泄漏。
- **后台路由脑（`llm-default`，35 次聊天路由调用）：** 输入 `3,308–4,512` tokens，推理耗时 `2,688–4,859 ms`，思考用量 `39–159` tokens；`route_voice_utterance` 3 次耗时 `828–2,407 ms`。

## 关键收敛项说明

1. **确定性路由采样与原生多轮：** `ResponsesClient.create` 补齐显式 `temperature` 透传并在 `turn_router` 固定 `temperature=0.0`；将 `recent_dialogue` 改为原生多轮消息并置当前 `message` 于末位，消除了长上下文错判历史句及画布查询退化。
2. **思考预算与工具列表序：** 将 `instant_read` 高频动作提前并剔除冗余 `video_activity`／`clock`，配合 `thinking_budget_tokens=160`，解决复杂多条件／动态查询在 `128` tokens 内提前截断导致误选的问题。
3. **澄清换题与结构校验：** `_build_router_messages` 与条件化 `clarification_id` 约束换题忽略上轮澄清；`_COMPACT_ARRANGEMENT_SCHEMA` 配合无预约词过滤防止立即任务误触发调度校验；`_disambiguate_route` 按 `recalled_source` 前缀分子类避免评论／回复与视频来源交叉误拦；`_LIVE_PROFILE` 规则保证「去看绑定号主 B 站主页」升级为 `owner_profile`。
