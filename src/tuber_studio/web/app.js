"use strict";

import { $, node, notice, safeMessage } from "./modules/core.js";
import { api as baseApi } from "./modules/api.js";
import { initInferenceProvidersUI, publicSettings } from "./modules/providers.js";
import { initVoiceSettings, renderVoicePreview, voiceAvailability } from "./modules/voices.js";
import { MemeGallery, MemePicker, memePreview } from "./memes.js";
import { MessagesPanel } from "./messages.js";
import {
  initOperationsModule,
  updatePermissionCapsule as updateCapsuleModule,
  savePermissionMatrix as saveMatrixModule,
} from "./modules/operations.js?v=20261105.27";
import {
  renderPersona,
  initCharacterStudioUI,
} from "./modules/character-studio.js?v=20261105.27";
import {
  selectedThread,
  initCommentsModule,
} from "./modules/comments.js?v=20261105.26";
import {
  localDateTime,
  initDynamicsModule,
} from "./modules/dynamics.js?v=20261105.26";
import {
  initMaintenanceModule,
} from "./modules/maintenance.js?v=20261105.26";
import {
  renderLiveFeatures,
  renderControls,
  renderServiceStatusCapsule,
  initLiveModule,
} from "./modules/live.js?v=20261105.27";

const api = (path, options = {}) => baseApi(path, options, showLogin);
const updatePermissionCapsule = () => updateCapsuleModule(api, accountPanel);
const savePermissionMatrix = options => saveMatrixModule(api, accountPanel, options);

const model = {
  authenticated: false, status: null, page: "comments", characters: [], versions: [],
  characterId: null, character: null, characterDirty: false, assets: [], voices: [],
  settings: null, socket: null, polling: false, comments: [], selectedComments: new Set(),
  commentId: null, draftDirty: false, draftId: null, policy: null,
  commentPolicyDirty: false,
  importPreviewSignature: null, categorySaving: new Set(),
  modeSwitch: null, modeResult: null,
  threadContextPending: null,
  retention: null, retentionPreview: null, retentionDirty: false, retentionPending: null,
  dynamicPolicy: null, dynamicPolicyDirty: false, dynamicSaving: false, dynamicEvaluating: false,
  dynamicPosts: [], dynamicDecisions: [], dynamicMemories: [], dynamicPostId: null, dynamicPostDirty: false, dynamicDecisionId: null,
  dynamicPostPending: false, dynamicMemorySaving: new Set(),
  dynamicListsLoaded: false,
  dynamicMemorySummary: null,
  dynamicMemeCache: new Map(), dynamicMemePending: new Set(), dynamicMemeContext: null, dynamicMemePreviewKey: null,
};

let commentsModule = null;
let dynamicsModule = null;
let characterStudio = null;
let maintenanceModule = null;
let liveModule = null;

const refreshComments = async () => commentsModule?.refreshComments();
const refreshDynamics = async () => dynamicsModule?.refreshDynamics();
const refreshThreadContext = async () => commentsModule?.refreshThreadContext();
const refreshLive = async () => liveModule?.refreshLive();

const commentContinuation = new OperationContinuationView($("comment-continuation"), api, notice, refreshComments);
const dynamicContinuation = new OperationContinuationView($("dynamic-continuation"), api, notice, refreshDynamics);
const dynamicDecisionContinuation = new OperationContinuationView($("dynamic-decision-continuation"), api, notice, refreshDynamics);
const cachePanel = new CachePanel($("cache-panel"), api, notice);
const inferenceNodePanel = new InferenceNodePanel($("inference-node-panel"), api, async () => {
  model.modeResult = null;
  try {
    await refreshSettings();
    renderStatus(await api("/api/status"));
  } catch (error) {
    notice(`节点已配对，状态刷新失败：${error.message}`, true);
  }
});
setInterval(() => { if (model.authenticated) cachePanel.refresh(); }, 5000);
setInterval(() => { if (model.authenticated) inferenceNodePanel.refresh(); }, 10000);

