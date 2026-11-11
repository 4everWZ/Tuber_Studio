import { $ } from "./core.js";
import { renderPersona } from "./character-studio.js?v=20261107.12";
import { renderControls, renderServiceStatusCapsule } from "./live.js?v=20261111.02";
import { renderVoicePreview } from "./voices.js";
import { describe, label, occupancyText, tokenCount } from "./workspace-display.js";
import { inferenceDisplay } from "./inference-status.js?v=20261111.02";

export function renderWorkspaceStatus(model, status) {
  model.status = status;
  renderServiceStatusCapsule(status, model.settings);
  renderControls(model);
  renderPersona(model);
  $("overview-live-state").textContent = label(status.state);
  $("overview-queue").textContent = status.queue_length ?? "—";
  $("overview-player").textContent = status.overlay_connected ? "已连接" : "未连接";
  $("overview-speech").textContent = {
    gpt_sovits: "本地语音", openai_compatible: "兼容 API", prerecorded: "预录音频",
  }[status.providers?.tts] || "待确认";
  if (status.context) renderContext(status.context);
  renderVoicePreview(model);
  if ($("character-version")) {
    $("character-version").title = `当前运行角色：${status.character?.name || "—"} v${status.character?.version ?? "—"}`;
  }
  // Keep the workspace status bar aligned with the live panel.
  if ($("sb-status-mode")) $("sb-status-mode").textContent = status.mode === "live" ? "直播模式" : "运营模式";
  if ($("sb-status-char")) $("sb-status-char").textContent = `${status.character?.name || "大肥鱼"} v${status.character?.version ?? 1}`;
  if ($("sb-status-engine")) $("sb-status-engine").textContent = `${inferenceDisplay(status, model.settings).text} · 直播${label(status.state)}`;
}

export function refreshWorkspaceClock() {
  const clock = $("sb-status-time");
  if (clock) clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

export function renderContext(context) {
  const usable = context.usable !== false;
  const external = context.source === "external_declared";
  const error = context.error || "";
  describe($("context-details"), [
    ["活动范围", context.scope || "暂无活动历史"],
    ["配置目标", tokenCount(context.configured_tokens)],
    [external ? "API 服务容量声明" : "实际服务容量", tokenCount(context.server_tokens)],
    [external ? "API 上限声明" : "模型原生容量", tokenCount(context.native_tokens)],
    ["完整输入占用", `${tokenCount(context.active_tokens ?? 0)}（${occupancyText(context)}）`],
    ["累计已安装压缩轮数", context.compact_count ?? 0],
    ["最近操作尝试轮数", context.compaction_passes_last ?? 0],
    ["最近摘要预算", (context.summary_budgets_last || []).map(value => `${value} tokens`).join(" / ") || "尚未压缩"],
    ["二轮触发占用", `${context.secondary_threshold_percent ?? 40}%`],
    ["最近压缩提示", context.last_compaction_warning || "无"],
    ["最近压缩错误", context.last_compaction_error || "无"]
  ]);
  if ($("context-profile-note")) {
    $("context-profile-note").textContent = error || (external ?
      "外部 API 的容量来自操作者声明；可用连接、实际 token 用量与容量上限是不同证据，不能标为本机已验证 128K。" :
      "模型原生能力、运行服务实际容量及配置目标分别显示。上方设置保存成功后立即应用，并重新校验服务容量。");
  }
  const generate = $("generate-drafts");
  if (generate) {
    if (generate.getAttribute("aria-busy") !== "true") generate.disabled = !usable;
    generate.title = usable ? "" : error || "等待实际上下文能力验证";
  }
}
