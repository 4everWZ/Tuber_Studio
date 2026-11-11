"use strict";

import { $ } from "./core.js";
import { inferenceDisplay } from "./inference-status.js?v=20261111.03";
import { inferenceModeChanging } from "./live-controls.js?v=20261107.12";

export function liveEnabled(model) {
  return model.settings?.features?.live_enabled === true;
}

export function resourcesTransitioning(model) {
  const inference = model.status?.inference;
  return !!(model.modeSwitch || inferenceModeChanging(inference));
}

function resourceMode(inference) {
  return ["text", "live"].includes(inference?.mode) ? inference.mode : null;
}

function resourceModeName(mode) {
  return mode === "live" ? "直播模式" : "运营模式";
}

export function renderModeControls(inference = {}, model) {
  const hasBroker = model.settings?.inference_broker?.enabled === true;
  const directText = model.status?.providers?.llm_route === "direct" &&
    inference.mode === "text" && model.status?.operations_waiting === false;
  const currentMode = directText || (inference.state && inference.state !== "error") ? resourceMode(inference) : null;
  const transitioning = resourcesTransitioning(model);
  document.querySelectorAll("[data-inference-mode]").forEach(button => {
    const target = button.dataset.inferenceMode;
    button.setAttribute("aria-pressed", String(target === currentMode));
    button.disabled = !hasBroker || transitioning || target === currentMode || (target === "live" && !liveEnabled(model));
    button.hidden = !hasBroker;
  });
  const pending = model.modeSwitch?.target || inference.operation?.mode;
  const feedback = $("resource-mode-feedback");
  feedback.textContent = transitioning ? `正在切换至${resourceModeName(pending)}…` :
    model.modeResult?.text || (!hasBroker ? "" :
      !liveEnabled(model) ? "当前配置未启用直播" : !currentMode ? "等待设备确认模式" : "");
  feedback.classList.toggle("error-text", !transitioning && model.modeResult?.error === true);
}

export function renderControls(model) {
  const status = model.status || { state: "stopped" };
  const enabled = liveEnabled(model);
  if ($("compact-live") && $("compact-live").getAttribute("aria-busy") !== "true") {
    $("compact-live").disabled = !enabled;
  }
  renderModeControls(status.inference, model);
}

export function renderServiceStatusCapsule(status, settings) {
  const capsule = $("service-status-capsule");
  const state = inferenceDisplay(status, settings);
  const online = state.connected;
  capsule.className = `service-status-capsule ${online === false ? "offline" : online ? "online" : ""}`;
  capsule.querySelector(".status-text").textContent = state.text;
}

export async function switchResourceMode(target, model, {
  api,
  notice,
  renderStatusFn,
  renderVoicePreviewFn,
  loadSettingsFn,
}) {
  if (target === "live" && !liveEnabled(model)) {
    notice("直播特性尚未在此安装中启用。");
    return;
  }
  if (!model.settings?.inference_broker?.enabled || model.modeSwitch) return;
  if (target === "text" && ["live", "paused", "degraded"].includes(model.status?.state) &&
      !confirm("切回运营模式会结束当前直播会话。继续切换？")) return;
  model.modeSwitch = { target };
  model.modeResult = null;
  renderModeControls(model.status?.inference, model);
  renderControls(model);
  if (renderVoicePreviewFn) renderVoicePreviewFn();
  try {
    const result = await api("/api/inference/mode", { method: "POST", body: { mode: target } });
    if (resourceMode(result) !== target) {
      throw new Error("接口返回的资源模式与请求不符，请检查实际资源状态。");
    }
    model.modeSwitch = null;
    model.modeResult = null;
    if (renderStatusFn) renderStatusFn({ ...model.status, inference: result });
  } catch (error) {
    model.modeSwitch = null;
    model.modeResult = { error: true, text: `切换失败：${error.message}` };
  } finally {
    renderControls(model);
    if (renderVoicePreviewFn) renderVoicePreviewFn();
  }
  try {
    if (renderStatusFn) renderStatusFn(await api("/api/status"));
    if (loadSettingsFn) await loadSettingsFn();
  } catch (error) {
    if (!model.modeResult) notice(`状态刷新失败：${error.message}`, true);
  }
}

export function initLiveModule(model, options) {
  document.querySelectorAll("[data-inference-mode]").forEach(button => {
    button.addEventListener("click", () => switchResourceMode(button.dataset.inferenceMode, model, options));
  });
}