const messagesPanel = new MessagesPanel($("messages-panel"), api, notice);
const automationPanel = new AutomationPanel($("automation-panel"), api, notice);
const dailyMemoryUI = new DailyMemoryUI({ container: $("daily-memory-card"), api, notify: notice });
const interactiveGamesUI = new InteractiveGamesUI({ notify: notice });
const memeGallery = new MemeGallery($("meme-gallery"), api, notice, () => {
  model.dynamicMemeCache.clear();
  messagesPanel.memeCache.clear();
});
const accountPanel = new BilibiliAccountUI({
  header: $("global-bilibili-account"),
  settings: $("account-settings"),
  api,
  notify: notice,
  onSessionRevoked: showLogin,
  onChange: () => {
    if (!model.authenticated) return;
    Promise.allSettled([
      commentsModule?.loadPolicy(),
      updatePermissionCapsule(),
      ...(model.page === "messages" ? [messagesPanel.refresh()] : [])
    ]).then(results => results.forEach(result => {
      if (result.status === "rejected") notice(result.reason.message, true);
    }));
  }
});

const dynamicMemePicker = new MemePicker(api, (id, asset) => dynamicsModule?.selectDynamicMeme(id, asset, model.dynamicMemeContext), {
  title: "选择动态配图",
  description: "只显示此动态所属角色已启用且可用的图片。最多附加1张，保留原正文；修改配图后需要重新审核。"
});

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
  checkpoint: "进度已保存", needs_material: "等待补充材料", deciding: "Agent 处理中", awaiting_review: "待审核", needs_reconciliation: "发送结果待核对", waiting: "等待下一步", abandoned: "已暂缓",
};

function label(value) { return names[value] || value || "—"; }
function display(value) { return value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value); }
function list(data) { return Array.isArray(data) ? data : data?.items || data?.results || []; }
function time(value) {
  if (!value) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString("zh-CN", { hour12: false });
}
function badge(value) {
  const good = ["completed", "approved", "published", "live", "ready", "copied"];
  const bad = ["failed", "error", "cancelled", "rejected", "degraded", "publish_uncertain", "publish_failed"];
  return node("span", label(value), `badge ${good.includes(value) ? "good" : bad.includes(value) ? "bad" : ""}`);
}
function setBadge(element, value) {
  const valueNode = badge(value);
  element.className = valueNode.className;
  element.textContent = valueNode.textContent;
}

function describe(element, entries) {
  if (!element) return;
  element.replaceChildren();
  entries.forEach(([key, value]) => element.append(node("dt", key), node("dd", display(value))));
}

function empty(tbody, colspan, text) {
  if (!tbody) return;
  tbody.replaceChildren();
  const row = node("tr");
  const cell = node("td", text, "empty-cell");
  cell.colSpan = colspan;
  row.append(cell);
  tbody.append(row);
}

function cell(row, value, small) {
  const element = node("td");
  element.append(value instanceof Node ? value : node("span", display(value)));
  if (small) element.append(node("small", small, "cell-sub"));
  row.append(element);
  return element;
}

async function busy(button, action) {
  if (!button) return action();
  const wasDisabled = button.disabled;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await action();
  } catch (error) {
    notice(error.message, true);
    return undefined;
  } finally {
    button.disabled = wasDisabled;
    button.removeAttribute("aria-busy");
    if (model.status) renderControls(model);
    if (model.status?.context) renderContext(model.status.context);
    if (button.id === "submit-import") button.disabled = !model.importPreviewSignature;
    if (button.id === "preview-submit") renderVoicePreview(model);
    if (["approve-draft", "reject-draft", "copy-draft", "publish-draft"].includes(button.id)) commentsModule?.renderDraft();
    if (["approve-dynamic", "reject-dynamic", "publish-dynamic"].includes(button.id)) dynamicsModule?.renderDynamicPost();
    if (["save-dynamic-policy", "stop-dynamics", "evaluate-dynamics"].includes(button.id)) dynamicsModule?.renderDynamicPolicy();
  }
}

function showLogin() {
  model.authenticated = false;
  $("workspace").hidden = true;
  $("login").hidden = false;
  accountPanel.deactivate();
  messagesPanel.closeImagePicker();
}

function applyWorkbenchSettings(settings) {
  model.settings = { ...model.settings, ...settings };
  inferenceProviders.sync();
  voiceSettings.sync();
  renderLiveFeatures(model);
  if (model.status) renderStatus(model.status);
  else renderVoicePreview(model);
  let overlayUrl;
  try { overlayUrl = new URL(model.settings.overlay_url || "/overlay", location.origin).href; }
  catch { overlayUrl = `${location.origin}/overlay`; }
  $("overlay-url").value = overlayUrl;
  $("open-overlay").href = overlayUrl;
}

