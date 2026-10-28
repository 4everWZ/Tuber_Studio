"use strict";

const $ = id => document.getElementById(id);
const model = {
  authenticated: false, status: null, page: "comments", characters: [], versions: [],
  characterId: null, character: null, characterDirty: false, assets: [], voices: [],
  settings: null, socket: null, polling: false, comments: [], selectedComments: new Set(),
  commentId: null, draftDirty: false, draftId: null, policy: null,
  importPreviewSignature: null, categorySaving: new Set(),
};
const names = {
  stopped: "已停止", live: "运行中", paused: "已暂停", degraded: "降级运行", ready: "就绪",
  connecting: "连接中", error: "错误", queued: "排队中", generating: "生成台词", synthesizing: "合成语音",
  ready_to_play: "等待播放器", playing: "播放中", completed: "已完成", failed: "失败", cancelled: "已取消",
  expired: "已过期", filtered: "已过滤", duplicate: "重复事件", dropped: "已丢弃", selected: "已选中",
  received: "已收到", skipped: "已跳过", pending: "等待中", running: "处理中", pending_review: "待审核",
  approved: "已审核", rejected: "已拒绝", copied: "已复制", published: "已发布", publishing: "发布中",
  publish_uncertain: "发布结果待核对", publish_failed: "发布失败", question: "问题",
  suggestion: "建议 / 反馈", interaction: "适合互动", spam: "垃圾", general: "一般评论", chat: "弹幕",
  gift: "礼物", song: "点歌", sfx: "预录音效", disconnect: "断线", mock: "离线演示", llama_cpp: "llama.cpp",
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
  const element = $("notice"); element.textContent = message; element.hidden = !message;
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
    if (["approve-draft", "reject-draft", "copy-draft", "publish-draft"].includes(button.id)) renderDraft();
  }
}
function showLogin() {
  model.authenticated = false; $("workspace").hidden = true; $("login").hidden = false;
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
    button.disabled = action === "start" ? status.state !== "stopped" :
      action === "pause" ? !["live", "degraded"].includes(status.state) :
      action === "resume" ? status.state !== "paused" :
      action === "stop" ? status.state === "stopped" :
      action === "mute" ? !!status.muted : action === "unmute" ? !status.muted :
      action === "skip" ? !status.current_job : action === "clear" ? !status.queue_length : false;
  });
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
  $("current-detail").textContent = job ? `任务 ${job.id} · ${job.emotion || "—"}` : status.state === "stopped" ? "启动 Mock 直播，在调试台注入事件。" : "等待下一条可回应事件。";
  describe($("live-current-detail"), job ? [["任务 ID", job.id], ["源事件", job.event_id], ["角色版本", `${job.character_id} · v${job.character_version}`], ["LLM / TTS", `${ms(job.llm_ms)} / ${ms(job.tts_ms)}`], ["错误", job.error || "无"]] : [["队列长度", status.queue_length ?? 0], ["会话", status.session_id || "未启动"]]);
  const last = status.last_played;
  $("last-played").textContent = last ? typeof last === "string" ? last : `${last.text || last.spoken_text || ""}${last.completed_at ? ` · ${time(last.completed_at)}` : ""}` : "暂无已确认播放记录。";
  const providers = status.providers || {};
  describe($("provider-summary"), [["平台", "Mock · 真实直播接入后置"], ["LLM", display(providers.llm)], ["TTS", display(providers.tts)], ...(status.inference ? [["推理资源", inferenceLabel(status.inference)]] : []), ["播放器", status.overlay_connected ? "已连接" : "未连接"], ["会话", status.session_id || "未启动"]]);
  renderCharacterImage(); renderControls();
  renderContext(status.context);
  renderInference(status.inference);
}
function inferenceLabel(inference) {
  const models = {llm2b: "MiniCPM 2B", "2b": "MiniCPM 2B", llm1b: "MiniCPM 1B", "1b": "MiniCPM 1B", tts: "语音模型"};
  const prefix = inference.mode === "exclusive" ? "GPU 互斥 · " : "";
  if (inference.switching_to) return `${prefix}正在加载 ${models[inference.switching_to] || inference.switching_to}…`;
  if (inference.error) return `${prefix}资源切换失败`;
  if (["loading", "switching", "starting"].includes(inference.state)) return `${prefix}模型加载中…`;
  const active = inference.active ?? inference.active_provider;
  return active ? `${prefix}${models[active] || active} 驻留` : `${prefix}模型已休眠`;
}
function renderInference(inference) {
  $("inference-state").hidden = !inference;
  if (!inference) { $("inference-error").hidden = true; return; }
  $("inference-state").textContent = inferenceLabel(inference);
  $("inference-state").classList.toggle("bad", !!inference.error);
  $("inference-state").title = inference.switching_to ? "资源切换期间任务等待模型加载。上下文容量验证与模型驻留状态分别记录。" : "驻留或休眠表示模型资源状态，不改变已验证的上下文容量。";
  $("inference-error").hidden = !inference.error;
  $("inference-error").textContent = inference.error ? `推理资源切换失败：${inference.error}` : "";
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
  const verified = !!context.verified && !unavailable;
  const rounds = `累计 ${context.compact_count ?? 0} 轮 · 最近 ${context.compaction_passes_last ?? 0} 轮`;
  const concise = verified ? `${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)} · ${occupancyText(context)} · ${rounds}` : unavailable ? "上下文配置不可用" : "上下文能力待验证";
  $("context-state").textContent = concise; $("context-state").classList.toggle("bad", unavailable);
  const error = context.profile_error || (mismatch ? `上下文配置未满足：目标 ${tokenCount(context.configured_tokens)}，实际服务 ${tokenCount(context.server_tokens)}，模型原生 ${tokenCount(context.native_tokens)}。` : "");
  const errors = [error, context.last_compaction_error ? `最近压缩失败：${context.last_compaction_error}` : ""].filter(Boolean);
  $("context-error").hidden = !errors.length; $("context-error").textContent = errors.join(" ");
  const warning = context.last_compaction_warning ? `压缩提示：${context.last_compaction_warning}` : "";
  $("context-warning").hidden = !warning;
  if ($("context-warning").textContent !== warning) $("context-warning").textContent = warning;
  $("comment-context-state").textContent = verified ? `真实完整输入占用 ${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)}（${occupancyText(context)}）；压缩${rounds}。` : error || `上下文目标 ${tokenCount(context.configured_tokens)}，实际服务 ${tokenCount(context.server_tokens)}，尚未验证。`;
  $("context-verified").textContent = verified ? "实际服务已验证" : unavailable ? "配置不可用" : "待验证";
  $("context-verified").className = `badge ${verified ? "good" : unavailable ? "bad" : ""}`;
  describe($("context-details"), [["配置目标", tokenCount(context.configured_tokens)], ["实际服务容量", tokenCount(context.server_tokens)], ["模型原生容量", tokenCount(context.native_tokens)], ["完整输入占用", `${tokenCount(context.active_tokens ?? 0)}（${occupancyText(context)}）`], ["累计已安装压缩轮数", context.compact_count ?? 0], ["最近操作尝试轮数", context.compaction_passes_last ?? 0], ["二轮触发占用", `${context.secondary_threshold_percent ?? 40}%`], ["最近压缩提示", context.last_compaction_warning || "无"], ["最近压缩错误", context.last_compaction_error || "无"]]);
  $("context-profile-note").textContent = error || "模型原生能力、运行服务容量及配置目标分别显示；修改配置后需重启并重新验证。";
  const generate = $("generate-drafts");
  if (generate.getAttribute("aria-busy") !== "true") generate.disabled = !verified;
  generate.title = verified ? "" : error || "等待实际上下文能力验证";
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
  model.voices = list(await api("/api/voices")); const tbody = $("voices"); const select = $("preview-voice");
  tbody.replaceChildren(); select.replaceChildren();
  if (!model.voices.length) { empty(tbody, 4, "暂无可用音色。配置声音 Provider 后刷新。"); return; }
  model.voices.forEach(voice => {
    const row = node("tr"); cell(row, voice.name || voice.id, voice.id); cell(row, voice.provider || voice.kind || "—", voice.weights_version || voice.version || "");
    cell(row, voice.reference_asset || voice.reference_audio || voice.params?.ref_audio_path || "—"); cell(row, voice.license_status || "待确认", voice.source || voice.source_url || ""); tbody.append(row);
    const option = node("option", voice.name || voice.id); option.value = voice.id; select.append(option);
  });
}
async function loadSettings() {
  model.settings = await api("/api/settings"); $("settings-json").textContent = JSON.stringify(model.settings, null, 2);
  let url = model.settings.overlay_url || "/overlay";
  try { url = new URL(url, location.origin).href; } catch { url = `${location.origin}/overlay`; }
  $("overlay-url").value = url; $("open-overlay").href = url;
}
function commentQuery() {
  const params = new URLSearchParams();
  [["video_id", "filter-video"], ["category", "filter-category"], ["status", "filter-status"]].forEach(([key, id]) => { if ($(id).value.trim()) params.set(key, $(id).value.trim()); });
  return params.toString();
}
async function refreshComments() {
  const [comments, tasks] = await Promise.all([api(`/api/comments?${commentQuery()}`), api("/api/comment-tasks")]);
  model.comments = list(comments); renderComments(); renderCommentTasks(list(tasks));
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
      cell(row, text, `${comment.display_name || "匿名观众"} · ${comment.platform_user_id || "未知 ID"} · ${comment.video_id || ""}${comment.like_count != null ? ` · ${comment.like_count} 赞` : ""}`);
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
  model.commentId = id; model.draftDirty = false; model.draftId = null; renderComments();
}
function renderDraft(force = false) {
  const comment = model.comments.find(item => item.id === model.commentId);
  if (!comment) {
    if (model.draftDirty) return;
    $("draft-source").textContent = "选择列表中的评论，查看角色草稿及来源。";
    $("draft-text").value = ""; $("draft-text").disabled = true; $("draft-status").textContent = "选择评论";
    describe($("draft-meta"), []); ["approve-draft", "reject-draft", "copy-draft", "publish-draft"].forEach(id => { $(id).disabled = true; }); return;
  }
  const draft = comment.draft;
  $("draft-source").textContent = `${comment.display_name || "匿名观众"}：${comment.content}`;
  setBadge($("draft-status"), draft?.status || "未生成");
  describe($("draft-meta"), [["来源评论", `${comment.video_id} / ${comment.comment_id}`], ["角色版本", draft ? `${draft.character_id} · v${draft.character_version}` : "—"], ["模型", draft?.model || "—"], ...(draft?.publish_receipt || draft?.receipt ? [["平台回执", draft.publish_receipt || draft.receipt]] : [])]);
  if (force || !model.draftDirty || model.draftId !== draft?.id) { $("draft-text").value = draft?.text || ""; model.draftId = draft?.id || null; model.draftDirty = false; }
  const locked = draft && ["published", "publishing", "publish_uncertain"].includes(draft.status);
  $("draft-text").disabled = !draft || locked;
  $("approve-draft").disabled = !draft || locked;
  $("reject-draft").disabled = !draft || locked;
  $("copy-draft").disabled = !draft;
  $("publish-draft").disabled = !draft || draft.status !== "approved" || !model.policy?.credentials_configured || model.draftDirty;
  $("draft-note").textContent = draft?.error ? `错误：${draft.error}` : model.draftDirty ? "文字已修改，先审核保存后再发布或复制。" : model.policy?.auto_reply ? "自动回复已开启；新草稿可能自动发布。当前草稿以此处状态与平台回执为准。" : "人工审核模式。审核通过后可复制；账号凭证已配置时可发布。";
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
async function changePage() {
  const requested = location.hash.slice(1) || "comments";
  model.page = ["comments", "overview", "live", "characters", "voices", "assets", "settings"].includes(requested) ? requested : "comments";
  document.querySelectorAll("[data-section]").forEach(section => { section.hidden = section.dataset.section !== model.page; });
  document.querySelectorAll("[data-page]").forEach(link => { const active = link.dataset.page === model.page; link.classList.toggle("selected", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  document.title = `${({comments: "评论工作台", overview: "总览", live: "直播控制", characters: "角色", voices: "语音", assets: "素材", settings: "设置"})[model.page]} · Tuber Studio`;
  if (!model.authenticated) return;
  try { if (model.page === "comments") await refreshComments(); else if (["overview", "live"].includes(model.page)) await refreshLive(); }
  catch (error) { notice(error.message, true); }
}
async function poll() {
  if (!model.authenticated || model.polling || document.hidden) return;
  model.polling = true;
  try {
    renderStatus(await api("/api/status"));
    if (["overview", "live"].includes(model.page)) await refreshLive();
    if (model.page === "comments") await refreshComments();
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
  $("provider-check").hidden = false; $("provider-check").textContent = "检查中…";
  try { $("provider-check").textContent = JSON.stringify(await api("/api/providers/check", {method: "POST"}), null, 2); }
  catch (error) { $("provider-check").textContent = error.message; throw error; }
});
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
$("preview-form").onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    $("preview-state").textContent = "等待合成…"; $("preview-audio").pause();
    try { const result = await api("/api/tts/preview", {method: "POST", body: {text: $("preview-text").value, voice_id: $("preview-voice").value}}); $("preview-audio").src = result.audio_ref; $("preview-audio").hidden = false; $("preview-state").textContent = `合成完成${result.duration != null ? ` · ${Number(result.duration).toFixed(2)} 秒` : ""}，点击播放试听。`; }
    catch (error) { $("preview-state").textContent = `合成失败：${error.message}`; throw error; }
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
    const targets = ["comment_id", "platform_user_id", "display_name", "content", "like_count", "created_at"];
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
    if (!items.length) empty(tbody, 5, "没有可预览的数据。");
    else items.slice(0, 10).forEach(item => {
      const row = node("tr"); cell(row, item.comment_id); cell(row, item.platform_user_id || "未知 ID", item.display_name || "匿名观众");
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
async function compactContext(scope, videoId) {
  if (scope === "comments" && !videoId) throw new Error("请先填写要压缩的评论视频 ID 或导入批次。");
  const payload = {scope, ...(scope === "comments" ? {video_id: videoId} : {})};
  $("compaction-result").textContent = "正在使用本地模型压缩上下文…";
  notice("上下文压缩已开始，请等待本地模型返回。");
  try {
    const result = await api("/api/context/compact", {method: "POST", body: payload});
    const message = result.message || result.detail || (result.compacted === false ? "没有可压缩的历史。" : "压缩请求已完成，请查看实际上下文占用与压缩次数。");
    $("compaction-result").textContent = message; notice(message);
    renderStatus(await api("/api/status"));
  } catch (error) { $("compaction-result").textContent = `压缩失败：${error.message}`; throw error; }
}
$("compact-live").onclick = () => busy($("compact-live"), () => compactContext("live"));
$("compact-comments-settings").onclick = () => busy($("compact-comments-settings"), () => compactContext("comments", $("compact-video-id").value.trim()));
$("compact-comments").onclick = () => busy($("compact-comments"), () => compactContext("comments", $("filter-video").value.trim()));
$("character-image").onerror = () => { $("character-image").hidden = true; };
window.addEventListener("hashchange", changePage);
window.addEventListener("beforeunload", event => { if (model.characterDirty || model.draftDirty) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
setInterval(poll, 2500);
enterWorkspace().catch(error => { showLogin(); if (error.status !== 401) $("login-error").textContent = error.message; });
