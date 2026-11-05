/**
 * Operations Workbench & Permission Matrix Controller
 * Manages autonomous browsing, permission tiers, optimistic updates, and visual fallback.
 */

import { $, safeMessage, notice } from "./core.js";

let currentAutomationStatus = null;
let matrixUserInteractingUntil = 0;
let matrixSaveTimeout = null;

export function getAutomationStatus() {
  return currentAutomationStatus;
}

export function calculatePermissionTier(settings = {}, account = null, hasCsrf = true) {
  const hasSessdata = Boolean(account?.configured?.sessdata);
  const accountLoggedIn = account?.state === "logged_in";

  if (!hasSessdata || !hasCsrf || !accountLoggedIn) {
    return {
      tier: "readonly",
      icon: "🛡️",
      text: "权限：只读监控（未登录或缺少写入凭证）",
      badgeText: "只读监控态（缺少凭证）"
    };
  }

  const allDisabled = (
    !settings.top_level_enabled &&
    !settings.reply_enabled &&
    !settings.video_like_enabled &&
    !settings.video_fav_enabled &&
    !settings.mentions_enabled
  );

  if (allDisabled) {
    return {
      tier: "readonly",
      icon: "🛡️",
      text: "权限：只读监控（全部动作已禁止）",
      badgeText: "只读监控态（全动作禁止）"
    };
  }

  const hasAutoPublish = Boolean(
    settings.top_level_auto_publish ||
    settings.reply_auto_publish ||
    settings.video_like_auto_publish ||
    settings.video_fav_auto_publish ||
    settings.handoff_auto_send
  );

  if (hasAutoPublish) {
    const maxDaily = settings.max_per_day || 20;
    return {
      tier: "autopublish",
      icon: "⚡",
      text: `权限：模型自动放行（每日上限 ${maxDaily} 条）`,
      badgeText: "完全自治态（自动放行）"
    };
  }

  return {
    tier: "draft",
    icon: "📝",
    text: "权限：草稿生成·人工审核后发布",
    badgeText: "草稿审核态（人机协同）"
  };
}

export function applyPermissionTierUI(tierInfo) {
  const { tier, icon, text, badgeText } = tierInfo;

  // 1. Topbar capsule
  const capsule = $("permission-capsule");
  if (capsule) {
    capsule.dataset.tier = tier;
    const textEl = $("capsule-text");
    const iconEl = $("capsule-icon");
    if (textEl) textEl.textContent = text;
    if (iconEl) iconEl.textContent = icon;
  }

  // 2. Operations workbench capsule
  const opCapsule = $("op-permission-capsule-btn");
  if (opCapsule) {
    opCapsule.dataset.tier = tier;
    opCapsule.innerHTML = `<span>${icon} ${text}</span>`;
  }

  // 3. Settings matrix header badge
  const matrixBadge = $("matrix-capsule-badge");
  if (matrixBadge) {
    matrixBadge.textContent = badgeText;
    matrixBadge.className = `badge ${tier === "autopublish" ? "active" : tier === "draft" ? "good" : ""}`;
  }
}

export function getOptimisticSettingsFromDOM() {
  const topVal = document.querySelector('input[name="matrix_top_level"]:checked')?.value || "1";
  const replyVal = document.querySelector('input[name="matrix_reply"]:checked')?.value || "1";
  const dmVal = document.querySelector('input[name="matrix_dm"]:checked')?.value || "1";
  const likeVal = document.querySelector('input[name="matrix_video_like"]:checked')?.value || "1";
  const favVal = document.querySelector('input[name="matrix_video_fav"]:checked')?.value || "1";

  const maxRepliesVideo = parseInt($("matrix-max-replies-video")?.value || "2", 10);
  const maxPerDay = parseInt($("matrix-max-per-day")?.value || "20", 10);
  const autoDowngrade = !!$("matrix-auto-downgrade")?.checked;

  const current = currentAutomationStatus?.settings || {};

  return {
    ...current,
    top_level_enabled: topVal !== "0",
    top_level_auto_publish: topVal === "2",
    reply_enabled: replyVal !== "0",
    reply_auto_publish: replyVal === "2",
    comment_handoff_enabled: dmVal !== "0",
    handoff_auto_send: dmVal === "2",
    video_like_enabled: likeVal !== "0",
    video_like_auto_publish: likeVal === "2",
    video_fav_enabled: favVal !== "0",
    video_fav_auto_publish: favVal === "2",
    max_replies_per_video: maxRepliesVideo,
    max_per_day: maxPerDay,
    auto_downgrade_on_error: autoDowngrade,
  };
}

