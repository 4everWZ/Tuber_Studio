# 自主浏览与互动验收

目标：按用户选定的tmp/Tuber_Studio_Autonomous_Browsing_Interaction_SRS_v0.1.md完成文本路线；正式行为由[自主浏览契约](../specs/autonomous-browsing.md)维护。本计划只维护未完成验收和需求映射，不保存原稿副本。

代码阶段已完成：发现与CC、内容／兴趣判断、主动留言／通知、审核、私信移交、账号仲裁、停止恢复、前端及缓存。[代码验收](../../evaluations/results/autonomous-browsing-acceptance-20261101.json)包含功能故障测试、真实应用浏览器流程和Jetson并发准备；不等于真实账号或模型语义通过。新自动化关闭，视觉不部署，现有环境指纹保持不变。

| 验收范围 | 已有证据 | 剩余边界 |
| --- | --- | --- |
| AC-01–03：覆盖、独立入口、紧停／重启 | test_operations_control、test_browsing_api；[真实应用UI](../../evaluations/results/autonomous-browsing-ui-20261101.json) | 不开启操作者真实账号收发 |
| AC-04–08：发现、筛选、CC与引用 | test_bilibili_browsing、test_browsing_evidence、test_browsing；长字幕按Provider token分块且保留全部引用 | 当前账号推荐与真实CC访问权限待验 |
| AC-09：视觉契约 | test_browsing_evidence受控Provider、禁用／取消／实际帧时间 | 无真实视觉模型，不宣称图像理解 |
| AC-10–15：互动、去重、移交与关系 | test_browsing、test_browsing_notifications、test_account_relationships | Mini语义未通过，保持审核 |
| AC-16–19：授权、额度、一次提交与可见性 | test_operations_control、test_comments、test_direct_messages、test_bilibili_browsing | 无真实平台写入回执；不得自动补发 |
| AC-20–23：并发、租约、预算与顺序 | test_browsing_store、test_generation_meter、test_browsing_notifications、test_browsing | 真实平台限流及长期负载待账号验收 |
| AC-24：并发资源 | [Jetson准备测量](../../evaluations/results/autonomous-browsing-preparation-20261101.json)：Worker1/4相同24请求，冷缓存约1.206×，热缓存0请求 | 源响应／延迟仿真，非B站网络或GPU并行测量 |
| AC-25–26：界面、隐私、材料失效 | UI、API、证据及缓存测试；音乐仍最高15GiB，清理后不从任务表恢复字幕正文 | 真实平台场景仍需核验 |

## 下一步

- [Mini实测](../../evaluations/results/autonomous-browsing-mini-20261101.json)保留思考与2048输出额度：12/12结构完整、7/12语义判据通过，主要问题为公开／私信误判和注入互动未跳过。结构化约束修复了空正文／漏引用，不能据此解除语义审核门槛。后续使用相同样本及人工判据验收，不以加额度或并发替代质量。
- 具备账号后，按[外部服务与账号计划](p0-runtime.md)验证推荐、CC、@、收到回复、文字动态及私信读取；写入仅选择获明确授权的具体用例并保存回执。读取不证明写权限，当前不发平台消息。
- 环境与代码保持分离；此实现不新增依赖、常驻模型或Node进程，不需要重建／拉取环境镜像。
