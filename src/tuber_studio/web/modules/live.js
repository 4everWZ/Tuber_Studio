"use strict";

import { $ } from "./core.js";
import { inferenceDisplay } from "./inference-status.js?v=20261111.07";

export function liveEnabled(model) {
  return model.settings?.features?.live_enabled === true;
}

export function renderControls(model) {
  const enabled = liveEnabled(model);
  if ($("compact-live") && $("compact-live").getAttribute("aria-busy") !== "true") {
    $("compact-live").disabled = !enabled;
  }
}

export function renderServiceStatusCapsule(status, settings) {
  const capsule = $("service-status-capsule");
  const state = inferenceDisplay(status, settings);
  const online = state.connected;
  capsule.className = `service-status-capsule ${online === false ? "offline" : online ? "online" : ""}`;
  capsule.querySelector(".status-text").textContent = state.connected === true ? "已连接" :
    state.connected === false ? "连接异常" : state.connection === "离线演示" ? state.connection : "待连接";
}