export function renderVisualFallbackStatus(status) {
  const matrixVisualEl = $("matrix-visual-status");
  if (!matrixVisualEl) return;

  const vision = status?.vision;
  const isAdmitted = vision?.admitted === true;
  const isAvailable = vision?.available === true;

  matrixVisualEl.hidden = false;
  if (isAdmitted && isAvailable) {
    matrixVisualEl.className = "hint visual-status-box ok";
    matrixVisualEl.style.color = "var(--ok, #2e7d32)";
    matrixVisualEl.textContent = "👁️ 视觉多模态分析: 已准入并就绪（支持视频关键帧抽帧分析）";
  } else {
    matrixVisualEl.className = "hint visual-status-box fallback";
    matrixVisualEl.style.color = "var(--text, #374151)";
    const detail = vision?.reason || "模型未配置视觉或未通过多模态准入";
    matrixVisualEl.textContent = `📝 视觉回退: 纯字幕与文本分析决策模式（${detail}，系统已自动平滑回退，不影响日常运营）`;
  }
}

export async function updatePermissionCapsule(api, accountPanel) {
  try {
    const status = await api("/api/automation");
    currentAutomationStatus = status;

    const account = accountPanel?.value;
    const hasSessdata = Boolean(account?.configured?.sessdata);
    const hasCsrf = Boolean(account?.configured?.csrf);
    const settings = (Date.now() < matrixUserInteractingUntil)
      ? getOptimisticSettingsFromDOM()
      : (status.settings || {});

    const tierInfo = calculatePermissionTier(settings, account, hasCsrf);
    applyPermissionTierUI(tierInfo);

    // Matrix Credentials state
    const sessdataEl = $("matrix-sessdata-status");
    if (sessdataEl) {
      sessdataEl.textContent = hasSessdata
        ? (account?.state === "logged_in" ? `已登录 (UID ${account?.profile?.uid || "—"})` : "已配置 · 待验证")
        : "未配置";
      sessdataEl.className = `status-tag ${hasSessdata && account?.state === "logged_in" ? "ok" : "warn"}`;
    }

    const csrfEl = $("matrix-csrf-status");
    if (csrfEl) {
      csrfEl.textContent = hasCsrf ? "已就绪（具备写入授权）" : "未配置（写入被禁用）";
      csrfEl.className = `status-tag ${hasCsrf ? "ok" : "warn"}`;
    }

    if (Date.now() >= matrixUserInteractingUntil) {
      renderPermissionMatrix(status);
    }
    renderOperationsQuickBar(status);
    renderVisualFallbackStatus(status);
  } catch (e) {
    console.warn("Failed to update permission capsule:", e);
  }
}

