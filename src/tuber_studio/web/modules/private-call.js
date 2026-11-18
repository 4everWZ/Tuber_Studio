import { api } from "./api.js";
import { PrivatePlayback } from "./private-call-playback.js";
import { FinalUtterances, showHandoff } from "./private-call-handoff.js";

const byId = id => document.getElementById(id);
const open = byId("private-call-open"), dialog = byId("private-call-dialog");
const start = byId("private-call-start"), end = byId("private-call-end"), status = byId("private-call-state");
const menu = byId("operator-chat-more-panel"), workspace = byId("workspace");
let current;

async function check() {
  if (menu.hidden || workspace.hidden || !window.isSecureContext || !navigator.mediaDevices?.getUserMedia ||
      !window.AudioWorkletNode || !window.AudioContext) return;
  try { open.disabled = open.hidden = !(await api("/api/chat/private-call")).configured || workspace.hidden; }
  catch { open.disabled = open.hidden = true; }
}
new MutationObserver(check).observe(menu, {attributes: true, attributeFilter: ["hidden"]});

function finish(call, message) {
  if (call !== current || !call) return;
  current = null;
  byId("private-call-utterances").replaceChildren();
  call.abort.abort();
  clearTimeout(call.deadline);
  clearInterval(call.receipts);
  clearInterval(call.heartbeat);
  if (call.capture) { call.capture.port.onmessage = null; call.capture.port.close(); call.capture.disconnect(); }
  call.source?.disconnect();
  call.stream?.getTracks().forEach(track => track.stop());
  call.playback?.clear();
  call.send({type: "session.close"});
  if (call.socket) {
    call.socket.onmessage = call.socket.onopen = call.socket.onclose = call.socket.onerror = null;
    call.socket.close();
  }
  if (call.context) void call.context.close().catch(() => {});
  start.disabled = false;
  end.textContent = "关闭";
  status.textContent = message;
}

async function capture(call, identity) {
  status.textContent = "等待麦克风授权…";
  await call.context.audioWorklet.addModule("/static/modules/private-call-capture.js");
  if (current !== call) return;
  const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true,
    noiseSuppression: true}, video: false});
  if (current !== call) { stream.getTracks().forEach(track => track.stop()); return; }
  call.stream = stream;
  clearTimeout(call.deadline);
  stream.getAudioTracks()[0].onended = () => finish(call, "麦克风已断开");
  call.capture = new AudioWorkletNode(call.context, "private-call-capture");
  call.source = call.context.createMediaStreamSource(stream);
  call.source.connect(call.capture);
  call.capture.connect(call.context.destination);
  call.capture.port.onmessage = ({data}) => {
    if (current !== call) return;
    if (call.socket.bufferedAmount > 128000) { finish(call, "网络积压，通话已结束"); return; }
    const audio = btoa(String.fromCharCode(...new Uint8Array(data)));
    call.send({type: "input_audio_buffer.append", format: "pcm16", sample_rate_hz: 16000, audio});
  };
  status.textContent = `正在与${identity}私聊 · 麦克风已开启`;
}

async function begin() {
  if (current) return;
  const call = {abort: new AbortController(), utterances: new FinalUtterances(), send(event) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(event));
  }};
  current = call;
  start.disabled = true;
  end.textContent = "挂断";
  status.textContent = "正在连接私聊语音…";
  call.deadline = setTimeout(() => finish(call, "语音握手超时"), 30000);
  try {
    call.context = new AudioContext({sampleRate: 16000});
    await call.context.resume();
    if (current !== call) return;
    const config = await api("/api/chat/private-call/ticket", {method: "POST", signal: call.abort.signal});
    if (current !== call) return;
    const url = new URL(config.socket);
    url.search = new URLSearchParams({ticket: config.ticket, duplex: "1", autostart: "0"});
    call.socket = new WebSocket(url);
    call.playback = new PrivatePlayback(call.context, event => call.send(event));
    call.socket.onopen = () => call.send({type: "session.update", session: {
      instructions: config.instructions, modalities: ["audio", "text"], input_audio_format: "pcm16",
      output_audio_format: "pcm16", playback_commit_policy: "ack_only", extra_body: {auto_response: true},
    }});
    call.socket.onerror = () => finish(call, "无法连接私聊语音服务");
    call.socket.onclose = () => finish(call, "私聊连接已断开");
    call.socket.onmessage = async ({data}) => {
      if (current !== call) return;
      try {
        const event = JSON.parse(data);
        if (event.type === "error") throw Error("服务未接受本次通话");
        if (event.type === "session.created" && !call.ready) {
          const caps = event.session?.capabilities;
          if (caps?.implementation_level !== "model_native_duplex" || (caps.chunk_period_ms ?? 1000) !== 1000)
            throw Error("未协商到支持的原生双工语音");
          clearTimeout(call.deadline);
          call.deadline = setTimeout(() => finish(call, "麦克风授权超时，通话已结束"), 60000);
          call.ready = true;
          call.receipts = setInterval(() => call.playback.acknowledge(), 200);
          call.heartbeat = setInterval(() => call.send({type: "session.heartbeat"}), 20000);
          await capture(call, config.character);
        } else if (call.stream) {
          const text = call.utterances.accept(event);
          if (text) showHandoff(byId("private-call-utterances"), text);
          call.playback.event(event);
        }
      } catch (error) {
        finish(call, error.name === "NotAllowedError" ? "未获麦克风权限，通话已结束" :
          error.name === "NotFoundError" ? "未找到麦克风" : "语音协议或设备不可用，通话已结束");
      }
    };
  } catch (error) { finish(call, error.status ? error.message : "无法建立私聊语音连接"); }
}

open.onclick = () => {
  byId("operator-chat-more-toggle").click();
  status.textContent = "尚未开始通话";
  dialog.showModal();
};
start.onclick = begin;
end.onclick = () => { finish(current, "通话已结束"); dialog.close(); };
dialog.addEventListener("close", () => finish(current, "通话已结束"));
function leave() { finish(current, "通话已结束"); if (dialog.open) dialog.close(); }
window.addEventListener("pagehide", leave);
window.addEventListener("hashchange", () => { if (!["", "#chat"].includes(location.hash)) leave(); });
new MutationObserver(() => { if (workspace.hidden) { leave(); open.disabled = open.hidden = true; } })
  .observe(workspace, {attributes: true, attributeFilter: ["hidden"]});
