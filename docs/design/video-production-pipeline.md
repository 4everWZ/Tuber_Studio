# Wave 2：Remotion + Code-as-Video 自动制片与投稿管线设计

## 1. 目标与系统边界

本规范定义工坊自动化视频创作与投稿流水线。整体沿用工坊「核心持有状态与授权、推理与媒体服务解耦」原则：

- **频道分流与工坊控制面（`core/editorial.py` + `L3/L4`）：** 区分 `comment`（单视频 1–3 句短评）、`dynamic`（单点心情/轻量随想/成片预告）与 `video`（多镜叙事弧与高视觉信息增益成片，如 LoRA 微调前后对比、看片盘点、开源项目架构与 UI 演示、深度科普），持有选题来源、剧本与分镜 JSON、视频渲染作业状态机、发布模式开关（人工预览审阅 vs 自动投稿）、幂等投稿意图与平台回执。
- **推理与配音（外部 Omni 网关）：** `/llm/v1/responses`（`llm-default`）支持 Two-Stage Director（Stage A 章节大纲 -> Stage B 逐章分镜）输出结构化 Remotion JSON；`POST /v1/audio/speech` 负责生成高质量口播 WAV。
- **独立渲染工作器（Remotion）：** 通过受控本地子进程或专用构建镜像运行 `remotion render`，由 `remotion-studio/` 多主题动效引擎按帧驱动渲染。
- **投稿执行（Web API / `biliup` CLI）：** 作为 `infra/platforms/bilibili` 下的独立受控适配器调用，按单次凭据生命周期复用工作站本地已登录 B 站凭据，仅在状态机完成终态核验后写入平台回执。

---

## 2. 六阶段流水线与电影级分镜架构

```text
[1. 选题分流与公开深度调研]  ──► assess_editorial_channel + remotion_research.py
        │  (watched_video / public_incident / GitHub 仓库深度抓取 / allowlist 工坊文档 / 本地 UI 截图)
        ▼
[2. Two-Stage Director 剧本策划]  ──► remotion_planner.py (Stage A 章节大纲 -> Stage B 逐镜填充)
        │                              6 种视频模式 × 3 档时长预设 × 10 种 Remotion 模板
        ▼
[3. 参考音色 TTS、音量包络与音效套件]  ──► /v1/audio/speech + remotion_audio_qc.py
        │                                     生成 narration.wav, audioEnvelope[], bgm.wav, 5 路 SFX, SRT/VTT
        ▼
[4. Code-as-Video 多镜动效渲染]
        └─► Remotion Studio (`dark_tech` / `aqua_light` / `cinema`, 虚拟运镜, 转场层, 节拍步进 reveal_step)
            + 浮动去框 AquaOverlay（受 audioEnvelope[] 声波驱动 + avatar_layout 自适应）
        │
        ▼
[5. FFmpeg 混音、Contact Sheet 与 VLM 审片]  ──► loudnorm 归一化 + 响度/时长质检 + contact_sheet.png + 可选 VLM 评审
        │
        ▼
[6. 审核/自动投稿分支 (preview_review vs auto_publish)]
        ├─ preview_review: 停留在「视频稿件队列」，等待工作台人工预览后确认发布
        └─ auto_publish:   仅在来源全公开、质检通过且无风控冷却时触发幂等投稿
```

### 2.1 结构化剧本与分镜契约（Code-as-Video Props）

模型仅生成受 Strict Schema 约束的**场景数组**与口播文本，不直接写任意可执行脚本或 shell 命令：

| 字段 | 说明 |
| --- | --- |
| `title` / `summary` / `tags` | 投稿标题（<=80 字）、简介（附公开来源 URL）、分区与标签。 |
| `video_mode` / `duration_preset` | 6 种创作模式（`commentary` / `daily_vlog` / `watched_roundup` / `project_showcase` / `explainer` / `self_intro`）与 3 档时长预设（`short` 4–6 镜 / `standard` 8–12 镜 / `deep_dive` 12–18 镜）。 |
| `scenes[]` | 有序场景列表（支持 1–24 镜），每项指定 10 种模板之一（`TitleCard`、`CodeWalkthrough`、`ChartCompare`、`MemeCutaway`、`QuoteSpotlight`、`TimelineSummary`、`ChapterBumper`、`ArchitectureFlow`、`UIShowcase`、`KineticStatement`），以及 `chapter_title`、`theme`（`dark_tech` / `aqua_light` / `cinema`）、`transition`、`camera_motion`、`avatar_layout`。 |
| `scenes[].visual_props` | 支持扩展结构：`chapter_number`、`stat_callout`、`diagram_nodes[]`、`diagram_edges[]`、`code_steps[]`、`ui_focus_regions[]`、上传图片 `uploaded_image_file` 与上传短视频片段 `uploaded_video_file`（每任务最多 12 份上传媒体，图片 `<=8MB`，`.mp4`/`.webm` `<=30MB`）。 |
| `scenes[].lines[]` | 口播台词（每镜最多 16 句），含 `avatar_cue`、`emphasis[]`、`reveal_step`、`visual_focus` 与逐句音效 `sfx`（`none` / `pop` / `whoosh` / `chime` / `chapter_hit`）。 |
| `sources[]` | 引用的公开来源标识（含 `brief_text` 最多 30000 字、`reference_urls` 最多 6 条与 `include_workspace_docs` 白名单仓库文档）；所有扩展视觉字段（含节点、代码步进、标注与焦点区）均经过 `assert_public_video_sources` 隔离校验，严禁引用 `owner` 记忆、凭据或 `dm_inbox` 私聊原文。 |

