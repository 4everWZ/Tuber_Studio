import {api} from "./api.js";
import {PrivatePlayback} from "./private-call-playback.js";

const base = "/api/chat/private-call/avatars";
const dialog = document.getElementById("private-call-dialog");
const select = document.createElement("select"), label = document.createElement("label");
label.append("通话形象 ", select);
label.hidden = true;
const video = document.createElement("video"), caption = document.createElement("div");
video.playsInline = true;
video.autoplay = true;
video.hidden = true;
video.width = 240;
video.setAttribute("aria-label", "实时通话形象");
const interrupt = document.createElement("button");
interrupt.type = "button";
interrupt.textContent = "打断回答";
interrupt.hidden = true;
document.getElementById("private-call-avatar").before(label, video, caption, interrupt);
let selection, revision = 0;
new MutationObserver(async () => {
  const version = ++revision;
  selection = null;
  label.hidden = true;
  select.replaceChildren(new Option("仅语音", ""));
  if (!dialog.open || !window.RTCPeerConnection) return;
  try {
    const data = await api(base);
    if (revision !== version || !data.profiles.includes("original") || !data.profiles.includes("q")) return;
    selection = data.selection;
    select.add(new Option("常规版", "original"));
    select.add(new Option("Q 版", "q"));
    label.hidden = false;
  } catch { /* Voice calls remain available independently. */ }
}).observe(dialog, {attributes: true, attributeFilter: ["open"]});

const release = path => path && fetch(path, {method: "DELETE", keepalive: true}).catch(() => {});
const iceGatherTimeout = Symbol("ice_gather_timeout");

// Only retain recognized Realtime control and transport codes. Providers may
// put arbitrary strings in error.code, error.param and error.message.
const realtimeCodes = new Set([
  "bad_event", "bad_audio", "invalid_json", "unknown_event", "event_too_large",
  "session_closed", "unknown_session", "response_not_active", "response_already_active",
  "playback_ack_too_late", "playback_item_not_found", "playback_item_mismatch", "item_not_found",
  "input_audio_buffer_empty", "input_backpressure", "output_backpressure", "resource_exhausted",
  "internal_error", "engine_error", "commit_aborted", "runtime_append_failed",
  "runtime_append_task_failed", "runtime_abort_failed", "runtime_signal_failed",
  "runtime_data_plane_stream_failed", "runtime_data_plane_text_without_audio", "runtime_touch_failed",
]);

export function portraitPlayback(call, url) {
  select.disabled = true;
  call.abort.signal.addEventListener("abort", () => { select.disabled = false; }, {once: true});
  if (!selection || !select.value) return null;
  url.searchParams.set("avatar", "1");
  return new PortraitPlayback(call, select.value, selection);
}

class PortraitPlayback {
  constructor(call, profile, selection) {
    this.call = call;
    this.profile = profile;
    this.selection = selection;
    this.fallback = new PrivatePlayback(call.context, event => call.send(event));
    this.spans = [];
    this.subtitles = [];
    this.version = 0;
    this.generation = 0;
    if ("jitterBufferTarget" in (window.RTCRtpReceiver?.prototype || {})) {
      this.smooth = document.createElement("input");
      this.smooth.type = "checkbox";
      this.smoothLabel = document.createElement("label");
      this.smoothLabel.append(this.smooth, "减少画面卡顿");
      this.smoothLabel.title = "音视频同时增加接收缓冲，播放可能稍晚";
      this.smoothLabel.hidden = true;
      interrupt.before(this.smoothLabel);
      this.smooth.onchange = () => this.applyBufferTarget();
    }
    this.resetResponse();
    this.phase = "attach";
    this.deadline = setTimeout(() => this.fail("attach_timeout"), 30000);
    caption.textContent = "正在连接实时形象…";
    call.abort.signal.addEventListener("abort", () => this.close(), {once: true});
    interrupt.hidden = false;
    interrupt.onclick = () => {
      this.diagnose("interrupt", "interrupt_button");
      this.block();
      this.dropPeer();
      // The native duplex engine cancels active output and clears queued playback
      // with one clear. A separate cancel also fences the Avatar source.
      call.send({type: "output_audio_buffer.clear"});
    };
  }

  // Emit only local enums and counters, before closing the transports.
  diagnose(action, cause, code = null) {
    const peer = this.peer, socket = this.call.socket;
    const diagnosticCode = cause === "realtime_error_event"
      ? (realtimeCodes.has(code) ? code : "other")
      : (Number.isInteger(code) ? code : null);
    window.dispatchEvent(new CustomEvent("studio-whep-diagnostic", {detail: {
      ms: performance.now(), action, cause, phase: this.phase, version: this.version,
      generation: Number.isSafeInteger(this.generation) ? this.generation : null,
      code: diagnosticCode, peer: peer?.connectionState ?? null,
      ice: peer?.iceConnectionState ?? null, gathering: peer?.iceGatheringState ?? null,
      signaling: peer?.signalingState ?? null, eventsWs: this.events?.readyState ?? null,
      realtimeWs: socket?.readyState ?? null, realtimeBufferedBytes: socket?.bufferedAmount ?? null,
    }}));
  }

