"use strict";
(() => {
  const get = id => document.getElementById(id), audio = new Audio(); audio.preload = "auto";
  const renderer = new LiveScene(get("scene"));
  let socket, active, knownOwner = false, generation = 0, reconnectTimer, view = null, loading = false;
  function status(text, error = false) { get("connection").textContent = text; get("connection").classList.toggle("error",error); }
  function send(data) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data)); }
  function valid(job) { return job && active === job && job.generation === generation; }
  function acknowledge(type, job, error) { if (valid(job)) send({type,job_id:job.job_id,playback_token:job.playback_token,...(error ? {error:String(error).slice(0,300)} : {})}); }
  function mouth(job) { if (!job?.started || audio.paused || audio.ended || audio.volume === 0) return 0; const activity = job.activity; return activity?.envelope[Math.floor(audio.currentTime / activity.step)] || 0; }
  function report() {
    if (!active?.started || !knownOwner) return;
    send({type:"progress",job_id:active.job_id,playback_token:active.playback_token,position:audio.currentTime,paused:audio.paused,mouth:mouth(active)});
  }
  function draw() {
    if (!view) return;
    const projection = {...view};
    projection.playback = active ? {song:active.media,text:active.text,animation_id:active.animation_id,emotion:active.emotion,position:audio.currentTime,duration:active.duration,started:active.started,paused:audio.paused,mouth:mouth(active)} : null;
    renderer.update(projection);
  }
  function stop() {
    generation++; active = null; audio.onplaying = null; audio.onended = null; audio.onerror = null;
    audio.pause(); audio.removeAttribute("src"); audio.load(); draw();
  }
  function fail(job,error,autoplay = false) {
    if (!valid(job)) return; acknowledge("failed",job,error); stop(); status(`音频未播放：${String(error).slice(0,140)}`,true);
    if (autoplay) get("enable-audio").hidden = false;
  }
  async function play(message) {
    stop(); const job = {...message,generation,started:false}; active = job;
    if (!message.audio_ref) return fail(job,"播放指令缺少音频文件");
    audio.volume = typeof message.volume === "number" ? Math.max(0,Math.min(1,message.volume)) : 1;
    audio.onplaying = () => { if (!valid(job)) return; if (!job.started) { job.started = true; acknowledge("started",job); } get("enable-audio").hidden = true; status(""); report(); draw(); };
    audio.onended = () => { if (!valid(job)) return; if (!job.started) return fail(job,"音频未开始便结束"); acknowledge("ended",job); stop(); status(""); };
    audio.onerror = () => fail(job,audio.error?.message || "音频读取失败"); audio.src = message.audio_ref;
    try { await audio.play(); } catch(error) { fail(job,error.message,error.name === "NotAllowedError"); }
  }
  async function sync() {
    if (loading) return; loading = true;
    try { const response = await fetch("/api/live/view",{credentials:"same-origin"}); if (!response.ok) return; view = await response.json(); if (JSON.stringify(view.scene) !== renderer.signature) renderer.load(view.scene); draw(); }
    catch { /* The playback websocket owns connection failure and cancellation. */ }
    finally { loading = false; }
  }
  function connect() {
    clearTimeout(reconnectTimer);
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/overlay`);
    socket.onopen = () => status("已连接，等待播放权确认");
    socket.onmessage = event => {
      let message; try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "hello") { knownOwner = true; status(""); sync(); }
      else if (message.type === "standby") { knownOwner = false; stop(); status(message.reason || "已有播放页面，当前待机"); sync(); }
      else if (message.type === "play" && knownOwner) { if (active?.job_id !== message.job_id || active?.playback_token !== message.playback_token) play(message); }
      else if (message.type === "stop") { if (!message.job_id || !active || active.job_id === message.job_id) { stop(); status(""); } }
      else if (message.type === "character") { stop(); sync(); }
      else if (message.type === "media-control" && knownOwner && active?.job_id === message.job_id) {
        if (message.paused) { audio.pause(); report(); draw(); }
        else { const job = active; audio.play().catch(error => fail(job,error.message)); }
      }
    };
    socket.onclose = event => { knownOwner = false; stop(); if ([1008,4401,4403].includes(event.code)) return status("播放器凭证无效，请从工作台打开新的播放器。",true); status("播放器已断线，正在重连；未完成任务不会重播。",true); reconnectTimer = setTimeout(connect,3000); };
    socket.onerror = () => status("播放器连接失败，检查服务与凭证。",true);
  }
  get("enable-audio").onclick = async () => {
    const bytes = new Uint8Array(844), data = new DataView(bytes.buffer);
    const write = (offset,text) => { for (let i=0;i<text.length;i++) bytes[offset+i] = text.charCodeAt(i); };
    write(0,"RIFF"); data.setUint32(4,836,true); write(8,"WAVEfmt "); data.setUint32(16,16,true); data.setUint16(20,1,true); data.setUint16(22,1,true); data.setUint32(24,8000,true); data.setUint32(28,8000,true); data.setUint16(32,1,true); data.setUint16(34,8,true); write(36,"data"); data.setUint32(40,800,true); bytes.fill(128,44);
    const url = URL.createObjectURL(new Blob([bytes],{type:"audio/wav"})), unlock = new Audio(url);
    try { await unlock.play(); unlock.pause(); get("enable-audio").hidden = true; status("音频已启用，等待下一次播报"); }
    catch { status("请允许站点自动播放音频，再注入下一条事件。",true); } finally { URL.revokeObjectURL(url); }
  };
  async function boot() {
    const token = new URLSearchParams(location.hash.slice(1)).get("token");
    if (token) { try { const response = await fetch("/api/overlay/auth",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",body:JSON.stringify({token})}); if (!response.ok) throw new Error("播放器凭证无效"); history.replaceState(null,"",location.pathname+location.search); } catch(error) { return status(error.message,true); } }
    await sync(); connect();
  }
  const tick = setInterval(draw,100), heartbeat = setInterval(report,500), refresh = setInterval(sync,1000);
  window.addEventListener("pagehide",() => { clearInterval(tick); clearInterval(heartbeat); clearInterval(refresh); clearTimeout(reconnectTimer); stop(); socket?.close(); });
  boot();
})();
