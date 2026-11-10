"use strict";

import { $, node, notice, safeMessage } from "./modules/core.js?v=20261105.32";
import { api as baseApi } from "./modules/api.js";
import { initOperatorChat } from "./modules/operator-chat.js";
import { commentQueueViews, initCommentQueue } from "./modules/comment-queue.js?v=20261107.21";
import { initDiscoveryPanel, initIssueLog } from "./modules/discovery-panel.js?v=20261107.23";
import { initOperationsActivity } from "./modules/operations-activity.js?v=20261110.01";
import { initInferenceProvidersUI, publicSettings } from "./modules/providers.js?v=20261107.12";
import { initVoiceSettings, renderVoicePreview, voiceAvailability } from "./modules/voices.js";
import { MemeGallery, MemePicker, memePreview } from "./memes.js";
import { MessagesPanel } from "./messages.js?v=20261107.20";
import { UserMemoryEditor } from "./user-memory.js";
import {
  initOperationsModule,
  updatePermissionCapsule as updateCapsuleModule,
} from "./modules/operations.js?v=20261110.01";
import {
  renderPersona,
  initCharacterStudioUI,
} from "./modules/character-studio.js?v=20261107.12";
import {
  selectedThread,
  initCommentsModule,
} from "./modules/comments.js?v=20261107.17";
import {
  initDynamicsModule,
} from "./modules/dynamics.js?v=20261107.17";
import {
  initMaintenanceModule,
} from "./modules/maintenance.js?v=20261107.12";
import {
  renderControls,
  initLiveModule,
} from "./modules/live.js?v=20261111.01";

import {
  badge, cell, dateTime, describe, display, empty, label, list, occupancyText, setBadge, tokenCount,
} from "./modules/workspace-display.js?v=20261107.23";
import { initWorkspaceNavigation } from "./modules/workspace-navigation.js?v=20261111.01";
import { refreshWorkspaceClock, renderContext, renderWorkspaceStatus } from "./modules/workspace-status.js?v=20261111.01";

const api = async (path, options = {}) => {
  const source = location.hash || "#chat";
  try {
    return await baseApi(path, options, showLogin);
  } catch (error) {
    if (model.authenticated && error.status !== 401) issueLog.record(error.message, source);
    throw error;
  }
};
const updatePermissionCapsule = options => updateCapsuleModule(api, accountPanel, options);

const model = {
  authenticated: false, status: null, page: "chat", characters: [], versions: [],
  characterId: null, character: null, characterDirty: false, assets: [], voices: [],
  settings: null, socket: null, polling: false, comments: [], selectedComments: new Set(),
  commentId: null, policy: null,
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

const refreshComments = async () => commentsModule?.refreshComments();
const refreshDynamics = async () => dynamicsModule?.refreshDynamics();
const refreshThreadContext = async () => commentsModule?.refreshThreadContext();

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
setInterval(() => { if (model.authenticated && !document.hidden && model.page === "overview") cachePanel.refresh(); }, 5000);
setInterval(() => { if (model.authenticated && !document.hidden && model.page === "settings") inferenceNodePanel.refresh(); }, 10000);

const userMemoryEditor = new UserMemoryEditor(api);
const messagesPanel = new MessagesPanel($("messages-panel"), api, notice, scope => {
  const characterId = model.status?.character?.id;
  if (characterId) userMemoryEditor.open({ ...scope, characterId });
});
messagesPanel.mountSettings($("message-policy-settings"));
const automationPanel = new AutomationPanel($("automation-panel"), api, notice);
const issueLog = initIssueLog(api, () => model.page);
const operatorChat = initOperatorChat((path, options) => baseApi(path, options, showLogin));
document.addEventListener("workspace-issue", event => {
  event.preventDefault();
  issueLog.record(event.detail);
});
const discoveryPanel = initDiscoveryPanel(api);
const refreshOperationsActivity = initOperationsActivity(api);
const commentQueue = initCommentQueue(api, refreshComments, discoveryPanel.status, {
  onDraftUpdated: (draft, key) => commentsModule?.draftUpdated(draft, key),
  onSelectionCleared: () => commentsModule?.clearCurrent(),
});
const dailyMemoryUI = new DailyMemoryUI({ container: $("daily-memory-card"), api, notify: notice });
const interactiveGamesUI = new InteractiveGamesUI({ api, notify: notice });
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

$("open-comment-memory").onclick = () => {
  const comment = model.comments?.find(item => item.id === model.commentId);
  const accountId = comment?.relationship_account_id;
  const characterId = model.status?.character?.id;
  if (!accountId || !characterId || !comment?.platform_user_id) return;
  userMemoryEditor.open({ platform: comment.platform, userId: comment.platform_user_id, accountId,
    characterId, name: comment.display_name, onChange: refreshComments });
};

const dynamicMemePicker = new MemePicker(api, (id, asset) => dynamicsModule?.selectDynamicMeme(id, asset, model.dynamicMemeContext), {
  title: "选择动态配图",
  description: "只显示此动态所属角色已启用且可用的图片。最多附加1张，保留原正文；修改配图后需要重新审核。"
});

async function showCommentSources(videoId) {
  $("filter-video").value = videoId;
  for (const id of ["filter-thread", "filter-category", "filter-status"]) $(id).value = "";
  model.selectedComments.clear();
  model.commentId = null;
  model.commentSourcePage = 0;
  await commentQueue.open("sources");
  $("comment-import").open = false;
  $("queue-records").scrollIntoView({ block: "start", behavior: "smooth" });
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
    if (button.id === "preview-submit") renderVoicePreview(model);
    if (["approve-dynamic", "reject-dynamic", "publish-dynamic"].includes(button.id)) dynamicsModule?.renderDynamicPost();
    if (["save-dynamic-policy", "evaluate-dynamics"].includes(button.id)) dynamicsModule?.renderDynamicPolicy();
  }
}