  resetResponse() {
    this.response = null;
    this.received = this.lastAck = 0;
    this.item = null;
  }

  block() {
    this.blocked = this.response;
    this.spans = [];
    this.subtitles = [];
    caption.textContent = "";
    this.fallback.clear();
  }

  async gateway(event) {
    if (this.attaching || this.closed || this.failed) return;
    this.attaching = true;
    try {
      if (!event.avatar_available || !event.source_session_id) { this.fail("source_unavailable"); return; }
      const joined = await api(base, {method: "POST", body: {profile: this.profile,
        selection: this.selection, source_session_id: event.source_session_id, guard: this.call.guard}});
      const path = base + "/" + joined.handle;
      if (this.closed || this.failed) { await release(path); return; }
      this.path = path;
      this.generation = joined.generation;
      this.phase = "await_snapshot";
      this.events = new WebSocket(joined.events);
      this.events.onmessage = message => {
        try { this.avatarEvent(JSON.parse(message.data)); } catch { this.fail("avatar_event_error"); }
      };
      this.events.onclose = event => this.fail("events_ws_close", event.code);
      this.events.onerror = () => this.fail("events_ws_error");
      this.heartbeat = setInterval(async () => {
        try { if ((await api(path)).state !== "active") this.fail("heartbeat_inactive"); }
        catch (error) { this.fail("heartbeat_error", error?.status); }
      }, 15000);
    } catch (error) { this.fail("attach_error", error?.status); }
  }

  avatarEvent(event) {
    if (this.closed || this.failed) return;
    if (event.type === "avatar.snapshot" || event.type === "avatar.cleared") {
      if (event.generation < this.generation) return;
      this.generation = event.generation;
      this.spans = [];
      this.subtitles = [];
      caption.textContent = "";
      this.diagnose("resubscribe", event.type);
      this.resubscribe();
    } else if (["avatar.failed", "avatar.closed", "avatar.resync_required"].includes(event.type)) {
      this.fail(event.type);
    } else if (event.generation === this.generation) {
      if (event.type === "avatar.audio.queued") this.spans = [...this.spans.slice(-119), event];
      if (event.type === "avatar.subtitle") this.subtitles = [...this.subtitles.slice(-119), event];
    }
  }

  dropPeer() {
    this.version++;
    clearTimeout(this.reconnect);
    this.stopGather?.();
    const peer = this.peer;
    this.peer = null;
    peer?.close();
    video.onplaying = video.onpause = video.onwaiting = null;
    video.pause();
    video.muted = true;
    video.srcObject?.getTracks().forEach(track => track.stop());
    video.srcObject = null;
    video.hidden = true;
    const location = this.location;
    this.location = null;
    this.lastTimeline = -1;
    this.audibleStart = null;
    return release(location);
  }

  resubscribe() {
    const dropped = this.dropPeer(), version = this.version;
    this.phase = "release_previous";
    clearTimeout(this.deadline);
    this.deadline = setTimeout(() => this.fail("subscribe_timeout"), 20000);
    // The clear and its acknowledgement can fence successive generations.
    this.reconnect = setTimeout(async () => {
      await dropped;
      if (version !== this.version) return;
      try { await this.subscribe(version); }
      catch (error) {
        if (version === this.version) this.fail(error === iceGatherTimeout ? "ice_gather_timeout" : "subscribe_error");
      }
    }, 100);
  }

  applyBufferTarget() {
    for (const receiver of this.peer?.getReceivers() || []) {
      receiver.jitterBufferTarget = this.smooth.checked ? 240 : null;
    }
  }

  async subscribe(version) {
    this.phase = "local_offer";
    const peer = this.peer = new RTCPeerConnection({iceServers: []});
    const stream = new MediaStream();
    peer.addTransceiver("audio", {direction: "recvonly"});
    peer.addTransceiver("video", {direction: "recvonly"});
    if (this.smooth?.checked) this.applyBufferTarget();
    peer.ontrack = ({track}) => stream.addTrack(track);
    peer.onconnectionstatechange = () => {
      if (this.peer !== peer) return;
      this.diagnose("state", "peer_state_change");
      if (peer.connectionState === "failed") this.fail("peer_failed");
    };
    peer.oniceconnectionstatechange = () => {
      if (this.peer === peer) this.diagnose("state", "ice_state_change");
    };
    await peer.setLocalDescription(await peer.createOffer());
    if (version !== this.version) return;
    this.phase = "ice_gathering";
    if (peer.iceGatheringState !== "complete") await new Promise((resolve, reject) => {
      const timer = setTimeout(reject, 8000, iceGatherTimeout);
      this.stopGather = () => { clearTimeout(timer); resolve(); };
      peer.onicegatheringstatechange = () => {
        if (peer.iceGatheringState === "complete") { clearTimeout(timer); resolve(); }
      };
    });
    if (version !== this.version) return;
    this.stopGather = null;
    this.phase = "whep_offer";
    const reply = await fetch(this.path + "/whep", {method: "POST", headers: {"Content-Type": "application/sdp"},
      body: peer.localDescription.sdp});
    if (!reply.ok) {
      if (version === this.version) this.fail("whep_http_error", reply.status);
      return;
    }
    const location = reply.headers.get("Location");
    if (version !== this.version) { await release(location); return; }
    this.location = location;
    this.origin = Number(reply.headers.get("X-Avatar-Timeline-Origin-Ms"));
    this.phase = "remote_answer";
    await peer.setRemoteDescription({type: "answer", sdp: await reply.text()});
    if (version !== this.version) return;
    video.srcObject = stream;
    video.muted = !!this.muted;
    video.hidden = false;
    video.onplaying = () => { this.audibleStart = this.origin + video.currentTime * 1000; };
    video.onpause = video.onwaiting = () => { this.audibleStart = null; };
    this.phase = "start_playback";
    await video.play();
    if (version === this.version) {
      this.phase = "playing";
      this.diagnose("playback", "video_play");
      if (this.smoothLabel) this.smoothLabel.hidden = false;
      clearTimeout(this.deadline);
    }
  }

