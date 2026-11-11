import { liveActionDisabled } from "./live-controls.js?v=20261111";

const jobLabels = {
  received: "已收到", queued: "排队中", generating: "生成中", synthesizing: "准备音频",
  ready_to_play: "等待播放器", playing: "播出中", completed: "已完成", cancelled: "已取消",
  failed: "失败", filtered: "已过滤", expired: "已过期", dropped: "未加入队列",
};
const sessionLabels = { stopped: "未开播", live: "本地会话运行中", paused: "队列已暂停", degraded: "连接降级" };
const kindLabels = { chat: "弹幕", gift: "礼物", song: "歌曲", sfx: "音效" };

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function elapsed(seconds) {
  const total = Math.floor(Math.max(0, Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function timeOf(timestamp) {
  if (!Number.isFinite(timestamp)) return "";
  return new Date(timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function moveSongInQueue(queue, id, direction) {
  const order = queue.map(item => item.id);
  const songs = queue.flatMap((item, index) => item.type === "song" ? [index] : []);
  const index = songs.findIndex(position => order[position] === id);
  const target = index + direction;
  if (index < 0 || ![-1, 1].includes(direction) || target < 0 || target >= songs.length) return null;
  [order[songs[index]], order[songs[target]]] = [order[songs[target]], order[songs[index]]];
  return order;
}

export class LiveDesk {
  constructor(root, { api, visible }) {
    this.root = root;
    this.api = api;
    this.visible = visible;
    this.preview = new LiveScene(this.find("[data-desk-preview]"), { editable: false });
    this.pending = false;
    this.follow = true;
    this.feedTab = "chat";
    this.signatures = {};
    this.chatIds = new Set();
    this.unread = 0;
    for (const button of root.querySelectorAll("[data-desk-action]")) {
      button.onclick = () => this.control(button.dataset.deskAction);
    }
    for (const button of root.querySelectorAll("[data-desk-feed]")) {
      button.onclick = () => this.selectFeed(button.dataset.deskFeed);
      button.onkeydown = event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const target = event.key === "Home" ? "chat" : event.key === "End" ? "dialogue" :
          this.feedTab === "chat" ? "dialogue" : "chat";
        this.selectFeed(target);
        this.find(`[data-desk-feed="${target}"]`).focus();
      };
    }
    this.find("[data-desk-chat]").onscroll = () => {
      const chat = this.find("[data-desk-chat]");
      if (chat.hidden) return;
      this.follow = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 24;
      if (this.follow) this.unread = 0;
      this.renderFollow();
    };
    this.find("[data-desk-follow]").onclick = () => {
      this.follow = !this.follow;
      if (this.follow) this.followChat();
      this.renderFollow();
    };
    this.find("[data-desk-refresh]").onclick = () => this.refresh();
    this.find("[data-desk-open-output]").onclick = () => this.openOutput();
    this.find("[data-desk-copy-output]").onclick = () => this.copyOutput();
    const mock = this.find("[data-desk-mock]");
    mock.onsubmit = event => this.inject(event);
    mock.elements.type.onchange = () => {
      const sfx = mock.elements.type.value === "sfx";
      this.find("[data-desk-asset-label]").hidden = !sfx;
      mock.elements.asset.required = sfx;
      mock.elements.text.required = !["sfx", "disconnect"].includes(mock.elements.type.value);
    };
    this.selectFeed("chat");
    this.renderControls();
  }

  find(selector) { return this.root.querySelector(selector); }

  say(selector, text) {
    const target = this.find(selector);
    if (target.textContent !== text) target.textContent = text;
  }

  refresh() {
    if (!this.visible()) return;
    if (!this.pollTask) this.pollTask = this.load().finally(() => { this.pollTask = null; });
    return this.pollTask;
  }

  async load() {
    try {
      const view = await this.api("/api/live/studio");
      const changedSession = !this.view || this.view.session_id !== view.session_id;
      this.view = view;
      this.fresh = true;
      this.say("[data-desk-load-error]", "");
      if (changedSession) {
        this.signatures = {};
        this.chatIds.clear();
        this.follow = true;
        this.unread = 0;
        this.find("[data-desk-dialogue]").scrollTop = 0;
      }
      const sceneSignature = JSON.stringify(view.scene);
      if (this.signatures.scene !== sceneSignature) {
        this.preview.load(view.scene);
        this.signatures.scene = sceneSignature;
      }
      this.preview.update(view);
      this.renderProgram();
      this.renderQueue();
      this.renderFeed(changedSession);
    } catch (error) {
      this.fresh = false;
      this.say("[data-desk-load-error]", `运行状态读取失败：${error.message}。可刷新重试。`);
    } finally {
      this.renderControls();
    }
  }

  renderProgram() {
    const { current, playback, last_spoken: lastSpoken } = this.view;
    const song = playback?.song?.song || current?.song;
    const audible = playback?.started && !playback.paused && !this.view.muted;
    const phase = this.view.muted ? "已静音" : playback?.paused ? "歌曲已暂停" : audible ? "正在播出" :
      playback ? "等待播放器确认" : jobLabels[current?.status] || "空闲";
    this.say("[data-desk-session]", sessionLabels[this.view.state] || this.view.state);
    this.say("[data-desk-player]", this.view.overlay_connected ? "播放器已连接" : "播放器未连接");
    this.find("[data-desk-open-output]").hidden = this.view.overlay_connected;
    this.say("[data-desk-mute-state]", this.view.muted ? "已静音" : "");
    this.say("[data-desk-current-state]", phase);
    this.say("[data-desk-current-title]", song?.title || (current ? kindLabels[current.type] || "互动播报" : "暂无当前节目"));
    const quality = song?.quality;
    const qualityText = quality?.format === "flac" ? `FLAC ${quality.bits_per_sample}-bit / ${quality.sample_rate / 1000} kHz` :
      quality?.bitrate ? `${quality.format?.toUpperCase() || "音频"} ${Math.round(quality.bitrate / 1000)} kbps` : "";
    const trackDetails = song ? [song.artist, current?.requester && `点歌 · ${current.requester}`, qualityText] : [current?.requester];
    this.say("[data-desk-current-detail]", trackDetails.filter(Boolean).join(" · "));
    const caption = audible && !song ? playback.text || "" : "";
    this.say("[data-desk-caption]", caption);
    this.find("[data-desk-caption]").hidden = !caption;
    this.say("[data-desk-last-spoken]", lastSpoken?.text || "");
    this.find("[data-desk-last]").hidden = !lastSpoken?.text || Boolean(caption);
    const hasProgress = Boolean(playback?.started && playback.duration > 0);
    this.find("[data-desk-clock]").hidden = !hasProgress;
    if (hasProgress) {
      const progress = this.find("[data-desk-progress]");
      progress.max = playback.duration;
      progress.value = Math.min(playback.duration, Math.max(0, Number(playback.position) || 0));
      this.say("[data-desk-position]", `${elapsed(playback.position)} / ${elapsed(playback.duration)}`);
    }
    const notices = [this.view.error, current?.error, playback?.visual_warning].filter(Boolean);
    this.say("[data-desk-runtime-error]", [...new Set(notices)].join(" · "));
  }

  renderQueue() {
    const songs = this.view.queue.filter(item => item.type === "song");
    const other = this.view.queue.length - songs.length;
    this.say("[data-desk-song-count]", `${songs.length} 首待播`);
    this.say("[data-desk-other-count]", other ? `另有 ${other} 条待播互动` : "");
    const signature = JSON.stringify(songs);
    if (this.signatures.queue === signature) return;
    this.signatures.queue = signature;
    const list = this.find("[data-desk-queue]");
    const focused = list.contains(document.activeElement) ? document.activeElement.dataset : null;
    const rows = songs.map((item, index) => {
      const row = element("li", null, "desk-queue-row");
      const info = element("div", null, "desk-row-content");
      const song = item.media?.song;
      const title = song?.title || item.text || "未命名歌曲";
      info.append(element("strong", title), element("small", [song?.artist, item.requester && `点歌 · ${item.requester}`].filter(Boolean).join(" · ")));
      if (item.preparation) {
        const [state, error] = item.preparation;
        const label = { waiting: "等待准备", pending: "等待准备", ready: "准备就绪", preparing: "准备中", failed: "准备失败" }[state];
        info.append(element("small", error || label || "等待准备", error ? "desk-error" : ""));
      }
      const actions = element("div", null, "desk-row-actions");
      for (const [action, label, edge] of [["up", "上移", index === 0], ["down", "下移", index === songs.length - 1], ["remove", "移除", false]]) {
        const button = element("button", label);
        button.type = "button";
        button.dataset.queueAction = action;
        button.dataset.queueId = item.id;
        button.dataset.edge = String(edge);
        button.setAttribute("aria-label", `${label} ${title}`);
        button.onclick = () => this.changeSong(item.id, action);
        actions.append(button);
      }
      row.append(element("span", String(index + 1), "desk-index"), info, actions);
      return row;
    });
    list.replaceChildren(...rows);
    this.find("[data-desk-queue-empty]").hidden = songs.length > 0;
    if (focused) {
      const target = [...list.querySelectorAll("button")].find(button =>
        button.dataset.queueId === focused.queueId && button.dataset.queueAction === focused.queueAction);
      (target || this.find("[data-desk-queue-heading]")).focus({ preventScroll: true });
    }
  }

  renderFeed(changedSession) {
    const chat = this.view.chat || [];
    if (!changedSession && !this.follow) this.unread += chat.filter(item => !this.chatIds.has(item.id)).length;
    this.chatIds = new Set(chat.map(item => item.id));
    this.renderFeedList("chat", chat, item => {
      const row = this.feedRow(item, item.received_at, jobLabels[item.status] || item.status);
      row.append(element("p", item.text || "（无文字）"));
      if (item.reason) row.append(element("small", item.reason));
      return row;
    });
    this.renderFeedList("dialogue", this.view.dialogue || [], item => {
      const state = item.status === "completed" ? "已确认播放" : jobLabels[item.status] || item.status;
      const row = this.feedRow(item, item.created_at, state);
      if (item.status === "completed" && item.text) row.append(element("p", item.text));
      if (item.type === "song" && item.song_title) row.append(element("p", item.song_title));
      if (item.error && item.status !== "completed") {
        row.append(element("small", item.error, "desk-error"));
      }
      return row;
    });
    if (this.follow) this.followChat();
    this.renderFollow();
  }

  feedRow(item, time, state) {
    const row = element("li", null, "desk-feed-row");
    const meta = element("div", null, "desk-feed-meta");
    meta.append(element("strong", item.requester || "直播间"), element("span", kindLabels[item.type] || "互动"),
      element("span", state || ""), element("time", timeOf(time)));
    row.append(meta);
    return row;
  }

  renderFeedList(tab, entries, renderRow) {
    const signature = JSON.stringify(entries);
    if (this.signatures[tab] === signature) return;
    this.signatures[tab] = signature;
    const pane = this.find(`[data-desk-${tab}]`);
    const scroll = pane.scrollTop;
    pane.querySelector("ol").replaceChildren(...entries.map(renderRow));
    pane.querySelector("[data-feed-empty]").hidden = entries.length > 0;
    pane.scrollTop = scroll;
  }

  selectFeed(tab) {
    this.feedTab = tab;
    for (const button of this.root.querySelectorAll("[data-desk-feed]")) {
      const selected = button.dataset.deskFeed === tab;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    this.find("[data-desk-chat]").hidden = tab !== "chat";
    this.find("[data-desk-dialogue]").hidden = tab !== "dialogue";
    this.find("[data-desk-follow]").hidden = tab !== "chat";
    if (tab === "chat" && this.follow) this.followChat();
    this.renderFollow();
  }

  followChat() {
    const chat = this.find("[data-desk-chat]");
    if (chat.hidden) return;
    chat.scrollTop = chat.scrollHeight;
    this.unread = 0;
  }

  renderFollow() {
    const button = this.find("[data-desk-follow]");
    button.setAttribute("aria-pressed", String(this.follow));
    button.textContent = this.follow ? "跟随最新：开" : this.unread ? `跳到最新 · ${this.unread} 条` : "跟随最新：关";
  }

  renderControls() {
    const enabled = this.view?.live_enabled === true;
    const transitioning = this.view?.inference_transitioning;
    const state = this.view;
    const playback = state?.playback;
    const canAct = !this.pending && this.fresh;
    const liveStatus = { state: state?.state, muted: state?.muted, current_job: state?.current, queue_length: state?.queue.length };
    for (const button of this.root.querySelectorAll("[data-desk-action]")) {
      const action = button.dataset.deskAction;
      button.disabled = !canAct || (action === "media" ? !enabled || !playback?.song || !playback.started :
        liveActionDisabled(action, liveStatus, { enabled, transitioning }));
      if (action === "media") button.textContent = playback?.paused ? "继续歌曲" : "暂停歌曲";
      if (action === "pause") button.hidden = state?.state === "paused";
      if (action === "resume") {
        button.hidden = !["paused", "degraded"].includes(state?.state);
        button.textContent = state?.state === "degraded" ? "恢复连接" : "恢复队列";
      }
      if (action === "mute") button.hidden = !!state?.muted;
      if (action === "unmute") button.hidden = !state?.muted;
    }
    for (const button of this.root.querySelectorAll("[data-queue-action]")) {
      button.disabled = !canAct || !enabled || button.dataset.edge === "true";
    }
    this.find("[data-desk-mock] fieldset").disabled = !canAct || !enabled || transitioning;
    this.say("[data-desk-feature]", !state ? "正在读取直播配置" :
      enabled ? (transitioning ? "正在切换推理资源" : "") : "直播功能未启用，可在推理设置中配置。");
  }

  async run(action, { queueEdit = false } = {}) {
    if (this.pending || !this.fresh) return;
    this.pending = true;
    this.say("[data-desk-action-error]", "");
    this.say("[data-desk-action-state]", "正在处理…");
    this.renderControls();
    try {
      await action();
      this.say("[data-desk-action-state]", "操作已提交。");
    } catch (error) {
      this.say("[data-desk-action-state]", "");
      const raced = queueEdit && [400, 404, 409, 422].includes(error.status);
      this.say("[data-desk-action-error]", raced ? "状态已变化，正在读取最新队列。请检查后再操作。" : error.message);
    } finally {
      await this.pollTask;
      await this.refresh();
      this.pending = false;
      this.renderControls();
    }
  }

  control(action) {
    if (action === "clear" && !confirm("清空全部待播歌曲和互动？当前播放保持。")) return;
    const endpoint = action === "media" ? (this.view.playback?.paused ? "media-resume" : "media-pause") : action;
    return this.run(() => this.api(`/api/live/${endpoint}`, { method: "POST" }));
  }

  changeSong(id, action) {
    if (action === "remove") return this.run(() => this.api(`/api/live/queue/${encodeURIComponent(id)}`, { method: "DELETE" }), { queueEdit: true });
    const order = moveSongInQueue(this.view.queue, id, action === "up" ? -1 : 1);
    if (order) return this.run(() => this.api("/api/live/queue", { method: "PUT", body: order }), { queueEdit: true });
  }

  inject(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    return this.run(async () => {
      const type = fields.get("type");
      const result = await this.api("/api/live/mock-event", { method: "POST", body: {
        type, display_name: fields.get("name"),
        platform_user_id: "workbench-debug", text: fields.get("text"),
        metadata: type === "sfx" ? { asset_id: fields.get("asset").trim() } : {},
      } });
      if (result.status !== "queued" && result.reason !== "已计票" && !(type === "disconnect" && result.status === "filtered")) {
        throw new Error(result.reason || result.status);
      }
      form.elements.text.value = "";
    });
  }

  openOutput() {
    const url = this.settings?.overlay_url;
    if (!url) return;
    const output = window.open("about:blank", "_blank");
    if (!output) return this.say("[data-desk-action-error]", "请允许打开 OBS 播放器窗口。");
    output.opener = null;
    output.location = url;
  }

  async copyOutput() {
    try {
      await navigator.clipboard.writeText(this.settings.overlay_url);
      this.say("[data-desk-action-state]", "已复制 OBS 浏览器源地址。");
    } catch {
      this.say("[data-desk-action-error]", "无法复制地址，请检查浏览器剪贴板权限。");
    }
  }
}
