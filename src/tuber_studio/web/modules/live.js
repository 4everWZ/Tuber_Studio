"use strict";

import { $ } from "./core.js";

export function liveEnabled(model) {
  return model.settings?.features?.live_enabled === true;
}

export function resourcesTransitioning(model) {
  const inference = model.status?.inference;
  return !!(model.modeSwitch || (inference?.operation?.mode != null && inference.operation.mode !== inference.mode));
}

export function resourceMode(inference, model) {
  return model?.modeSwitch?.target || inference?.operation?.mode || inference?.mode || "text_only";
}

export function resourceModeName(mode) {
  return { live: "直播优先", text_only: "纯文本节约" }[mode] || mode || "未报告";
}

export function resourceName(key) {
  return { tts: "TTS 服务", vlm: "视觉多模态", whisper: "语音识别" }[key] || key;
}

export function inferenceResidents(inference) {
  const residents = [];
  if (inference?.tts?.resident) residents.push("TTS 语音");
  if (inference?.vlm?.loaded) residents.push("视觉多模态");
  return residents.length ? residents.join(" · ") : "当前仅驻留文本大模型";
}

export function inferenceLabel(inference) {
  if (inference?.kind === "llama_cpp") return "本地 llama.cpp / Jetson 原生";
  if (inference?.kind === "openai_compatible") return "远程 OpenAI 兼容 Broker";
  return inference?.kind || "—";
}

export function renderLiveFeatures(model) {
  const enabled = liveEnabled(model);
  const liveNotice = $("live-feature-notice");
  if (liveNotice) liveNotice.hidden = enabled;
  const startBtn = $("start");
  if (startBtn && !enabled) startBtn.title = "直播特性未启用";
}

export function renderModeControls(inference = {}, model) {
  const currentMode = resourceMode(inference, model);
  const transitioning = resourcesTransitioning(model);
  const btnLive = $("mode-live");
  const btnText = $("mode-text");
  if (btnLive && btnText) {
    btnLive.classList.toggle("selected", currentMode === "live");
    btnText.classList.toggle("selected", currentMode === "text_only");
    btnLive.disabled = transitioning || currentMode === "live";
    btnText.disabled = transitioning || currentMode === "text_only";
  }
}

export function renderControls(model) {
  const status = model.status || { state: "stopped" };
  const enabled = liveEnabled(model);
  document.querySelectorAll("[data-action]").forEach(button => {
    if (button.getAttribute("aria-busy") === "true") return;
    const action = button.dataset.action;
    if (!enabled && !(action === "stop" && status.state !== "stopped")) {
      button.disabled = true;
      return;
    }
    button.disabled = action === "start" ? status.state !== "stopped" :
      action === "pause" ? !["live", "degraded"].includes(status.state) :
      action === "resume" ? status.state !== "paused" :
      action === "stop" ? status.state === "stopped" :
      action === "mute" ? !!status.muted :
      action === "unmute" ? !status.muted :
      action === "skip" ? !status.current_job :
      action === "clear" ? !status.queue_length : false;
    if (["start", "resume"].includes(action) && resourcesTransitioning(model)) {
      button.disabled = true;
    }
  });
  $("event-form")?.querySelectorAll("input, select, textarea, button").forEach(input => {
    if (input.getAttribute("aria-busy") !== "true") {
      input.disabled = !enabled || resourcesTransitioning(model);
    }
  });
  if ($("compact-live") && $("compact-live").getAttribute("aria-busy") !== "true") {
    $("compact-live").disabled = !enabled;
  }
  renderLiveFeatures(model);
  renderModeControls(status.inference, model);
}

export function renderInference(inference, model, describe) {
  describe($("inference-details"), [
    ["运行架构", inferenceLabel(inference)],
    ["活跃模型", inference?.active_model || inference?.model || "—"],
    ["资源模式", resourceModeName(resourceMode(inference, model))],
    ["驻留资源", inferenceResidents(inference)],
    ["端点状态", inference?.remote_reachable !== false ? "正常直通" : "不可达 / 离线"],
  ]);
  renderModeControls(inference, model);
}

export function renderServiceStatusCapsule(status, node) {
  const capsule = $("service-status-capsule");
  if (!capsule) return;
  const inference = status.inference || {};
  const tts = inference.tts || {};
  const isOnline = inference.remote_reachable !== false;
  capsule.className = `service-status-capsule ${isOnline ? "online" : "offline"}`;
  const dot = capsule.querySelector(".status-dot") || node("span", "", "status-dot");
  const text = capsule.querySelector(".status-text") || node("span", "", "status-text");
  const hostLabel = inference.host ? ` [${inference.host}]` : "";
  const ttsState = tts.resident ? "TTS 已就绪" : "TTS 待命卸载 (省显存)";
  text.textContent = isOnline ?
    `🚀 节点在线${hostLabel} · 模型: ${inference.active_model || inference.model || "MiniCPM-V-4.7-1B"} · ${ttsState}` :
    `⚠️ 节点离线${hostLabel} · 请检查 9012 Broker 服务`;
  if (!capsule.contains(dot)) capsule.prepend(dot);
  if (!capsule.contains(text)) capsule.append(text);
}