function showLogin() {
  model.authenticated = false;
  model.socket?.close();
  $("workspace").hidden = true;
  $("login").hidden = false;
  accountPanel.deactivate();
  messagesPanel.closeImagePicker();
  userMemoryEditor.close();
  operatorChat.reset();
}

function applyWorkbenchSettings(settings) {
  model.settings = { ...model.settings, ...settings };
  const localAccess = model.settings.local_access === true;
  $("logout").hidden = localAccess;
  $("logout-sidebar").hidden = localAccess;
  accountPanel.setLocalAccess(localAccess);
  inferenceProviders.sync();
  voiceSettings.sync();
  if (model.status) renderStatus(model.status);
  else renderVoicePreview(model);
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
    accountPanel.activate(),
    api("/api/status").then(async status => {
      renderStatus(status);
      await characterStudio?.loadCharacters();
    })
  ]);
  model.authenticated = true;
  $("login").hidden = true;
  $("workspace").hidden = false;
  connectAdmin();
  await changePage();
  if (model.page !== "automation") await Promise.all([discoveryPanel.refresh(), updatePermissionCapsule({ background: true })]);
  void issueLog.refresh();
}

function connectAdmin() {
  if (model.socket) model.socket.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  model.socket = new WebSocket(`${protocol}//${location.host}/ws/admin`);
  model.socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === "context_updated" && message.context) renderContext(message.context);
    if (message.type === "status") renderStatus(message.data);
  };
}

function renderStatus(status) {
  renderWorkspaceStatus(model, status);
}
setInterval(refreshWorkspaceClock, 1000);

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

