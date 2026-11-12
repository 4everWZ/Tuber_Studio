import { $, safeMessage, notice } from "./core.js";

let matrixUserInteractingUntil = 0;
let matrixSaveTimeout = null;

function hasAutomaticRules(settings) {
  return (settings.top_level_enabled && settings.top_level_auto_publish) ||
    (settings.reply_enabled && settings.reply_auto_publish) ||
    (settings.comment_like_enabled && settings.comment_like_auto_publish) ||
    (settings.video_like_enabled && settings.video_like_auto_publish) ||
    (settings.video_fav_enabled && settings.video_fav_auto_publish) ||
    (settings.comment_handoff_enabled && settings.handoff_auto_send);
}

function calculatePermissionTier(status, account, hasCsrf) {
  const settings = status.settings || {};
  const effectiveConfigured = status?.effective?.comments?.credentials_configured ?? true;
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
    !settings.comment_like_enabled &&
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
    if (status?.automatic_sending_ready !== true) {
      return {
        tier: "draft",
        icon: "📝",
        text: "权限：已配置自动放行 · 等待连接就绪",
        badgeText: "等待账号或模型连接"
      };
    }
    if (!status.policy?.enabled || status.state !== "running") {
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
      text: "权限：已授权动作可自动放行",
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

function applyPermissionTierUI(tierInfo) {
  const { tier, badgeText } = tierInfo;

  const matrixBadge = $("matrix-capsule-badge");
  if (matrixBadge) {
    matrixBadge.textContent = badgeText;
    matrixBadge.className = `badge ${tier === "autopublish" ? "active" : tier === "draft" ? "good" : ""}`;
  }
}

function getOptimisticSettingsFromDOM() {
  const topVal = document.querySelector('input[name="matrix_top_level"]:checked')?.value || "1";
  const replyVal = document.querySelector('input[name="matrix_reply"]:checked')?.value || "1";
  const commentLikeVal = document.querySelector('input[name="matrix_comment_like"]:checked')?.value || "0";
  const dmVal = document.querySelector('input[name="matrix_dm"]:checked')?.value || "1";
  const likeVal = document.querySelector('input[name="matrix_video_like"]:checked')?.value || "1";
  const favVal = document.querySelector('input[name="matrix_video_fav"]:checked')?.value || "1";

  const autoDowngrade = !!$("matrix-auto-downgrade")?.checked;

  return {
    discovery_enabled: $("matrix-video-enabled")?.checked === true,
    top_level_enabled: topVal !== "0",
    top_level_auto_publish: topVal === "2",
    reply_enabled: replyVal !== "0",
    reply_auto_publish: replyVal === "2",
    comment_like_enabled: commentLikeVal !== "0",
    comment_like_auto_publish: commentLikeVal === "2",
    comment_handoff_enabled: dmVal !== "0",
    handoff_auto_send: dmVal === "2",
    video_like_enabled: likeVal !== "0",
    video_like_auto_publish: likeVal === "2",
    video_fav_enabled: favVal !== "0",
    video_fav_auto_publish: favVal === "2",
    auto_downgrade_on_error: autoDowngrade,
  };
}

function renderVisualFallbackStatus(status) {
  const matrixVisualEl = $("matrix-visual-status");
  if (!matrixVisualEl) return;

  const visual = status?.vision?.images === true;
  matrixVisualEl.hidden = false;
  matrixVisualEl.className = "hint visual-status-box";
  matrixVisualEl.textContent = visual ? "观看时读取字幕并抽样画面" : "当前只能读取字幕。可在推理设置中配置支持多模态的视觉接口。";
}

function renderPermissionMatrix(status) {
  if (Date.now() < matrixUserInteractingUntil) return;
  const matrixEl = $("operations-permission-matrix");
  if (matrixEl && matrixEl.contains(document.activeElement)) return;

  const settings = status.settings || {};
  if (document.activeElement !== $("matrix-video-enabled")) $("matrix-video-enabled").checked = settings.discovery_enabled !== false;

  // 1. Top level
  const topLevelVal = !settings.top_level_enabled ? "0" : settings.top_level_auto_publish ? "2" : "1";
  const topRadio = document.querySelector(`input[name="matrix_top_level"][value="${topLevelVal}"]`);
  if (topRadio && document.activeElement?.name !== "matrix_top_level") topRadio.checked = true;

  // 2. Reply
  const replyVal = !settings.reply_enabled ? "0" : settings.reply_auto_publish ? "2" : "1";
  const replyRadio = document.querySelector(`input[name="matrix_reply"][value="${replyVal}"]`);
  if (replyRadio && document.activeElement?.name !== "matrix_reply") replyRadio.checked = true;
  const commentLikeVal = !settings.comment_like_enabled ? "0" : settings.comment_like_auto_publish ? "2" : "1";
  const commentLikeRadio = document.querySelector(`input[name="matrix_comment_like"][value="${commentLikeVal}"]`);
  if (commentLikeRadio && document.activeElement?.name !== "matrix_comment_like") commentLikeRadio.checked = true;

  // 3. DM
  const messages = status.base?.messages;
  const dmVal = !messages?.enabled || !messages.allow_comment_handoff ? "0" : messages.auto_send ? "2" : "1";
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

  if ($("matrix-auto-downgrade") && document.activeElement !== $("matrix-auto-downgrade")) {
    $("matrix-auto-downgrade").checked = settings.auto_downgrade_on_error !== false;
  }
}

export function renderPermissionStatus(status, account) {
  const hasSessdata = Boolean(account?.configured?.sessdata);
  const hasCsrf = Boolean(account?.configured?.csrf);
  const tierInfo = calculatePermissionTier(status, account, hasCsrf);
  applyPermissionTierUI(tierInfo);
  const sendingState = $("automatic-sending-state");
  sendingState.hidden = !hasAutomaticRules(status.settings || {}) || status.automatic_sending_ready !== false;
  const blockers = (status.blockers || []).map(value => typeof value === "string" ? value : value.message).filter(Boolean);
  const credentialsConfigured = account ? hasSessdata && hasCsrf : status.effective?.comments?.credentials_configured;
  sendingState.textContent = sendingState.hidden ? "" : blockers.length ? `尚未就绪：${blockers.join("；")}` :
    credentialsConfigured === false ? "请在账号设置中补全登录和写入凭据。" : "等待当前账号和模型连接状态更新。";

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

  renderPermissionMatrix(status);
  renderVisualFallbackStatus(status);
}

export async function saveMatrixSettings(api, { silent: isSilent = false } = {}) {
  const { comment_handoff_enabled, handoff_auto_send, ...payload } = getOptimisticSettingsFromDOM();

  const fb = $("matrix-save-feedback");
  if (fb && !isSilent) fb.textContent = "正在保存权限矩阵…";

  let browsingSaved = false;
  try {
    const saved = await api("/api/automation/settings", { method: "PUT", body: payload });
    browsingSaved = true;
    const messages = saved.base?.messages;
    const enabled = comment_handoff_enabled || messages?.enabled || false;
    if (messages && (messages.enabled !== enabled || messages.allow_comment_handoff !== comment_handoff_enabled || messages.auto_send !== handoff_auto_send)) {
      await api("/api/messages/policy", {
        method: "PUT",
        body: { ...messages, enabled, allow_comment_handoff: comment_handoff_enabled, auto_send: handoff_auto_send },
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
}

export function initPermissionMatrix(save) {
  if ($("matrix-save")) $("matrix-save").onclick = () => save();

  const matrixContainer = $("operations-permission-matrix");
  if (matrixContainer) {
    const handleMatrixChange = () => {
      // Debounced auto-save
      matrixUserInteractingUntil = Date.now() + 5000;
      clearTimeout(matrixSaveTimeout);
      matrixSaveTimeout = setTimeout(() => {
        save({ silent: true }).catch(console.error);
      }, 100);
    };

    matrixContainer.addEventListener("change", handleMatrixChange);
    matrixContainer.addEventListener("click", (e) => {
      if (e.target && e.target.type === "radio") {
        handleMatrixChange();
      }
    });

    matrixContainer.addEventListener("focusin", () => {
      matrixUserInteractingUntil = Date.now() + 5000;
    });
  }
}