export function renderPermissionMatrix(status) {
  if (Date.now() < matrixUserInteractingUntil) return;
  const matrixEl = $("operations-permission-matrix");
  if (matrixEl && matrixEl.contains(document.activeElement)) return;

  const settings = status.settings || {};

  // 1. Top level
  const topLevelVal = !settings.top_level_enabled ? "0" : settings.top_level_auto_publish ? "2" : "1";
  const topRadio = document.querySelector(`input[name="matrix_top_level"][value="${topLevelVal}"]`);
  if (topRadio && document.activeElement?.name !== "matrix_top_level") topRadio.checked = true;

  // 2. Reply
  const replyVal = !settings.reply_enabled ? "0" : settings.reply_auto_publish ? "2" : "1";
  const replyRadio = document.querySelector(`input[name="matrix_reply"][value="${replyVal}"]`);
  if (replyRadio && document.activeElement?.name !== "matrix_reply") replyRadio.checked = true;

  // 3. DM
  const dmVal = !settings.comment_handoff_enabled ? "0" : settings.handoff_auto_send ? "2" : "1";
  const dmRadio = document.querySelector(`input[name="matrix_dm"][value="${dmVal}"]`);
  if (dmRadio && document.activeElement?.name !== "matrix_dm") dmRadio.checked = true;

  // 4. Video Like
  const likeVal = !settings.video_like_enabled ? "0" : settings.video_like_auto_publish ? "2" : "1";
  const likeRadio = document.querySelector(`input[name="matrix_video_like"][value="${likeVal}"]`);
  if (likeRadio && document.activeElement?.name !== "matrix_video_like") likeRadio.checked = true;

  // 5. Video Favorite
  const favVal = !settings.video_fav_enabled ? "0" : settings.video_fav_auto_publish ? "2" : "1";
  const favRadio = document.querySelector(`input[name="matrix_video_fav"][value="${favVal}"]`);
  if (favRadio && document.activeElement?.name !== "matrix_video_fav") favRadio.checked = true;

  // 6. Thresholds
  if ($("matrix-max-replies-video") && document.activeElement !== $("matrix-max-replies-video")) {
    $("matrix-max-replies-video").value = settings.max_replies_per_video ?? 2;
  }
  if ($("matrix-max-per-day") && document.activeElement !== $("matrix-max-per-day")) {
    $("matrix-max-per-day").value = settings.max_per_day ?? 20;
  }
  if ($("matrix-auto-downgrade") && document.activeElement !== $("matrix-auto-downgrade")) {
    $("matrix-auto-downgrade").checked = settings.auto_downgrade_on_error !== false;
  }

  // 7. Advanced settings
  if ($("exp-candidate-limit") && document.activeElement !== $("exp-candidate-limit")) {
    $("exp-candidate-limit").value = settings.candidate_limit ?? 20;
  }
  if ($("exp-inspect-limit") && document.activeElement !== $("exp-inspect-limit")) {
    $("exp-inspect-limit").value = settings.inspect_limit ?? 4;
  }
  if ($("exp-decision-max-tokens") && document.activeElement !== $("exp-decision-max-tokens")) {
    $("exp-decision-max-tokens").value = settings.decision_max_tokens ?? 8192;
  }
  if ($("exp-deadline-seconds") && document.activeElement !== $("exp-deadline-seconds")) {
    $("exp-deadline-seconds").value = settings.deadline_seconds ?? 1800;
  }
  if ($("exp-autonomous-topics") && document.activeElement !== $("exp-autonomous-topics")) {
    $("exp-autonomous-topics").checked = Boolean(settings.autonomous_topics);
  }
  if ($("exp-visual-enabled") && document.activeElement !== $("exp-visual-enabled")) {
    $("exp-visual-enabled").checked = settings.visual_enabled !== false;
  }
}

export function renderOperationsQuickBar(status) {
  const settings = status.settings || {};
  if ($("op-keywords") && document.activeElement !== $("op-keywords")) {
    $("op-keywords").value = (settings.keywords || []).join(", ");
  }
  if ($("op-source") && document.activeElement !== $("op-source")) {
    const src = (settings.sources && settings.sources[0]) || "recommend";
    $("op-source").value = src;
  }
  if ($("op-max-replies") && document.activeElement !== $("op-max-replies")) {
    $("op-max-replies").value = settings.max_replies_per_video ?? 2;
  }

  const policyEnabled = status.policy?.enabled === true;
  const hasActiveRound = Boolean(
    status.active_round || (status.rounds && status.rounds.length) || (status.state === "running" && !policyEnabled)
  );

  const toggleBtn = $("op-toggle-auto");
  if (toggleBtn) {
    toggleBtn.textContent = policyEnabled ? "停止自主运营" : "开启自主运营";
    toggleBtn.className = policyEnabled ? "danger" : "primary";
    toggleBtn.title = policyEnabled ? "点击关闭后台周期性自主运营" : "点击开启后台周期性自主运营";
  }

  const stopRoundBtn = $("op-stop-round");
  if (stopRoundBtn) {
    stopRoundBtn.disabled = !hasActiveRound;
    stopRoundBtn.title = hasActiveRound ? "点击中止当前正在执行的这一轮运营任务" : "当前无正在运行的轮次";
  }

  const previewBtn = $("op-preview-round");
  if (previewBtn) {
    previewBtn.disabled = hasActiveRound;
    previewBtn.textContent = hasActiveRound ? "轮次执行中…" : "单轮运营预览";
  }
}

