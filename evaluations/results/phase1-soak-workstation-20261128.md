# Phase 1 30-Minute Workstation Soak & Avalon Gate Report · 2026-11-28

> **Gate Verdict:** **FAIL** on the Avalon Comment Gate (`0 / 2` = `0.0%` anonymous `public_visible`, consecutive-2 `self_only` cooldown triggered).  
> **Code / Persona / Dynamic / Code-as-Video Checks:** **PASS** (`0` CoT leaks, `0` `"待审 · "` / telemetry jargon, `0` `&#34;` entities, `0` unrelated cross-video links, closeout dynamic `#27` anonymously visible `200`, Code-as-Video `251.84s` `1920x1080` `14`-scene QC passed).

---

## 1. Deployment & Character Sync Summary

- **Branch & Commits:** `feat/phase1-ops-avalon-video` (`f85b1610`), split into 4 logical commits:
  1. `37d57b96` — `fix(inference,dynamics): raise thinking budget, block CoT leaks, fix #24 matching & &#34; unescaping`
  2. `626cfa77` — `feat(persona): restore sharp civil tsundere Aqua, natural association & anti-boilerplate`
  3. `66a11f29` — `feat(bilibili,operations): add session cookies, watch heartbeat & jitter pacing against Avalon`
  4. `f85b1610` — `feat(video,operations): natural closeouts and 90-300s multi-template Code-as-Video`
- **Pre-Deploy Backup & Rollback:**
  - Consistent SQLite `.backup` saved to `<backup-dir>/before-phase1-ops-avalon-video.sqlite3` (`PRAGMA integrity_check = ok`).
  - Previous image `tuber-studio-core:4273e2d` kept tagged for rollback.
- **Overlay Image & Health Verification:**
  - Deployed overlay image `tuber-studio-core:f85b1610` without rebuilding the Docker base image and with zero SQLite migration conflicts.
  - Verified `/healthz` (`200`, `{"status":"ok"}`), `/dafeiyu/healthz` (`200`), authenticated system/provider checks (`foreground`: `131072` ctx, `background`: `262144` ctx), automation/schedules APIs, and video environment readiness.
- **Aqua (`whale`) Character Card Sync:**
  - Updated active `whale` card (`v3`) cleanly via `PUT /api/v1/persona/characters/whale`, preserving historical versions (`v1`–`v3` in `character_versions`) and `characters` receipt bindings; verified updated `self_description` and `26`-item `blacklist`.

---

## 2. 30-Minute Autonomous Soak Window (`sch-cc98d869762e`)

- **Schedule Window:** `2026-11-28 01:46` – `02:16` (`Asia/Taipei`, `duration_minutes=30`, `scope_discovery=true`, `scope_inbox=true`).
- **Closeout Configuration:** `dynamic_mode="automatic"`, `video_mode="automatic_review"` (`02:18` – `02:48` `Asia/Taipei`), video policy `publish_mode="preview_review"`, `auto_publish_enabled=false`.
- **Persisted Policy Note:** Workstation `browsing` policy had `top_level_auto_publish=false` and `reply_auto_publish=false`, so newly drafted soak comments remained in `pending_review`, while pre-existing queued comments in `operation_send_queue` executed during the soak.

### Activity Deltas (Baseline -> End of 30m Window)

| Metric | Delta | Breakdown |
| --- | --- | --- |
| `browsing_rounds` | `+60` | `12` discovery completed (+`1` running at snapshot), `46` inbox completed (+`1` partial) |
| `browsing_events` | `+321` | `46` `comment_visit` drafted, `1` `mention` drafted, `1` `reply` drafted, `79` skipped, `91` archived, `63` revisit_parked, `25` revisit_merged, `10` received, `4` context_ready, `1` invalid |
| `browsing_intents` | `+67` | `44` `reply_public` (`pending_review`), `6` `top_level` (`pending_review`), `11` `comment_like` (`published`), `6` `video_like` (`published`) |
| `account_writes` (in-window) | `+20` | `11` `comment_likes` (`published`), `6` `video_likes` (`published`), `2` `comments` (`published`), `1` `messages` (`rejected`) |
| Closeout Dynamic | `+1` | `dynamic_posts` `#27` (`1275455259411480581`, `published` at closeout) |

---

## 3. Content & Grounding Quality Audit

Checked all `50` newly generated comment drafts (`44` `reply_public` + `6` `top_level`), `2` soak-published comments, closeout dynamic `#27`, and `subtitles.srt` (`45` cues / `2,803` chars):

