import { api } from "./api.js";
import { PrivatePlayback } from "./private-call-playback.js";
import { guardCall } from "./private-call-guard.js";
import { FinalUtterances, transcriptReview } from "./private-call-handoff.js";

const byId = id => document.getElementById(id);
const open = byId("private-call-open"), dialog = byId("private-call-dialog");
const start = byId("private-call-start"), end = byId("private-call-end"), status = byId("private-call-state");
const workspace = byId("workspace"), chat = open.closest("[data-section='chat']");
let current;
const temporary = byId("private-call-temporary");
const options = byId("private-call-options"), topic = byId("private-call-topic");
temporary.onchange = () => { options.hidden = temporary.checked; };
const mute = byId("private-call-mute");
mute.onclick = () => {
  const playback = current?.playback;
  if (!playback) return;
  playback.setMuted(!playback.muted);
  mute.setAttribute("aria-pressed", String(playback.muted));
  mute.textContent = playback.muted ? "恢复播放" : "播放静音";
};

async function check() {
  if (chat.hidden || workspace.hidden || !window.isSecureContext || !navigator.mediaDevices?.getUserMedia ||
      !window.AudioWorkletNode || !window.AudioContext) return;
  try {
    const state = await api("/api/chat/private-call");
    open.disabled = open.hidden = !state.configured || workspace.hidden || chat.hidden;
    const recordings = byId("private-call-recordings");
    recordings.replaceChildren();
    for (const [profile, label] of [["mature", "常规版通话实录"], ["q", "Q 版通话实录"]]) {
      const url = state.recordings?.[profile];
      if (!url) continue;
      const link = document.createElement("a");
      link.href = url;
      link.textContent = label;
      link.target = "_blank";
      link.rel = "noopener";
      recordings.append(link);
    }
    recordings.hidden = !recordings.childElementCount;
  }
  catch { open.disabled = open.hidden = true; }
}
const visibility = new MutationObserver(check);
for (const target of [workspace, chat]) visibility.observe(target, {attributes: true, attributeFilter: ["hidden"]});
check();

function finish(call, cause, message, code = null) {
  if (call !== current || !call) return;
  current = null;
  call.playback?.diagnose?.("call_end", cause, code);
  mute.hidden = true;
  mute.textContent = "播放静音";
  mute.setAttribute("aria-pressed", "false");
  call.review?.close();
  temporary.disabled = false;
  topic.disabled = false;
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
  stream.getAudioTracks()[0].onended = () => finish(call, "microphone_ended", "麦克风已断开");
  call.capture = new AudioWorkletNode(call.context, "private-call-capture");
  call.source = call.context.createMediaStreamSource(stream);
  call.source.connect(call.capture);
  call.capture.connect(call.context.destination);
  call.capture.port.onmessage = ({data}) => {
    if (current !== call) return;
    if (call.socket.bufferedAmount > 128000) { finish(call, "realtime_send_backlog", "网络积压，通话已结束"); return; }
    const audio = btoa(String.fromCharCode(...new Uint8Array(data)));
    call.send({type: "input_audio_buffer.append", format: "pcm16", sample_rate_hz: 16000, audio});
  };
  status.textContent = `正在与${identity}私聊 · 麦克风已开启`;
}