async function refreshSettings() {
  applyWorkbenchSettings(await api("/api/settings"));
}

async function enterWorkspace() {
  voiceSettings.reset();
  await refreshSettings();
  await Promise.all([
    loadVoices(),
    loadAssets(),
    characterStudio?.loadCharacters(),
    accountPanel.activate(),
    api("/api/status").then(renderStatus)
  ]);
  model.authenticated = true;
  $("login").hidden = true;
  $("workspace").hidden = false;
  connectAdmin();
  await changePage();
}

function connectAdmin() {
  if (model.socket) model.socket.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  model.socket = new WebSocket(`${protocol}//${location.host}/ws/admin`);
  model.socket.onmessage = async event => {
    const message = JSON.parse(event.data);
    if (message.type === "job_updated" || message.type === "event_queued") {
      if (model.page === "live") await refreshLive();
    }
    if (message.type === "context_updated" && message.context) renderContext(message.context);
    if (message.type === "status") renderStatus(message.data);
  };
}

function renderStatus(status) {
  model.status = status;
  renderServiceStatusCapsule(status, model.settings);
  renderControls(model);
  renderPersona(model);
  setBadge($("live-state"), status.state);
  const job = status.current_job;
  setBadge($("live-current-status"), job?.status || "ready");
  $("live-current-text").textContent = job?.text || (job ? "正在处理当前事件…" : "等待事件。");
  $("live-current-detail").textContent = job?.error || status.error || "";
  $("last-played").textContent = status.last_played?.text || "暂无已确认播放记录。";
  if (status.context) renderContext(status.context);
  renderVoicePreview(model);
  if ($("character-version")) {
    $("character-version").title = `当前运行角色：${status.character?.name || "—"} v${status.character?.version ?? "—"}`;
  }
}

