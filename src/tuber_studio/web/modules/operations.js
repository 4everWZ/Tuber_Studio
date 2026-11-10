import { $, safeMessage, notice } from "./core.js";
import { initPermissionMatrix, renderPermissionStatus, saveMatrixSettings } from "./permission-matrix.js?v=20261107.12";
import { renderOperationCapabilities } from "./operation-capabilities.js?v=20261107.17";

let currentAutomationStatus = null;
let statusRequest = 0;
let statusRequestedAt = 0;
let controlPending = false;

export async function updatePermissionCapsule(api, accountPanel, { background = false } = {}) {
  const now = Date.now();
  if (background && now - statusRequestedAt < 8000) return;
  statusRequestedAt = now;
  const request = ++statusRequest;
  try {
    const status = await api("/api/automation");
    if (request !== statusRequest) return;
    currentAutomationStatus = status;

    renderPermissionStatus(status, accountPanel?.value);
    renderOperationsQuickBar(status);
  } catch (e) {
    console.warn("Failed to update permission capsule:", e);
  }
}

const roundNumberFields = [
  ["daily_watch_limit", "op-daily-watch", 0],
  ["max_replies_per_video", "op-max-replies", 2],
  ["max_per_hour", "op-max-per-hour", 5],
  ["max_per_day", "matrix-max-per-day", 20],
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
    $("exp-frame-sampling").value = settings.frame_sampling || "baseline";
    $("op-business-timezone").value = settings.business_timezone || "Asia/Taipei";
  }
  $("op-save-quick-config").disabled = roundSettingsSaving;

  const policyEnabled = status.policy?.enabled === true;
  $("op-home-state").textContent = status.policy?.emergency ? "已紧急停止" : status.state === "stopping" ? "正在停止自主运营" : status.state === "blocked" ? "自主运营受阻" : policyEnabled ? "自主运营中" : "自主运营已停止";
  $("discovery-top-state").textContent = $("op-home-state").textContent;
  renderOperationCapabilities(status);
  const activeRounds = status.active_rounds ?? (status.active_round ? [status.active_round] : []);
  const hasActiveRound = activeRounds.length > 0;
  const hasVideoRound = activeRounds.some(round => !round.config?.interaction_only);
  const emergency = Boolean(status.policy?.emergency);

  const toggleBtn = $("op-toggle-auto");
  if (toggleBtn) {
    toggleBtn.textContent = policyEnabled ? "停止自主运营" : "开始自主运营";
    toggleBtn.disabled = emergency || controlPending;
    toggleBtn.setAttribute("aria-busy", String(controlPending));
  }
  $("op-emergency").disabled = emergency;
  $("op-reset-stop").hidden = !emergency;

  const stopRoundBtn = $("op-stop-round");
  if (stopRoundBtn) {
    stopRoundBtn.disabled = !hasActiveRound;
    stopRoundBtn.title = hasActiveRound ? "中止所有正在执行的轮次，保留自动运行设置" : "当前无正在运行的轮次";
  }

  const roundSummary = $("op-active-rounds");
  if (roundSummary) {
    const names = activeRounds.map(round => round.config?.interaction_only ? "互动收件箱" :
      round.preview ? "只读预览" : "刷视频");
    roundSummary.textContent = hasActiveRound ? `当前轮次 · ${names.join(" · ")}` : "暂无正在执行的轮次";
  }

  const previewBtn = $("op-preview-round");
  if (previewBtn) {
    previewBtn.disabled = hasVideoRound;
    previewBtn.textContent = "只读单轮预览";
  }
}

export async function savePermissionMatrix(api, accountPanel, options) {
  await saveMatrixSettings(api, options);
  await updatePermissionCapsule(api, accountPanel);
}

export function initOperationsModule(api, accountPanel) {
  initPermissionMatrix(options => savePermissionMatrix(api, accountPanel, options));

  const roundForm = $("op-round-settings");
  if (roundForm) {
    const feedback = $("op-round-settings-note");
    roundForm.addEventListener("input", () => { roundSettingsDirty = true; feedback.textContent = "有未保存的修改。"; });
    roundForm.onsubmit = async event => {
      event.preventDefault();
      const save = $("op-save-quick-config");
      if (save.disabled) return;
      const payload = Object.fromEntries(roundNumberFields.map(([name, id]) => [name, Number($(id).value)]));
      payload.business_timezone = $("op-business-timezone").value.trim() || "Asia/Taipei";
      payload.visual_enabled = $("exp-visual-enabled").checked;
      payload.frame_sampling = $("exp-frame-sampling").value;
      const fields = [...roundForm.querySelectorAll("input, select")];
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
        feedback.textContent = "轮次参数已保存。";
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
      if (controlPending) return;
      controlPending = true;
      if (currentAutomationStatus) renderOperationsQuickBar(currentAutomationStatus);
      try {
        const isEnabled = currentAutomationStatus?.policy?.enabled === true;
        const target = !isEnabled;
        await api("/api/automation/enable", { method: "POST", body: { enabled: target } });
        notice(target ? "自主运营已开启。" : "自主运营已停止。");
        await updatePermissionCapsule(api, accountPanel);
      } catch (e) {
        notice(e.message, true);
      } finally {
        controlPending = false;
        if (currentAutomationStatus) renderOperationsQuickBar(currentAutomationStatus);
      }
    };
  }

  if ($("op-stop-round")) {
    $("op-stop-round").onclick = async () => {
      try {
        await api("/api/automation/stop", { method: "POST", body: { emergency: false } });
        notice("已请求中止所有当前轮次。");
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
