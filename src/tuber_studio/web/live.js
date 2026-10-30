"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const names = {avatar:"角色形象", title:"直播标题", now:"当前歌曲", lyrics:"歌词字幕", queue:"点歌队列", chat:"弹幕互动", gifts:"礼物心意", decoration:"小装饰"};
  const icons = {avatar:"♙",title:"T",now:"♫",lyrics:"≋",queue:"☷",chat:"☏",gifts:"♡",decoration:"✧"};
  let state, music, status, dirty = false, saved, polling = false, authenticated = false;
  const scene = new LiveScene($("scene"), {editable:true, onChange:changed, onSelect:select, onWarning:text=>feedback(text,true)});
  function element(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function feedback(text, error = false) { $("feedback").hidden = !text; $("feedback").textContent = text; $("feedback").classList.toggle("error",error); }
  async function api(path, options = {}) {
    const headers = {...options.headers}; let body = options.body;
    if (body && !(body instanceof File) && !(body instanceof Blob)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
    const response = await fetch(path, {...options, body, headers, credentials:"same-origin"});
    const data = await response.json();
    if (!response.ok) { if (response.status === 401) login(); throw new Error(typeof data.detail === "string" ? data.detail : "请检查输入内容"); }
    return data;
  }
  const online = new OnlineSongRequests(api, feedback);
  const cachePanel = new CachePanel($("cache-panel"), api, feedback);
  setInterval(() => { if (authenticated) cachePanel.refresh(); }, 5000);
  async function run(button, action) {
    button.disabled = true; button.setAttribute("aria-busy","true");
    try { await action(); } catch (error) { feedback(error.message,true); }
    finally { button.disabled = button.id === "save-layout" || button.id === "undo-layout" ? !dirty : false; button.removeAttribute("aria-busy"); if (authenticated) await poll(); }
  }
  function login() { authenticated = false; $("login").hidden = false; $("workspace").hidden = true; }
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
    music = await api("/api/live/music"); const config = music.settings;
    $("default-mode").value = config.default_mode; $("request-mode").value = config.default_mode;
    $("music-volume").value = config.volume; $("music-limit").value = config.queue_limit; $("music-cooldown").value = config.cooldown_seconds;
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
      const button = element("button","点歌"); button.disabled = !song.live_allowed;
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
  function renderQueue() {
    $("queue-count").textContent = state.queue.length;
    $("queue").replaceChildren(...state.queue.map((q,index) => {
      const row = element("li"), info = element("div",null,"queue-info");
      info.append(element("strong",q.media?.song.title || q.text),element("small",`${q.requester} · ${q.media ? q.media.mode === "sing" ? "角色演唱" : "直接播放" : q.type === "gift" ? "礼物" : "互动"}`));
      const up = element("button","↑"); up.disabled = index === 0; up.setAttribute("aria-label","上移待播任务");
      up.onclick = () => run(up, async () => { const order = state.queue.map(item => item.id); [order[index-1],order[index]] = [order[index],order[index-1]]; await api("/api/live/queue",{method:"PUT",body:order}); });
      const remove = element("button","×"); remove.setAttribute("aria-label","移除待播任务");
      remove.onclick = () => run(remove, () => api(`/api/live/queue/${q.id}`,{method:"DELETE"}));
      if (q.preparation) info.append(element("small",q.preparation[1] || q.preparation[0],q.preparation[1] ? "error" : ""));
      row.append(element("span",String(index+1).padStart(2,"0")),info,up,remove); return row;
    }));
    if (!state.queue.length) $("queue").append(element("li","还没有待播任务，下一首留给你。","empty"));
  }
  async function poll() {
    if (polling || !authenticated) return; polling = true;
    try {
      const values = await Promise.all([api("/api/live/view"),api("/api/status"),api("/api/live/online")]);
      [state,status] = values; online.update(values[2]);
      if (!scene.scene || (!dirty && JSON.stringify(state.scene) !== scene.signature)) loadScene(state.scene);
      scene.update(state); renderQueue();
      $("session-state").textContent = state.muted ? "已静音" : ({stopped:"未开播",live:"直播中",paused:"队列已暂停",degraded:"连接降级"}[state.state] || state.state);
      $("player-state").textContent = state.overlay_connected ? "播放器已连接" : "播放器未连接";
      $("character-name").textContent = `${state.character.name} · v${state.character.version}`;
      $("voice-state").textContent = `说话音色：${status.character.voice_profile}`;
      const playing = state.playback, current = status.current_job;
      const track = playing?.song?.song; const quality = track?.quality;
      const qualityText = quality ? ` · ${track.source === "bodian" ? "波点" : "咪咕"} ${quality.format.toUpperCase()}${quality.format === "flac" ? ` ${quality.bits_per_sample}-bit / ${quality.sample_rate/1000} kHz` : ` ${Math.round(quality.bitrate/1000)} kbps`}` : "";
      $("current-title").textContent = playing?.song?.song.title || (current ? current.text || "正在准备互动" : "准备好，就开始吧");
      $("current-detail").textContent = playing ? `${playing.paused ? "已暂停" : playing.started ? "播放中" : "等待播放"} · ${Math.floor(playing.position)} / ${Math.round(playing.duration)} 秒${qualityText}${playing.visual_warning ? " · "+playing.visual_warning : ""}` : ({generating:"正在生成互动",synthesizing:"正在准备音频／转换演唱",ready_to_play:"等待播放器"}[current?.status] || "Mock 直播 · 真实平台尚未接入");
      $("media-toggle").disabled = !playing?.song || !playing.started;
      $("media-toggle").textContent = playing?.paused ? "继续歌曲" : "暂停歌曲";
      $("runtime-error").textContent = status.error || "";
    } catch(error) { feedback(error.message,true); } finally { polling = false; }
  }
  async function boot() { authenticated = true; try { await loadMusic(); await poll(); $("login").hidden = true; $("workspace").hidden = false; feedback(""); scene.fit(); } catch(error) { login(); $("login-error").textContent = error.message; } }
  $("login-form").onsubmit = async event => { event.preventDefault(); try { await api("/api/auth",{method:"POST",body:{token:$("token").value}}); $("token").value = ""; await boot(); } catch(error) { $("login-error").textContent = error.message; } };
  $("save-layout").onclick = event => run(event.currentTarget, async () => { const value = await api("/api/live/scene",{method:"PUT",body:scene.scene}); loadScene(value); feedback("布局已保存，OBS画面将同步更新。"); });
  $("undo-layout").onclick = () => loadScene(saved);
  const reset = element("button","恢复默认布局","full"); reset.id = "reset-layout"; $("add-decoration").after(reset);
  reset.onclick = () => { if (confirm("将当前布局恢复为默认场景？保存前可撤销。")) run(reset,async () => { scene.load(await api("/api/live/scene/default")); changed(); }); };
  for (const field of ["x","y","width","height","z","text","visible","locked"]) $("item-"+field).onchange = event => {
    const value = event.target.type === "checkbox" ? event.target.checked : field === "text" ? event.target.value : Number(event.target.value);
    scene.edit(scene.selected,{[field]:value});
  };
  $("scene-title").onchange = () => { scene.scene.title = $("scene-title").value; scene.update(state); changed(); };
  $("scene-background").onchange = () => { const value = structuredClone(scene.scene); value.background = $("scene-background").value; scene.load(value); changed(); };
  for (const dimension of ["width","height"]) $("canvas-"+dimension).onchange = () => {
    const value = Number($("canvas-"+dimension).value); if (value < (dimension === "width" ? 640 : 360) || value > (dimension === "width" ? 3840 : 2160)) return;
    const updated = structuredClone(scene.scene), ratio = value / updated[dimension]; updated[dimension] = value;
    for (const item of updated.items) { const position = dimension === "width" ? "x" : "y"; item[dimension] = Math.max(dimension === "width" ? 40 : 30,Math.min(value,item[dimension]*ratio)); item[position] = Math.max(0,Math.min(value-item[dimension],item[position]*ratio)); }
    scene.load(updated); $("canvas-size").textContent = `${updated.width} × ${updated.height}`; changed();
  };
  $("toggle-edit").onclick = event => { scene.editable = !scene.editable; scene.canvas.classList.toggle("editing",scene.editable); event.target.textContent = scene.editable ? "完成编排" : "编辑场景"; event.target.setAttribute("aria-pressed",String(scene.editable)); };
  $("add-decoration").onclick = () => { if (scene.scene.items.length >= 32) return feedback("最多32个场景组件",true); scene.scene.items.push({id:"deco_"+crypto.randomUUID().slice(0,8),kind:"decoration",x:50,y:200,width:100,height:100,z:5,text:"🫧",visible:true,locked:false}); scene.load(scene.scene); scene.select(scene.scene.items.at(-1).id); changed(); };
  for (const button of document.querySelectorAll("[data-control]")) button.onclick = () => run(button, () => api(`/api/live/${button.dataset.control}`,{method:"POST"}));
  $("media-toggle").onclick = event => run(event.currentTarget, () => api(`/api/live/media-${state.playback?.paused ? "resume" : "pause"}`,{method:"POST"}));
  $("clear-queue").onclick = event => { if (confirm("清空所有待播任务？当前播放保持。")) run(event.currentTarget, () => api("/api/live/clear",{method:"POST"})); };
  $("open-overlay").onclick = event => { const page = window.open("about:blank","_blank"); if (!page) return feedback("请允许打开播放器窗口",true); page.opener = null; run(event.currentTarget,async () => { try { const config = await api("/api/settings"); page.location = config.overlay_url; } catch(error) { page.close(); throw error; } }); };
  $("event-form").onsubmit = event => { event.preventDefault(); run(event.submitter,async () => { const result = await api("/api/live/mock-event",{method:"POST",body:{type:$("event-type").value,display_name:$("event-name").value,platform_user_id:"workbench-debug",text:$("event-text").value}}); feedback(result.reason || "事件已加入队列",result.status !== "queued"); }); };
  $("music-form").onsubmit = event => { event.preventDefault(); run(event.submitter,async () => {
    await api("/api/live/music",{method:"PUT",body:{...music.settings,default_mode:$("default-mode").value,volume:Number($("music-volume").value),queue_limit:Number($("music-limit").value),cooldown_seconds:Number($("music-cooldown").value)}}); await loadMusic(); feedback("点歌设置已保存，对新确认项生效。");
  }); };
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
  window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  setInterval(poll,1000); boot();
})();