export async function savePermissionMatrix(api, accountPanel, options = {}) {
  const isSilent = options && options.silent === true;
  const optimisticSettings = getOptimisticSettingsFromDOM();

  // 1. Instant Optimistic UI Update
  const account = accountPanel?.value;
  const hasCsrf = Boolean(account?.configured?.csrf);
  applyPermissionTierUI(calculatePermissionTier(optimisticSettings, account, hasCsrf));

  const current = currentAutomationStatus?.settings || {};
  const candidateLimit = $("exp-candidate-limit") ? parseInt($("exp-candidate-limit").value || "20", 10) : current.candidate_limit;
  const inspectLimit = $("exp-inspect-limit") ? parseInt($("exp-inspect-limit").value || "4", 10) : current.inspect_limit;
  const decisionTokens = $("exp-decision-max-tokens") ? parseInt($("exp-decision-max-tokens").value || "8192", 10) : current.decision_max_tokens;
  const deadlineSec = $("exp-deadline-seconds") ? parseInt($("exp-deadline-seconds").value || "1800", 10) : current.deadline_seconds;
  const autonomousTopics = $("exp-autonomous-topics") ? Boolean($("exp-autonomous-topics").checked) : current.autonomous_topics;
  const visualEnabled = $("exp-visual-enabled") ? Boolean($("exp-visual-enabled").checked) : current.visual_enabled;

  const allowedBrowsingKeys = new Set([
    "sources", "keywords", "watch_videos", "autonomous_topics", "discovery_enabled",
    "top_level_enabled", "top_level_auto_publish", "reply_enabled", "reply_auto_publish",
    "video_like_enabled", "video_like_auto_publish", "video_fav_enabled", "video_fav_auto_publish",
    "max_replies_per_video", "auto_downgrade_on_error", "mentions_enabled", "mention_reply_enabled",
    "replies_enabled", "comment_handoff_enabled", "visual_enabled", "workers", "candidate_limit",
    "inspect_limit", "max_search_terms", "max_pages", "max_reads", "max_model_calls",
    "max_input_tokens", "max_output_tokens", "decision_max_tokens", "deadline_seconds",
    "interval_seconds", "notification_interval_seconds", "read_retries", "max_takeovers",
    "max_per_hour", "max_per_day", "recipient_cooldown_seconds", "top_level_limit",
    "recent_sample_limit", "exclude_words", "allowed_author_uids", "excluded_author_uids",
    "allowed_categories", "min_duration_seconds", "max_duration_seconds", "published_after",
    "published_before", "languages", "report_enabled"
  ]);

  const merged = {
    ...current,
    ...optimisticSettings,
    candidate_limit: Math.max(1, Math.min(20, candidateLimit)),
    inspect_limit: Math.max(1, Math.min(20, inspectLimit)),
    decision_max_tokens: Math.max(512, Math.min(65536, decisionTokens)),
    deadline_seconds: Math.max(60, Math.min(3600, deadlineSec)),
    autonomous_topics: autonomousTopics,
    visual_enabled: visualEnabled,
  };

  const payload = {};
  for (const [k, v] of Object.entries(merged)) {
    if (allowedBrowsingKeys.has(k)) {
      payload[k] = v;
    }
  }

  const fb = $("matrix-save-feedback");
  if (fb && !isSilent) fb.textContent = "正在保存权限矩阵…";

  try {
    await api("/api/automation/settings", { method: "PUT", body: payload });
    // Also sync message handoff policy to messages API
    try {
      const msgStatus = await api("/api/messages");
      if (msgStatus && msgStatus.policy) {
        await api("/api/messages/policy", {
          method: "PUT",
          body: {
            ...msgStatus.policy,
            allow_comment_handoff: optimisticSettings.comment_handoff_enabled,
            auto_send: optimisticSettings.handoff_auto_send,
          },
        });
      }
    } catch (_dmErr) {
      // Ignored if direct messages not active
    }

    if (currentAutomationStatus) {
      currentAutomationStatus.settings = {
        ...(currentAutomationStatus.settings || {}),
        ...payload,
        handoff_auto_send: optimisticSettings.handoff_auto_send,
      };
    }
    const timeStr = new Date().toLocaleTimeString();
    if (fb) {
      fb.textContent = isSilent ? `✓ 设置已自动保存生效 (${timeStr})` : "✓ 权限矩阵已保存并即时生效";
      fb.style.color = "var(--ok, #2e7d32)";
    }
    if (!isSilent) {
      notice("权限矩阵已保存并即时生效。");
    }
    await updatePermissionCapsule(api, accountPanel);
  } catch (err) {
    if (fb) {
      fb.textContent = `保存失败：${safeMessage(err.message)}`;
      fb.style.color = "var(--danger, #d32f2f)";
    }
    notice(err.message, true);
  }
}