async function begin(previous = null) {
  if (current) return;
  const call = {abort: new AbortController(), utterances: new FinalUtterances(), send(event) {
    if (this.invalidated && event.type === "playback.ack") return;
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(event));
  }};
  current = call;
  start.disabled = true;
  end.textContent = "挂断";
  status.textContent = "正在连接私聊语音…";
  call.deadline = setTimeout(() => finish(call, "realtime_handshake_timeout", "语音握手超时"), 30000);
  try {
    call.context = new AudioContext({sampleRate: 16000});
    await call.context.resume();
    if (current !== call) return;
    const ephemeral = call.ephemeral = temporary.checked;
    temporary.disabled = topic.disabled = true;
    const subject = ephemeral ? "" : topic.value.trim();
    const turn = !ephemeral && !subject && (previous ? previous.turn :
      byId("operator-chat-feed").lastElementChild?.dataset.requestId);
    const config = await api("/api/chat/private-call/ticket", {method: "POST", signal: call.abort.signal,
      body: {temporary: ephemeral, ...(subject ? {topic: subject} : turn ? {topic_turn: turn} : {})}});
    if (current !== call) return;
    const use = config.topic_source ? {...config.memory_use, topic_source: config.topic_source} : config.memory_use;
    async function validateMemory() {
      if (!config.memory_use) return true;
      try {
        const path = config.topic_source ? "/api/chat/private-call/memory-validate" : "/api/agent/memory/validate";
        await api(path, {method: "POST", body: use, signal: call.abort.signal});
        return current === call;
      } catch (error) {
        invalidate(error.status === 409 ? "changed" : "closed");
        return false;
      }
    }
    if (previous && config.call_scope !== previous.scope) {
      finish(call, "scope_changed", "角色或授权已变化，请重新开始通话"); return;
    }
    function invalidate(reason) {
      if (current !== call) return;
      call.invalidated = true;
      const muted = !!call.playback?.muted;
      call.send({type: "response.cancel"});
      call.send({type: "output_audio_buffer.clear"});
      finish(call, reason === "changed" ? "memory_changed" : "memory_guard_closed", "通话记忆已变化，正在重新连接");
      if (reason === "changed" && dialog.open && !workspace.hidden) {
        void begin({scope: config.call_scope, muted, turn});
      } else status.textContent = "记忆或授权暂时无法核对，通话已结束";
    }
    if (config.memory_use) {
      call.guard = await guardCall(call, use, invalidate);
      if (current !== call) return;
    }
    if (!ephemeral) call.review = transcriptReview(byId("private-call-review"), byId("private-call-transcript"),
      byId("private-call-utterances"), config);
    const url = new URL(config.socket);
    url.search = new URLSearchParams({ticket: config.ticket, duplex: "1", autostart: "0"});
    call.playback = new PrivatePlayback(call.context, event => call.send(event));
    if (previous?.muted) {
      call.playback.setMuted(true);
      mute.textContent = "恢复播放"; mute.setAttribute("aria-pressed", "true");
    }
    call.socket = new WebSocket(url);
    mute.hidden = false;
    call.socket.onopen = () => call.send({type: "session.update", session: {
      instructions: config.instructions, modalities: ["audio", "text"], input_audio_format: "pcm16",
      output_audio_format: "pcm16", playback_commit_policy: "ack_only", extra_body: {auto_response: true},
    }});
    call.socket.onerror = () => finish(call, "realtime_ws_error", "无法连接私聊语音服务");
    call.socket.onclose = event => finish(call, "realtime_ws_close", "私聊连接已断开", event.code);
    call.socket.onmessage = async ({data}) => {
      if (current !== call) return;
      try {
        const event = JSON.parse(data);
        if (event.type === "error") {
          // A response can finish on the engine while its audio is still playing.
          if (call.ready && event.error?.type === "invalid_request_error" && event.error.code === "response_not_active") {
            call.playback.diagnose?.("response_error", "realtime_error_event", event.error.code);
            return;
          }
          finish(call, "realtime_error_event", "语音协议或设备不可用，通话已结束", event.error?.code);
          return;
        }
        if (event.type === "session.created" && !call.ready) {
          const caps = event.session?.capabilities;
          if (caps?.implementation_level !== "model_native_duplex" || (caps.chunk_period_ms ?? 1000) !== 1000)
            throw Error("未协商到支持的原生双工语音");
          clearTimeout(call.deadline);
          call.deadline = setTimeout(() => finish(call, "microphone_timeout", "麦克风授权超时，通话已结束"), 60000);
          call.ready = true;
          call.receipts = setInterval(() => call.playback.acknowledge(), 200);
          call.heartbeat = setInterval(() => call.send({type: "session.heartbeat"}), 20000);
          await capture(call, config.character);
        } else if (call.stream) {
          const text = !ephemeral && call.utterances.accept(event);
          if (text) call.review.add(text);
          if (config.memory_use && ["response.audio.delta", "response.output_audio.delta"].includes(event.type)) {
            call.pendingAudio = (call.pendingAudio || 0) + 1;
            if (call.pendingAudio > 8) { finish(call, "memory_backlog", "记忆核对积压，通话已结束"); return; }
            call.audioQueue = (call.audioQueue || Promise.resolve()).then(async () => {
              if (current === call && await validateMemory()) call.playback.event(event);
            }).catch(() => finish(call, "playback_error", "语音播放失败，通话已结束")).finally(() => { call.pendingAudio--; });
          } else call.playback.event(event);
        }
      } catch (error) {
        finish(call, "realtime_message_or_device_error", error.name === "NotAllowedError" ? "未获麦克风权限，通话已结束" :
          error.name === "NotFoundError" ? "未找到麦克风" : "语音协议或设备不可用，通话已结束");
      }
    };
  } catch (error) { finish(call, "call_setup_error", error.status ? error.message : "无法建立私聊语音连接"); }
}

open.onclick = () => {
  options.open = false;
  topic.value = "";
  status.textContent = "尚未开始通话";
  dialog.showModal();
};
start.onclick = () => begin();
end.onclick = () => { finish(current, "hangup_button", "通话已结束"); dialog.close(); };
dialog.addEventListener("close", () => finish(current, "dialog_closed", "通话已结束"));
function leave(cause) { finish(current, cause, "通话已结束"); if (dialog.open) dialog.close(); }
window.addEventListener("pagehide", () => leave("page_hide"));
window.addEventListener("hashchange", () => { if (!["", "#chat"].includes(location.hash)) leave("navigation"); });
new MutationObserver(() => { if (workspace.hidden) { leave("workspace_hidden"); open.disabled = open.hidden = true; } })
  .observe(workspace, {attributes: true, attributeFilter: ["hidden"]});
