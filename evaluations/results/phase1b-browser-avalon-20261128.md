# Phase 1b Browser-UI Comment Publisher & Avalon Re-Test · 2026-11-28

> **Avalon Gate Verdict:** **FAIL** (`0 / 2` = `0.0%` anonymous `public_visible`, consecutive-2 `self_only` cooldown triggered at `03:16:08` Taipei).  
> **Browser-UI Publisher Verification:** **PASS** — Chromium/Playwright `browser_ui` transport launched cleanly, navigated Shadow DOM, matched Lit `rpid`, intercepted `/x/v2/reply/add` receipts (`transport="browser_ui"`), and triggered zero CAPTCHA (`need_captcha=false`, `code=0`).

---

## 1. Commits & Deployment Summary

- **Branch & Commits (`feat/phase1-ops-avalon-video`, no push):**
  1. `2d6d34e6` — `feat(bilibili): add persistent Chromium/Playwright UI comment publisher`
  2. `aca6788c` — `chore(video,evaluation): raise default render concurrency/timeout and record phase 1 soak report`
- **Pre-Deploy Backup & Rollback:**
  - Consistent SQLite `.backup` saved to `<backup-dir>/before-phase1b-browser-avalon.sqlite3` (`508M`, `PRAGMA integrity_check = ok`).
  - Previous images `tuber-studio-core:4273e2d` and `tuber-studio-core:f85b1610` kept tagged for rollback.
- **Deployment & Runtime Checks:**
  - Deployed overlay image `tuber-studio-core:aca6788c` cleanly (`dafeiyu-studio-tuber-core-1` healthy, `studio-edge` reattached).
  - `/healthz`: `200` (`{"status":"ok"}`).
  - `BilibiliAdapter.resolve_comment_transport()`: `"browser_ui"`.
  - Persistent Chromium profile directory `<data-root>/bilibili_browser_profile/<account-uid>` verified writable by container UID `65532` and populated during test runs.
  - Cleared stale pre-soak duplicate `c967bd8a-ee79-42bf-8868-26fe1e3f305e` (`BV1KMsb6oEXw`) via `POST /api/v1/operations/intents/{id}/review` (`action="reject"`).
  - Confirmed prior `01:50:59` cooldown (`02:20:59` expiry) had naturally expired before starting re-test.

---

## 2. Controlled Browser-UI Re-Test Results

Selected fresh Phase 1 `pending_review` drafts from distinct videos, approved & published sequentially via studio review/publish API (`transport="browser_ui"`), waiting > 90s after each publish for independent visibility checks:

| # | Intent | Kind | Target Video | Transport | Receipt / CAPTCHA | `verify-visibility` | Anonymous Playwright DOM |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `a624fd0f-...` | `reply_public` | `BV1wQs16aEF3` | `browser_ui` | `code=0`, `rpid=321510869249`, `need_captcha=false` | `self_only` (`auth_visible=true`, `anon_visible=false`, `anon_root_visible=true`) | `root_hit=true`, `target_hit=null` (`self_only`) |
| 2 | `45a4e272-...` | `reply_public` | `BV1YdxR6CE79` | `browser_ui` | `code=0`, `rpid=321511108449`, `need_captcha=false` | `self_only` (`auth_visible=true`, `anon_visible=false`, `anon_root_visible=true`) | `root_hit=true`, `target_hit=null` (`self_only`) |

- **Pacing & Guardrails:**
  - Comment #1 published at `03:11:12` Taipei; visibility checked at `03:14:55` & `03:15:22` (`self_only`, `streak=1/2`).
  - Comment #2 published at `03:16:08` Taipei (~`296s` / `4m56s` after #1 on a separate BV); visibility checked at `03:17:55` (`self_only`).
  - Consecutive-2 `self_only` cooldown activated automatically (`operation_visibility_risk`: `streak=2/2`, `active=true`, 30-minute pause until `03:46:08` Taipei).
  - Per cooldown guardrails, remaining candidate approvals were halted immediately without bypassing the active cooldown.

---

## 3. Avalon Gate Evaluation & Root-Cause Signal

- **Avalon Comment Gate:** **FAIL** (`0 / 2` = `0.0%` anonymous `public_visible` vs `>= 70%` threshold; consecutive-2 `self_only` cooldown triggered).
- **Engineering Conclusion:**
  - Switching from HTTP API to real headless Chromium UI (`BilibiliBrowserCommentPublisher` with persistent profile, organic cookies, human typing cadence, and Shadow DOM interaction) executed cleanly with `code=0`, valid `rpid` receipts, and zero CAPTCHAs.
  - However, both replies across two separate videos (`BV1wQs16aEF3`, `BV1YdxR6CE79`) were still shadow-filtered (`self_only`) by Bilibili immediately on submission, proving the remaining `self_only` behavior is **not** caused by HTTP client headers/TLS/fingerprinting alone and points to account-level / IP-reputation / Avalon comment risk state (or reply-specific filtering) on Bilibili's side.