### 2.2 Audio-First 节拍同步、包络闪避与视觉质检

1. **Audio-First 对齐与包络提取：** 逐句调用 TTS 合成 WAV，读取实测帧数并填充气口停顿；随后通过 `extract_audio_envelope` 计算逐帧平滑语音包络 `audioEnvelope[]`，在 Remotion 中驱动 BGM 自动闪避（说话时压低至 `~0.07`，停顿/章节过门升至 `~0.22`）与浮动 `AquaOverlay` 声纹光环。
2. **多音效与 BGM 套件：** `prepare_bgm_and_sfx_kit` 优先复用 `assets/bgm/*.wav`，缺失时合成 Lo-fi 和弦铺底与 5 路短音效（`transition`、`whoosh`、`pop`、`chime`、`chapter_hit`）。
3. **Contact Sheet 与可选 VLM 审片：** 渲染后处理阶段调用 `extract_scene_contact_sheet` 为每个分镜截取 45% 位置代表帧并拼合 `contact_sheet.png`（通过 `/api/v1/operations/videos/{job_id}/media/contact_sheet` 提供预览），支持触发非阻塞 `POST /api/v1/operations/videos/{job_id}/visual-review` 检查版式与单调性风险。

### 2.3 工作台辅助接口与本地 UI 截屏

- `POST /api/v1/operations/videos/pitch-ideas`：结合已看视频、公开记忆与输入提示，产出跨 `video` / `dynamic` 渠道的选题建议（`VideoIdeaPitch[]`）。
- `POST /api/v1/operations/videos/{job_id}/capture-ui`：仅限本机回环地址（`127.0.0.1` / `localhost`）的 Playwright Chromium 1920x1080 截屏，直接存入任务的 `public/uploads/` 供 `UIShowcase` 聚焦高亮。
- `POST /api/v1/operations/videos/{job_id}/uploads`：支持上传公开参考图片（PNG/JPEG/WebP）与 `.mp4`/`.webm` 录屏片段（Magic-byte 校验，`<=30MB`），并在生成工程时自动绑定未手动指定的视频片段。

---

## 3. 凭据复用与发布控制开关

### 3.1 凭据隔离（不落仓库、不进提示词）

- 沿用本地秘密存储中的 B 站登录凭据（`SESSDATA`、`bili_jct`、`DedeUserID`），优先走原生 Web 投稿接口；调用 `biliup` CLI 时仅在受限临时目录现生成一次性 `cookies.json`（权限 `0600`）并在 `finally` 中立即清理，日志严格脱敏。

### 3.2 发布模式与幂等回执

- 策略开关 `video_production.publish_mode` 默认 `preview_review`，仅在人工确认或满足全公开 + 质检通过 + 非风控冷却的 `auto_publish` 门禁时提交。
- 复用 `canonical_intent("bilibili", "video_publish", content_hash, character_id)` 保证同一成片在网络超时或重启恢复时只结算一次投稿回执；结果不确定时置为 `publish_uncertain` 并阻止重复投稿。

---

## 4. 模块布局与验收入口

- **L0 频道分流契约：** `src/tuber_studio/core/editorial.py`
- **L3 技能模块化拆分：**
  - `src/tuber_studio/agent/skills/remotion_assets.py`（严格 Schema、素材暂存、上传校验、公共文本隔离收集）
  - `src/tuber_studio/agent/skills/remotion_research.py`（GitHub 深度抓取、白名单仓库文档、回环 UI 截屏）
  - `src/tuber_studio/agent/skills/remotion_planner.py`（Two-Stage Director、6 模式电影级兜底、选题 Pitch、场景默认值规范化）
  - `src/tuber_studio/agent/skills/remotion_audio_qc.py`（逐帧包络、Lo-fi BGM + 5 路 SFX 套件、Contact Sheet、可选 VLM 审片）
  - `src/tuber_studio/agent/skills/remotion_video.py`（状态机编排、`TaskRuntime` 工具注册与幂等发布）
- **L4 HTTP 路由：** `src/tuber_studio/app/routes/video_production.py`（`/api/v1/operations/videos/*`）
- **回归测试：** `tests/agent/test_remotion_cinematic_pipeline.py`、`tests/agent/test_heartbeat_and_remotion.py`、`tests/agent/test_video_rich_inputs.py`、`tests/agent/test_remotion_dynamic_memes.py`、`tests/app/test_video_production_routes.py`、`tests/architecture/test_layer_boundaries.py`。
