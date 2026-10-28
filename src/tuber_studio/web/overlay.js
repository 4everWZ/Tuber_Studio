"use strict";

(() => {
  const get = id => document.getElementById(id);
  const audio = new Audio(); audio.preload = "auto";
  let socket = null, active = null, character = null, assets = [], reconnectTimer = null;
  let knownOwner = false, generation = 0, fallbackTried = false;
  if (new URLSearchParams(location.search).get("background") === "scene") document.body.classList.add("scene");

  function status(message, error = false) {
    get("connection").textContent = message; get("connection").classList.toggle("error", error);
  }
  function source(asset) { return asset?.url || asset?.path; }
  function applicable() {
    return assets.filter(asset => !asset.character_id || asset.character_id === character?.id);
  }
  function findAsset(tags) {
    for (const tag of tags) {
      const asset = applicable().find(item => [item.id, item.tag, item.event_tag].includes(tag) && !/\.(wav|mp3|ogg|flac)(?:\?|$)/i.test(source(item) || ""));
      if (asset && source(asset)) return source(asset);
    }
    return null;
  }
  function showImage(tags = ["idle", "default"]) {
    fallbackTried = false; const image = get("avatar"); const url = findAsset(tags);
    if (!url) { image.hidden = true; get("placeholder").hidden = false; return; }
    image.hidden = false; get("placeholder").hidden = true;
    if (image.getAttribute("src") !== url) image.src = url;
  }
  get("avatar").onerror = () => {
    const image = get("avatar"); const fallback = findAsset(["idle", "default"]);
    if (!fallbackTried && fallback && image.getAttribute("src") !== fallback) { fallbackTried = true; image.src = fallback; }
    else { image.hidden = true; get("placeholder").hidden = false; }
  };
  function send(message) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }
  function acknowledge(type, job, error) {
    if (!job || active !== job || job.generation !== generation) return;
    send({type, job_id: job.job_id, playback_token: job.playback_token, ...(error ? {error: String(error).slice(0, 300)} : {})});
  }
  function stop() {
    generation += 1; active = null;
    audio.onplaying = null; audio.onended = null; audio.onerror = null;
    audio.pause(); audio.removeAttribute("src"); audio.load();
    get("subtitle").textContent = ""; get("avatar-wrap").classList.remove("speaking"); showImage();
  }
  function fail(job, error, autoplay = false) {
    if (active !== job || generation !== job.generation) return;
    acknowledge("failed", job, error);
    stop(); status(`音频未播放：${String(error).slice(0, 140)}`, true);
    if (autoplay) get("enable-audio").hidden = false;
  }
  function play(message) {
    stop(); character = message.character || character; assets = message.assets || assets;
    const job = {...message, generation, started: false}; active = job;
    if (!message.audio_ref) { fail(job, "播放指令缺少音频文件"); return; }
    showImage([message.animation_id, message.emotion, "talking", "talk", "idle", "default"]);
    audio.onplaying = () => {
      if (active !== job || generation !== job.generation || job.started) return;
      job.started = true; get("enable-audio").hidden = true;
      get("subtitle").textContent = message.text || "";
      get("avatar-wrap").classList.add("speaking");
      acknowledge("started", job); status(`${character?.name || "角色"} · 播放中`);
    };
    audio.onended = () => {
      if (active !== job || generation !== job.generation) return;
      if (!job.started) { fail(job, "音频未开始便结束"); return; }
      acknowledge("ended", job); stop(); status("播放器就绪");
    };
    audio.onerror = () => fail(job, audio.error?.message || "音频读取失败");
    audio.src = message.audio_ref;
    const promise = audio.play();
    if (promise) promise.catch(error => fail(job, error.message || "浏览器拒绝播放", error.name === "NotAllowedError"));
  }
  get("enable-audio").onclick = async () => {
    // A user gesture unlocks playback for future commands. Never replay a failed job.
    const bytes = new Uint8Array(844); const view = new DataView(bytes.buffer);
    const write = (offset, value) => { for (let index = 0; index < value.length; index += 1) bytes[offset + index] = value.charCodeAt(index); };
    write(0, "RIFF"); view.setUint32(4, bytes.length - 8, true); write(8, "WAVEfmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, 8000, true); view.setUint32(28, 8000, true); view.setUint16(32, 1, true);
    view.setUint16(34, 8, true); write(36, "data"); view.setUint32(40, 800, true); bytes.fill(128, 44);
    const url = URL.createObjectURL(new Blob([bytes], {type: "audio/wav"})); const unlock = new Audio(url);
    try { await unlock.play(); unlock.pause(); get("enable-audio").hidden = true; status(knownOwner ? "音频已启用，等待下一次播报" : "音频已启用，等待播放器连接"); }
    catch { status("请在浏览器站点设置中允许自动播放音频，再注入下一条事件。", true); }
    finally { URL.revokeObjectURL(url); }
  };
  function connect() {
    clearTimeout(reconnectTimer);
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/overlay`);
    socket.onopen = () => { status("已连接，等待播放权确认"); };
    socket.onmessage = event => {
      let message; try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "hello") {
        knownOwner = true; character = message.character || character; assets = message.assets || assets;
        showImage(); status(`${character?.name || "角色"} · 播放器就绪`);
      } else if (message.type === "standby") {
        knownOwner = false; stop(); status(message.reason || "已有播放页面，当前页面待机");
      } else if (message.type === "play") {
        if (!knownOwner) return;
        if (active?.job_id === message.job_id && active?.playback_token === message.playback_token) return;
        play(message);
      } else if (message.type === "stop") {
        if (!message.job_id || !active || active.job_id === message.job_id) { stop(); status(knownOwner ? "播报已停止" : "播放器待机"); }
      } else if (message.type === "character") {
        stop(); character = message.character || character; assets = message.assets || assets;
        showImage(); status(`${character?.name || "角色"} · ${knownOwner ? "播放器就绪" : "待机"}`);
      }
    };
    socket.onclose = event => {
      knownOwner = false; stop();
      if (event.code === 1008 || event.code === 4401 || event.code === 4403) { status("播放器凭证无效。请从控制台复制新的播放器链接。", true); return; }
      status("播放器已断线，正在重连；未完成任务不会重播。", true);
      reconnectTimer = setTimeout(connect, 3000);
    };
    socket.onerror = () => { status("播放器连接失败，检查服务与凭证。", true); };
  }
  async function boot() {
    const token = new URLSearchParams(location.hash.slice(1)).get("token");
    if (token) {
      try {
        const response = await fetch("/api/overlay/auth", {method: "POST", headers: {"Content-Type": "application/json"}, credentials: "same-origin", body: JSON.stringify({token})});
        if (!response.ok) throw new Error("播放器凭证无效，请重新复制控制台中的链接。");
        history.replaceState(null, "", location.pathname + location.search);
      } catch (error) { status(error.message, true); return; }
    }
    connect();
  }
  window.addEventListener("pagehide", () => { clearTimeout(reconnectTimer); stop(); socket?.close(); });
  boot();
})();
