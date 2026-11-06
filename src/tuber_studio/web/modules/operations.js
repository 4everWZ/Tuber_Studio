/**
 * Operations Workbench & Permission Matrix Controller
 * Manages autonomous browsing, permission tiers, optimistic updates, and visual fallback.
 */

import { $, safeMessage, notice } from "./core.js";

let currentAutomationStatus = null;
let statusRequest = 0;
let statusRequestedAt = 0;
let matrixUserInteractingUntil = 0;
let matrixSaveTimeout = null;

export function getAutomationStatus() {
  return currentAutomationStatus;
}

function hasAutomaticRules(settings) {
  return (settings.top_level_enabled && settings.top_level_auto_publish) ||
    (settings.reply_enabled && settings.reply_auto_publish) ||
    (settings.video_like_enabled && settings.video_like_auto_publish) ||
    (settings.video_fav_enabled && settings.video_fav_auto_publish) ||
    (settings.comment_handoff_enabled && settings.handoff_auto_send);
}

export function calculatePermissionTier(settings = {}, account = null, hasCsrf = true) {
  const effectiveConfigured = currentAutomationStatus?.effective?.comments?.credentials_configured ?? true;
  const hasSessdata = account ? Boolean(account?.configured?.sessdata) : effectiveConfigured;
  const accountLoggedIn = account
    ? (account?.state === "logged_in" || account?.state === "unverified" || Boolean(account?.profile?.uid))
    : effectiveConfigured;
  const effectiveCsrf = account ? Boolean(hasCsrf && (account?.configured?.csrf ?? true)) : true;

  if (!hasSessdata || !effectiveCsrf || !accountLoggedIn) {
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

  if (hasAutomaticRules(settings)) {
    const maxDaily = settings.max_per_day || 20;
    if (currentAutomationStatus?.automatic_sending_ready !== true) {
      return {
        tier: "draft",
        icon: "📝",
        text: "权限：已配置自动放行 · 当前待审核",
        badgeText: "已配置自动放行 · 模型验收待完成"
      };
    }
    if (!currentAutomationStatus.policy?.enabled || currentAutomationStatus.state !== "running") {
      return {
        tier: "draft",
        icon: "📝",
        text: "权限：已配置自动放行 · 自主运营未运行",
        badgeText: "已配置自动放行 · 自主运营未运行"
      };
    }
    return {
      tier: "autopublish",
      icon: "⚡",
      text: `权限：已授权动作可自动放行（每日上限 ${maxDaily} 条）`,
      badgeText: "已授权动作可自动放行"
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

  const opCapsule = $("op-permission-capsule-btn");
  if (opCapsule) {
    opCapsule.dataset.tier = tier;
    opCapsule.innerHTML = `<span>${icon} ${text}</span>`;
  }

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

  const maxPerDay = parseInt($("matrix-max-per-day")?.value || "20", 10);
  const autoDowngrade = !!$("matrix-auto-downgrade")?.checked;

  return {
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
    max_per_day: Math.max(1, Math.min(200, maxPerDay || 20)),
    auto_downgrade_on_error: autoDowngrade,
  };
}

export function renderVisualFallbackStatus(status) {
  const matrixVisualEl = $("matrix-visual-status");
  if (!matrixVisualEl) return;

  const ready = status?.vision?.admitted === true && status.vision.available === true;
  matrixVisualEl.hidden = false;
  matrixVisualEl.className = "hint visual-status-box";
  matrixVisualEl.textContent = ready ? "视频可使用画面和字幕" : "视频使用字幕 · 视觉未就绪";
}

export async function updatePermissionCapsule(api, accountPanel, { background = false } = {}) {
  const now = Date.now();
  if (background && now - statusRequestedAt < 8000) return;
  statusRequestedAt = now;
  const request = ++statusRequest;
  try {
    const status = await api("/api/automation");
    if (request !== statusRequest) return;
    currentAutomationStatus = status;

    const account = accountPanel?.value;
    const hasSessdata = Boolean(account?.configured?.sessdata);
    const hasCsrf = Boolean(account?.configured?.csrf);
    const tierInfo = calculatePermissionTier(status.settings, account, hasCsrf);
    applyPermissionTierUI(tierInfo);
    const sendingState = $("automatic-sending-state");
    sendingState.hidden = !hasAutomaticRules(status.settings) || status.automatic_sending_ready !== false;
    sendingState.textContent = sendingState.hidden ? "" :
      "当前模型与角色组合缺少有效的语义验收，全渠道运营生成的草稿仍需审核。只读预览也会保留草稿。";

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
  const messages = status.base?.messages;
  const dmVal = !messages?.allow_comment_handoff ? "0" : messages.auto_send ? "2" : "1";
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
  if ($("matrix-max-per-day") && document.activeElement !== $("matrix-max-per-day")) {
    $("matrix-max-per-day").value = settings.max_per_day ?? 20;
  }
  if ($("matrix-auto-downgrade") && document.activeElement !== $("matrix-auto-downgrade")) {
    $("matrix-auto-downgrade").checked = settings.auto_downgrade_on_error !== false;
  }
}

const roundNumberFields = [
  ["max_replies_per_video", "op-max-replies", 2],
  ["interval_seconds", "op-interval", 300],
  ["workers", "op-workers", 4],
  ["candidate_limit", "exp-candidate-limit", 20],
  ["inspect_limit", "exp-inspect-limit", 4],
  ["decision_max_tokens", "exp-decision-max-tokens", 8192],
  ["deadline_seconds", "exp-deadline-seconds", 1800],
];
let roundSettingsDirty = false;
let roundSettingsSaving = false;

export function renderOperationsQuickBar(status) {
  const settings = status.settings || {};
  if (!roundSettingsDirty) {
    for (const [name, id, fallback] of roundNumberFields) $(id).value = settings[name] ?? fallback;
    $("exp-visual-enabled").checked = settings.visual_enabled !== false;
  }
  $("op-save-quick-config").disabled = roundSettingsSaving;

  const policyEnabled = status.policy?.enabled === true;
  const hasActiveRound = Boolean(status.active_round);
  const emergency = Boolean(status.policy?.emergency);

  const toggleBtn = $("op-toggle-auto");
  if (toggleBtn) {
    toggleBtn.textContent = policyEnabled ? "停止全渠道运营" : "开启全渠道运营";
    toggleBtn.disabled = emergency || Boolean(status.discovery?.enabled);
  }
  $("op-emergency").disabled = emergency;
  $("op-reset-stop").hidden = !emergency;

  const stopRoundBtn = $("op-stop-round");
  if (stopRoundBtn) {
    stopRoundBtn.disabled = !hasActiveRound;
    stopRoundBtn.title = hasActiveRound ? "点击中止当前正在执行的这一轮运营任务" : "当前无正在运行的轮次";
  }

  const previewBtn = $("op-preview-round");
  if (previewBtn) {
    previewBtn.disabled = hasActiveRound;
    previewBtn.textContent = "只读单轮预览";
  }
}

export async function savePermissionMatrix(api, accountPanel, options = {}) {
  const isSilent = options && options.silent === true;
  const { comment_handoff_enabled, handoff_auto_send, ...payload } = getOptimisticSettingsFromDOM();

  const fb = $("matrix-save-feedback");
  if (fb && !isSilent) fb.textContent = "正在保存权限矩阵…";

  let browsingSaved = false;
  try {
    const saved = await api("/api/automation/settings", { method: "PUT", body: payload });
    browsingSaved = true;
    const messages = saved.base?.messages;
    if (messages && (messages.allow_comment_handoff !== comment_handoff_enabled || messages.auto_send !== handoff_auto_send)) {
      await api("/api/messages/policy", {
        method: "PUT",
        body: { ...messages, allow_comment_handoff: comment_handoff_enabled, auto_send: handoff_auto_send },
      });
    }
    const timeStr = new Date().toLocaleTimeString();
    if (fb) {
      fb.textContent = isSilent ? `✓ 设置已自动保存生效 (${timeStr})` : "✓ 权限矩阵已保存并即时生效";
      fb.style.color = "var(--ok, #2e7d32)";
    } else if (!isSilent) {
      notice("权限矩阵已保存并即时生效。");
    }
  } catch (err) {
    const message = `${browsingSaved ? "运营设置已保存，私信设置保存失败" : "保存失败"}：${safeMessage(err.message)}`;
    if (fb) {
      fb.textContent = message;
      fb.style.color = "var(--danger, #d32f2f)";
    } else {
      notice(message, true);
    }
  }
  await updatePermissionCapsule(api, accountPanel);
}

export function initOperationsModule(api, accountPanel) {
  function jumpToPermissionMatrix() {
    location.hash = "operations-permission-matrix";
  }

  if ($("op-permission-capsule-btn")) $("op-permission-capsule-btn").onclick = jumpToPermissionMatrix;
  if ($("matrix-save")) $("matrix-save").onclick = () => savePermissionMatrix(api, accountPanel, { silent: false });

  const matrixContainer = $("operations-permission-matrix");
  if (matrixContainer) {
    const handleMatrixChange = () => {
      // Debounced auto-save
      matrixUserInteractingUntil = Date.now() + 5000;
      clearTimeout(matrixSaveTimeout);
      matrixSaveTimeout = setTimeout(() => {
        savePermissionMatrix(api, accountPanel, { silent: true }).catch(console.error);
      }, 100);
    };

    matrixContainer.addEventListener("change", handleMatrixChange);
    matrixContainer.addEventListener("click", (e) => {
      if (e.target && e.target.type === "radio") {
        handleMatrixChange();
      }
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

  const roundForm = $("op-round-settings");
  if (roundForm) {
    const feedback = $("op-round-settings-note");
    roundForm.addEventListener("input", () => { roundSettingsDirty = true; feedback.textContent = "有未保存的修改。"; });
    roundForm.onsubmit = async event => {
      event.preventDefault();
      const save = $("op-save-quick-config");
      if (save.disabled) return;
      const payload = Object.fromEntries(roundNumberFields.map(([name, id]) => [name, Number($(id).value)]));
      payload.visual_enabled = $("exp-visual-enabled").checked;
      const fields = [...roundForm.querySelectorAll("input")];
      roundSettingsSaving = true;
      save.disabled = true;
      for (const field of fields) field.disabled = true;
      feedback.textContent = "正在保存轮次参数…";
      try {
        const status = await api("/api/automation/settings", { method: "PUT", body: payload });
        ++statusRequest;
        currentAutomationStatus = status;
        roundSettingsDirty = false;
        renderOperationsQuickBar(status);
        feedback.textContent = "轮次参数已保存，从下一轮起使用。";
      } catch (err) {
        feedback.textContent = `保存失败：${safeMessage(err.message)}`;
      } finally {
        roundSettingsSaving = false;
        for (const field of fields) field.disabled = false;
        save.disabled = false;
      }
    };
  }

  if ($("op-preview-round")) {
    $("op-preview-round").onclick = async () => {
      try {
        await api("/api/automation/round", { method: "POST", body: { preview: true } });
        notice("单轮运营预览已排队启动，候选将存为待审核草稿。");
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
        if (target) {
          const account = accountPanel?.value?.profile?.uid || currentAutomationStatus?.account_id || "待核验";
          const role = currentAutomationStatus?.character?.id || "当前角色";
          if (!confirm(`开启全渠道运营？B 站 UID ${account}，角色 ${role}。评论、通知和私信会按现有授权与额度自动处理。`)) return;
        }
        await api("/api/automation/enable", { method: "POST", body: { enabled: target } });
        notice(target ? "自主运营已开启。" : "自主运营已停止。");
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
        await updatePermissionCapsule(api, accountPanel);
      } catch (e) {
        notice(e.message, true);
      }
    };
  }
  for (const [id, url, body, message] of [
    ["op-emergency", "/api/automation/stop", { emergency: true }, "已紧急停止所有自动收发。"],
    ["op-reset-stop", "/api/automation/reset-stop", undefined, "已解除紧急停止，可手动开始。"],
  ]) {
    $(id).onclick = async () => {
      $(id).disabled = true;
      try {
        await api(url, { method: "POST", body });
        notice(message);
      } catch (error) {
        notice(error.message, true);
      } finally {
        $(id).disabled = false;
        await updatePermissionCapsule(api, accountPanel);
      }
    };
  }
}