async function loadWorkspacePage({ anchor, routeKind, routeId, taskId }) {
  if (model.page !== "comments") commentQueue.leave();
  if (!model.authenticated) return;
  if (model.page === "chat") {
    await operatorChat.refresh();
  } else if (model.page === "comments") {
    if (routeKind === "activity") { location.hash = "automation"; return; }
    const queueView = commentQueueViews.includes(routeKind) ? routeKind : null;
    const [channel, identity] = queueView ? [routeId, taskId] : [routeKind, routeId];
    const validKey = ["comments", "browsing", "jobs"].includes(channel) && /^[\w-]+$/.test(identity || "");
    if (routeKind === "review" && routeId === "task" && /^[\w-]+$/.test(taskId || "")) await commentQueue.trackGeneration(taskId);
    else await commentQueue.open(anchor || routeKind === "sources" ? "sources" : queueView, validKey ? `${channel}/${identity}` : null);
  } else if (model.page === "automation") {
    if (routeKind === "video") { history.replaceState(null, "", "#automation"); refreshOperationsActivity.setKind("videos"); }
    await Promise.allSettled([updatePermissionCapsule(), discoveryPanel.refresh(), refreshOperationsActivity()]);
  } else if (model.page === "automation-records") {
    const round = routeKind === "round" && /^[\w-]+$/.test(routeId || "") ? routeId : null;
    await Promise.allSettled([updatePermissionCapsule(), automationPanel.openRound(round, /^[\w-]+$/.test(taskId || "") ? taskId : null), issueLog.refresh()]);
  } else if (model.page === "automation-settings") {
    await Promise.allSettled([discoveryPanel.refresh(), updatePermissionCapsule(), automationPanel.loadRecovery(), dynamicsModule?.refreshPolicy(), messagesPanel.refresh()]);
  } else if (model.page === "issues") {
    issueLog.render();
    await issueLog.refresh();
  } else if (model.page === "messages") {
    await messagesPanel.refresh();
  } else if (model.page === "dynamics") {
    await refreshDynamics();
  } else if (model.page === "assets") {
    await memeGallery.refresh();
  } else if (["live", "live-scene", "live-music", "live-settings"].includes(model.page)) {
    const frame = document.querySelector(`[data-section="${model.page}"] iframe`);
    if (!frame.hasAttribute("src")) frame.src = frame.dataset.src;
  } else if (model.page === "live-interaction") {
    await interactiveGamesUI.refresh();
  } else if (model.page === "characters") {
    renderPersona(model);
    await dailyMemoryUI.refresh();
  } else if (model.page === "overview") {
    await Promise.allSettled([cachePanel.refresh(), maintenanceModule?.loadRetention()]);
  } else if (model.page === "accounts") {
    await Promise.allSettled([accountPanel.settingsOpened(), updatePermissionCapsule(), commentsModule?.loadPolicy()]);
  } else if (model.page === "settings") {
    await Promise.allSettled([inferenceNodePanel.refresh(), inferenceProviders.refresh()]);
  }
}

async function poll() {
  if (!model.authenticated || model.polling || document.hidden) return;
  model.polling = true;
  try {
    const updates = [api("/api/status").then(renderStatus), discoveryPanel.refresh(), updatePermissionCapsule({ background: true })];
    if (model.page === "automation") updates.push(refreshOperationsActivity());
    if (model.page === "comments") updates.push(commentQueue.refresh());
    if (model.page === "messages") updates.push(messagesPanel.refresh({ background: true }));
    if (model.page === "dynamics") updates.push(dynamicsModule?.refreshDynamics({ background: true }));
    if (model.page === "automation-records") updates.push(automationPanel.refresh({ background: true }));
    if (model.page === "issues" || model.page === "automation-records") updates.push(issueLog.refresh());
    await Promise.allSettled(updates);
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
  onScopeChanged: showCommentSources,
  onDraftsRequested: (taskId, ids) => commentQueue.trackGeneration(taskId, ids),
  onCommentSelected: comment => commentQueue.showComment(comment),
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
  onCommentsImported: showCommentSources,
});

initLiveModule(model, {
  api,
  notice,
  renderStatusFn: renderStatus,
  renderVoicePreviewFn: () => renderVoicePreview(model),
  loadSettingsFn: refreshSettings,
});

initOperationsModule(api, accountPanel);
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
    $("login-error").textContent = "";
    try {
      await api("/api/auth", { method: "POST", body: { token } });
      $("token").value = "";
      localStorage.removeItem("tuber_token");
      await enterWorkspace();
    } catch (error) {
      $("login-error").textContent = error.message;
    }
  });
});

$("logout").addEventListener("click", event => busy(event.currentTarget, async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
}));

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
    } catch (error) {
      if ($("preview-state")) $("preview-state").textContent = `合成失败：${safeMessage(error.message)}`;
      throw error;
    }
  });
});


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


if ($("refresh-operations")) {
  $("refresh-operations").onclick = async () => {
    await Promise.allSettled([updatePermissionCapsule(), discoveryPanel.refresh()]);
    notice("运营状态已刷新。");
  };
}

const changePage = initWorkspaceNavigation(model, loadWorkspacePage);
window.addEventListener("beforeunload", event => {
  if (commentQueue.hasUnsaved() || model.characterDirty || inferenceProviders.dirty || inferenceNodePanel.dirty || voiceSettings.dirty || interactiveGamesUI.rulesDirty || model.dynamicPolicyDirty || model.dynamicPostDirty || dynamicsModule?.hasUnsaved() || messagesPanel.hasUnsaved()) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});
setInterval(poll, 2500);
setInterval(() => {
  if (model.authenticated && !document.hidden && !["issues", "automation-records"].includes(model.page)) {
    void issueLog.refresh();
  }
}, 10000);

// Auto-login or show workspace
enterWorkspace().catch(error => {
  showLogin();
  if (error.status !== 401 && $("login-error")) $("login-error").textContent = error.message;
});