  event(event) {
    if (this.closed) return;
    const id = event.response_id || event.response?.id;
    const epoch = event.metadata?.epoch ?? event.epoch;
    if (Number.isInteger(epoch)) {
      if (epoch < this.epoch) return;
      if (epoch > this.epoch) this.block();
      this.epoch = epoch;
    }
    if (event.type === "barge_in" || event.type === "output_audio_buffer.cleared" ||
        (event.type === "turn.signal" && event.event === "barge_in")) {
      // The Avatar subscription restarts on its own generation fence.
      this.blocked = id || this.response;
      this.fallback.event(event);
      return;
    }
    if (["response.audio.delta", "response.output_audio.delta"].includes(event.type) && id) {
      if (this.response !== id) { this.resetResponse(); this.response = id; }
      if (id === this.blocked) return;
      this.item = event.item_id || this.item || "item_" + id;
      this.received += atob(event.delta).length / 2 * 1000 / (event.sample_rate_hz || 24000);
      if (this.muted) this.blocked = id;
    }
    if (this.failed) this.fallback.event(event);
  }

  acknowledge() {
    if (this.failed) { this.fallback.acknowledge(); return; }
    if (!this.peer || this.peer.connectionState !== "connected" || this.events?.readyState !== WebSocket.OPEN ||
        video.paused || video.muted || !video.volume || video.readyState < 3 || !video.currentTime ||
        !video.srcObject?.getAudioTracks().some(t => t.readyState === "live" && !t.muted)) return;
    const time = Math.floor(this.origin + video.currentTime * 1000);
    if (!(time > this.lastTimeline)) return;
    this.lastTimeline = time;
    this.events.send(JSON.stringify({type: "playback.ack", generation: this.generation, played_ms: time}));
    while (this.subtitles.length && this.subtitles[0].start_ms <= time) {
      const sub = this.subtitles.shift();
      if (sub.response_id !== this.blocked) caption.textContent = (caption.textContent + sub.text).slice(-800);
    }
    if (!this.response || this.blocked === this.response) return;
    // The receiver may have joined mid-reply. Only commit a contiguous
    // prefix whose corresponding samples fell on this audible timeline.
    let played = this.lastAck;
    for (const span of this.spans) {
      if (span.response_id !== this.response || span.source_start_ms > played + 1 ||
          this.audibleStart === null || this.audibleStart > span.start_ms + played - span.source_start_ms + 1 ||
          span.start_ms > time) continue;
      played = Math.max(played, Math.floor(Math.min(this.received,
        span.source_start_ms + Math.min(time, span.end_ms) - span.start_ms)));
    }
    if (played > this.lastAck) {
      this.call.send({type: "playback.ack", response_id: this.response, item_id: this.item,
        played_ms: played, committed_ms: played});
      this.lastAck = played;
    }
  }

  speaking() { return this.failed && this.fallback.speaking(); }
  clear() { this.fallback.clear(); }
  setMuted(value) {
    this.muted = video.muted = value;
    if (value) this.blocked = this.response;
    this.fallback.setMuted(value);
  }

  detach() {
    this.smoothLabel?.remove();
    clearTimeout(this.deadline);
    clearInterval(this.heartbeat);
    if (this.events) { this.events.onclose = this.events.onerror = null; this.events.close(); }
    this.dropPeer();
    void release(this.path);
    this.path = null;
  }

  fail(cause, code = null) {
    if (this.failed || this.closed) return;
    this.diagnose("voice_fallback", cause, code);
    this.failed = true;
    this.blocked = this.response;
    this.detach();
    caption.textContent = "实时形象不可用，继续语音通话";
  }

  close() {
    this.closed = true;
    this.detach();
    this.fallback.clear();
    caption.textContent = "";
    interrupt.hidden = true;
    interrupt.onclick = null;
  }
}