function tokenCount(value) {
  return typeof value === "number" ? `${value.toLocaleString("en-US")} tokens` : "—";
}
function occupancyText(context) {
  if (!context?.configured_tokens || context.active_tokens == null) return "—";
  return `${Math.round((context.active_tokens / context.configured_tokens) * 100)}%`;
}
function renderContext(context) {
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

async function loadAssets() {
  model.assets = list(await api("/api/assets"));
  const grid = $("asset-grid");
  if (!grid) return;
  grid.replaceChildren();
  if (!model.assets.length) {
    grid.append(node("p", "暂无已配置素材。", "muted"));
    return;
  }
  model.assets.forEach(asset => {
    const card = node("article", null, "asset-card");
    const preview = node("div", null, "asset-preview");
    const url = asset.url || asset.path || "";
    const audioAsset = /\.(wav|mp3|ogg|flac)(?:\?|$)/i.test(url) || asset.kind === "audio" || asset.type === "audio";
    if (audioAsset) {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = url;
      preview.append(audio);
    } else {
      const image = document.createElement("img");
      image.src = url;
      image.alt = asset.tag || asset.event_tag || asset.id;
      image.loading = "lazy";
      image.onerror = () => {
        image.hidden = true;
        preview.append(node("span", "素材不可用", "muted"));
      };
      preview.append(image);
    }
    const meta = node("div", null, "asset-meta");
    meta.append(node("h3", asset.id), badge(asset.tag || asset.event_tag || asset.kind || "素材"), node("p", `${asset.character_id || "通用"} · ${asset.license_status || "许可待确认"}`, "muted"));
    const source = asset.source || asset.source_url;
    if (source) {
      const p = node("p");
      if (/^https?:\/\//.test(source)) {
        const link = node("a", "查看来源");
        link.href = source;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        p.append(link);
      } else {
        p.textContent = display(source);
      }
      meta.append(p);
    }
    card.append(preview, meta);
    grid.append(card);
  });
}

async function loadVoices() {
  const select = $("preview-voice");
  if (!select) return;
  const previous = select.value;
  model.voices = list(await api("/api/voices"));
  const tbody = $("voices");
  tbody?.replaceChildren();
  select.replaceChildren();
  if (!model.voices.length) {
    if (tbody) empty(tbody, 2, "暂无可用音色。");
    renderVoicePreview(model);
    return;
  }
  model.voices.forEach(voice => {
    if (tbody) {
      const row = node("tr");
      cell(row, voice.name || voice.id);
      cell(row, voice.license_status || "待确认");
      tbody.append(row);
    }
    const option = node("option", voice.name || voice.id);
    option.value = voice.id;
    select.append(option);
  });
  if (model.voices.some(voice => voice.id === previous)) select.value = previous;
  renderVoicePreview(model, !previous);
}

function dateTime(value) {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? display(value) : date.toISOString().replace("T", " ").slice(0, 19);
}

async function compactContext(scope, videoId, threadId) {
  if (scope === "comments" && (!videoId || !threadId)) throw new Error("请先选择要压缩的视频与单个楼ID；不同楼上下文不会合并。");
  const payload = { scope, ...(scope === "comments" ? { video_id: videoId, thread_id: threadId } : {}) };
  if ($("compaction-result")) $("compaction-result").textContent = "正在使用当前文本模型压缩上下文…";
  notice("上下文压缩已开始，请等待当前文本模型返回。");
  try {
    const result = await api("/api/context/compact", { method: "POST", body: payload });
    const message = result.message || result.detail || (result.compacted === false ? "没有可压缩的历史。" : "压缩请求已完成，请查看实际上下文占用与压缩次数。");
    if ($("compaction-result")) $("compaction-result").textContent = message;
    notice(message);
    renderStatus(await api("/api/status"));
    if (scope === "comments") await refreshThreadContext();
  } catch (error) {
    if ($("compaction-result")) $("compaction-result").textContent = `压缩失败：${error.message}`;
    throw error;
  }
}

async function changePage() {
  const requested = location.hash.slice(1) || "comments";
  const anchors = { "account-settings": "accounts", "live-game-panel": "live", "interactive-rules-card": "live" };
  const page = Object.hasOwn(anchors, requested) ? anchors[requested] : requested === "automation" ? "comments" : requested;
  const pages = {
    comments: ["operations", "评论"], messages: ["operations", "私信"], dynamics: ["operations", "动态"],
    live: ["live", "直播"],
    characters: ["character", "角色"], voices: ["character", "语音"], assets: ["character", "素材"],
    settings: ["settings", "推理"], accounts: ["settings", "账号与权限"], overview: ["settings", "维护"],
  };
  model.page = Object.hasOwn(pages, page) ? page : "comments";
  const [workspace, title] = pages[model.page];
  document.querySelectorAll("[data-section]").forEach(section => {
    section.hidden = section.dataset.section !== model.page;
  });
  document.querySelectorAll("[data-workspace-tabs]").forEach(tabs => {
    tabs.hidden = tabs.dataset.workspaceTabs !== workspace;
  });
  document.querySelectorAll("[data-page], [data-workspace]").forEach(link => {
    const active = link.dataset.page === model.page || link.dataset.workspace === workspace;
    link.classList.toggle("selected", active);
    if (active) link.setAttribute("aria-current", link.dataset.workspace ? "location" : "page");
    else link.removeAttribute("aria-current");
  });
  document.title = `${title} · Tuber Studio`;
  if (Object.hasOwn(anchors, requested)) $(requested).scrollIntoView();

  if (!model.authenticated) return;
  try {
    if (model.page === "comments") {
      await Promise.allSettled([refreshComments(), automationPanel.refresh(), updatePermissionCapsule()]);
    } else if (model.page === "messages") {
      await messagesPanel.refresh();
    } else if (model.page === "dynamics") {
      await refreshDynamics();
    } else if (model.page === "assets") {
      await memeGallery.refresh();
    } else if (model.page === "live") {
      await Promise.allSettled([refreshLive(), dailyMemoryUI.refresh(), interactiveGamesUI.refresh()]);
    } else if (model.page === "voices") {
      await updateVoiceCacheUI();
    } else if (model.page === "characters") {
      renderPersona(model);
    } else if (model.page === "overview") {
      await Promise.allSettled([maintenanceModule?.loadRetention(), cachePanel.refresh(), updateVoiceCacheUI()]);
    } else if (model.page === "accounts") {
      await Promise.allSettled([accountPanel.settingsOpened(), updatePermissionCapsule()]);
    } else if (model.page === "settings") {
      await Promise.all([inferenceNodePanel.refresh(), inferenceProviders.refresh()]);
    }
  } catch (error) {
    notice(error.message, true);
  }
}

async function updateVoiceCacheUI() {
  const statsEl = $("voice-cache-stats");
  if (!statsEl) return;
  try {
    const data = await api("/api/cache");
    const speech = data.categories?.speech;
    if (speech) {
      statsEl.textContent = `已用 ${cachePanel.bytes(speech.bytes)} / 上限 ${cachePanel.bytes(speech.limit_bytes)} · 可回收 ${cachePanel.bytes(speech.reclaimable_bytes)} · 受保护 ${cachePanel.bytes(speech.protected_bytes)}${speech.writing ? " · 正在写入" : ""}`;
    }
  } catch (_) {}
}

async function poll() {
  if (!model.authenticated || model.polling || document.hidden) return;
  model.polling = true;
  try {
    renderStatus(await api("/api/status"));
    if (model.page === "live") await refreshLive();
    if (model.page === "comments") await refreshComments();
  } catch (_) {
    // Non-blocking background poll
  } finally {
    model.polling = false;
  }
}

// -------------------------------------------------------------
// Module Wires & Initialization
// -------------------------------------------------------------

characterStudio = initCharacterStudioUI(model, {
  api,
  busy,
  notice,
  node,
  list,
  memeGallery,
  renderStatus,
});

commentsModule = initCommentsModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  list,
  label,
  badge,
  empty,
  tokenCount,
  occupancyText,
  commentContinuation,
  setBadge,
  describe,
});

