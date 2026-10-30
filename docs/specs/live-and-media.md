# 直播、演出与音乐契约

非直播工作台与本地Mock直播均可用。MiniCPM5-2B／真实128K与Genie TTS已完成共驻、动态语音与浏览器真实播放ACK验证，停止、text卸载TTS及回live已验证；资源条件和持续运行边界由[资源规范](models-and-audio.md)维护，证据见[当前声音与共驻结果](../../evaluations/results/yaoyao-v2-voice-20261030.json)。模板仍默认text且关闭直播，设备须由Operator显式启用Mock／live，导航不自动切换。B站／抖音真实直播接入仍后置；Mock播放不代表授权Adapter、完整调度策略、音乐或OBS本体已验收。以下标签保留原优先级，不表示全部条目已完成。

## 接入与调度

| ID | 原优先级 | 要求 |
| --- | --- | --- |
| LIVE-01 | P0 | MUST Mock可注入弹幕、礼物、点歌、断线等，不需真实开播。 |
| LIVE-02 | P1 | MUST B站Adapter优先使用授权开放平台，认证、心跳、连接／重连封装在内；权限不足明确不可用。抖音另按实际权限适配。 |
| LIVE-03 | P0 | MUST 统一平台、房间、事件ID、用户ID、类型、时间、内容、礼物／金额元数据。 |
| LIVE-04 | P0 | MUST 去重、限流、垃圾过滤、TTL，重复不重复播报。 |
| LIVE-05 | P1 | MUST 有界优先级队列按价值、等待、同用户冷却、预计播报时长安排，不FIFO读完。 |
| LIVE-06 | P1 | MUST 礼物优先但保留普通互动份额，比例及连续抢占数可配置。 |
| LIVE-07 | P1 | MUST 相似话题去重／合并，超限合并或丢弃低价值旧事件，不阻塞到OOM。 |
| LIVE-08 | P0 | MUST 人工暂停／恢复、静音、跳过、清队列及紧急停音。 |
| LIVE-09 | P1 | SHOULD 冷场话题／预制台词／GIF，有最小冷却和最大发言频率，不无限推理。 |
| LIVE-10 | P1 | MUST 断线、超时、模型失败有限重试／降级，错误可见。 |
| LIVE-11 | P1 | MUST 实时展示当前事件、队列、最近播放、耗时及连接状态。 |

UC-01：选角色连接 → 规范事件 → 去重／筛选／合并 → 调度 → 必要上下文 → 本地短句 → TTS → 语音／表情／字幕 → 仅写回实际完成互动。

UC-02：礼物按唯一ID、档位和冷却选预录或语音／特效并记录，不无限抢占普通弹幕。UC-05：冷场按可关闭的频率上限触发话题／口头禅／音乐衔接。UC-06：断线或Provider故障显示错误并有限恢复，不重谢旧礼物、不读过时消息。

### 不变式

- 人工停止／静音／接管先于自动语音；新礼物默认不打断已开始音频，人工或明确紧急事件除外。
- 每个语音事件只有一个有效状态，WS重连／重启不重放旧礼物或已完成任务。
- 礼物和普通消息同样受内容约束，付费不增加工具权限。
- 默认单LLM／单TTS、有限队列，直播优先于离线评论；模型驻留与生成按资源规范控制。

P1可调建议为普通弹幕60%／礼物25%／主动话题15%，以实际音频时长记账；空队列可借余量，结合aging与同用户冷却防饿死。该比例不是未经测试的公平性结论。优先级依次为人工控制、时效反馈／礼物、选中普通互动、冷场／预录、离线分析／维护。

## 事件与播放状态

PlatformConnector.events() → AsyncIterator[NormalizedEvent]。事件至少包含 event_id／platform／room_id／session_id／type／platform_user_id／display_name／timestamp／text／metadata，时间含时区。平台无稳定ID时生成注明来源的组合去重键，不假设绝对唯一；昵称不决定身份。Scheduler.submit(event) → JobID 不承诺每条消息获得Speech Slot。

