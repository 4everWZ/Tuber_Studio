"use strict";

const $ = id => document.getElementById(id);
const model = {
  authenticated: false, status: null, page: "comments", characters: [], versions: [],
  characterId: null, character: null, characterDirty: false, assets: [], voices: [],
  settings: null, socket: null, polling: false, comments: [], selectedComments: new Set(),
  commentId: null, draftDirty: false, draftId: null, policy: null,
  importPreviewSignature: null, categorySaving: new Set(),
  modeSwitch: null, modeResult: null,
  threadContextPending: null,
  retention: null, retentionPreview: null, retentionDirty: false, retentionPending: null,
  providersDirty: false, providersSaving: false,
  dynamicPolicy: null, dynamicPolicyDirty: false, dynamicSaving: false, dynamicEvaluating: false,
  dynamicPosts: [], dynamicDecisions: [], dynamicMemories: [], dynamicPostId: null, dynamicPostDirty: false,
  dynamicPostPending: false, dynamicMemorySaving: new Set(),
  dynamicListsLoaded: false,
  dynamicMemorySummary: null,
};
const cachePanel = new CachePanel($("cache-panel"), api, notice);
setInterval(() => { if (model.authenticated) cachePanel.refresh(); }, 5000);
const messagesPanel = new MessagesPanel($("messages-panel"), api, notice);
const names = {
  stopped: "已停止", live: "运行中", paused: "已暂停", degraded: "降级运行", ready: "就绪",
  connecting: "连接中", error: "错误", queued: "排队中", generating: "生成台词", synthesizing: "合成语音",
  ready_to_play: "等待播放器", playing: "播放中", completed: "已完成", failed: "失败", cancelled: "已取消",
  expired: "已过期", filtered: "已过滤", duplicate: "重复事件", dropped: "已丢弃", selected: "已选中",
  private_handoff: "已转私信", received: "已收到", skipped: "已跳过", pending: "等待中", running: "处理中", pending_review: "待审核",
  approved: "已审核", rejected: "已拒绝", copied: "已复制", published: "已发布", publishing: "发布中",
  publish_uncertain: "发布结果待核对", publish_failed: "发布失败", question: "问题",
  suggestion: "建议 / 反馈", interaction: "适合互动", spam: "垃圾", general: "一般评论", chat: "弹幕",
  gift: "礼物", song: "点歌", sfx: "预录音效", disconnect: "断线", mock: "离线演示", llama_cpp: "llama.cpp",
  invalidated: "已失效", post: "生成动态", wait: "等待", role_story: "虚构角色故事",
};
function label(value) { return names[value] || value || "—"; }
function display(value) { return value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value); }
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function list(data) { return Array.isArray(data) ? data : data?.items || data?.results || []; }
function time(value) {
  if (!value) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString("zh-CN", {hour12: false});
}
function badge(value) {
  const good = ["completed", "approved", "published", "live", "ready", "copied"];
  const bad = ["failed", "error", "cancelled", "rejected", "degraded", "publish_uncertain", "publish_failed"];
  return node("span", label(value), `badge ${good.includes(value) ? "good" : bad.includes(value) ? "bad" : ""}`);
}
function setBadge(element, value) { const valueNode = badge(value); element.className = valueNode.className; element.textContent = valueNode.textContent; }
function notice(message, error = false) {
  const element = $("notice"); element.textContent = safeMessage(message); element.hidden = !message;
  element.classList.toggle("error", error);
}
function describe(element, entries) {
  element.replaceChildren();
  entries.forEach(([key, value]) => element.append(node("dt", key), node("dd", display(value))));
}
function empty(tbody, colspan, text) {
  tbody.replaceChildren(); const row = node("tr"); const cell = node("td", text, "empty-cell");
  cell.colSpan = colspan; row.append(cell); tbody.append(row);
}
function cell(row, value, small) {
  const element = node("td");
  element.append(value instanceof Node ? value : node("span", display(value)));
  if (small) element.append(node("small", small, "cell-sub"));
  row.append(element); return element;
}
async function api(path, options = {}) {
  const config = {...options, headers: {...options.headers}, credentials: "same-origin"};
  if (options.body !== undefined && typeof options.body !== "string") {
    config.headers["Content-Type"] = "application/json"; config.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, config);
  const text = await response.text(); let result;
  try { result = text ? JSON.parse(text) : {}; } catch { result = {detail: text}; }
  if (!response.ok) {
    if (response.status === 401 && model.authenticated) showLogin();
    const error = new Error(display(result.detail || result.error || `${response.status} ${response.statusText}`));
    error.status = response.status; throw error;
  }
  return result;
}
async function busy(button, action) {
  const wasDisabled = button.disabled; button.disabled = true; button.setAttribute("aria-busy", "true");
  try { return await action(); } catch (error) { notice(error.message, true); return undefined; }
  finally {
    button.disabled = wasDisabled; button.removeAttribute("aria-busy");
    if (model.status) renderControls();
    if (model.status?.context) renderContext(model.status.context);
    if (button.id === "submit-import") button.disabled = !model.importPreviewSignature;
    if (button.id === "preview-submit") renderVoicePreview();
    if (["approve-draft", "reject-draft", "copy-draft", "publish-draft"].includes(button.id)) renderDraft();
    if (["approve-dynamic", "reject-dynamic", "publish-dynamic"].includes(button.id)) renderDynamicPost();
    if (["save-dynamic-policy", "stop-dynamics", "evaluate-dynamics"].includes(button.id)) renderDynamicPolicy();
  }
}
function showLogin() {
  model.authenticated = false; $("workspace").hidden = true; $("login").hidden = false;
  clearProviderSecrets();
  model.socket?.close(); model.socket = null;
}
async function enterWorkspace() {
  const status = await api("/api/status");
  model.authenticated = true; $("login").hidden = true; $("workspace").hidden = false;
  renderStatus(status); connectAdmin();
  const results = await Promise.allSettled([loadCharacters(), loadAssets(), loadVoices(), loadSettings(), loadPolicy()]);
  results.forEach(result => { if (result.status === "rejected") notice(result.reason.message, true); });
  await changePage();
}
function connectAdmin() {
  if (!model.authenticated || model.socket) return;
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/admin`);
  model.socket = socket;
  socket.onopen = () => { $("connection").textContent = "实时连接"; };
  socket.onmessage = event => {
    try { const data = JSON.parse(event.data); if (data.type === "status") renderStatus(data.data); } catch { /* Keep polling available. */ }
  };
  socket.onclose = () => {
    if (model.socket === socket) model.socket = null;
    $("connection").textContent = "轮询更新";
    if (model.authenticated) setTimeout(connectAdmin, 4000);
  };
  socket.onerror = () => { $("connection").textContent = "轮询更新"; };
}
function renderControls() {
  const status = model.status; if (!status) return;
  document.querySelectorAll("[data-action]").forEach(button => {
    if (button.getAttribute("aria-busy") === "true") return;
    const action = button.dataset.action;
    if (!liveEnabled() && !(action === "stop" && status.state !== "stopped")) { button.disabled = true; return; }
    button.disabled = action === "start" ? status.state !== "stopped" :
      action === "pause" ? !["live", "degraded"].includes(status.state) :
      action === "resume" ? status.state !== "paused" :
      action === "stop" ? status.state === "stopped" :
      action === "mute" ? !!status.muted : action === "unmute" ? !status.muted :
      action === "skip" ? !status.current_job : action === "clear" ? !status.queue_length : false;
    if (["start", "resume"].includes(action) && resourcesTransitioning()) button.disabled = true;
  });
  $("event-form").querySelectorAll("input, select, textarea, button").forEach(input => { if (input.getAttribute("aria-busy") !== "true") input.disabled = !liveEnabled() || resourcesTransitioning(); });
  if ($("compact-live").getAttribute("aria-busy") !== "true") $("compact-live").disabled = !liveEnabled();
  renderLiveFeatures();
}
function liveEnabled() { return model.settings?.features?.live_enabled === true; }
function resourcesTransitioning() {
  const inference = model.status?.inference;
  return !!model.modeSwitch || !!inference?.switching_to || ["loading", "switching", "starting"].includes(inference?.state);
}
function localVoiceId() {
  return model.settings?.tts?.voice_id || model.voices.find(voice => (voice.provider || voice.kind) === "gpt_sovits")?.id;
}
function localEngineLabel() {
  const backend = model.settings?.tts?.local_backend;
  return backend === "genie_onnx_int8" ? "Genie ONNX INT8" : backend || "compact（兼容配置，未准入）";
}
function renderLiveFeatures() {
  const enabled = liveEnabled();
  $("live-nav-label").textContent = enabled ? "Mock 直播" : "直播（后续）";
  document.querySelector('[data-inference-mode="live"]').textContent = enabled ? "直播模式" : "直播（后续）";
  $("live-feature-notice").textContent = enabled ? "本地 Mock 直播已开放：启动会加载直播资源；仅处理手动注入事件，不连接 B站或抖音。停止直播后，切回文本模式才会卸载 TTS。" : "直播与本地动态语音后续开放。当前可在评论工作台处理文本，或在设置中配置外部模型与语音 API。";
  $("live-feature-notice").hidden = false;
  $("overlay-heading").textContent = enabled ? "OBS / Mock 播放器配置" : "OBS 播放器配置（直播后续）";
  $("overlay-help").textContent = enabled ? "手动打开播放器后，在 Mock 直播中注入事件即可播报。仅一个 overlay 页面获得播放权，第二个页面进入待机；不会连接真实直播平台。" : "直播与本地动态语音后续开放。已有播放器入口保留；仅一个 overlay 页面获得播放权，第二个页面进入待机。";
  const localOption = $("tts-kind").querySelector('option[value="gpt_sovits"]');
  localOption.textContent = enabled ? "本地 GPT-SoVITS 音色" : "本地 GPT-SoVITS（后续）";
  $("tts-local-engine").textContent = model.settings ? `已保存本地引擎：${localEngineLabel()} · 音色：${localVoiceId() || "未配置"}。${model.settings.tts?.kind !== "gpt_sovits" ? "当前语音来源未使用此引擎。" : !enabled ? "本地动态语音尚未启用。" : model.settings.tts?.local_backend !== "genie_onnx_int8" ? "此后端未准入本地试听。" : "需手动进入直播模式；引擎与音色由服务配置，只读展示。"}` : "本地引擎：正在读取已保存配置。";
  $("tts-settings-help").textContent = enabled ? "外部 API 使用 external_api 音色；本地 Genie 仅使用服务选定音色。试听不会切换资源模式或替换角色音色。" : "配置外部 API 后，在语音页选择 external_api 试听；已有角色音色不会自动替换。本地动态语音和直播后续开放。";
}
function renderStatus(status) {
  if (!status) return; model.status = status;
  setBadge($("live-state"), status.state);
  if (status.muted) $("live-state").textContent += " · 静音";
  const character = status.character;
  $("global-character").textContent = character ? `${character.name} · v${character.version}` : "未选择角色";
  $("overlay-state").textContent = status.overlay_connected ? "播放器已连接" : "播放器未连接";
  $("runtime-error").textContent = status.error ? display(status.error) : "";
  $("runtime-error").hidden = !status.error;
  $("metric-queue").textContent = display(status.queue_length ?? 0);
  const timings = status.timings || {};
  const ms = value => value == null ? "—" : `${Math.round(value)} ms`;
  $("metric-time").textContent = `${ms(timings.llm_ms)} / ${ms(timings.tts_ms)}`;
  const resources = status.resources || {};
  $("metric-ram").textContent = resources.ram_used_mb == null ? "—" : `${(resources.ram_used_mb / 1024).toFixed(1)} / ${(resources.ram_total_mb / 1024).toFixed(1)} GB`;
  $("metric-cpu").textContent = resources.cpu_percent == null ? "—" : `${resources.cpu_percent}% / ${Math.round(resources.process_mb || 0)} MB`;
  $("current-character-name").textContent = character ? `${character.name} · v${character.version}` : "未选择角色";
  const job = status.current_job;
  setBadge($("current-status"), job?.status || "ready"); setBadge($("live-current-status"), job?.status || "ready");
  const text = job?.text || job?.spoken_text || (job ? "正在处理观众事件…" : "当前没有待播报台词。");
  $("current-text").textContent = text; $("live-current-text").textContent = text;
  $("current-detail").textContent = job ? `任务 ${job.id} · ${job.emotion || "—"}` : !liveEnabled() ? "首版以评论与文本为主，直播与本地动态语音后续开放。" : status.state === "stopped" ? "启动 Mock 直播，在调试台注入事件。" : "等待下一条可回应事件。";
  describe($("live-current-detail"), job ? [["任务 ID", job.id], ["源事件", job.event_id], ["角色版本", `${job.character_id} · v${job.character_version}`], ["LLM / TTS", `${ms(job.llm_ms)} / ${ms(job.tts_ms)}`], ["错误", job.error || "无"]] : [["队列长度", status.queue_length ?? 0], ["会话", status.session_id || "未启动"]]);
  const last = status.last_played;
  $("last-played").textContent = last ? typeof last === "string" ? last : `${last.text || last.spoken_text || ""}${last.completed_at ? ` · ${time(last.completed_at)}` : ""}` : "暂无已确认播放记录。";
  const providers = status.providers || {};
  describe($("provider-summary"), [["平台", "Mock · 真实直播接入后置"], ["LLM", display(providers.llm)], ["TTS", display(providers.tts)], ...(status.inference ? [["推理资源", inferenceLabel(status.inference)]] : []), ["播放器", status.overlay_connected ? "已连接" : "未连接"], ["会话", status.session_id || "未启动"]]);
  renderCharacterImage(); renderControls();
  renderContext(status.context);
  renderInference(status.inference); renderVoicePreview();
  if (model.dynamicPolicy) renderDynamicPolicy();
}
function resourceMode(inference) {
  if (inference?.mode === "exclusive") return "text";
  return ["text", "live"].includes(inference?.mode) ? inference.mode : null;
}
function resourceName(key) {
  if (key === "tts" && model.settings?.tts?.local_backend === "genie_onnx_int8") return "Genie TTS";
  return ({llm2b: "MiniCPM 2B", "2b": "MiniCPM 2B", llm1b: "MiniCPM 1B", "1b": "MiniCPM 1B", llm1bq8: "MiniCPM 1B Q8", "1bq8": "MiniCPM 1B Q8", llmqwen35: "Qwen3.5-2B", qwen35: "Qwen3.5-2B", tts: "TTS"})[key] || key;
}
function inferenceResidents(inference) {
  if (inference.mode === "standalone") {
    if (model.settings?.llm?.kind === "openai_compatible") return "API 模型驻留由服务方管理";
    return model.settings?.llm?.kind === "demo" ? "演示模式不加载模型" : "独立端点，驻留状态未报告";
  }
  const providers = Object.entries(inference.providers || {});
  if (providers.length) {
    const residents = providers.filter(([, provider]) => provider?.running === true).map(([key]) => resourceName(key));
    const sleeping = providers.filter(([key, provider]) => key === "tts" && provider?.is_sleeping === true && provider?.running !== true).map(([key]) => resourceName(key));
    if (residents.length) return `驻留：${residents.join("、")}${sleeping.length ? ` · ${sleeping.join("、")}休眠` : ""}`;
    return providers.some(([, provider]) => provider?.running == null) ? "驻留状态待确认" : "无模型驻留";
  }
  const active = inference.active ?? inference.active_provider;
  return active ? `驻留：${resourceName(active)}` : "模型已休眠";
}
function inferenceLabel(inference) {
  const mode = resourceMode(inference);
  const prefix = mode === "live" ? "直播 · " : mode === "text" ? "文本 · " : inference.mode === "standalone" ? "独立Provider · " : "";
  const residents = inferenceResidents(inference);
  if (inference.switching_to) return `${prefix}正在加载 ${resourceName(inference.switching_to)}… · ${residents}`;
  if (inference.error) return `${prefix}资源切换失败 · ${residents}`;
  if (["loading", "switching", "starting"].includes(inference.state)) return `${prefix}模型加载中… · ${residents}`;
  return `${prefix}${inference.busy ? "资源忙 · " : ""}${residents}`;
}
function renderModeControls(inference) {
  const actual = resourceMode(inference), pending = model.modeSwitch;
  const transitioning = !!inference?.switching_to || ["loading", "switching", "starting"].includes(inference?.state);
  const selected = pending ? pending.previousMode : actual;
  document.querySelectorAll("[data-inference-mode]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.inferenceMode === selected));
    button.disabled = !actual || !!pending || transitioning || (button.dataset.inferenceMode === "live" && !liveEnabled()) || (button.dataset.inferenceMode === actual && !inference?.error);
    button.setAttribute("aria-busy", String(!!pending || transitioning));
  });
  const feedback = $("resource-mode-feedback");
  const result = model.modeResult?.mode === actual ? model.modeResult : null;
  $("resource-mode-help").textContent = liveEnabled() ? "切回文本会停止当前直播并卸载TTS；直播模式预加载并共驻LLM与TTS。" : "首版以评论与文本为主；直播和本地动态语音后续开放。默认 MiniCPM 模型只处理文本。";
  const text = pending ? `正在切换到${pending.target === "live" ? "直播" : "文本"}，等待模型就绪…` : inference?.error ? `资源错误：${inference.error}` :
    inference?.mode === "standalone" ? "当前独立Provider不支持资源模式切换。" : !actual ? "等待资源模式状态。" :
    transitioning ? "资源切换中，页面导航不会改变工作模式。" : result?.text || `当前模式：${actual === "live" ? "直播" : "文本"}`;
  if (feedback.textContent !== text) feedback.textContent = text;
  feedback.classList.toggle("error-text", !!(result?.error || inference?.error) && !pending);
}
function renderInference(inference) {
  $("inference-state").hidden = !inference;
  renderModeControls(inference);
  if (!inference) { $("inference-error").hidden = true; return; }
  $("inference-state").textContent = inferenceLabel(inference);
  $("inference-state").classList.toggle("bad", !!inference.error);
  $("inference-state").title = inference.mode === "standalone" ? "独立 API 的模型驻留由服务方管理；容量声明与实际 token 用量分别显示。" : inference.switching_to ? "资源切换期间任务等待模型加载。上下文容量验证与模型驻留状态分别记录。" : "驻留或休眠表示模型资源状态，不改变已验证的上下文容量。";
  $("inference-error").hidden = !inference.error;
  $("inference-error").textContent = inference.error ? `推理资源切换失败：${inference.error}` : "";
}
async function switchResourceMode(target) {
  if (target === "live" && !liveEnabled()) { notice("直播与本地动态语音后续开放。"); return; }
  const previousMode = resourceMode(model.status?.inference);
  if (!previousMode || model.modeSwitch) return;
  model.modeSwitch = {target, previousMode}; model.modeResult = null;
  renderModeControls(model.status.inference); renderControls(); renderVoicePreview();
  try {
    const result = await api("/api/inference/mode", {method: "POST", body: {mode: target}});
    if (resourceMode(result) !== target) throw new Error("接口返回的资源模式与请求不符，请检查实际资源状态。");
    model.modeSwitch = null;
    model.modeResult = {mode: target, error: false, text: `已切换到${target === "live" ? "直播" : "文本"}模式。`};
    renderStatus({...model.status, inference: result});
    try { renderStatus(await api("/api/status")); }
    catch (error) { notice(`模式已切换，状态刷新失败：${error.message}`, true); }
  } catch (error) {
    model.modeSwitch = null; model.modeResult = {mode: previousMode, error: true, text: `切换失败：${error.message}`};
    renderModeControls(model.status?.inference);
    try { renderStatus(await api("/api/status")); } catch { /* Keep the last confirmed state visible. */ }
  } finally {
    renderControls(); renderVoicePreview();
  }
}
function tokenCount(value) {
  if (value == null) return "未报告";
  return typeof value === "number" && Number.isFinite(value) ? `${value.toLocaleString("zh-CN")} tokens` : display(value);
}
function occupancyText(context) {
  const value = context.occupancy_percent;
  return typeof value === "number" && Number.isFinite(value) ? `约 ${value.toFixed(2)}%` : "未报告";
}
function renderContext(context) {
  if (!context) return;
  const configured = Number(context.configured_tokens || 0), server = Number(context.server_tokens || 0), native = Number(context.native_tokens || 0);
  const mismatch = (configured > 0 && server > 0 && server < configured) || (configured > 0 && native > 0 && native < configured);
  const unavailable = !!context.profile_error || mismatch;
  const external = model.settings?.llm?.kind === "openai_compatible";
  const verified = !!context.verified && !unavailable && !external;
  const usable = !unavailable && (verified || (external && configured >= 4096));
  const rounds = `累计 ${context.compact_count ?? 0} 轮 · 最近 ${context.compaction_passes_last ?? 0} 轮`;
  const concise = unavailable ? "上下文配置不可用" : external ? `API 声明 ${tokenCount(context.configured_tokens)} · ${occupancyText(context)} · ${rounds}` : verified ? `${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)} · ${occupancyText(context)} · ${rounds}` : "上下文能力待验证";
  $("context-state").textContent = concise; $("context-state").classList.toggle("bad", unavailable);
  const error = context.profile_error || (mismatch ? `上下文配置未满足：目标 ${tokenCount(context.configured_tokens)}，实际服务 ${tokenCount(context.server_tokens)}，模型原生 ${tokenCount(context.native_tokens)}。` : "");
  const errors = [error, context.last_compaction_error ? `最近压缩失败：${context.last_compaction_error}` : ""].filter(Boolean);
  $("context-error").hidden = !errors.length; $("context-error").textContent = errors.join(" ");
  const warning = context.last_compaction_warning ? `压缩提示：${context.last_compaction_warning}` : "";
  $("context-warning").hidden = !warning;
  if ($("context-warning").textContent !== warning) $("context-warning").textContent = warning;
  $("comment-context-state").textContent = error || (external ? `API 窗口由操作者声明：${tokenCount(context.configured_tokens)}；当前完整输入 ${tokenCount(context.active_tokens ?? 0)}（${occupancyText(context)}），压缩${rounds}。容量未经本机验证。` : verified ? `真实完整输入占用 ${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)}（${occupancyText(context)}）；压缩${rounds}。` : `上下文目标 ${tokenCount(context.configured_tokens)}，实际服务 ${tokenCount(context.server_tokens)}，尚未验证。`);
  $("context-verified").textContent = unavailable ? "配置不可用" : external ? "API 容量由操作者声明" : verified ? "实际服务已验证" : "待验证";
  $("context-verified").className = `badge ${verified ? "good" : unavailable ? "bad" : ""}`;
  describe($("context-details"), [["活动范围", context.scope || "暂无活动历史"], ["配置目标", tokenCount(context.configured_tokens)], [external ? "API 服务容量声明" : "实际服务容量", tokenCount(context.server_tokens)], [external ? "API 上限声明" : "模型原生容量", tokenCount(context.native_tokens)], ["完整输入占用", `${tokenCount(context.active_tokens ?? 0)}（${occupancyText(context)}）`], ["累计已安装压缩轮数", context.compact_count ?? 0], ["最近操作尝试轮数", context.compaction_passes_last ?? 0], ["最近摘要预算", (context.summary_budgets_last || []).map(value => `${value} tokens`).join(" / ") || "尚未压缩"], ["二轮触发占用", `${context.secondary_threshold_percent ?? 40}%`], ["最近压缩提示", context.last_compaction_warning || "无"], ["最近压缩错误", context.last_compaction_error || "无"]]);
  $("context-profile-note").textContent = error || (external ? "外部 API 的容量来自操作者声明；可用连接、实际 token 用量与容量上限是不同证据，不能标为本机已验证 128K。" : "模型原生能力、运行服务实际容量及配置目标分别显示。上方设置保存成功后立即应用，并重新校验服务容量。");
  const generate = $("generate-drafts");
  if (generate.getAttribute("aria-busy") !== "true") generate.disabled = !usable;
  generate.title = usable ? "" : error || "等待实际上下文能力验证";
}
function renderCharacterImage() {
  const image = $("character-image"); const id = model.status?.character?.id;
  const asset = model.assets.find(item => item.character_id === id && ["idle", "default"].includes(item.tag || item.event_tag));
  const url = asset?.url || asset?.path;
  if (!url) { image.hidden = true; return; }
  if (image.dataset.url !== url) { image.dataset.url = url; image.src = url; image.hidden = false; }
}
async function refreshLive() {
  const results = await Promise.all([api("/api/jobs?limit=50"), api("/api/events?limit=50")]);
  const jobs = list(results[0]); renderJobs($("overview-jobs"), jobs.slice(0, 8)); renderJobs($("live-jobs"), jobs, true);
  const events = list(results[1]); const tbody = $("events");
  if (!events.length) return empty(tbody, 5, "暂无事件。在上方调试台注入一条弹幕。");
  tbody.replaceChildren();
  events.forEach(event => {
    const payload = event.payload || event;
    const row = node("tr"); cell(row, time(payload.timestamp || event.created_at), event.event_id || event.platform_event_id || event.id);
    cell(row, payload.display_name || "匿名观众", `${payload.platform || "—"} · ${payload.platform_user_id || "未知 ID"}`);
    cell(row, label(payload.type)); cell(row, payload.text || "—");
    const duplicateCount = event.duplicate_count || event.duplicates || 0;
    cell(row, badge(event.status), event.reason || event.error || (duplicateCount ? `重复 ${duplicateCount} 次` : "")); tbody.append(row);
  });
}
function renderJobs(tbody, jobs, withId = false) {
  if (!jobs.length) return empty(tbody, 5, "暂无任务。注入事件后可查看处理状态。");
  tbody.replaceChildren();
  jobs.forEach(job => {
    const row = node("tr"); cell(row, time(job.created_at), withId ? job.id : "");
    cell(row, `${job.character_id || "—"} · v${job.character_version ?? "—"}`); cell(row, job.text || job.spoken_text || "—");
    cell(row, badge(job.status));
    cell(row, job.llm_ms != null || job.tts_ms != null ? `${Math.round(job.llm_ms || 0)} / ${Math.round(job.tts_ms || 0)} ms` : "—", job.error || job.reason || ""); tbody.append(row);
  });
}
async function loadCharacters() {
  model.characters = list(await api("/api/characters")); renderCharacterList();
  if (!model.characterId && model.characters.length) await selectCharacter(model.status?.character?.id || model.characters[0].id);
}
function renderCharacterList() {
  const container = $("character-list"); container.replaceChildren();
  if (!model.characters.length) { container.append(node("p", "暂无角色配置。", "muted")); return; }
  model.characters.forEach(character => {
    const button = node("button", character.name || character.id, character.id === model.characterId ? "selected" : "");
    button.append(node("small", `${character.id} · v${character.version ?? character.active_version ?? "—"}${character.id === model.status?.character?.id ? " · 当前角色" : ""}`));
    button.addEventListener("click", () => busy(button, async () => {
      if (model.characterDirty && !confirm("角色配置有未保存修改。放弃修改并切换所选角色？")) return;
      await selectCharacter(character.id);
    })); container.append(button);
  });
}
async function selectCharacter(id, version) {
  const versions = list(await api(`/api/characters/${encodeURIComponent(id)}/versions`));
  model.characterId = id; model.versions = versions;
  const select = $("character-version"); select.replaceChildren();
  versions.slice().sort((a, b) => b.version - a.version).forEach(character => {
    const option = node("option", `v${character.version}${character.name ? ` · ${character.name}` : ""}`);
    option.value = character.version; select.append(option);
  });
  const target = versions.find(character => character.version === Number(version)) || versions.slice().sort((a, b) => b.version - a.version)[0] || model.characters.find(character => character.id === id);
  if (!target) throw new Error("角色配置不可用");
  select.disabled = false; select.value = String(target.version);
  setCharacterEditor(target); renderCharacterList();
}
function setCharacterEditor(character) {
  model.character = character; model.characterDirty = false;
  $("character-editor").value = JSON.stringify(character, null, 2);
  ["character-editor", "save-character", "reset-character", "activate-character"].forEach(id => { $(id).disabled = false; });
  $("editor-title").textContent = `${character.name || character.id} · v${character.version}`;
  $("character-preview").textContent = JSON.stringify({core: character.core, style: character.style, rules: character.rules, lore: character.lore}, null, 2);
  $("character-dirty").textContent = "";
}
async function loadAssets() {
  model.assets = list(await api("/api/assets")); const grid = $("asset-grid"); grid.replaceChildren();
  if (!model.assets.length) { grid.append(node("p", "暂无已配置素材。", "muted")); return; }
  model.assets.forEach(asset => {
    const card = node("article", null, "asset-card"); const preview = node("div", null, "asset-preview");
    const url = asset.url || asset.path || "";
    const audioAsset = /\.(wav|mp3|ogg|flac)(?:\?|$)/i.test(url) || asset.kind === "audio" || asset.type === "audio";
    if (audioAsset) { const audio = document.createElement("audio"); audio.controls = true; audio.preload = "none"; audio.src = url; preview.append(audio); }
    else { const image = document.createElement("img"); image.src = url; image.alt = asset.tag || asset.event_tag || asset.id; image.loading = "lazy"; image.onerror = () => { image.hidden = true; preview.append(node("span", "素材不可用", "muted")); }; preview.append(image); }
    const meta = node("div", null, "asset-meta"); meta.append(node("h3", asset.id), badge(asset.tag || asset.event_tag || asset.kind || "素材"), node("p", `${asset.character_id || "通用"} · ${asset.license_status || "许可待确认"}`, "muted"));
    const source = asset.source || asset.source_url; if (source) { const p = node("p"); if (/^https?:\/\//.test(source)) { const link = node("a", "查看来源"); link.href = source; link.target = "_blank"; link.rel = "noopener noreferrer"; p.append(link); } else p.textContent = display(source); meta.append(p); }
    card.append(preview, meta); grid.append(card);
  }); renderCharacterImage();
}
async function loadVoices() {
  const previous = $("preview-voice").value;
  model.voices = list(await api("/api/voices")); const tbody = $("voices"); const select = $("preview-voice");
  tbody.replaceChildren(); select.replaceChildren();
  if (!model.voices.length) { empty(tbody, 4, "暂无可用音色。配置声音 Provider 后刷新。"); renderVoicePreview(); return; }
  model.voices.forEach(voice => {
    const row = node("tr"); cell(row, voice.name || voice.id, voice.id); cell(row, voice.provider || voice.kind || "—", voice.weights_version || voice.version || "");
    cell(row, voice.reference_asset || voice.reference_audio || voice.params?.ref_audio_path || "—"); cell(row, voice.license_status || "待确认", voice.source || voice.source_url || ""); tbody.append(row);
    const option = node("option", voice.name || voice.id); option.value = voice.id; select.append(option);
  });
  if (model.voices.some(voice => voice.id === previous)) select.value = previous;
  renderVoicePreview();
}
function safeMessage(message) {
  let text = String(message ?? "");
  ["llm", "tts", "vision"].forEach(kind => {
    const secret = $(`${kind}-key`)?.value;
    if (secret) text = text.split(secret).join("[密钥已隐藏]");
  });
  return text;
}
function publicSettings(value, key = "") {
  if (key === "credentials" && value != null) return "[已隐藏]";
  if (typeof value === "string") {
    if (/api[_-]?key|secret|password|authorization|(?:^|_)token$/i.test(key)) return "[已隐藏]";
    return safeMessage(value).replace(/([#?&](?:token|key)=)[^&#\s]+/gi, "$1[已隐藏]");
  }
  if (Array.isArray(value)) return value.map(item => publicSettings(item, key));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, publicSettings(item, name)]));
  return value;
}
function clearProviderSecrets() {
  ["llm", "tts", "vision"].forEach(kind => {
    $(`${kind}-key`).value = ""; $(`${kind}-clear-key`).checked = false;
  });
}
function providerFormFromSettings(settings) {
  const llm = settings.llm || {}, tts = settings.tts || {}, vision = settings.vision || {};
  const fields = {
    "llm-kind": llm.kind || "llama_cpp", "llm-base-url": llm.base_url || "", "llm-model": llm.model || "",
    "llm-context-tokens": llm.context_tokens ?? 131072, "llm-native-tokens": llm.native_context_tokens ?? model.status?.context?.native_tokens ?? 131072,
    "tts-kind": tts.kind || "prerecorded", "tts-base-url": tts.base_url || "", "tts-model": tts.model || "", "tts-voice": tts.voice || "",
    "vision-kind": vision.kind || "disabled", "vision-location": vision.location || "local", "vision-base-url": vision.base_url || "", "vision-model": vision.model || "",
  };
  Object.entries(fields).forEach(([id, value]) => { $(id).value = String(value); });
  clearProviderSecrets(); model.providersDirty = false;
}
function renderProviderForm() {
  $("provider-fields").disabled = !model.settings || model.providersSaving;
  $("save-providers").disabled = !model.settings || model.providersSaving;
  $("save-providers").setAttribute("aria-busy", String(model.providersSaving));
  $("reset-providers").disabled = !model.settings || model.providersSaving || !model.providersDirty;
  $("check-providers-settings").disabled = model.providersSaving;
  $("providers-dirty").textContent = model.providersSaving ? "正在保存并应用…" : model.providersDirty ? "有未保存修改" : model.settings ? "已保存配置" : "正在读取已保存配置…";
  const external = $("llm-kind").value === "openai_compatible", demo = $("llm-kind").value === "demo";
  ["llm-base-url", "llm-model", "llm-key"].forEach(id => { $(id).disabled = demo; });
  $("llm-base-url").required = !demo; $("llm-model").required = !demo;
  $("llm-probe-help").hidden = !external;
  $("llm-capacity-help").textContent = external ? "默认窗口 131072（128K），可填写不小于 4096 的整数。服务上限由操作者依据 API 提供方能力声明；连接成功或用量计数不能证明服务实际支持该上限，也不等同于本机验证。" : "默认窗口 131072（128K），可填写不小于 4096 的整数。当前 MiniCPM5-2B 原生上限 131072；增大必须有实际服务支持，不能通过 RoPE 参数伪扩展。";
  const ttsApi = $("tts-kind").value === "openai_compatible";
  $("tts-api-fields").hidden = !ttsApi;
  ["tts-base-url", "tts-model", "tts-voice"].forEach(id => { $(id).required = ttsApi; $(id).disabled = !ttsApi; });
  $("tts-key").disabled = !ttsApi;
  const visionApi = $("vision-kind").value === "openai_compatible";
  $("vision-api-fields").hidden = !visionApi; $("vision-location").disabled = !visionApi;
  ["vision-base-url", "vision-model"].forEach(id => { $(id).required = visionApi; $(id).disabled = !visionApi; });
  $("vision-key").disabled = !visionApi;
  $("vision-settings-state").textContent = visionApi ? "已选择独立多模态接入；保存后仅应用接口配置，不会开启看图工作流。当前文本端点不代表具备视觉能力。" : "多模态接口关闭，不会向视觉服务发送请求。";
  ["llm", "tts", "vision"].forEach(kind => {
    $(`${kind}-key-state`).textContent = model.settings?.provider_credentials?.[kind] ? "已有保存密钥（不回显）" : "未保存密钥";
  });
  renderLiveFeatures();
}
function applySettings(settings, fillForm = false) {
  model.settings = {...model.settings, ...settings}; settings = model.settings;
  $("settings-json").textContent = JSON.stringify(publicSettings(settings), null, 2);
  if (fillForm || !model.providersDirty) providerFormFromSettings(settings);
  renderProviderForm(); renderVoicePreview();
  if (model.status) renderStatus(model.status);
  let url = model.settings.overlay_url || "/overlay";
  try { url = new URL(url, location.origin).href; } catch { url = `${location.origin}/overlay`; }
  $("overlay-url").value = url; $("open-overlay").href = url;
}
async function loadSettings() { applySettings(await api("/api/settings")); }
function readProviderPayload() {
  const integer = (id, title) => {
    const value = Number($(id).value);
    if (!$(id).value.trim() || !Number.isSafeInteger(value) || value < 4096) throw new Error(`${title}须为不小于 4096 的整数 tokens。`);
    return value;
  };
  const context = integer("llm-context-tokens", "上下文窗口"), native = integer("llm-native-tokens", "模型原生 / 服务声明上限");
  if (context > native) throw new Error("上下文窗口不能超过模型原生 / 服务声明上限。请按实际服务能力填写。");
  const payload = {
    llm: {kind: $("llm-kind").value, base_url: $("llm-base-url").value.trim(), model: $("llm-model").value.trim(), context_tokens: context, native_context_tokens: native},
    tts: {kind: $("tts-kind").value, base_url: $("tts-base-url").value.trim(), model: $("tts-model").value.trim(), voice: $("tts-voice").value.trim()},
    vision: {kind: $("vision-kind").value, location: $("vision-location").value, base_url: $("vision-base-url").value.trim(), model: $("vision-model").value.trim()},
    credentials: {}, clear_credentials: [],
  };
  ["llm", "tts", "vision"].forEach(kind => {
    const key = $(`${kind}-key`), clear = $(`${kind}-clear-key`).checked;
    if (!key.disabled && key.value && clear) throw new Error(`${kind.toUpperCase()} 请分别选择输入新密钥或清除已有密钥，不能同时操作。`);
    if (clear) payload.clear_credentials.push(kind);
    else if (!key.disabled && key.value) payload.credentials[kind] = key.value;
  });
  return payload;
}
function voiceAvailability(voice) {
  const provider = voice?.provider || voice?.kind;
  if (provider === "gpt_sovits") {
    const tts = model.settings?.tts;
    if (!liveEnabled()) return "本地动态语音后续开放";
    if (tts?.kind !== "gpt_sovits") return "当前语音来源未使用本地引擎";
    if (tts.local_backend !== "genie_onnx_int8") return "当前本地后端未准入，请使用服务配置的 Genie ONNX INT8";
    if (voice.id !== localVoiceId()) return "此音色未由当前 Genie 服务选用，不能串用";
    if (resourcesTransitioning()) return "直播资源正在加载，请等待模型就绪";
    if (model.status?.inference?.error) return `本地语音资源错误：${model.status.inference.error}`;
    if (resourceMode(model.status?.inference) !== "live") return "本地试听需先手动切换顶部「直播模式」；不会自动切换";
    const provider = model.status.inference.providers?.tts;
    if (provider?.error) return `本地语音资源错误：${provider.error}`;
    if (["loading", "switching", "starting"].includes(provider?.state)) return "本地 TTS 正在加载，请等待模型就绪";
    if (model.status.inference.providers?.tts?.running !== true) return "本地 TTS 尚未驻留，请检查直播资源状态";
  }
  if (provider === "openai_compatible" && (model.settings?.tts?.kind !== "openai_compatible" || !model.settings?.tts?.model || !model.settings?.tts?.voice)) return "请先配置外部语音 API 的模型和 Voice ID";
  if (!["gpt_sovits", "openai_compatible", "prerecorded"].includes(provider)) return "此语音 Provider 尚未准入";
  return "";
}
function renderVoicePreview() {
  const select = $("preview-voice");
  [...select.options].forEach(option => {
    const voice = model.voices.find(item => item.id === option.value), reason = voiceAvailability(voice);
    option.disabled = !!reason; option.textContent = `${voice?.name || option.value}${reason ? "（暂不可用）" : ""}`;
  });
  const selected = model.voices.find(voice => voice.id === select.value);
  const reason = selected ? voiceAvailability(selected) : "暂无可用音色";
  const button = $("preview-submit");
  if (button.getAttribute("aria-busy") !== "true") button.disabled = !!reason;
  $("voice-preview-help").textContent = reason || ((selected?.provider || selected?.kind) === "gpt_sovits" ? `本地引擎：${localEngineLabel()}，仅使用服务选定音色。点击生成后再播放；不会启动直播或切换资源模式。` : "预录和已配置的外部语音 API 可独立试听；不会启动直播、切换资源模式或替换角色音色。");
}
async function checkProviderConnections(output) {
  $(output).hidden = false; $(output).textContent = "检查已保存配置的连接中…";
  try { $(output).textContent = JSON.stringify(publicSettings(await api("/api/providers/check", {method: "POST"})), null, 2); }
  catch (error) { $(output).textContent = safeMessage(error.message); throw error; }
}
function dateTime(value) {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? display(value) : date.toISOString();
}
function renderRetentionControls() {
  const loaded = !!model.retention, pending = !!model.retentionPending;
  $("retention-never").disabled = !loaded || pending;
  $("retention-days").disabled = !loaded || pending || $("retention-never").checked;
  $("retention-days").required = !$("retention-never").checked;
  $("retention-before").disabled = !loaded || pending;
  $("retention-save").disabled = !loaded || pending || !model.retentionDirty;
  $("retention-preview").disabled = !loaded || pending || model.retentionDirty;
  $("retention-confirm").disabled = pending || !model.retentionPreview;
  $("retention-cleanup").disabled = pending || !model.retentionPreview || !$("retention-confirm").checked;
  for (const kind of ["save", "preview", "cleanup"]) $("retention-" + kind).setAttribute("aria-busy", String(model.retentionPending === kind));
}
function invalidateRetentionPreview(message = "请先预览待清理范围，再明确确认。", error = false) {
  model.retentionPreview = null; $("retention-confirm").checked = false;
  $("retention-preview-panel").hidden = true; $("retention-cleanup-state").textContent = message;
  $("retention-cleanup-state").classList.toggle("error-text", error);
  renderRetentionControls();
}
async function loadRetention() {
  try {
    model.retention = await api("/api/retention");
    if (!model.retentionDirty) {
      $("retention-never").checked = model.retention.days == null;
      $("retention-days").value = model.retention.days ?? 30;
    }
    const lastCompleteDay = new Date(); lastCompleteDay.setUTCDate(lastCompleteDay.getUTCDate() - 1);
    $("retention-before").max = lastCompleteDay.toISOString().slice(0, 10);
    const policy = model.retention;
    describe($("retention-status"), [["已保存策略", policy.days == null ? "永不自动清理" : `保留 ${policy.days} 天`], ["自动清理", policy.enabled ? "已启用" : "已关闭"], ["最近执行（UTC）", dateTime(policy.last_run)], ["下次执行（UTC）", dateTime(policy.next_run)]]);
    $("retention-last-result").hidden = policy.last_result == null;
    $("retention-last-result").textContent = policy.last_result == null ? "" : JSON.stringify(policy.last_result, null, 2);
    $("retention-policy-state").textContent = model.retentionDirty ? "有未保存修改，请保存后预览。" : "显示服务已保存策略。";
    $("retention-policy-state").classList.remove("error-text");
  } catch (error) {
    $("retention-policy-state").textContent = `留存策略读取失败：${error.message}`;
    $("retention-policy-state").classList.add("error-text");
    throw error;
  } finally { renderRetentionControls(); }
}
function retentionRequest() {
  if (model.retentionDirty) throw new Error("请先保存留存策略，再预览清理范围。");
  const selectedDate = $("retention-before").value;
  if (!selectedDate) {
    if (!model.retention || model.retention.days == null || model.retention.enabled === false) throw new Error("当前不自动清理；手动清理请指定截至日期。");
    return {};
  }
  const cutoff = new Date(selectedDate + "T00:00:00.000Z");
  if (Number.isNaN(cutoff.getTime())) throw new Error("请选择有效的截至日期。");
  cutoff.setUTCDate(cutoff.getUTCDate() + 1);
  if (cutoff.getTime() > Date.now()) throw new Error("截至日期必须是已结束的 UTC 日期；未来截止时间不能清理。");
  return {before: cutoff.toISOString()};
}
async function retentionAction(kind, action) {
  if (model.retentionPending) return;
  model.retentionPending = kind; renderRetentionControls();
  try { await action(); }
  catch (error) {
    if (kind !== "save") invalidateRetentionPreview(`操作未完成：${error.message} 请重新预览后再清理。`, true);
    else { $("retention-policy-state").textContent = `策略保存失败：${error.message}`; $("retention-policy-state").classList.add("error-text"); }
    notice(error.message, true);
  } finally { model.retentionPending = null; renderRetentionControls(); }
}
function commentQuery() {
  const params = new URLSearchParams();
  [["video_id", "filter-video"], ["thread_id", "filter-thread"], ["category", "filter-category"], ["status", "filter-status"]].forEach(([key, id]) => { if ($(id).value.trim()) params.set(key, $(id).value.trim()); });
  return params.toString();
}
async function refreshComments() {
  const [comments, tasks] = await Promise.all([api(`/api/comments?${commentQuery()}`), api("/api/comment-tasks")]);
  model.comments = list(comments); renderComments(); renderCommentTasks(list(tasks));
  await refreshThreadContext();
}
function selectedThread() {
  const selected = model.comments.find(item => item.id === model.commentId);
  let videoId = $("filter-video").value.trim(), threadId = $("filter-thread").value.trim();
  if ((!videoId || !threadId) && selected) {
    const selectedId = selected.thread_id || selected.comment_id;
    if ((!videoId || videoId === selected.video_id) && (!threadId || threadId === selectedId)) {
      videoId = selected.video_id; threadId = selectedId;
    }
  }
  return videoId && threadId ? {videoId, threadId, key: JSON.stringify([videoId, threadId])} : null;
}
async function refreshThreadContext() {
  const target = selectedThread();
  if (!target) { $("thread-context-state").textContent = "选择评论或筛选单楼，查看该楼上下文；不同楼不会合并。"; return; }
  if (model.threadContextPending === target.key) return;
  model.threadContextPending = target.key;
  $("thread-context-state").textContent = `正在读取 ${target.videoId} / 楼 ${target.threadId} 的上下文…`;
  try {
    const query = new URLSearchParams({video_id: target.videoId, thread_id: target.threadId});
    const context = await api(`/api/context/status?${query}`);
    if (selectedThread()?.key !== target.key) return;
    $("thread-context-state").textContent = `所选楼 ${target.videoId} / ${target.threadId}：${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)}（${occupancyText(context)}），累计压缩 ${context.compact_count ?? 0} 轮、最近 ${context.compaction_passes_last ?? 0} 轮。${context.last_compaction_warning || ""}`;
  } catch (error) {
    if (selectedThread()?.key === target.key) $("thread-context-state").textContent = `所选楼上下文读取失败：${error.message}`;
  } finally { if (model.threadContextPending === target.key) model.threadContextPending = null; }
}
function renderComments() {
  const tbody = $("comments"); $("comment-count").textContent = `(${model.comments.length})`;
  // Preserve an open category menu or an in-flight save while background polling continues.
  if (model.categorySaving.size || document.activeElement?.matches("#comments .category-select")) {
    updateCommentSelection(); renderDraft(); return;
  }
  if (!model.comments.length) empty(tbody, 4, "暂无匹配评论。读取视频或导入已有评论后开始。");
  else {
    tbody.replaceChildren();
    model.comments.forEach(comment => {
      const row = node("tr", null, comment.id === model.commentId ? "selected-row" : "");
      const check = document.createElement("input"); check.type = "checkbox"; check.checked = model.selectedComments.has(comment.id); check.setAttribute("aria-label", `选择 ${comment.display_name || "观众"} 的评论`);
      check.onchange = () => { check.checked ? model.selectedComments.add(comment.id) : model.selectedComments.delete(comment.id); updateCommentSelection(); }; cell(row, check);
      const text = node("button", comment.content, "comment-link"); text.onclick = () => selectComment(comment.id);
      const contentCell = cell(row, text, `${comment.display_name || "匿名观众"} · ${comment.platform_user_id || "未知 ID"} · ${comment.video_id || ""}${comment.like_count != null ? ` · ${comment.like_count} 赞` : ""}`);
      const threadButton = node("button", `楼 ${comment.thread_id || comment.comment_id}`, "thread-link");
      threadButton.title = "只查看此楼，并将压缩操作限定到该楼";
      threadButton.onclick = () => busy(threadButton, async () => {
        $("filter-video").value = comment.video_id; $("filter-thread").value = comment.thread_id || comment.comment_id;
        $("compact-video-id").value = comment.video_id; $("compact-thread-id").value = comment.thread_id || comment.comment_id;
        await refreshComments();
      });
      contentCell.append(threadButton);
      if (comment.parent_comment_id) contentCell.append(node("small", `父评论 ${comment.parent_comment_id}`, "cell-sub"));
      const categorySelect = node("select", null, "category-select");
      categorySelect.setAttribute("aria-label", `评论 ${comment.comment_id} 的分类`);
      ["question", "suggestion", "interaction", "spam", "general"].forEach(value => {
        const option = node("option", label(value)); option.value = value; categorySelect.append(option);
      });
      categorySelect.value = comment.category || "general";
      const categoryCell = cell(row, categorySelect);
      const categoryFeedback = node("small", comment.category_source === "operator" ? "人工分类" : "", "cell-sub");
      categoryFeedback.setAttribute("aria-live", "polite"); categoryCell.append(categoryFeedback);
      categorySelect.onchange = () => saveCommentCategory(comment, categorySelect, categoryFeedback);
      cell(row, comment.draft ? badge(comment.draft.status) : "未生成"); tbody.append(row);
    });
  }
  updateCommentSelection(); renderDraft();
}
async function saveCommentCategory(comment, select, feedback) {
  const previous = comment.category || "general", category = select.value;
  model.categorySaving.add(comment.id); select.disabled = true; feedback.textContent = "保存中…";
  try {
    const result = await api(`/api/comments/${encodeURIComponent(comment.id)}/category`, {method: "PUT", body: {category}});
    const current = model.comments.find(item => item.id === comment.id);
    if (current) { current.category = result.category || category; current.category_source = "operator"; }
    comment.category = result.category || category; comment.category_source = "operator";
    select.value = comment.category; feedback.textContent = "人工分类 · 已保存";
  } catch (error) { select.value = previous; feedback.textContent = "保存失败，可重试"; notice(error.message, true); }
  finally { model.categorySaving.delete(comment.id); select.disabled = false; }
}
function updateCommentSelection() {
  $("selected-comment-count").textContent = `已选 ${model.selectedComments.size} 条`;
  const selected = model.comments.filter(comment => model.selectedComments.has(comment.id)).length;
  $("select-comments").checked = !!model.comments.length && selected === model.comments.length;
  $("select-comments").indeterminate = selected > 0 && selected < model.comments.length;
}
function selectComment(id) {
  if (model.draftDirty && !confirm("回复有未保存修改。放弃修改并查看其他评论？")) return;
  model.commentId = id; model.draftDirty = false; model.draftId = null;
  const comment = model.comments.find(item => item.id === id);
  if (comment) { $("compact-video-id").value = comment.video_id; $("compact-thread-id").value = comment.thread_id || comment.comment_id; }
  renderComments();
  refreshThreadContext();
}
function renderAnswerCheck(draft) {
  let check = draft?.answer_check;
  if (!check) { $("draft-answer-check").textContent = "暂无来源与完整性检查记录。"; return; }
  if (typeof check === "string") {
    try { check = JSON.parse(check); } catch { $("draft-answer-check").textContent = "检查记录格式异常，无法展示证据。"; return; }
  }
  if (!check || typeof check !== "object" || Array.isArray(check)) { $("draft-answer-check").textContent = "检查记录格式异常，无法展示证据。"; return; }
  const evidence = {};
  ["sources", "required_slots", "missing", "retried", "warning"].forEach(key => { if (Object.hasOwn(check, key)) evidence[key] = check[key]; });
  $("draft-answer-check").textContent = Object.keys(evidence).length ? JSON.stringify(evidence, null, 2) : "检查记录未提供来源、缺项、重试或提示字段。";
}
function renderDraft(force = false) {
  const comment = model.comments.find(item => item.id === model.commentId);
  if (!comment) {
    if (model.draftDirty) return;
    renderAnswerCheck(null);
    $("draft-source").textContent = "选择列表中的评论，查看角色草稿及来源。";
    $("open-private-draft").hidden = true; $("draft-text").value = ""; $("draft-text").disabled = true; $("draft-status").textContent = "选择评论";
    describe($("draft-meta"), []); ["approve-draft", "reject-draft", "copy-draft", "publish-draft"].forEach(id => { $(id).disabled = true; }); return;
  }
  const draft = comment.draft;
  renderAnswerCheck(draft);
  $("draft-source").textContent = `${comment.display_name || "匿名观众"}：${comment.content}`;
  setBadge($("draft-status"), draft?.status || "未生成");
  describe($("draft-meta"), [["来源评论", `${comment.video_id} / ${comment.comment_id}`], ["与当前OC的关系", typeof comment.relationship?.affinity === "number" ? `${comment.relationship.affinity} / 100 · ${comment.relationship.stage}` : "匿名用户"], ["楼ID", comment.thread_id || comment.comment_id], ["父评论ID", comment.parent_comment_id || "顶层"], ["角色版本", draft ? `${draft.character_id} · v${draft.character_version}` : "—"], ["模型", draft?.model || "—"], ...(draft?.publish_receipt || draft?.receipt ? [["平台回执", draft.publish_receipt || draft.receipt]] : [])]);
  if (force || !model.draftDirty || model.draftId !== draft?.id) { $("draft-text").value = draft?.text || ""; model.draftId = draft?.id || null; model.draftDirty = false; }
  const locked = draft && ["published", "publishing", "publish_uncertain", "private_handoff"].includes(draft.status);
  $("draft-text").disabled = !draft || locked;
  $("approve-draft").disabled = !draft || locked;
  $("reject-draft").disabled = !draft || locked;
  $("copy-draft").disabled = !draft || draft.status === "private_handoff";
  $("open-private-draft").hidden = draft?.status !== "private_handoff";
  $("publish-draft").disabled = !draft || draft.status !== "approved" || !model.policy?.credentials_configured || model.draftDirty;
  $("draft-note").textContent = draft?.status === "private_handoff" ? "在私信工作台审核这份回复并查看发送回执。" : draft?.error ? `错误：${draft.error}` : model.draftDirty ? "文字已修改，先审核保存后再发布或复制。" : model.policy?.auto_reply ? "自动回复已开启；新草稿可能自动发布。当前草稿以此处状态与平台回执为准。" : "人工审核模式。审核通过后可复制；账号凭证已配置时可发布。";
}
function renderCommentTasks(tasks) {
  const tbody = $("comment-tasks"); if (!tasks.length) return empty(tbody, 4, "暂无评论任务。选中评论后生成角色草稿。");
  tbody.replaceChildren(); tasks.forEach(task => { const row = node("tr"); cell(row, task.kind || task.type || "生成回复", task.id); cell(row, badge(task.status)); cell(row, task.total != null ? `${task.done || 0} / ${task.total}${task.errors ? ` · ${task.errors} 失败` : ""}` : task.progress ?? task.result ?? task.completed_count ?? "—"); cell(row, task.error || task.reason || "—"); tbody.append(row); });
}
async function loadPolicy() {
  model.policy = await api("/api/comments/policy");
  const policy = model.policy;
  $("auto-browse").checked = !!policy.auto_browse; $("auto-reply").checked = !!policy.auto_reply;
  $("watch-videos").value = (policy.watch_videos || []).join("\n");
  $("browse-interval").value = policy.browse_interval_seconds || 300;
  $("replies-hour").value = policy.max_replies_per_hour || 5; $("replies-day").value = policy.max_replies_per_day || 20;
  $("credential-state").textContent = policy.credentials_configured ? "已配置（不回显）" : "未配置";
  $("reply-mode").textContent = policy.auto_reply ? "自动回复已开启" : "人工审核";
  $("comment-fetch-mode").textContent = policy.auto_reply ? "公开读取 · 自动回复已开启" : "公开读取 · 人工审核";
  $("reply-mode").className = `badge ${policy.auto_reply ? "active" : ""}`; renderDraft();
}
async function loadPopularVideos() {
  const data = list(await api("/api/comments/videos")); const container = $("popular-videos");
  container.hidden = false; container.replaceChildren();
  if (!data.length) { container.append(node("p", "暂无可读取的热门视频，可直接输入视频链接。", "muted")); return; }
  data.slice(0, 12).forEach(video => {
    const button = node("button", video.title); button.append(node("small", `${typeof video.owner === "object" ? video.owner.name || "" : video.owner || ""} · ${video.bvid}`));
    button.onclick = () => { $("comment-video").value = video.bvid; $("filter-video").value = video.bvid; $("fetch-state").textContent = `已选择：${video.title}。点击「读取评论」获取内容。`; container.hidden = true; $("refresh-videos").textContent = "逛逛热门视频"; $("comment-video").focus(); }; container.append(button);
  });
  $("refresh-videos").textContent = "收起热门视频";
}
function boundedInteger(id, title, minimum, maximum) {
  const value = Number($(id).value);
  if (!$(id).value.trim() || !Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error(`${title}须为 ${minimum} 至 ${maximum} 的整数。`);
  return value;
}
function renderDynamicPolicy() {
  const policy = model.dynamicPolicy;
  $("dynamic-policy-fields").disabled = !policy || model.dynamicSaving;
  $("save-dynamic-policy").disabled = !policy || model.dynamicSaving || !model.dynamicPolicyDirty;
  $("stop-dynamics").disabled = !policy?.enabled || model.dynamicSaving;
  $("evaluate-dynamics").disabled = !policy || model.dynamicEvaluating || model.dynamicSaving;
  $("evaluate-dynamics").setAttribute("aria-busy", String(model.dynamicEvaluating));
  $("save-dynamic-policy").setAttribute("aria-busy", String(model.dynamicSaving));
  if (!policy) return;
  $("dynamic-mode").textContent = !policy.enabled ? "运营已停止" : policy.auto_publish ? "运营中 · 自动发布" : "运营中 · 人工审核";
  $("dynamic-mode").className = `badge ${policy.enabled ? "active" : ""}`;
  const character = model.status?.character;
  describe($("dynamic-policy-status"), [["当前角色", character ? `${character.name} · v${character.version}` : "未报告"], ["共用B站账号", policy.credentials_configured ? "已配置（不回显）" : "未配置，可手动生成草稿"], ["下次评估（当地时间）", localDateTime(policy.next_evaluation_at)], ["退避至（当地时间）", localDateTime(policy.backoff_until)], ["最近错误", policy.last_error || "无"], ["表单", model.dynamicPolicyDirty ? "有未保存修改" : "显示已保存策略"]]);
}
function localDateTime(value) {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? display(value) : date.toLocaleString("zh-CN", {hour12: false});
}
function dynamicPolicyFromServer(policy) {
  model.dynamicPolicy = policy;
  if (!model.dynamicPolicyDirty) {
    $("dynamic-enabled").checked = !!policy.enabled;
    $("dynamic-auto-publish").checked = policy.auto_publish !== false;
    $("dynamic-evaluate-interval").value = policy.evaluate_interval_seconds ?? 1800;
    $("dynamic-min-post-interval").value = policy.min_post_interval_seconds ?? 21600;
    $("dynamic-max-posts").value = policy.max_posts_per_day ?? 2;
    $("dynamic-memory-summary-max").value = policy.memory_summary_max_tokens ?? 2048;
  }
  renderDynamicPolicy();
}
function appendDynamicSources(container, sources) {
  container.replaceChildren();
  if (!Array.isArray(sources) || !sources.length) { container.append(node("p", "暂无可展示的来源原文。", "hint")); return; }
  sources.forEach(source => {
    const entry = node("div", null, "dynamic-source");
    entry.append(node("strong", `${source.fictional ? "虚构角色故事" : "公开来源"} · ${source.type || "来源"} · ${source.id || "—"}`));
    entry.append(node("p", source.text || "未返回来源文字。"));
    if (source.original_comment) entry.append(node("p", `原评论：${display(source.original_comment)}`, "hint"));
    if (source.source_url) {
      try {
        const url = new URL(source.source_url);
        if (url.protocol === "https:") { const link = node("a", "打开来源"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; entry.append(link); }
      } catch { /* Source text remains visible without an unsafe or invalid link. */ }
    }
    container.append(entry);
  });
}
function renderDynamicPosts() {
  const body = $("dynamic-posts"); $("dynamic-count").textContent = `${model.dynamicPosts.length} 条`;
  if (!model.dynamicPosts.length) empty(body, 3, "暂无动态。可先评估并生成一份待审核草稿。");
  else {
    body.replaceChildren();
    model.dynamicPosts.forEach(post => {
      const row = node("tr", null, post.id === model.dynamicPostId ? "selected-row" : "");
      cell(row, localDateTime(post.created_at), `${post.character_id} · v${post.character_version}`);
      const button = node("button", post.content || "查看动态", "comment-link");
      button.onclick = () => {
        if (model.dynamicPostDirty && post.id !== model.dynamicPostId && !confirm("动态有未保存修改。放弃修改并选择另一条？")) return;
        if (post.id !== model.dynamicPostId) model.dynamicPostDirty = false;
        model.dynamicPostId = post.id; renderDynamicPosts();
      };
      cell(row, button); cell(row, badge(post.status)); body.append(row);
    });
  }
  renderDynamicPost();
}
function renderDynamicPost() {
  const post = model.dynamicPosts.find(item => item.id === model.dynamicPostId);
  const editable = !!post && ["pending_review", "approved"].includes(post.status);
  if (post) setBadge($("dynamic-post-status"), post.status);
  else { $("dynamic-post-status").className = "badge"; $("dynamic-post-status").textContent = "选择动态"; }
  $("dynamic-post-text").disabled = !editable || model.dynamicPostPending;
  if (!model.dynamicPostDirty) $("dynamic-post-text").value = post?.content || "";
  $("approve-dynamic").disabled = !editable || model.dynamicPostPending;
  $("reject-dynamic").disabled = !editable || model.dynamicPostPending;
  $("publish-dynamic").disabled = !post || post.status !== "approved" || model.dynamicPostDirty || model.dynamicPostPending || !model.dynamicPolicy?.credentials_configured;
  describe($("dynamic-post-meta"), post ? [["动态ID", post.id], ["角色", `${post.character_id} · v${post.character_version}`], ["模型", post.model], ["决策原因", post.reason || "未报告"], ["发布时间（当地时间）", localDateTime(post.published_at)]] : []);
  appendDynamicSources($("dynamic-post-sources"), post?.sources);
  $("dynamic-post-receipt").textContent = JSON.stringify(publicSettings({receipt: post?.receipt ?? null, error: post?.error ?? null}), null, 2);
  $("dynamic-post-note").textContent = !post ? "从列表选择动态，查看来源、决策与平台回执。" : model.dynamicPostDirty ? "有未保存修改；审核通过后保存，再发布。" : post.error ? `平台操作未完成：${post.error}` : post.status === "publish_uncertain" ? "发布结果不确定，请核对平台回执；不会自动重发。" : post.status === "published" ? "已发布，平台回执见下方。" : !model.dynamicPolicy?.credentials_configured ? "未配置B站账号；可生成和审核草稿，发布前请到评论设置配置凭证。" : "手动评估生成的草稿始终保留人工审核，后台不会补发。";
  $("dynamic-post-note").classList.toggle("error-text", !!post?.error || post?.status === "publish_uncertain");
}
function renderDynamicDecisions() {
  const body = $("dynamic-decisions");
  if (!model.dynamicDecisions.length) return empty(body, 4, "暂无评估决策。手动评估或开启运营后查看。");
  body.replaceChildren();
  model.dynamicDecisions.forEach(decision => {
    const row = node("tr"); cell(row, localDateTime(decision.created_at), `${decision.character_id} · v${decision.character_version}${decision.manual ? " · 手动" : " · 后台"}`);
    cell(row, badge(decision.action), label(decision.status)); cell(row, decision.reason || "未报告", decision.error || "");
    const details = node("details"); details.append(node("summary", `来源 ${list(decision.sources).length} 条`));
    const sources = node("div"); appendDynamicSources(sources, decision.sources); details.append(sources); cell(row, details); body.append(row);
  });
}
function renderDynamicMemories() {
  if (model.dynamicMemorySaving.size) return;
  const body = $("dynamic-memories");
  if (!model.dynamicMemories.length) return empty(body, 3, "当前角色暂无可选择的来源记忆；已发布回复无需手动授权。");
  body.replaceChildren();
  model.dynamicMemories.forEach(memory => {
    const row = node("tr"), check = node("input"); check.type = "checkbox"; check.checked = !!memory.public;
    check.setAttribute("aria-label", `允许记忆 ${memory.id} 用于公开动态`);
    check.onchange = async () => {
      const before = !!memory.public; model.dynamicMemorySaving.add(memory.id); check.disabled = true;
      try {
        await api(`/api/dynamics/memories/${encodeURIComponent(memory.id)}`, {method: "PUT", body: {public: check.checked}});
        memory.public = check.checked;
        $("dynamic-memory-state").textContent = check.checked ? "已允许该记忆用于公开动态。" : "已撤销该记忆的后续公开使用；已发布内容保留原回执。";
        $("dynamic-memory-state").classList.remove("error-text");
      } catch (error) {
        check.checked = before; $("dynamic-memory-state").textContent = `授权保存失败：${safeMessage(error.message)}`;
        $("dynamic-memory-state").classList.add("error-text");
      } finally { model.dynamicMemorySaving.delete(memory.id); check.disabled = false; }
    };
    cell(row, check); cell(row, memory.text, `${memory.character_id} · v${memory.character_version} · ${memory.id}`);
    cell(row, memory.fictional || memory.kind === "role_story" ? "虚构角色故事" : label(memory.kind), localDateTime(memory.created_at)); body.append(row);
  });
}
function renderDynamicMemorySummary(summary) {
  const state = $("dynamic-summary-valid");
  state.textContent = summary.valid ? "有效" : "待更新";
  state.className = `badge ${summary.valid ? "good" : ""}`;
  describe($("dynamic-summary-meta"), [["角色", summary.character_id ? `${summary.character_id} · v${summary.character_version}` : "未报告"], ["实际用量 / 摘要上限", `${tokenCount(summary.token_count)} / ${tokenCount(summary.max_tokens)}`], ["最近更新（当地时间）", localDateTime(summary.updated_at)]]);
  $("dynamic-summary-state").textContent = summary.valid ? "当前有效摘要供运营评估使用；原文保留在来源核对中。" : "尚无有效摘要或来源／策略已变化；下次评估将更新摘要。";
  const body = $("dynamic-summary-entries"), entries = list(summary.entries);
  if (!entries.length) { empty(body, 3, "暂无运营记忆摘要摘录。公开来源就绪后评估生成。"); return; }
  body.replaceChildren();
  entries.forEach(entry => {
    const row = node("tr"); cell(row, entry.source_id || "未返回来源ID"); cell(row, entry.quote || "未返回摘录");
    cell(row, entry.fictional ? "虚构角色故事" : "公开互动摘录"); body.append(row);
  });
}
async function refreshDynamics() {
  const [policy, posts, decisions, memories, summary] = await Promise.all([api("/api/dynamics/policy"), api("/api/dynamics/posts"), api("/api/dynamics/decisions"), api("/api/dynamics/memories?limit=100"), api("/api/dynamics/memory-summary")]);
  const first = !model.dynamicListsLoaded;
  const credentialsChanged = model.dynamicPolicy?.credentials_configured !== policy.credentials_configured;
  dynamicPolicyFromServer(policy);
  if (first || JSON.stringify(model.dynamicPosts) !== JSON.stringify(list(posts))) {
    model.dynamicPosts = list(posts); renderDynamicPosts();
  } else if (credentialsChanged) renderDynamicPost();
  if (first || JSON.stringify(model.dynamicDecisions) !== JSON.stringify(list(decisions))) {
    model.dynamicDecisions = list(decisions); renderDynamicDecisions();
  }
  if (!model.dynamicMemorySaving.size && (first || JSON.stringify(model.dynamicMemories) !== JSON.stringify(list(memories)))) {
    model.dynamicMemories = list(memories); renderDynamicMemories();
  }
  if (JSON.stringify(model.dynamicMemorySummary) !== JSON.stringify(summary)) {
    model.dynamicMemorySummary = summary; renderDynamicMemorySummary(summary);
  }
  model.dynamicListsLoaded = true;
}
async function saveDynamicPolicy(stop = false) {
  if (model.dynamicSaving) return;
  const payload = stop ? {enabled: false} : {
    enabled: $("dynamic-enabled").checked, auto_publish: $("dynamic-auto-publish").checked,
    evaluate_interval_seconds: boundedInteger("dynamic-evaluate-interval", "动态评估间隔", 300, 86400),
    min_post_interval_seconds: boundedInteger("dynamic-min-post-interval", "最短发布间隔", 21600, 604800),
    max_posts_per_day: boundedInteger("dynamic-max-posts", "每日动态数", 1, 2),
    memory_summary_max_tokens: boundedInteger("dynamic-memory-summary-max", "运营记忆摘要长度", 256, 4096),
  };
  model.dynamicSaving = true; renderDynamicPolicy();
  try {
    const policy = await api("/api/dynamics/policy", {method: "PUT", body: payload});
    if (stop) $("dynamic-enabled").checked = false;
    else model.dynamicPolicyDirty = false;
    dynamicPolicyFromServer(policy);
    $("dynamic-policy-state").classList.remove("error-text");
    $("dynamic-policy-state").textContent = !policy.enabled ? "动态运营已停止；已发布请求以平台回执为准。" : policy.auto_publish ? "运营已开启，后台评估可直接发布；手动评估仍只生成草稿。" : "运营已开启，所有新动态保留人工审核。";
    notice($("dynamic-policy-state").textContent);
  } catch (error) {
    $("dynamic-policy-state").textContent = `策略保存失败：${safeMessage(error.message)}。输入已保留。`;
    $("dynamic-policy-state").classList.add("error-text"); throw error;
  } finally { model.dynamicSaving = false; renderDynamicPolicy(); }
}
async function reviewDynamic(action) {
  const post = model.dynamicPosts.find(item => item.id === model.dynamicPostId);
  if (!post || model.dynamicPostPending) return;
  model.dynamicPostPending = true; renderDynamicPost();
  try {
    await api(`/api/dynamics/posts/${encodeURIComponent(post.id)}/review`, {method: "POST", body: {action, text: $("dynamic-post-text").value}});
    model.dynamicPostDirty = false; await refreshDynamics();
    $("dynamic-post-note").textContent = action === "approve" ? "审核已保存，尚未手动发布。" : "动态已拒绝。";
  } finally { model.dynamicPostPending = false; renderDynamicPost(); }
}
async function changePage() {
  const requested = location.hash.slice(1) || "comments";
  model.page = ["comments", "messages", "dynamics", "overview", "live", "characters", "voices", "assets", "settings"].includes(requested) ? requested : "comments";
  document.querySelectorAll("[data-section]").forEach(section => { section.hidden = section.dataset.section !== model.page; });
  document.querySelectorAll("[data-page]").forEach(link => { const active = link.dataset.page === model.page; link.classList.toggle("selected", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  document.title = `${({comments: "评论工作台", messages: "私信工作台", dynamics: "动态运营", overview: "总览", live: "直播控制", characters: "角色", voices: "语音", assets: "素材", settings: "设置"})[model.page]} · Tuber Studio`;
  if (!model.authenticated) return;
  try { if (model.page === "comments") await refreshComments(); else if (model.page === "messages") await messagesPanel.refresh(); else if (model.page === "dynamics") await refreshDynamics(); else if (["overview", "live"].includes(model.page)) await refreshLive(); else if (model.page === "settings") await Promise.all([loadRetention(), cachePanel.refresh()]); }
  catch (error) { notice(error.message, true); }
}
async function poll() {
  if (!model.authenticated || model.polling || document.hidden) return;
  model.polling = true;
  try {
    renderStatus(await api("/api/status"));
    if (["overview", "live"].includes(model.page)) await refreshLive();
    if (model.page === "comments") await refreshComments();
    if (model.page === "dynamics") await refreshDynamics();
    if (model.page === "messages") await messagesPanel.refresh();
  } catch (error) { $("connection").textContent = "连接失败，等待重试"; if (error.status !== 401) notice(error.message, true); }
  finally { model.polling = false; }
}

$("login-form").onsubmit = event => {
  event.preventDefault(); const button = event.submitter; $("login-error").textContent = "";
  busy(button, async () => {
    try { await api("/api/auth", {method: "POST", body: {token: $("token").value}}); $("token").value = ""; await enterWorkspace(); }
    catch (error) { $("login-error").textContent = error.message; }
  });
};
$("logout").onclick = () => busy($("logout"), async () => { await api("/api/logout", {method: "POST"}); showLogin(); });
document.querySelectorAll("[data-inference-mode]").forEach(button => {
  button.onclick = () => switchResourceMode(button.dataset.inferenceMode);
});
document.querySelectorAll("[data-action]").forEach(button => {
  button.onclick = () => busy(button, async () => {
    const action = button.dataset.action;
    if (action === "clear" && !confirm("清空所有待播任务？当前播放不会因此标记为完成。")) return;
    const result = await api(`/api/live/${action}`, {method: "POST"});
    notice(`已执行：${button.textContent}`); renderStatus(result.state ? result : await api("/api/status")); await refreshLive();
  });
});
$("event-type").onchange = () => { $("sfx-label").hidden = $("event-type").value !== "sfx"; };
$("fill-greeting").onclick = () => { $("event-text").value = "大肥鱼，今天在忙什么？"; $("event-text").focus(); };
$("event-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    const payload = {event_id: $("event-id").value.trim() || crypto.randomUUID(), platform: "mock", room_id: "debug", platform_user_id: $("event-user").value.trim() || null, display_name: $("event-name").value.trim() || "匿名观众", type: $("event-type").value, timestamp: new Date().toISOString(), text: $("event-text").value, metadata: {}};
    if ($("event-asset").value.trim()) payload.metadata.asset_id = $("event-asset").value.trim();
    const result = await api("/api/live/mock-event", {method: "POST", body: payload});
    $("event-result").textContent = `${label(result.status || "received")} · ${payload.event_id}${result.reason ? ` · ${result.reason}` : ""}`;
    await refreshLive();
  });
};
$("refresh-live").onclick = () => busy($("refresh-live"), refreshLive);
$("check-providers").onclick = () => busy($("check-providers"), async () => {
  await checkProviderConnections("provider-check");
});
$("providers-form").addEventListener("input", () => { model.providersDirty = true; renderProviderForm(); });
$("providers-form").addEventListener("change", () => { model.providersDirty = true; renderProviderForm(); });
$("providers-form").onsubmit = async event => {
  event.preventDefault(); if (model.providersSaving) return;
  try {
    const payload = readProviderPayload();
    model.providersSaving = true; renderProviderForm();
    $("providers-save-state").classList.remove("error-text"); $("providers-save-state").textContent = "正在保存并应用，请等待服务返回…";
    const result = await api("/api/settings/providers", {method: "PUT", body: payload});
    applySettings(result, true);
    const message = result.restart_required === true ? "配置已保存；服务要求重启后生效，请按服务提示处理。" : "配置已保存并应用，无需手动重启。已有角色音色保持原选择。";
    $("providers-save-state").textContent = message; notice(message);
    const checks = await Promise.allSettled([api("/api/status"), loadVoices()]);
    if (checks[0].status === "fulfilled") renderStatus(checks[0].value);
    const failed = checks.filter(item => item.status === "rejected");
    if (failed.length) $("providers-save-state").textContent = `${message} 状态刷新失败：${safeMessage(failed[0].reason.message)}。可稍后刷新。`;
  } catch (error) {
    const message = `保存失败：${safeMessage(error.message)}${error.status === 409 ? " 请先暂停或等待正在生成 / 发布的任务结束，再重试。" : ""} 当前输入已保留。`;
    $("providers-save-state").textContent = message; $("providers-save-state").classList.add("error-text"); notice(message, true);
  } finally { model.providersSaving = false; renderProviderForm(); }
};
$("reset-providers").onclick = () => {
  if (!model.settings || model.providersSaving) return;
  providerFormFromSettings(model.settings); renderProviderForm();
  $("providers-save-state").textContent = "已恢复当前已保存配置；未提交的密钥输入已清除。"; $("providers-save-state").classList.remove("error-text");
};
$("check-providers-settings").onclick = () => busy($("check-providers-settings"), () => checkProviderConnections("providers-check-result"));
$("character-editor").oninput = () => { model.characterDirty = true; $("character-dirty").textContent = "有未保存修改"; };
$("character-version").onchange = () => {
  const version = Number($("character-version").value);
  if (model.characterDirty && !confirm("放弃未保存修改并查看此版本？")) { $("character-version").value = String(model.character.version); return; }
  const character = model.versions.find(item => item.version === version); if (character) setCharacterEditor(character);
};
$("reset-character").onclick = () => { if (!model.characterDirty || confirm("放弃未保存的角色修改？")) setCharacterEditor(model.character); };
$("character-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    let character; try { character = JSON.parse($("character-editor").value); } catch (error) { throw new Error(`角色 JSON 格式错误：${error.message}`); }
    if (!character || typeof character !== "object" || Array.isArray(character)) throw new Error("角色配置必须为 JSON 对象");
    const result = await api(`/api/characters/${encodeURIComponent(model.characterId)}`, {method: "PUT", body: character});
    model.characterDirty = false; await loadCharacters(); await selectCharacter(model.characterId, result.version); notice(`已保存新角色版本 v${result.version ?? ""}`);
  });
};
$("activate-character").onclick = () => busy($("activate-character"), async () => {
  if (model.characterDirty) throw new Error("请先保存或放弃未保存的角色修改，再激活版本。");
  if (!confirm(`激活 ${model.character.name} v${model.character.version}？旧角色尚未播报的任务将被取消。`)) return;
  await api(`/api/characters/${encodeURIComponent(model.characterId)}/activate`, {method: "POST", body: {version: model.character.version}});
  renderStatus(await api("/api/status")); await loadCharacters(); notice("角色版本已激活。");
});
$("clone-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    if (!model.characterId) throw new Error("请先选择要复制的角色");
    const id = $("clone-id").value.trim(); const name = $("clone-name").value.trim();
    await api(`/api/characters/${encodeURIComponent(model.characterId)}/clone`, {method: "POST", body: {id, name}});
    await loadCharacters(); await selectCharacter(id); notice("已创建新角色，可以编辑人格、风格和音色。");
  });
};
$("preview-voice").onchange = renderVoicePreview;
$("preview-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    const voice = model.voices.find(item => item.id === $("preview-voice").value);
    const unavailable = voiceAvailability(voice); if (unavailable) throw new Error(unavailable);
    $("preview-state").textContent = "等待合成…"; $("preview-audio").pause();
    try {
      const result = await api("/api/tts/preview", {method: "POST", body: {text: $("preview-text").value, voice_id: $("preview-voice").value}});
      $("preview-audio").src = result.audio_ref; $("preview-audio").hidden = false;
      const recorded = voice?.provider === "prerecorded";
      const actualText = recorded && result.text ? ` 实际录音台词：${result.text}` : "";
      $("preview-state").textContent = safeMessage(`${recorded ? "预录已准备" : "合成完成"}${result.duration != null ? ` · ${Number(result.duration).toFixed(2)} 秒` : ""}，点击播放试听。${actualText}${result.warning ? ` ${result.warning}` : ""}`);
    }
    catch (error) { $("preview-state").textContent = `合成失败：${safeMessage(error.message)}`; throw error; }
  });
};
$("copy-overlay").onclick = () => busy($("copy-overlay"), async () => { await navigator.clipboard.writeText($("overlay-url").value); notice("播放器链接已复制。"); });
$("refresh-videos").onclick = () => busy($("refresh-videos"), async () => {
  if (!$("popular-videos").hidden) { $("popular-videos").hidden = true; $("refresh-videos").textContent = "逛逛热门视频"; return; }
  await loadPopularVideos();
});
$("fetch-comments-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    $("fetch-state").textContent = "正在读取公开评论…";
    $("popular-videos").hidden = true; $("refresh-videos").textContent = "逛逛热门视频";
    try {
      const result = await api("/api/comments/fetch", {method: "POST", body: {video: $("comment-video").value.trim(), max_pages: Number($("comment-pages").value)}});
      $("fetch-state").textContent = `读取完成：导入 ${result.imported ?? 0} 条${result.duplicates != null ? `，重复 ${result.duplicates} 条` : ""}${result.warning ? `。${result.warning}` : ""}`;
      if (result.video_id) { $("filter-video").value = result.video_id; $("import-video").value = result.video_id; }
      await refreshComments();
    } catch (error) { $("fetch-state").textContent = `读取失败：${error.message}。可重试或导入已有评论。`; throw error; }
  });
};
function readImportPayload() {
  const raw = $("import-comments").value.trim(), payload = {video_id: $("import-video").value.trim()};
  if (!payload.video_id || !raw) throw new Error("请填写视频标识和评论数据");
  if (raw.startsWith("[") || raw.startsWith("{")) {
    let value; try { value = JSON.parse(raw); } catch (error) { throw new Error(`评论 JSON 格式错误：${error.message}`); }
    payload.items = Array.isArray(value) ? value : value.items;
    if (!Array.isArray(payload.items)) throw new Error("JSON 必须是评论数组，或含 items 数组的对象");
  } else payload.csv_text = raw;
  const mapping = $("import-mapping").value.trim();
  if (mapping) {
    try { payload.field_mapping = JSON.parse(mapping); } catch (error) { throw new Error(`字段映射 JSON 格式错误：${error.message}`); }
    if (!payload.field_mapping || typeof payload.field_mapping !== "object" || Array.isArray(payload.field_mapping)) throw new Error("字段映射必须是 JSON 对象");
    const targets = ["comment_id", "platform_user_id", "display_name", "content", "like_count", "created_at", "thread_id", "parent_comment_id"];
    Object.entries(payload.field_mapping).forEach(([target, source]) => {
      if (!targets.includes(target)) throw new Error(`不支持的目标字段：${target}`);
      if (typeof source !== "string" || !source.trim()) throw new Error(`${target} 的来源字段名必须是非空字符串`);
    });
  }
  return payload;
}
function invalidateImportPreview() {
  model.importPreviewSignature = null; $("submit-import").disabled = true;
  $("import-preview").hidden = true; $("import-preview-state").textContent = "输入已变更，请重新预览。";
}
["import-video", "import-comments", "import-mapping"].forEach(id => { $(id).addEventListener("input", invalidateImportPreview); });
$("preview-import").onclick = () => busy($("preview-import"), async () => {
  if (!$("import-comments-form").reportValidity()) return;
  invalidateImportPreview(); $("import-preview-state").textContent = "正在规范化并预览数据…";
  try {
    const payload = readImportPayload(), signature = JSON.stringify(payload);
    const result = await api("/api/comments/import/preview", {method: "POST", body: payload});
    let stillCurrent = false; try { stillCurrent = JSON.stringify(readImportPayload()) === signature; } catch { /* Input changed while previewing. */ }
    if (!stillCurrent) { invalidateImportPreview(); return; }
    const items = list(result); const tbody = $("import-preview-rows"); tbody.replaceChildren();
    if (!items.length) empty(tbody, 7, "没有可预览的数据。");
    else items.slice(0, 10).forEach(item => {
      const row = node("tr"); cell(row, item.comment_id); cell(row, item.thread_id || item.comment_id); cell(row, item.parent_comment_id || "—"); cell(row, item.platform_user_id || "未知 ID", item.display_name || "匿名观众");
      cell(row, item.content); cell(row, item.like_count ?? 0); cell(row, item.created_at || "—"); tbody.append(row);
    });
    $("import-preview").hidden = false;
    $("import-preview-state").textContent = `共 ${result.total ?? items.length} 条，预览前 ${Math.min(items.length, 10)} 条；尚未写入。`;
    model.importPreviewSignature = signature; $("submit-import").disabled = !items.length;
  } catch (error) { $("import-preview-state").textContent = `预览失败：${error.message}`; throw error; }
});
$("import-comments-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter || $("submit-import"), async () => {
    const payload = readImportPayload();
    if (!model.importPreviewSignature || JSON.stringify(payload) !== model.importPreviewSignature) throw new Error("请先预览当前数据，再提交导入");
    const result = await api("/api/comments/import", {method: "POST", body: payload});
    model.importPreviewSignature = null; $("submit-import").disabled = true;
    $("import-preview-state").textContent = `已导入 ${result.imported ?? 0} 条${result.duplicates != null ? `，重复 ${result.duplicates} 条` : ""}。修改数据后可再次预览。`;
    $("filter-video").value = result.video_id || payload.video_id; notice(`导入 ${result.imported ?? 0} 条评论。`); await refreshComments();
  });
};
$("filter-comments").onclick = () => busy($("filter-comments"), refreshComments);
$("analyze-comments").onclick = () => busy($("analyze-comments"), async () => { const result = await api("/api/comments/analyze", {method: "POST", body: {video_id: $("filter-video").value.trim() || undefined}}); notice(`归类完成：${JSON.stringify(result)}`); await refreshComments(); });
$("generate-drafts").onclick = () => busy($("generate-drafts"), async () => {
  if (!model.selectedComments.size) throw new Error("请先选择评论");
  await api("/api/comments/drafts", {method: "POST", body: {comment_ids: [...model.selectedComments]}});
  notice("生成任务已加入队列，可在下方查看进度。"); await refreshComments();
});
$("select-comments").onchange = () => { model.comments.forEach(comment => { $("select-comments").checked ? model.selectedComments.add(comment.id) : model.selectedComments.delete(comment.id); }); renderComments(); };
$("draft-text").oninput = () => { model.draftDirty = true; renderDraft(); };
async function reviewDraft(action) {
  const comment = model.comments.find(item => item.id === model.commentId); if (!comment?.draft) throw new Error("请先选择已有草稿的评论");
  await api(`/api/comments/drafts/${encodeURIComponent(comment.draft.id)}/review`, {method: "POST", body: {action, text: $("draft-text").value}});
  model.draftDirty = false; await refreshComments(); notice(action === "approve" ? "草稿已审核保存。" : "草稿已拒绝。");
}
$("approve-draft").onclick = () => busy($("approve-draft"), () => reviewDraft("approve"));
$("reject-draft").onclick = () => busy($("reject-draft"), () => reviewDraft("reject"));
$("copy-draft").onclick = () => busy($("copy-draft"), async () => {
  const comment = model.comments.find(item => item.id === model.commentId); if (!comment?.draft) return;
  if (model.draftDirty) throw new Error("请先审核保存修改，再复制已保存的回复");
  await navigator.clipboard.writeText($("draft-text").value);
  await api(`/api/comments/drafts/${encodeURIComponent(comment.draft.id)}/copy`, {method: "POST"});
  notice("回复已复制，未执行平台发布。"); await refreshComments();
});
$("publish-draft").onclick = () => busy($("publish-draft"), async () => {
  const comment = model.comments.find(item => item.id === model.commentId); if (!comment?.draft) return;
  if (model.draftDirty) throw new Error("请先审核保存修改");
  const result = await api(`/api/comments/drafts/${encodeURIComponent(comment.draft.id)}/publish`, {method: "POST"});
  notice(`平台已返回发布回执：${JSON.stringify(result)}`); await refreshComments();
});
$("comment-policy-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    const payload = {auto_browse: $("auto-browse").checked, auto_reply: $("auto-reply").checked, watch_videos: $("watch-videos").value.split(/[\s,，]+/).filter(Boolean), browse_interval_seconds: Number($("browse-interval").value), max_replies_per_hour: Number($("replies-hour").value), max_replies_per_day: Number($("replies-day").value)};
    if ($("bili-sessdata").value) payload.sessdata = $("bili-sessdata").value;
    if ($("bili-csrf").value) payload.csrf = $("bili-csrf").value;
    await api("/api/comments/policy", {method: "PUT", body: payload});
    $("bili-sessdata").value = ""; $("bili-csrf").value = ""; await loadPolicy(); notice(payload.auto_reply ? "自动回复设置已保存并开启。" : "回复设置已保存；当前为人工审核模式。");
  });
};
$("stop-auto-reply").onclick = () => busy($("stop-auto-reply"), async () => {
  await api("/api/comments/policy", {method: "PUT", body: {auto_reply: false}}); await loadPolicy(); notice("自动回复已停止。已提交给平台的请求以回执为准。");
});
$("dynamic-policy-form").addEventListener("input", () => { model.dynamicPolicyDirty = true; renderDynamicPolicy(); });
$("dynamic-policy-form").addEventListener("change", () => { model.dynamicPolicyDirty = true; renderDynamicPolicy(); });
$("dynamic-policy-form").onsubmit = event => { event.preventDefault(); busy($("save-dynamic-policy"), () => saveDynamicPolicy()); };
$("stop-dynamics").onclick = () => busy($("stop-dynamics"), () => saveDynamicPolicy(true));
$("refresh-dynamics").onclick = () => busy($("refresh-dynamics"), refreshDynamics);
$("evaluate-dynamics").onclick = () => busy($("evaluate-dynamics"), async () => {
  if (model.dynamicEvaluating || !model.dynamicPolicy) return;
  model.dynamicEvaluating = true; renderDynamicPolicy();
  $("dynamic-policy-state").classList.remove("error-text");
  $("dynamic-policy-state").textContent = "正在评估当前角色的公开来源，手动评估不会自动发布…";
  notice("");
  try {
    const result = await api("/api/dynamics/evaluate", {method: "POST", body: {}});
    await refreshDynamics();
    if (result.post_id != null && !model.dynamicPostDirty) { model.dynamicPostId = result.post_id; renderDynamicPosts(); }
    $("dynamic-policy-state").textContent = result.action === "wait" ? `本轮决定等待：${result.reason || "当前没有适合发布的内容。"}` : result.action === "error" ? `评估未完成：${result.error || result.reason || "请查看决策记录。"}` : "评估完成；生成的草稿等待人工审核，不会由后台补发。";
    $("dynamic-policy-state").classList.toggle("error-text", result.action === "error");
  } catch (error) {
    $("dynamic-policy-state").textContent = `评估失败：${safeMessage(error.message)}。可查看已保留的决策与错误。`;
    $("dynamic-policy-state").classList.add("error-text"); throw error;
  } finally { model.dynamicEvaluating = false; renderDynamicPolicy(); }
});
$("dynamic-post-text").oninput = () => { model.dynamicPostDirty = true; renderDynamicPost(); };
$("approve-dynamic").onclick = () => busy($("approve-dynamic"), () => reviewDynamic("approve"));
$("reject-dynamic").onclick = () => busy($("reject-dynamic"), () => reviewDynamic("reject"));
$("publish-dynamic").onclick = () => busy($("publish-dynamic"), async () => {
  const post = model.dynamicPosts.find(item => item.id === model.dynamicPostId);
  if (!post || post.status !== "approved" || model.dynamicPostDirty || model.dynamicPostPending) return;
  model.dynamicPostPending = true; renderDynamicPost();
  try {
    await api(`/api/dynamics/posts/${encodeURIComponent(post.id)}/publish`, {method: "POST"});
    await refreshDynamics();
  } catch (error) {
    await refreshDynamics().catch(() => {}); notice(`动态发布未完成：${safeMessage(error.message)}，请核对平台回执。`, true);
  } finally { model.dynamicPostPending = false; renderDynamicPost(); }
});
async function compactContext(scope, videoId, threadId) {
  if (scope === "comments" && (!videoId || !threadId)) throw new Error("请先选择要压缩的视频与单个楼ID；不同楼上下文不会合并。");
  const payload = {scope, ...(scope === "comments" ? {video_id: videoId, thread_id: threadId} : {})};
  $("compaction-result").textContent = "正在使用当前文本模型压缩上下文…";
  notice("上下文压缩已开始，请等待当前文本模型返回。");
  try {
    const result = await api("/api/context/compact", {method: "POST", body: payload});
    const message = result.message || result.detail || (result.compacted === false ? "没有可压缩的历史。" : "压缩请求已完成，请查看实际上下文占用与压缩次数。");
    $("compaction-result").textContent = message; notice(message);
    renderStatus(await api("/api/status"));
    if (scope === "comments") await refreshThreadContext();
  } catch (error) { $("compaction-result").textContent = `压缩失败：${error.message}`; throw error; }
}
$("compact-live").onclick = () => busy($("compact-live"), () => compactContext("live"));
$("compact-comments-settings").onclick = () => busy($("compact-comments-settings"), () => compactContext("comments", $("compact-video-id").value.trim(), $("compact-thread-id").value.trim()));
$("compact-comments").onclick = () => busy($("compact-comments"), () => {
  const target = selectedThread();
  return compactContext("comments", target?.videoId, target?.threadId);
});
$("rebuild-comments").onclick = () => busy($("rebuild-comments"), async () => {
  const target = selectedThread();
  if (!target) throw new Error("请先选择评论或筛选视频与单个楼ID，再重建该楼上下文。");
  $("thread-rebuild-state").textContent = `正在使用本地现存原文重建 ${target.videoId} / ${target.threadId}…`;
  try {
    const result = await api("/api/context/rebuild", {method: "POST", body: {scope: "comments", video_id: target.videoId, thread_id: target.threadId}});
    let message = result.message || result.detail || (result.rebuilt === false ? "本次未重建上下文，请检查该楼现存原文。" : `楼 ${target.videoId} / ${target.threadId} 上下文已重建，请查看实际占用。`);
    if (result.prefilled === false) message += " 诊断：未执行真实模型 prefill，不能据此认定模型已预热。";
    else if (result.prefilled === true) message += ` 模型 prefill 已完成${result.input_tokens != null ? `，输入 ${result.input_tokens} tokens` : ""}。`;
    $("thread-rebuild-state").textContent = message; notice(message);
    await refreshThreadContext();
  } catch (error) { $("thread-rebuild-state").textContent = `重建失败：${error.message}`; throw error; }
});
["retention-days", "retention-never"].forEach(id => $(id).addEventListener("input", () => {
  model.retentionDirty = true; $("retention-policy-state").textContent = "有未保存修改，请保存后预览。";
  $("retention-policy-state").classList.remove("error-text");
  invalidateRetentionPreview();
}));
$("retention-before").oninput = () => invalidateRetentionPreview("截至日期已变更，请重新预览。");
$("retention-confirm").onchange = renderRetentionControls;
$("retention-form").onsubmit = event => {
  event.preventDefault(); retentionAction("save", async () => {
    const days = $("retention-never").checked ? null : Number($("retention-days").value);
    if (days !== null && (!Number.isSafeInteger(days) || days < 1 || days > 36500)) throw new Error("保留天数必须为 1 至 36500 的整数。");
    await api("/api/retention", {method: "PUT", body: {days}});
    model.retentionDirty = false; invalidateRetentionPreview(); await loadRetention();
    $("retention-policy-state").textContent = "留存策略已保存；本次未执行手动清理。";
  });
};
$("retention-preview").onclick = () => retentionAction("preview", async () => {
  const payload = retentionRequest(), signature = JSON.stringify(payload);
  invalidateRetentionPreview("正在统计待清理记录；尚未删除…");
  const result = await api("/api/retention/preview", {method: "POST", body: payload});
  if (JSON.stringify(retentionRequest()) !== signature) return;
  if (result.cutoff == null) throw new Error("服务未返回清理截止时间，请指定日期后重新预览。");
  model.retentionPreview = {signature, cutoff: result.cutoff};
  $("retention-preview-cutoff").textContent = `删除时间早于 ${result.cutoff} 的适用记录（UTC，边界不包含）。`;
  const countLabels = {context_windows: "上下文窗口", grounded_memory: "来源检索索引", conversations: "对话记录", jobs: "直播任务", events: "直播事件", sessions: "直播会话", dynamic_memory_summaries: "运营记忆摘要"};
  describe($("retention-preview-counts"), Object.entries(result.counts || {}).map(([key, count]) => [countLabels[key] || key, count]));
  $("retention-preview-detail").textContent = JSON.stringify(result, null, 2);
  $("retention-preview-panel").hidden = false;
  $("retention-cleanup-state").textContent = "预览完成，尚未删除。请核对数量和范围后确认。";
});
$("retention-cleanup").onclick = () => retentionAction("cleanup", async () => {
  const preview = model.retentionPreview;
  if (!preview || !$("retention-confirm").checked || JSON.stringify(retentionRequest()) !== preview.signature) throw new Error("请先预览当前范围并明确确认清理。");
  const result = await api("/api/retention/cleanup", {method: "POST", body: {before: preview.cutoff, confirm: true}});
  invalidateRetentionPreview();
  $("retention-cleanup-state").textContent = `清理已完成，实际结果：${JSON.stringify(result.counts || result)}`;
  await loadRetention();
  if (model.page === "comments") await refreshComments();
});
$("character-image").onerror = () => { $("character-image").hidden = true; };
window.addEventListener("hashchange", changePage);
window.addEventListener("beforeunload", event => { if (model.characterDirty || model.draftDirty || model.providersDirty || model.dynamicPolicyDirty || model.dynamicPostDirty) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
setInterval(poll, 2500);
enterWorkspace().catch(error => { showLogin(); if (error.status !== 401) $("login-error").textContent = error.message; });