dynamicsModule = initDynamicsModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  list,
  label,
  badge,
  empty,
  describe,
  display,
  setBadge,
  tokenCount,
  safeMessage,
  publicSettings,
  memePreview,
  dynamicContinuation,
  dynamicDecisionContinuation,
  dynamicMemePicker,
});

maintenanceModule = initMaintenanceModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  list,
  describe,
  dateTime,
  refreshCommentsFn: refreshComments,
});

liveModule = initLiveModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  time,
  label,
  badge,
  empty,
  renderStatusFn: renderStatus,
  renderVoicePreviewFn: () => renderVoicePreview(model),
  loadSettingsFn: refreshSettings,
});

initOperationsModule(api, accountPanel, automationPanel);
const inferenceProviders = initInferenceProvidersUI(model, {
  api,
  busy,
  apply: applyWorkbenchSettings,
  refreshStatus: () => Promise.allSettled([api("/api/status").then(renderStatus), loadVoices()]),
});
const voiceSettings = initVoiceSettings(model, api, busy, applyWorkbenchSettings);

// Form submits & remaining listeners
$("login-form")?.addEventListener("submit", event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const token = $("token")?.value.trim();
    if (!token) return;
    await api("/api/auth", { method: "POST", body: { token } });
    $("token").value = "";
    localStorage.removeItem("tuber_token");
    await enterWorkspace();
  });
});

$("clone-form")?.addEventListener("submit", event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    if (!model.characterId) throw new Error("请先选择要复制的角色");
    const id = $("clone-id")?.value.trim();
    const name = $("clone-name")?.value.trim();
    await api(`/api/characters/${encodeURIComponent(model.characterId)}/clone`, { method: "POST", body: { id, name } });
    await characterStudio?.loadCharacters();
    await characterStudio?.selectCharacter(id);
    notice("已创建新角色，可以编辑人格、风格和音色。");
  });
});

$("preview-voice")?.addEventListener("change", () => renderVoicePreview(model));
$("preview-form")?.addEventListener("submit", event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const voice = model.voices.find(item => item.id === $("preview-voice")?.value);
    const unavailable = voiceAvailability(voice, model);
    if (unavailable) throw new Error(unavailable);
    if ($("preview-state")) $("preview-state").textContent = "等待合成…";
    $("preview-audio")?.pause();
    try {
      const result = await api("/api/tts/preview", {
        method: "POST",
        body: { text: $("preview-text")?.value, voice_id: $("preview-voice")?.value }
      });
      if ($("preview-audio")) {
        $("preview-audio").src = result.audio_ref;
        $("preview-audio").hidden = false;
      }
      const recorded = voice?.provider === "prerecorded";
      const actualText = recorded && result.text ? ` 实际录音台词：${result.text}` : "";
      if ($("preview-state")) {
        $("preview-state").textContent = safeMessage(`${recorded ? "预录已准备" : "合成完成"}${result.duration != null ? ` · ${Number(result.duration).toFixed(2)} 秒` : ""}，点击播放试听。${actualText}${result.warning ? ` ${result.warning}` : ""}`);
      }
      await updateVoiceCacheUI();
    } catch (error) {
      if ($("preview-state")) $("preview-state").textContent = `合成失败：${safeMessage(error.message)}`;
      throw error;
    }
  });
});