export async function refreshLive({ api, list, node, cell, time, label, badge, empty }) {
  const results = await Promise.all([api("/api/jobs?limit=50"), api("/api/events?limit=50")]);
  const jobs = list(results[0]);
  renderJobs($("overview-jobs"), jobs.slice(0, 8), false, { node, cell, time, badge, empty });
  renderJobs($("live-jobs"), jobs, true, { node, cell, time, badge, empty });
  const events = list(results[1]);
  const tbody = $("events");
  if (!tbody) return;
  if (!events.length) return empty(tbody, 5, "暂无事件。在上方调试台注入一条弹幕。");
  tbody.replaceChildren();
  events.forEach(event => {
    const payload = event.payload || event;
    const row = node("tr");
    cell(row, time(payload.timestamp || event.created_at), event.event_id || event.platform_event_id || event.id);
    cell(row, payload.display_name || "匿名观众", `${payload.platform || "—"} · ${payload.platform_user_id || "未知 ID"}`);
    cell(row, label(payload.type));
    cell(row, payload.text || "—");
    const duplicateCount = event.duplicate_count || event.duplicates || 0;
    cell(row, badge(event.status), event.reason || event.error || (duplicateCount ? `重复 ${duplicateCount} 次` : ""));
    tbody.append(row);
  });
}

export function renderJobs(tbody, jobs, withId, { node, cell, time, badge, empty }) {
  if (!tbody) return;
  if (!jobs.length) return empty(tbody, 5, "暂无任务。注入事件后可查看处理状态。");
  tbody.replaceChildren();
  jobs.forEach(job => {
    const row = node("tr");
    cell(row, time(job.created_at), withId ? job.id : "");
    cell(row, `${job.character_id || "—"} · v${job.character_version ?? "—"}`);
    cell(row, job.text || job.spoken_text || "—");
    cell(row, badge(job.status));
    cell(row, job.llm_ms != null || job.tts_ms != null ? `${Math.round(job.llm_ms || 0)} / ${Math.round(job.tts_ms || 0)} ms` : "—", job.error || job.reason || "");
    tbody.append(row);
  });
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
  const previousMode = resourceMode(model.status?.inference, model);
  if (!previousMode || model.modeSwitch) return;
  model.modeSwitch = { target, previousMode };
  model.modeResult = null;
  renderModeControls(model.status?.inference, model);
  renderControls(model);
  if (renderVoicePreviewFn) renderVoicePreviewFn();
  try {
    const result = await api("/api/inference/mode", { method: "POST", body: { mode: target } });
    if (resourceMode(result, model) !== target) {
      throw new Error("接口返回的资源模式与请求不符，请检查实际资源状态。");
    }
    model.modeSwitch = null;
    model.modeResult = { mode: target, error: false, text: `已切换到${resourceModeName(target)}模式。` };
    if (renderStatusFn) {
      renderStatusFn({ ...model.status, inference: result });
      try {
        renderStatusFn(await api("/api/status"));
      } catch (err) {
        notice(`模式已切换，状态刷新失败：${err.message}`, true);
      }
    }
    if (loadSettingsFn) await loadSettingsFn();
  } catch (error) {
    model.modeSwitch = null;
    model.modeResult = { mode: previousMode, error: true, text: `切换失败：${error.message}` };
    renderModeControls(model.status?.inference, model);
    if (renderStatusFn) {
      try {
        renderStatusFn(await api("/api/status"));
      } catch (_) {}
    }
  } finally {
    renderControls(model);
    if (renderVoicePreviewFn) renderVoicePreviewFn();
  }
}

export function initLiveModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  time,
  label,
  badge,
  empty,
  renderStatusFn,
  renderVoicePreviewFn,
  loadSettingsFn,
}) {
  const doRefreshLive = () => refreshLive({ api, list: arr => Array.isArray(arr) ? arr : arr?.items || [], node, cell, time, label, badge, empty });

  document.querySelectorAll("[data-inference-mode]").forEach(button => {
    button.addEventListener("click", () => switchResourceMode(button.dataset.inferenceMode, model, {
      api,
      notice,
      renderStatusFn,
      renderVoicePreviewFn,
      loadSettingsFn,
    }));
  });

  document.querySelectorAll("[data-action]").forEach(button => {
    button.addEventListener("click", () => busy(button, async () => {
      const action = button.dataset.action;
      if (action === "clear" && !confirm("清空所有待播任务？当前播放不会因此标记为完成。")) return;
      const result = await api(`/api/live/${action}`, { method: "POST" });
      notice(`已执行：${button.textContent}`);
      if (renderStatusFn) {
        renderStatusFn(result.state ? result : await api("/api/status"));
      }
      await doRefreshLive();
    }));
  });

  $("event-type")?.addEventListener("change", () => {
    if ($("sfx-label")) $("sfx-label").hidden = $("event-type").value !== "sfx";
  });

  $("fill-greeting")?.addEventListener("click", () => {
    if ($("event-text")) {
      $("event-text").value = "大肥鱼，今天在忙什么？";
      $("event-text").focus();
    }
  });

  $("event-form")?.addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const payload = {
        event_id: $("event-id")?.value.trim() || crypto.randomUUID(),
        platform: "mock",
        room_id: "debug",
        platform_user_id: $("event-user")?.value.trim() || null,
        display_name: $("event-name")?.value.trim() || "匿名观众",
        type: $("event-type")?.value || "chat",
        timestamp: new Date().toISOString(),
        text: $("event-text")?.value || "",
        metadata: {}
      };
      if ($("event-asset")?.value.trim()) {
        payload.metadata.asset_id = $("event-asset").value.trim();
      }
      const result = await api("/api/live/mock-event", { method: "POST", body: payload });
      if ($("event-result")) {
        $("event-result").textContent = `${label(result.status || "received")} · ${payload.event_id}${result.reason ? ` · ${result.reason}` : ""}`;
      }
      await doRefreshLive();
    });
  });

  return {
    refreshLive: doRefreshLive,
    renderControls: () => renderControls(model),
    switchResourceMode: (target) => switchResourceMode(target, model, {
      api,
      notice,
      renderStatusFn,
      renderVoicePreviewFn,
      loadSettingsFn,
    }),
  };
}
