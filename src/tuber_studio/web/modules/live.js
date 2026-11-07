"use strict";

import { $ } from "./core.js";
import { inferenceDisplay } from "./inference-status.js?v=20261107.08";
import { inferenceModeChanging, liveActionDisabled } from "./live-controls.js?v=20261107.12";

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

export function renderLiveFeatures(model) {
  const enabled = liveEnabled(model);
  const liveNotice = $("live-feature-notice");
  if (liveNotice) liveNotice.hidden = enabled;
  const startBtn = document.querySelector('[data-action="start"]');
  if (startBtn) startBtn.title = enabled ? "" : "直播功能未启用";
}

export function renderModeControls(inference = {}, model) {
  const currentMode = inference.state && inference.state !== "error" ? resourceMode(inference) : null;
  const hasBroker = model.settings?.inference_broker?.enabled === true;
  const transitioning = resourcesTransitioning(model);
  document.querySelectorAll("[data-inference-mode]").forEach(button => {
    const target = button.dataset.inferenceMode;
    button.setAttribute("aria-pressed", String(target === currentMode));
    button.disabled = !hasBroker || transitioning || target === currentMode || (target === "live" && !liveEnabled(model));
  });
  const pending = model.modeSwitch?.target || inference.operation?.mode;
  const feedback = $("resource-mode-feedback");
  feedback.textContent = transitioning ? `正在切换至${resourceModeName(pending)}…` :
    model.modeResult?.text || (!hasBroker ? "请先连接推理节点" :
      !liveEnabled(model) ? "当前配置未启用直播" : !currentMode ? "等待设备确认模式" : "");
  feedback.classList.toggle("error-text", !transitioning && model.modeResult?.error === true);
}

export function renderControls(model) {
  const status = model.status || { state: "stopped" };
  const enabled = liveEnabled(model);
  document.querySelectorAll("[data-action]").forEach(button => {
    if (button.getAttribute("aria-busy") === "true") return;
    const action = button.dataset.action;
    button.disabled = liveActionDisabled(action, status, { enabled, transitioning: resourcesTransitioning(model) });
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

export function renderServiceStatusCapsule(status, settings) {
  const capsule = $("service-status-capsule");
  const state = inferenceDisplay(status, settings);
  const online = state.connected;
  capsule.className = `service-status-capsule ${online === false ? "offline" : online ? "online" : ""}`;
  capsule.querySelector(".status-text").textContent = state.text;
}

export async function refreshLive({ api, list, node, cell, time, label, badge, empty }) {
  const results = await Promise.all([api("/api/jobs?limit=50"), api("/api/events?limit=50")]);
  const jobs = list(results[0]);
  renderJobs($("live-jobs"), jobs, { node, cell, time, badge, empty });
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

function renderMusicControls(view) {
  const playing = view.playback, song = playing?.song?.song;
  const button = $("live-song-toggle");
  button.dataset.mediaAction = playing?.paused ? "media-resume" : "media-pause";
  button.disabled = !song || !playing.started;
  button.textContent = playing?.paused ? "继续歌曲" : "暂停歌曲";
  $("live-song-state").textContent = song
    ? `${song.title} · ${playing.paused ? "已暂停" : playing.started ? "播放中" : "准备中"} · ${Math.floor(playing.position || 0)} 秒`
    : "当前无歌曲播放";
  $("live-song-queue").textContent = `待播歌曲 ${view.queue?.filter(item => item.type === "song").length ?? 0} 首`;
}

function renderJobs(tbody, jobs, { node, cell, time, badge, empty }) {
  if (!tbody) return;
  if (!jobs.length) return empty(tbody, 5, "暂无任务。注入事件后可查看处理状态。");
  tbody.replaceChildren();
  jobs.forEach(job => {
    const row = node("tr");
    cell(row, time(job.created_at), job.id);
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
  const doRefreshLive = () => Promise.all([
    refreshLive({ api, list: arr => Array.isArray(arr) ? arr : arr?.items || [], node, cell, time, label, badge, empty }),
    api("/api/live/view").then(renderMusicControls),
  ]);
  $("live-song-toggle").onclick = event => busy(event.currentTarget, async () => {
    await api(`/api/live/${$("live-song-toggle").dataset.mediaAction}`, { method: "POST" });
    await doRefreshLive();
  });

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