if ($("voice-cache-clear")) {
  $("voice-cache-clear").onclick = () => busy($("voice-cache-clear"), async () => {
    const res = await api("/api/cache/speech/clear", { method: "POST" });
    const stateEl = $("voice-cache-state");
    if (stateEl) {
      stateEl.textContent = `已清理 ${res.result?.removed_files || 0} 个音频文件，释放 ${cachePanel.bytes(res.result?.freed_bytes || 0)} 空间。`;
    }
    await updateVoiceCacheUI();
    await cachePanel.refresh();
  });
}

$("copy-overlay")?.addEventListener("click", () => busy($("copy-overlay"), async () => {
  await navigator.clipboard.writeText($("overlay-url")?.value || "");
  notice("播放器链接已复制。");
}));

$("compact-live")?.addEventListener("click", () => busy($("compact-live"), () => compactContext("live")));
$("compact-comments-settings")?.addEventListener("click", () => busy($("compact-comments-settings"), () => compactContext("comments", $("compact-video-id")?.value.trim(), $("compact-thread-id")?.value.trim())));
$("compact-comments")?.addEventListener("click", () => busy($("compact-comments"), () => {
  const target = selectedThread(model);
  return compactContext("comments", target?.videoId, target?.threadId);
}));

$("rebuild-comments")?.addEventListener("click", () => busy($("rebuild-comments"), async () => {
  const target = selectedThread(model);
  if (!target) throw new Error("请先选择评论或筛选视频与单个楼ID，再重建该楼上下文。");
  if ($("thread-rebuild-state")) $("thread-rebuild-state").textContent = `正在使用本地现存原文重建 ${target.videoId} / ${target.threadId}…`;
  try {
    const result = await api("/api/context/rebuild", {
      method: "POST",
      body: { scope: "comments", video_id: target.videoId, thread_id: target.threadId }
    });
    let message = result.message || result.detail || (result.rebuilt === false ? "本次未重建上下文，请检查该楼现存原文。" : `楼 ${target.videoId} / ${target.threadId} 上下文已重建，请查看实际占用。`);
    if (result.prefilled === false) message += " 诊断：未执行真实模型 prefill，不能据此认定模型已预热。";
    else if (result.prefilled === true) message += ` 模型 prefill 已完成${result.input_tokens != null ? `，输入 ${result.input_tokens} tokens` : ""}。`;
    if ($("thread-rebuild-state")) $("thread-rebuild-state").textContent = message;
    notice(message);
    await refreshThreadContext();
  } catch (error) {
    if ($("thread-rebuild-state")) $("thread-rebuild-state").textContent = `重建失败：${error.message}`;
    throw error;
  }
}));

$("character-image")?.addEventListener("error", () => {
  const img = $("character-image");
  if (img) img.hidden = true;
});

if ($("refresh-operations")) {
  $("refresh-operations").onclick = async () => {
    await Promise.allSettled([automationPanel.refresh(), updatePermissionCapsule()]);
    notice("社区运营数据已刷新。");
  };
}

window.addEventListener("hashchange", changePage);
window.addEventListener("beforeunload", event => {
  if (model.characterDirty || model.draftDirty || model.commentPolicyDirty || inferenceProviders.dirty || voiceSettings.dirty || model.dynamicPolicyDirty || model.dynamicPostDirty || automationPanel.dirty || automationPanel.draftDirty || messagesPanel.hasUnsaved()) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});
setInterval(poll, 2500);

// Auto-login or show workspace
enterWorkspace().catch(error => {
  showLogin();
  if (error.status !== 401 && $("login-error")) $("login-error").textContent = error.message;
});
