import { $ } from "./core.js";
import { renderControls, renderServiceStatusCapsule } from "./live.js?v=20261114.1";
import { renderVoicePreview } from "./voices.js";
import { describe, label, occupancyText, tokenCount } from "./workspace-display.js";
import { inferenceDisplay } from "./inference-status.js?v=20261111.07";

export function renderWorkspaceStatus(model, status) {
  model.status = status;
  renderServiceStatusCapsule(status, model.settings);
  renderControls(model);
  const character = status.character?.name || status.character?.id;
  $("persona-current").textContent = character || "正在读取当前角色";
  $("overview-live-state").textContent = label(status.state);
  $("overview-queue").textContent = status.queue_length ?? "—";
  $("overview-player").textContent = status.overlay_connected ? "已连接" : "未连接";
  const voiceId = status.character?.voice_profile || model.settings?.voice_id;
  $("overview-speech").textContent = model.voices.find(voice => voice.id === voiceId)?.name || voiceId || "待确认";
  if (status.context) renderContext(status.context);
  renderVoicePreview(model);
  // Keep the workspace status bar aligned with the live panel.
  if ($("sb-status-mode")) $("sb-status-mode").textContent = label(status.state);
  if ($("sb-status-char")) $("sb-status-char").textContent = character || "—";
  if ($("sb-status-engine")) $("sb-status-engine").textContent = inferenceDisplay(status, model.settings).connection;
}

export function refreshWorkspaceClock() {
  const clock = $("sb-status-time");
  if (clock) clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

export function renderContext(context) {
  const usable = context.usable !== false;
  const error = context.error || "";
  describe($("context-details"), [
    ["活动范围", context.scope || "暂无活动历史"],
    ["配置目标", tokenCount(context.configured_tokens)],
    ["实际服务容量", tokenCount(context.server_tokens)],
    ["服务报告上限", tokenCount(context.native_tokens)],
    ["完整输入占用", `${tokenCount(context.active_tokens ?? 0)}（${occupancyText(context)}）`],
    ["累计已安装压缩轮数", context.compact_count ?? 0],
    ["最近操作尝试轮数", context.compaction_passes_last ?? 0],
    ["最近摘要预算", (context.summary_budgets_last || []).map(value => `${value} tokens`).join(" / ") || "尚未压缩"],
    ["二轮触发占用", `${context.secondary_threshold_percent ?? 40}%`],
    ["最近压缩提示", context.last_compaction_warning || "无"],
    ["最近压缩错误", context.last_compaction_error || "无"]
  ]);
  if ($("context-profile-note")) {
    $("context-profile-note").textContent = error;
  }
  const generate = $("generate-drafts");
  if (generate) {
    if (generate.getAttribute("aria-busy") !== "true") generate.disabled = !usable;
    generate.title = usable ? "" : error || "等待实际上下文能力验证";
  }
}