| Check | Result | Count |
| --- | --- | --- |
| CoT leaks (`detect_reasoning_leak`) | Pass | `0` leaks across `52` comments + dynamic `#27` + subtitles |
| `"待审 · "` / telemetry jargon (`收到回执` / `公开页读回` / `运营收尾`) | Pass | `0` hits |
| HTML entities (`&#34;`, `&amp;`, `&lt;`, `&gt;`) | Pass | `0` hits |
| Unrelated cross-video BV / URL links in comments | Pass | `0` hits across `50` new drafts |
| Deprecated Avalon boilerplate openers (`这设定太损了` / `先吃为敬` / `白饭已经揭盖` / `哦鲸鲸`) | Pass | `0` hits across `52` comments |

- **Closeout Dynamic `#27` (`1275455259411480581`):**
  - Natural Aqua-voiced closeout referencing 2 verified public sources (`BV15Qsq68E7H`, `BV1uwsB6XERE#reply325217091424`) with zero `"待审 · "` markers or receipt jargon.
  - Read-only anonymous Bilibili check (`GET /x/polymer/web-dynamic/v1/detail?id=1275455259411480581&features=itemOpusStyle,...`): HTTP `200`, `code=0`, full opus text confirmed anonymously visible.

---

## 4. Code-as-Video Render & `ffprobe` Verification

- **Job:** `rem_recap_abc81320970f5545c8357c42` (`publish_mode="preview_review"`, `duration_preset="deep_dive"`, `14` scenes across 7 templates: `TitleCard`, `ChapterBumper` ×5, `MemeCutaway` ×2, `QuoteSpotlight` ×2, `KineticStatement` ×2, `UIShowcase`, `TimelineSummary`).
- **Timeout Finding & Mitigation:** The automatic closeout render at `--concurrency=2` hit `render_timeout_seconds=600.0` on 1080p CPU software rendering (`~89 MB` `pre-encode.mp4` at 10m). Raising workstation video policy to `max_render_concurrency=4` and `render_timeout_seconds=1800.0` (preserving `preview_review` and `auto_publish_enabled=false`) and re-running render produced a clean MP4.
- **`ffprobe` & QC Results:**
  - Status: `ready_for_review` (`109 MiB` / `113,962,225` bytes `output.mp4`, `cover.png`, `contact_sheet.png`)
  - Video stream: `h264`, `1920x1080`, `30/1` fps, `7555` frames
  - Audio stream: `aac`, `23611` frames (`loudnorm_applied = true`)
  - Duration: **`251.840000s`** (within `90.0s <= duration <= 300.0s`; drift vs `251.8333s` expected = `0.0067s`)
  - Bitrate: `3,620,146` bps
  - QC Report: `passed = true`, `issues = []`, `scene_frames = 14`, `cover_generated = true`, `contact_sheet_generated = true`

---

## 5. Avalon Comment Gate Audit — **FAIL**

- **Comments Published During 30m Window (`2` total):**
  1. `452e9332...` (`reply_public` on `BV1KMsb6oEXw`, `rpid=321507673073`, sent `01:47:08` Taipei — pre-soak queued item)
  2. `cadbbf7c...` (`reply_public` on `BV1KMsb6oEXw`, `rpid=321507837089`, sent `01:50:44` Taipei — pre-soak queued item, `216s` after #1 on the same BV)
- **Anonymous Bilibili Verification:**
  - Both internal `verify_publication` and independent read-only anonymous HTTP checks with session cookies (`buvid3`, `b_nut`, `buvid4`, no `SESSDATA`) confirmed both parent root comments exist (`root_found=true`) while neither reply appears anonymously (`in_thread=false`, `anon_visible=false`, `status="self_only"`).
- **Gate Evaluation:**
  - Anonymous `public_visible` rate: **`0 / 2` (`0.0%`)** (required `>= 70%`).
  - Consecutive-2 `self_only` cooldown: **Triggered** (`operation_visibility_risk` paused comment sending at `01:50:59` for 30 minutes until `02:20:59`).
  - **Result:** **FAIL**.
- **Follow-up Observation:** Because workstation `top_level_auto_publish` and `reply_auto_publish` were `false`, none of the `50` newly generated Phase 1 drafts were submitted, and both submitted comments came from the pre-soak queue targeting the same video `BV1KMsb6oEXw`. Re-testing the Avalon gate with approved/auto-published Phase 1 drafts spread across distinct videos (and clearing stale pre-soak queued items on the same BV) is recommended before enabling unattended auto-commenting.
