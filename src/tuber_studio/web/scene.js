"use strict";

// The same renderer is used by the silent editor and the playback-only OBS page.
window.LiveScene = class {
  constructor(viewport, {editable = false, onChange = () => {}, onSelect = () => {}, onWarning = () => {}} = {}) {
    this.viewport = viewport; this.editable = editable; this.onChange = onChange; this.onSelect = onSelect; this.onWarning = onWarning;
    this.failedAssets = new Set();
    this.canvas = document.createElement("div"); this.canvas.className = "scene-canvas";
    viewport.classList.add("scene-viewport"); viewport.replaceChildren(this.canvas);
    this.nodes = new Map(); this.selected = null; this.signature = "";
    this.observer = new ResizeObserver(() => this.fit()); this.observer.observe(viewport);
    this.canvas.addEventListener("pointerdown", event => this.pointer(event));
    this.canvas.addEventListener("keydown", event => this.key(event));
  }
  fit() {
    if (!this.scene) return;
    this.viewport.style.aspectRatio = `${this.scene.width}/${this.scene.height}`;
    this.scale = Math.min(this.viewport.clientWidth / this.scene.width, this.viewport.clientHeight / this.scene.height);
    this.canvas.style.width = `${this.scene.width}px`; this.canvas.style.height = `${this.scene.height}px`;
    this.canvas.style.transform = `scale(${this.scale})`;
    this.canvas.style.left = `${(this.viewport.clientWidth-this.scene.width*this.scale)/2}px`;
    this.canvas.style.top = `${(this.viewport.clientHeight-this.scene.height*this.scale)/2}px`;
  }
  load(scene) {
    this.scene = structuredClone(scene); this.signature = JSON.stringify(scene);
    this.canvas.className = `scene-canvas ${scene.background} ${this.editable ? "editing" : ""}`;
    this.canvas.replaceChildren(); this.nodes.clear();
    const bubbles = document.createElement("div"); bubbles.className = "scene-bubbles"; this.canvas.append(bubbles);
    for (const item of this.scene.items) {
      const element = document.createElement("section"); element.className = `scene-item scene-${item.kind}`;
      element.dataset.id = item.id; element.dataset.kind = item.kind;
      element.tabIndex = this.editable ? 0 : -1; element.setAttribute("aria-label", item.kind);
      const body = document.createElement("div"); body.className = "scene-body";
      if (["now", "queue", "chat"].includes(item.kind)) body.classList.add("scene-card");
      element.append(body);
      const handle = document.createElement("span"); handle.className = "scene-resize"; element.append(handle);
      this.nodes.set(item.id, element); this.canvas.append(element); this.place(item);
    }
    const label = document.createElement("span"); label.className = "scene-ai-label";
    label.textContent = "虚构 AI 同人角色 · 非官方"; this.canvas.append(label);
    this.select(this.selected); this.fit(); if (this.state) this.update(this.state);
  }
  place(item) {
    const element = this.nodes.get(item.id); if (!element) return;
    Object.assign(element.style, {left:`${item.x}px`, top:`${item.y}px`, width:`${item.width}px`, height:`${item.height}px`, zIndex:item.z});
    element.dataset.visible = item.visible; element.dataset.locked = item.locked;
    if (item.kind === "decoration") element.firstElementChild.style.fontSize = `${Math.min(item.width,item.height)*.9}px`;
  }
  select(id) {
    this.selected = id;
    for (const [key, element] of this.nodes) element.classList.toggle("selected", key === id);
    this.onSelect(this.scene?.items.find(item => item.id === id) || null);
  }
  edit(id, changes) {
    const item = this.scene.items.find(item => item.id === id); if (!item) return;
    Object.assign(item, changes);
    item.width = Math.max(40, Math.min(item.width, this.scene.width));
    item.height = Math.max(30, Math.min(item.height, this.scene.height));
    item.x = Math.max(0, Math.min(item.x, this.scene.width - item.width));
    item.y = Math.max(0, Math.min(item.y, this.scene.height - item.height));
    this.place(item); this.onChange(this.scene); this.onSelect(item);
    if (this.state) this.update(this.state);
  }
  pointer(event) {
    if (!this.editable || event.button !== 0) return;
    const element = event.target.closest(".scene-item"); if (!element) { this.select(null); return; }
    const item = this.scene.items.find(item => item.id === element.dataset.id);
    this.select(item.id); if (item.locked) return;
    event.preventDefault(); element.focus(); element.setPointerCapture(event.pointerId);
    const origin = {x:event.clientX, y:event.clientY, item:{...item}};
    const resize = event.target.classList.contains("scene-resize");
    const move = e => {
      const dx = (e.clientX - origin.x) / this.scale, dy = (e.clientY - origin.y) / this.scale;
      this.edit(item.id, resize ? {width:origin.item.width + dx, height:origin.item.height + dy} : {x:origin.item.x + dx, y:origin.item.y + dy});
    };
    const end = () => { element.removeEventListener("pointermove", move); element.removeEventListener("pointerup", end); element.removeEventListener("pointercancel", end); };
    element.addEventListener("pointermove", move); element.addEventListener("pointerup", end); element.addEventListener("pointercancel", end);
  }
  key(event) {
    if (!this.editable || !["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(event.key)) return;
    const item = this.scene.items.find(i => i.id === this.selected); if (!item || item.locked) return;
    event.preventDefault(); const step = event.shiftKey ? 10 : 1;
    this.edit(item.id, {x:item.x + (event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0), y:item.y + (event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0)});
  }
  update(state) {
    this.state = state; if (!this.scene) return;
    const playback = state.playback, media = playback?.song, song = media?.song;
    const position = playback?.position || 0;
    const make = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
    for (const item of this.scene.items) {
      const body = this.nodes.get(item.id).firstElementChild;
      if (item.kind === "avatar") {
        const assets = (state.assets || []).filter(a=>!this.failedAssets.has(a.url || "/"+a.path));
        const active = playback?.started && !playback.paused && !state.muted;
        const tags = song && active ? [playback.mouth > .012 ? "sing_open" : "sing_closed", "idle"] : active ? [playback.animation_id,playback.emotion,"talking","idle"] : ["idle"];
        const asset = tags.map(tag=>assets.find(a=>a.id===tag)).find(Boolean) || assets[0];
        let image = body.querySelector("img");
        if (asset) {
          if (!image) { image = make("img"); image.alt = state.character?.name || "角色"; image.draggable = false; body.replaceChildren(image); image.onerror = () => { this.failedAssets.add(image.getAttribute("src")); this.onWarning("角色素材加载失败，画面已降级为可用素材"); this.update(this.state); }; }
          const source = asset.url || "/" + asset.path; if (image.getAttribute("src") !== source) { image.hidden = false; image.src = source; }
        } else body.replaceChildren(make("span", "等待角色素材", "avatar-empty"));
      } else if (item.kind === "title") {
        body.replaceChildren(make("strong", this.scene.title), make("small", state.muted ? "已静音 · 深海休息中" : "水下小窝 · 聊聊天 · 唱首歌"));
      } else if (item.kind === "now") {
        const record = make("div", null, "record"), name = make("p", song?.title || "等待下一首歌", "song-name");
        const info = make("p", song ? `${media.mode === "sing" ? "角色演唱" : "直接播放 · "+(song.artist || "曲库音源")} · ${media.requester}` : "从曲库点一首喜欢的歌", "song-detail");
        info.title = info.textContent;
        const progress = make("div", null, "scene-progress"), fill = make("span");
        fill.style.width = `${Math.min(100, position / (playback?.duration || 1) * 100)}%`; progress.append(fill);
        body.replaceChildren(make("h3", "♫ 这一首，唱给你"), record, name, info, progress);
      } else if (item.kind === "lyrics") {
        const lyrics = song?.lyrics || [];
        const line = lyrics.filter(l => l.time <= position).at(-1);
        body.textContent = song ? (line?.text || (lyrics.length ? "♪" : "暂无同步歌词")) : (playback?.started ? playback.text : "等一首心动的歌 ♡");
      } else if (item.kind === "queue") {
        const list = make("ol"), songs = (state.queue || []).filter(q => q.media);
        songs.slice(0, 7).forEach((q, i) => { const li = make("li"), detail = make("span", q.media.song.title); detail.append(make("small", `${q.requester} · ${q.media.mode === "sing" ? "角色演唱" : "直接播放"}`)); li.append(make("b", String(i + 1).padStart(2,"0")), detail); list.append(li); });
        body.replaceChildren(make("h3", "♫ 点歌队列"), songs.length ? list : make("p", "还没有排队的歌，下一首留给你。"));
      } else if (item.kind === "chat") {
        const chats = (state.events || []).filter(e => e.type === "chat").slice(-3);
        body.replaceChildren(...(chats.length ? chats.map(e => { const p = make("p"); p.append(make("b", e.display_name), make("span", e.text)); return p; }) : [make("p", "☏ 鱼宝们在聊 · 等待弹幕") ]));
      } else if (item.kind === "gifts") {
        const gift = (state.events || []).filter(e => e.type === "gift").at(-1);
        body.textContent = gift ? `${gift.display_name} · ${gift.text}` : "本场心意 · 等待礼物";
      } else body.textContent = item.text;
    }
  }
};
