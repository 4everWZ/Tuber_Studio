# Wave 2：Remotion + Code-as-Video 自动制片与投稿管线设计

## 1. 目标与系统边界

本规范定义工坊下一阶段（Wave 2）的自动化视频创作与投稿流水线。整体沿用工坊「核心持有状态与授权、推理与媒体服务解耦」原则：

- **工坊控制面（Tuber Studio `L3/L4`）：** 持有选题来源、剧本与分镜 JSON、视频渲染作业状态机、发布模式开关（人工预览审阅 vs 自动投稿）、幂等投稿意图与平台回执。
- **推理与配音（外部 Omni 网关）：** `/llm/v1/responses`（`llm-default`）负责脚本与结构化 Remotion/Blender 编排 JSON；`POST /v1/audio/speech`（参考音色盲转写核对）负责生成高质量口播 WAV。
- **独立渲染工作器（Remotion + Blender/VRM）：** 通过受控本地子进程或专用构建镜像运行 `remotion render` 与可选 VRM/Blender 虚拟形象渲染，避免污染工坊核心 Python 运行时依赖。
- **投稿执行（`biliup` CLI）：** 作为 `infra/platforms/bilibili` 下的独立受控适配器调用，按既定单次凭据生命周期复用工作站本地已登录 B 站凭据，仅在状态机完成终态核验后写入平台回执。

---

## 2. 六阶段流水线架构

```text
[1. 选题与公开来源校验]
        │  (watched_video / public_incident / operator_topic / 热点搜证)
        ▼
[2. 结构化剧本与分镜生成]  ──► /llm/v1/responses (ScriptStoryboard JSON)
        │
        ▼
[3. 参考音色 TTS 与精确时码对齐]  ──► /v1/audio/speech (逐句合成 + 实测帧长)
        │                                生成 manifest.json (audio + srt + cue timings)
        ▼
[4. Code-as-Video 渲染合成]
        ├─► Remotion (React TSX 模板 + 实时动效图表/代码高亮/字幕层)
        └─► 可选 VRM/Blender 角色片段 (口型/动作参数 -> 带 Alpha 通道素材)
        │
        ▼
[5. FFmpeg 混音与成片质检]  ──► 音量响度、分辨率(1080p/60fps)、时长一致性、封面截取
        │
        ▼
[6. 审核/自动投稿分支 (preview_review vs auto_publish)]
        ├─ preview_review: 停留在「视频稿件队列」，等待工作台人工预览后确认发布
        └─ auto_publish:   直接触发一次性 biliup upload -> 轮询投稿状态并写回执
```

### 2.1 结构化剧本与分镜契约（Code-as-Video Props）

模型仅生成受 Strict Schema 约束的**场景数组**与口播文本，不直接写任意可执行脚本或 shell 命令：

| 字段 | 说明 |
| --- | --- |
| `title` / `summary` / `tags` | 投稿标题（<=80 字）、简介（附引用来源链接）、分区与标签。 |
| `scenes[]` | 有序场景列表，每项指定预置 Remotion 模板组件 ID（如 `TitleCard`、`CodeWalkthrough`、`ChartCompare`、`MemeCutaway`）、视觉 props、口播台词 `lines[]` 与可选角色表情/动作预设 `avatar_cue`。 |
| `sources[]` | 引用的公开来源标识；严禁引用 `owner` 作用域记忆或私聊记录。 |

### 2.2 TTS 驱动时间轴与 VRM/Blender 联动

1. **Audio-First 对齐：** 先遍历 `scenes[].lines[]` 调用既有 `infra/inference/omni_speech.py` 逐句合成 WAV，并读取实际采样率与帧数得到精确耗时（单位秒 / 30fps 或 60fps 帧号）。
2. **动态帧数回填：** 将每句音频的 `start_frame`、`duration_frames` 与字幕条目注入 Remotion `--props` JSON，确保动画切换、代码高亮步进与口播严丝合缝，杜绝音画漂移。
3. **可选 VRM/Blender 虚拟演出：** 当稿件开启角色出镜且节点配备虚拟形象资产时，依据口播音量包络与 `avatar_cue` 生成预设动作/口型曲线（BlendShape / Viseme JSON），由 headless Blender/WebGL 导出透明通道动画片段挂入 Remotion `<OffthreadVideo>` 图层；渲染不可用时自动降级为立绘/GIF 贴图方案。

### 2.3 Remotion 合成与 FFmpeg 成片质检

- 使用固定模板仓库执行 `npx remotion render <CompositionId> <output.mp4> --props=<props.json> --concurrency=2`（受全局并发信号量约束，同时至多 1 个重渲染任务）。
- 渲染完成后由 `ffprobe` / `ffmpeg` 执行硬性门禁校验：
  - 视频轨可解码且时长与合并后音频轨误差 `<= 0.5s`；
  - 音轨非全静音且经响度归一化（`loudnorm`）；
  - 输出大小与码率符合平台投递限制。

---

## 3. 凭据复用与发布控制开关

### 3.1 `biliup` CLI 凭据复用（不落仓库、不进提示词）

- 沿用现有本地秘密存储中的 B 站登录凭据（`SESSDATA`、`bili_jct`、`DedeUserID`、`buvid3` 等），每次授权投稿前在受限临时目录现生成一次性 `cookies.json`（权限 `0600`），通过 `biliup upload --user-cookie <temp-cookie>` 提交并在 `finally` 中立即清理临时文件。
- 所有 `biliup` 命令参数与工作目录均经过白名单转义与独立超时控制；日志仅脱敏记录返回值与 `bvid`/`aid`，禁止回显 Cookie 字段。

### 3.2 发布模式与幂等回执

- 提供策略开关 `video_production.publish_mode`：
  - `preview_review`（默认安全模式）：成片进入工作台队列 `ready_for_review`，号主可在浏览器在线播放预览、微调标题/标签/封面或一键驳回重渲染，点击确认后方才调用 `biliup` 投稿；
  - `auto_publish`：仅当来源全部公开、成片通过质检且账户无风控退避时自动入队投稿。
- 复用工坊 `canonical_intent("bilibili", "video_publish", content_hash, character_id)` 保证同一成片在网络超时或重启恢复时只结算一次投稿回执；若上传结果不确定（超时/断连），状态置为 `publish_uncertain`，等待核对稿件列表后再决定是否人工干预。

---

## 4. 资源治理与分阶段验收标准

1. **磁盘与缓存预算：** 渲染中间帧与成片输出写入独立缓存分类 `video_renders`，接入现有 `CacheManager` 定期清理过期中间工件，已发布成片仅保留摘要元数据、时间轴 Manifest 与平台 `bvid` 回执。
2. **分阶段验收：**
   - **Stage 1（离线成片）：** Mock 数据 + 真实 `/v1/audio/speech` WAV -> `remotion render` -> 输出 1080p MP4 并通过 FFprobe 音画对齐检验；
   - **Stage 2（工作台审阅流）：** 选题生成 -> 后台队列渲染 -> 前端预览/编辑元数据/驳回重制 -> `account_writes` 增量保持 `0`；
   - **Stage 3（受控实投）：** 在号主授权的单支测试稿件上验证 `biliup` 凭据注入、投稿成功回执写回及断网重连幂等防重发。
