"use strict";
import { $ } from "./modules/core.js";
import { api as baseApi } from "./modules/api.js";
import { LiveDesk } from "./modules/live-desk.js?v=20261111.1";

(() => {
  const focusView = new URLSearchParams(location.search).get("view");
  const page = ["scene", "music", "settings"].includes(focusView) ? focusView : "desk";
  const frame = window.frameElement;
  if (frame) document.body.dataset.liveEmbedded = "true";
  for (const section of document.querySelectorAll("[data-live-page]")) section.hidden = section.dataset.livePage !== page;
  document.querySelector(`[data-live-tab="${page}"]`).setAttribute("aria-current", "page");
  if (frame) {
    for (const link of document.querySelectorAll("[data-desk-link]")) {
      link.href = `/#live-${link.dataset.deskLink}`;
      link.target = "_top";
    }
  }
  const visible = () => !document.hidden && !frame?.closest("[hidden]");
  if (frame) {
    new ResizeObserver(([entry]) => {
      const height = Math.ceil(entry.target.getBoundingClientRect().height);
      if (height) frame.style.height = `${height + 24}px`;
    }).observe($("workspace"));
  }
  const names = {avatar:"角色形象", title:"直播标题", now:"当前歌曲", lyrics:"歌词字幕", queue:"点歌队列", chat:"弹幕互动", gifts:"礼物心意", decoration:"小装饰"};
  const icons = {avatar:"♙",title:"T",now:"♫",lyrics:"≋",queue:"☷",chat:"☏",gifts:"♡",decoration:"✧"};
  let state, music, settings, dirty = false, musicDirty = false, saved, polling = false, authenticated = false;
  function element(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function feedback(text, error = false) { $("feedback").hidden = !text; $("feedback").textContent = text; $("feedback").classList.toggle("error",error); }
  async function api(path, options = {}) {
    try { return await baseApi(path, options, login); }
    catch (error) {
      if (error.status && typeof error.body?.detail !== "string") error.message = "请检查输入内容";
      throw error;
    }
  }
  const accountPanel = new BilibiliAccountUI({header: $("global-bilibili-account"), api, notify: feedback});
  function login() { authenticated = false; accountPanel.deactivate(); $("login").hidden = false; $("workspace").hidden = true; }
  function bindLogin(boot) {
    $("login-form").onsubmit = async event => {
      event.preventDefault();
      try {
        await api("/api/auth", { method: "POST", body: { token: $("token").value } });
        $("token").value = "";
        await boot();
      } catch (error) { $("login-error").textContent = error.message; }
    };
  }
  if (page === "desk") {
    const desk = new LiveDesk($("live-desk"), { api, visible: () => authenticated && visible() });
    async function bootDesk() {
      try {
        desk.settings = await api("/api/settings");
        authenticated = true;
        $("login").hidden = true;
        $("workspace").hidden = false;
        await desk.refresh();
        if (authenticated && !frame) await accountPanel.activate();
      } catch (error) { login(); $("login-error").textContent = error.message; }
    }
    bindLogin(bootDesk);
    setInterval(() => desk.refresh(), 1000);
    bootDesk();
    return;
  }
  const scene = page === "scene" ? new LiveScene($("scene"), {editable:true, onChange:changed, onSelect:select, onWarning:text=>feedback(text,true)}) : null;
  const online = page === "music" ? new OnlineSongRequests(api, feedback) : null;
  if (page === "settings") {
    const cachePanel = new CachePanel($("cache-panel"), api, feedback);
    setInterval(() => { if (authenticated && visible()) cachePanel.refresh(); }, 5000);
  }
  async function run(button, action) {
    button.disabled = true; button.setAttribute("aria-busy","true");
    try { await action(); } catch (error) { feedback(error.message,true); }
    finally { button.disabled = button.id === "save-layout" || button.id === "undo-layout" ? !dirty : false; button.removeAttribute("aria-busy"); if (authenticated) await poll(); }
  }
  function changed() {
    dirty = true; $("save-state").textContent = "布局尚未保存";
    $("save-layout").disabled = false; $("undo-layout").disabled = false; renderLayers();
  }
  function select(item) {
    $("inspector-fields").disabled = !item; $("inspector-fields").hidden = !item; $("inspector-empty").hidden = !!item;
    $("selected-name").textContent = item ? names[item.kind] : "未选择";
    if (!item) return;
    for (const field of ["x","y","width","height","z","text"]) if (document.activeElement !== $("item-"+field)) $("item-"+field).value = typeof item[field] === "number" ? Math.round(item[field]) : item[field];
    $("item-visible").checked = item.visible; $("item-locked").checked = item.locked;
    for (const button of $("layers").children) button.setAttribute("aria-pressed",String(button.dataset.id === item.id));
  }
  function renderLayers() {
    if (!scene.scene) return;
    $("layers").replaceChildren(...scene.scene.items.map(item => {
      const button = element("button",null,"layer"); button.dataset.id = item.id;
      button.setAttribute("aria-pressed", String(scene.selected === item.id));
      button.append(element("span",icons[item.kind]), element("span", names[item.kind]), element("span",item.locked ? "锁" : item.visible ? "◉" : "○"));
      button.onclick = () => scene.select(item.id); return button;
    }));
  }
  function loadScene(value) {
    saved = structuredClone(value); scene.load(value); dirty = false;
    $("scene-title").value = value.title; $("scene-background").value = value.background;
    $("canvas-width").value = value.width; $("canvas-height").value = value.height;
    $("canvas-size").textContent = `${value.width} × ${value.height}`;
    $("save-state").textContent = "布局已保存"; $("save-layout").disabled = true; $("undo-layout").disabled = true;
    renderLayers();
  }
  async function loadMusic() {
    const first = !music;
    music = await api("/api/live/music"); const config = music.settings;
    if (!musicDirty) {
      $("default-mode").value = config.default_mode;
      $("music-volume").value = config.volume; $("music-limit").value = config.queue_limit; $("music-cooldown").value = config.cooldown_seconds;
    }
    if (first) $("request-mode").value = config.default_mode;
    $("singing-state").textContent = music.singing.reason;
    for (const id of ["default-mode","request-mode"]) { const option = $(id).querySelector('[value="sing"]'); option.disabled = !music.singing.available; option.textContent = music.singing.available ? "角色音色演唱" : "演唱（模型待接入）"; }
    renderSongs();
  }
  function renderSongs() {
    const query = $("song-search").value.trim().toLowerCase();
    const songs = music.songs.filter(song => `${song.title} ${song.artist}`.toLowerCase().includes(query));
    $("songs").replaceChildren(...songs.map(song => {
      const row = element("div",null,"song-row"), info = element("div",null,"song-info");
      info.append(element("strong",song.title), element("small",`${song.artist || "未填写演唱者"} · ${song.live_allowed ? "已确认直播用途" : "待确认用途"}${song.vocal ? " · 已有人声轨" : " · 口型待人声轨"}`));
      const button = element("button","点歌"); button.disabled = !song.live_allowed || settings?.features?.live_enabled !== true;
      button.onclick = () => run(button,async () => {
        const result = await api("/api/live/mock-event",{method:"POST",body:{type:"song",text:song.title,display_name:"主播",metadata:{song_id:song.id,mode:$("request-mode").value}}});
        if (result.status !== "queued") throw new Error(result.reason || result.status);
        feedback(`已加入队列：${song.title}`);
      });
      const remove = element("button","×","delete-song"); remove.setAttribute("aria-label",`移除歌曲 ${song.title}`);
      remove.onclick = () => { if (confirm(`从曲库移除「${song.title}」？已有排队项保留。`)) run(remove,async () => { await api(`/api/live/songs/${song.id}`,{method:"DELETE"}); await loadMusic(); }); };
      row.append(element("span","♫","song-icon"),info,button,remove); return row;
    }));
    if (!songs.length) { const empty = element("div",null,"empty"); empty.append(element("span","♫"),element("div",query ? "没有找到这首歌" : "曲库还是空的，导入一首获准使用的歌吧。")); $("songs").append(empty); }
  }
  function renderPoll() {
    const active = state.poll;
    const latest = active || state.activities?.at(-1);
    $("live-poll-start").disabled = !["live","paused"].includes(state.state) || active;
    $("live-poll-finish").disabled = !active;
    $("live-poll-state").textContent = !latest ? "暂无投票" : active
      ? `投票中 · ${latest.topic} · ${latest.options.join("／")}`
      : latest.counts ? `${latest.topic} · ${latest.total_votes} 票 · ${Object.entries(latest.counts).map(([name, count]) => name + " " + count).join("／")}${latest.complete ? "" : " · 统计未完整"}`
      : `${latest.topic} · ${latest.result || "已更新"}`;
  }
  function renderLiveOptions(options) {
    $("live-timezone").value = options.timezone || "";
    $("live-idle").checked = options.idle;
    $("live-web-fetch").checked = options.web_fetch;
    $("live-weather").checked = Boolean(options.weather);
    $("live-weather-name").value = options.weather?.name || "";
    $("live-weather-lat").value = options.weather?.latitude ?? "";
    $("live-weather-lon").value = options.weather?.longitude ?? "";
    $("live-weather").onchange();
  }
  async function poll() {
    if (polling || !authenticated || !visible()) return; polling = true;
    try {
      if (online) {
        online.update(await api("/api/live/online"));
      } else if (scene) {
        const [view, status] = await Promise.all([api("/api/live/view"), api("/api/status")]);
        state = view;
        if (!scene.scene || (!dirty && JSON.stringify(state.scene) !== scene.signature)) loadScene(state.scene);
        scene.update(state);
        $("character-name").textContent = `${state.character.name} · v${state.character.version}`;
        $("voice-state").textContent = `说话音色：${status.character.voice_profile}`;
      } else {
        state = await api("/api/live/view");
        renderPoll();
      }
    } catch(error) { feedback(error.message,true); } finally { polling = false; }
  }
  async function boot() {
    authenticated = true;
    try {
      settings = await api("/api/settings");
      if (page === "music") await loadMusic();
      if (page === "settings") renderLiveOptions(await api("/api/live/options"));
      await poll();
      if (!authenticated) return;
      $("login").hidden = true;
      $("workspace").hidden = false;
      if (!frame) await accountPanel.activate();
      scene?.fit();
    } catch (error) { login(); $("login-error").textContent = error.message; }
  }
  bindLogin(boot);
  $("save-layout").onclick = event => run(event.currentTarget, async () => { const value = await api("/api/live/scene",{method:"PUT",body:scene.scene}); loadScene(value); feedback("布局已保存，OBS画面将同步更新。"); });
  $("undo-layout").onclick = () => loadScene(saved);
  const reset = element("button","恢复默认布局","full"); reset.id = "reset-layout"; $("add-decoration").after(reset);
  reset.onclick = () => { if (confirm("将当前布局恢复为默认场景？保存前可撤销。")) run(reset,async () => { scene.load(await api("/api/live/scene/default")); changed(); }); };
  for (const field of ["x","y","width","height","z","text","visible","locked"]) $("item-"+field).onchange = event => {
    const value = event.target.type === "checkbox" ? event.target.checked : field === "text" ? event.target.value : Number(event.target.value);
    scene.edit(scene.selected,{[field]:value});
  };
  $("scene-title").oninput = event => { scene.scene.title = event.currentTarget.value; scene.update(state); changed(); };
  $("scene-background").onchange = () => { const value = structuredClone(scene.scene); value.background = $("scene-background").value; scene.load(value); changed(); };
  for (const dimension of ["width","height"]) $("canvas-"+dimension).onchange = () => {
    const value = Number($("canvas-"+dimension).value); if (value < (dimension === "width" ? 640 : 360) || value > (dimension === "width" ? 3840 : 2160)) return;
    const updated = structuredClone(scene.scene), ratio = value / updated[dimension]; updated[dimension] = value;
    for (const item of updated.items) { const position = dimension === "width" ? "x" : "y"; item[dimension] = Math.max(dimension === "width" ? 40 : 30,Math.min(value,item[dimension]*ratio)); item[position] = Math.max(0,Math.min(value-item[dimension],item[position]*ratio)); }
    scene.load(updated); $("canvas-size").textContent = `${updated.width} × ${updated.height}`; changed();
  };
  $("toggle-edit").onclick = event => { scene.editable = !scene.editable; scene.canvas.classList.toggle("editing",scene.editable); event.target.textContent = scene.editable ? "完成编排" : "编辑场景"; event.target.setAttribute("aria-pressed",String(scene.editable)); };
  $("add-decoration").onclick = () => { if (scene.scene.items.length >= 32) return feedback("最多32个场景组件",true); scene.scene.items.push({id:"deco_"+crypto.randomUUID().slice(0,8),kind:"decoration",x:50,y:200,width:100,height:100,z:5,text:"🫧",visible:true,locked:false}); scene.load(scene.scene); scene.select(scene.scene.items.at(-1).id); changed(); };
  $("live-weather").onchange = () => {
    const show = $("live-weather").checked;
    $("live-weather-location").hidden = !show;
    $("live-weather-location").disabled = !show;
  };
  $("live-options-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      const weather = $("live-weather").checked ? {
        name:$("live-weather-name").value.trim(),
        latitude:$("live-weather-lat").valueAsNumber,
        longitude:$("live-weather-lon").valueAsNumber,
      } : null;
      const options = await api("/api/live/options",{method:"PUT",body:{
        timezone:$("live-timezone").value.trim() || null,
        idle:$("live-idle").checked,web_fetch:$("live-web-fetch").checked,weather,
      }});
      renderLiveOptions(options);
      feedback("直播设置已保存。");
    });
  };
  $("live-poll-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      await api("/api/live/poll/start",{method:"POST",body:{
        topic:$("live-poll-topic").value.trim(),
        options:$("live-poll-options").value.split(/\r?\n/).map(text=>text.trim()).filter(Boolean),
        duration_seconds:$("live-poll-duration").valueAsNumber,
      }});
      $("live-poll-topic").value = "";
      $("live-poll-options").value = "";
    });
  };
  $("live-poll-finish").onclick = event => run(event.currentTarget, () => api("/api/live/poll/finish",{method:"POST"}));
  $("music-form").onsubmit = event => { event.preventDefault(); run(event.submitter,async () => {
    await api("/api/live/music",{method:"PUT",body:{...music.settings,default_mode:$("default-mode").value,volume:Number($("music-volume").value),queue_limit:Number($("music-limit").value),cooldown_seconds:Number($("music-cooldown").value)}}); musicDirty = false; await loadMusic(); feedback("点歌设置已保存，对新确认项生效。");
  }); };
  $("music-form").addEventListener("input", () => { musicDirty = true; });
  $("song-search").oninput = renderSongs;
  $("import-song").onclick = () => $("song-dialog").showModal();
  for (const id of ["close-import","cancel-import"]) $(id).onclick = () => $("song-dialog").close();
  function parseLyrics(text) {
    if (!text.trim()) return [];
    const lines = text.split(/\r?\n/).filter(line => line.trim()); const parsed = [];
    for (const line of lines) {
      if (/^\[(ti|ar|al|by|offset):/.test(line)) continue;
      const matches = [...line.matchAll(/\[(\d+):(\d{2}(?:\.\d{1,3})?)\]/g)];
      if (!matches.length) throw new Error("歌词请使用带时间戳的LRC格式");
      const lyric = line.replace(/\[[^\]]*\]/g,"").trim();
      for (const match of matches) parsed.push({time:Number(match[1])*60+Number(match[2]),text:lyric});
    }
    return parsed.sort((a,b) => a.time-b.time);
  }
  $("song-form").onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const lyrics = parseLyrics($("song-lyrics").value), files = {};
      for (const [key,id] of [["audio","song-file"],["vocal","vocal-file"],["accompaniment","accompaniment-file"]]) {
        const file = $(id).files[0]; if (!file) continue;
        if (file.size > 96*1024*1024) throw new Error("单个音频上限96MiB");
        $("import-state").textContent = `正在上传${key === "audio" ? "歌曲" : key === "vocal" ? "人声" : "伴奏"}…`;
        files[key] = (await api("/api/live/uploads",{method:"POST",body:file,headers:{"Content-Type":"audio/wav"}})).id;
      }
      const id = crypto.randomUUID();
      await api(`/api/live/songs/${id}`,{method:"PUT",body:{id,title:$("song-title").value.trim(),artist:$("song-artist").value.trim(),...files,lyrics,license_note:$("song-license").value,live_allowed:$("song-live-allowed").checked,commercial_allowed:$("song-commercial").checked}});
      $("song-dialog").close(); $("song-form").reset(); $("import-state").textContent = ""; await loadMusic(); feedback("歌曲已保存，可以选择模式点歌。");
    } catch(error) { $("import-state").textContent = error.message; } finally { button.disabled = false; }
  };
  window.addEventListener("beforeunload", event => { if (dirty || musicDirty) { event.preventDefault(); event.returnValue = ""; } });
  setInterval(poll,1000); boot();
})();