会话目标：stopped → connecting → ready → live ↔ paused → stopping → stopped，故障可为degraded／error。paused可收录但不启动后续生成／播放；stopping停新任务、取消可取消生成；degraded可见回退音效／静音／人工接管。

任务语义：received → normalized → filtered/queued → selected → generating → synthesizing → ready_to_play → playing → completed，另有expired／skipped／failed／cancelled，记录状态与时间。仅completed算回应。静音、跳过、停止、切角色／版本取消当前并通知停音，清队列不把任务记播完。

OverlayBus.emit(audio,animation,subtitle) → Ack，以相同播放ID／nonce关联媒体。单一播放者持当前nonce先started再ended才completed；缺播放器、错误、超时、断线、取消、重启不算完成也不重播。第二页待机，断线立即停音／清字幕，忽略过期确认。

持久语义包括events的平台事件键、场次、用户、类型、原始数据、状态；response_jobs的源事件集合、角色版本、状态、台词、情绪、音频引用及耗时。去重与保留期配合，重启取消遗留活动任务并保留完成事实。

## 表情、overlay与音乐

| ID | 原优先级 | 要求 |
| --- | --- | --- |
| MEDIA-01 | P0 | MUST 有待机、说话、开心、生气、困惑、礼物清单，支持PNG／GIF／WebP，无需Live2D。 |
| MEDIA-02 | P0 | MUST 独立/overlay，透明／场景背景、字幕和表情，适于OBS浏览器来源。 |
| MEDIA-03 | P1 | SHOULD 嘴／眼／特效分层与简单Web动画，减少整图制作。 |
| MEDIA-04 | P1 | MUST 有限情绪／动作映射素材，缺失回退待机／说话图，不影响语音。 |
| MEDIA-05 | P1 | MUST 点歌识别、去重、候选确认、排队、跳过、当前曲目及播放／暂停；Mock song不冒充音乐已播放。 |
| MEDIA-06 | P1 | MUST 播放与检索分离，本地曲库有直播许可，网易云仅候选搜索／元数据，会员权不等于直播权。 |
| MEDIA-07 | P1 | SHOULD 讲话时音乐ducking并限制同时音源数。 |

素材记录ID、角色、事件标签、路径、版本及许可；music_queue记录请求事件、曲目、来源、授权、状态。未授权歌曲不公开播放。礼物可按档位映射专属音效／动画，不靠无限竞价抢占。

默认大肥鱼的六种 GIF 采用同一版本的完整 Q 版角色图集，统一全身比例、蓝紫长发、单条鲸尾和鲸鱼围裙；素材版本与逐项来源保存在清单中，署名与修改说明随镜像分发。更换角色通过其 avatar_manifest 独立引用，不把默认 GIF 写死到播放器。

| 验收 ID | 通过条件 |
| --- | --- |
| AC-04 | 同一Mock事件两次注入日志可见，仅一次有效入队。 |
| AC-05 | 混合100条弹幕／10条礼物，队列有界，无无限重播，普通互动不永久饿死。 |
| AC-06 | 播报中静音／停止立即停止当前及后续自动音频，断线恢复不重播旧礼物。 |
| AC-09 | OBS浏览器来源显示角色／GIF／字幕，缺图不影响语音；浏览器确认不能代替OBS本体验证。 |
| AC-10 | 未许可音乐不公开播放，合法曲库可点歌、跳过和看队列。 |

真实B站接入按 [直播开放平台](https://open-live.bilibili.com/) 实际权限。交互与调度参考 [Open-LLM-VTuber Web Mode](https://docs.llmvtuber.com/en/docs/user-guide/frontend/web/) 和 [LiveStream-Agent](https://github.com/tian-xiu/LiveStream-Agent)，不直接继承其代码、行为承诺或素材许可。
