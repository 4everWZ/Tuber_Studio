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
    select.add(new Option("常规原稿", "original"));
    select.add(new Option("Q 版原稿", "q"));
    label.hidden = false;
  } catch { /* Voice calls remain available independently. */ }
}).observe(dialog, {attributes: true, attributeFilter: ["open"]});

const release = path => path && fetch(path, {method: "DELETE", keepalive: true}).catch(() => {});

export function portraitPlayback(call, url, memoryUse) {
  select.disabled = true;
  call.abort.signal.addEventListener("abort", () => { select.disabled = false; }, {once: true});
  if (!selection || !select.value) return null;
  // Revocable speech must pass the private call's chunk check before playback.
  if (memoryUse) {
    caption.textContent = "本次通话引用了可撤回的记忆，使用逐段核验的语音播放";
    call.abort.signal.addEventListener("abort", () => { caption.textContent = ""; }, {once: true});
    return null;
  }
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
    this.resetResponse();
    this.deadline = setTimeout(() => this.fail(), 30000);
    caption.textContent = "正在连接实时形象…";
    call.abort.signal.addEventListener("abort", () => this.close(), {once: true});
    interrupt.hidden = false;
    interrupt.onclick = () => {
      this.block();
      this.dropPeer();
      call.send({type: "response.cancel"});
      call.send({type: "output_audio_buffer.clear"});
    };
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
      if (!event.avatar_available || !event.source_session_id) throw Error();
      const joined = await api(base, {method: "POST", body: {profile: this.profile,
        selection: this.selection, source_session_id: event.source_session_id}});
      const path = base + "/" + joined.handle;
      if (this.closed || this.failed) { await release(path); return; }
      this.path = path;
      this.generation = joined.generation;
      this.events = new WebSocket(joined.events);
      this.events.onmessage = message => {
        try { this.avatarEvent(JSON.parse(message.data)); } catch { this.fail(); }
      };
      this.events.onclose = this.events.onerror = () => this.fail();
      this.heartbeat = setInterval(async () => {
        try { if ((await api(path)).state !== "active") this.fail(); } catch { this.fail(); }
      }, 15000);
    } catch { this.fail(); }
  }

  avatarEvent(event) {
    if (this.closed || this.failed) return;
    if (event.type === "avatar.snapshot" || event.type === "avatar.cleared") {
      if (event.generation < this.generation) return;
      this.generation = event.generation;
      this.spans = [];
      this.subtitles = [];
      caption.textContent = "";
      this.resubscribe();
    } else if (["avatar.failed", "avatar.closed", "avatar.resync_required"].includes(event.type)) {
      this.fail();
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
    clearTimeout(this.deadline);
    this.deadline = setTimeout(() => this.fail(), 20000);
    // cancel + clear + cleared can fence several generations in one turn.
    this.reconnect = setTimeout(async () => {
      await dropped;
      if (version !== this.version) return;
      try { await this.subscribe(version); }
      catch { if (version === this.version) this.fail(); }
    }, 100);
  }

  async subscribe(version) {
    const peer = this.peer = new RTCPeerConnection({iceServers: []});
    const stream = new MediaStream();
    peer.addTransceiver("audio", {direction: "recvonly"});
    peer.addTransceiver("video", {direction: "recvonly"});
    peer.ontrack = ({track}) => stream.addTrack(track);
    peer.onconnectionstatechange = () => {
      if (this.peer === peer && peer.connectionState === "failed") this.fail();
    };
    await peer.setLocalDescription(await peer.createOffer());
    if (version !== this.version) return;
    if (peer.iceGatheringState !== "complete") await new Promise((resolve, reject) => {
      const timer = setTimeout(reject, 8000);
      this.stopGather = () => { clearTimeout(timer); resolve(); };
      peer.onicegatheringstatechange = () => {
        if (peer.iceGatheringState === "complete") { clearTimeout(timer); resolve(); }
      };
    });
    if (version !== this.version) return;
    this.stopGather = null;
    const reply = await fetch(this.path + "/whep", {method: "POST", headers: {"Content-Type": "application/sdp"},
      body: peer.localDescription.sdp});
    if (!reply.ok) throw Error();
    const location = reply.headers.get("Location");
    if (version !== this.version) { await release(location); return; }
    this.location = location;
    this.origin = Number(reply.headers.get("X-Avatar-Timeline-Origin-Ms"));
    await peer.setRemoteDescription({type: "answer", sdp: await reply.text()});
    if (version !== this.version) return;
    video.srcObject = stream;
    video.muted = !!this.muted;
    video.hidden = false;
    video.onplaying = () => { this.audibleStart = this.origin + video.currentTime * 1000; };
    video.onpause = video.onwaiting = () => { this.audibleStart = null; };
    await video.play();
    if (version === this.version) {
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
    clearTimeout(this.deadline);
    clearInterval(this.heartbeat);
    if (this.events) { this.events.onclose = this.events.onerror = null; this.events.close(); }
    this.dropPeer();
    void release(this.path);
    this.path = null;
  }

  fail() {
    if (this.failed || this.closed) return;
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