export function initOperationsModule(api, accountPanel, automationPanel) {
  function jumpToPermissionMatrix() {
    location.hash = "settings";
    setTimeout(() => {
      const el = $("operations-permission-matrix");
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
        el.classList.add("highlight-panel");
        setTimeout(() => el.classList.remove("highlight-panel"), 2000);
      }
    }, 150);
  }

  if ($("permission-capsule")) $("permission-capsule").onclick = jumpToPermissionMatrix;
  if ($("op-permission-capsule-btn")) $("op-permission-capsule-btn").onclick = jumpToPermissionMatrix;
  if ($("matrix-save")) $("matrix-save").onclick = () => savePermissionMatrix(api, accountPanel, { silent: false });

  const matrixContainer = $("operations-permission-matrix");
  if (matrixContainer) {
    matrixContainer.addEventListener("change", () => {
      // Immediate optimistic update of top capsule & badge
      const optimistic = getOptimisticSettingsFromDOM();
      const account = accountPanel?.value;
      const hasCsrf = Boolean(account?.configured?.csrf);
      applyPermissionTierUI(calculatePermissionTier(optimistic, account, hasCsrf));

      // Debounced auto-save
      matrixUserInteractingUntil = Date.now() + 5000;
      clearTimeout(matrixSaveTimeout);
      matrixSaveTimeout = setTimeout(() => {
        savePermissionMatrix(api, accountPanel, { silent: true }).catch(console.error);
      }, 100);
    });

    matrixContainer.addEventListener("input", (e) => {
      matrixUserInteractingUntil = Date.now() + 5000;
      if (e.target.type === "number") {
        clearTimeout(matrixSaveTimeout);
        matrixSaveTimeout = setTimeout(() => {
          savePermissionMatrix(api, accountPanel, { silent: true }).catch(console.error);
        }, 600);
      }
    });

    matrixContainer.addEventListener("focusin", () => {
      matrixUserInteractingUntil = Date.now() + 5000;
    });
  }

  if ($("op-save-quick-config")) {
    $("op-save-quick-config").onclick = async () => {
      const rawKw = $("op-keywords")?.value || "";
      const keywords = rawKw
        .split(/[,，\n]/)
        .map(k => k.trim())
        .filter(k => k.length > 0)
        .slice(0, 20);
      const source = $("op-source")?.value || "recommend";
      const maxReplies = parseInt($("op-max-replies")?.value || "2", 10);

      const current = currentAutomationStatus?.settings || {};
      const payload = {
        ...current,
        keywords,
        sources: [source],
        max_replies_per_video: Math.max(0, Math.min(20, maxReplies)),
      };
      delete payload.comment_handoff_enabled;

      try {
        await api("/api/automation/settings", { method: "PUT", body: payload });
        notice("运营关键词与来源设置已保存。");
        await updatePermissionCapsule(api, accountPanel);
      } catch (err) {
        notice(err.message, true);
      }
    };
  }

  if ($("op-preview-round")) {
    $("op-preview-round").onclick = async () => {
      try {
        await api("/api/automation/round", { method: "POST", body: { preview: true } });
        notice("单轮运营预览已排队启动，候选将存为待审核草稿。");
        await automationPanel?.refresh();
        await updatePermissionCapsule(api, accountPanel);
      } catch (e) {
        notice(e.message, true);
      }
    };
  }

  if ($("op-toggle-auto")) {
    $("op-toggle-auto").onclick = async () => {
      try {
        const isEnabled = currentAutomationStatus?.policy?.enabled === true;
        const target = !isEnabled;
        await api("/api/automation/enable", { method: "POST", body: { enabled: target } });
        notice(target ? "自主运营已开启。" : "自主运营已停止。");
        await automationPanel?.refresh();
        await updatePermissionCapsule(api, accountPanel);
      } catch (e) {
        notice(e.message, true);
      }
    };
  }

  if ($("op-stop-round")) {
    $("op-stop-round").onclick = async () => {
      try {
        await api("/api/automation/stop", { method: "POST", body: { emergency: false } });
        notice("已请求中止当前轮次。");
        await automationPanel?.refresh();
        await updatePermissionCapsule(api, accountPanel);
      } catch (e) {
        notice(e.message, true);
      }
    };
  }
}
