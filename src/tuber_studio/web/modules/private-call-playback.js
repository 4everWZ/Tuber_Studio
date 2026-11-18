export class PrivatePlayback {
  constructor(context, send) {
    this.context = context;
    this.send = send;
    this.queue = [];
    this.cancelled = new Set();
    this.epoch = -1;
    this.tail = 0;
    this.response = null;
    this.offset = 0;
  }

  acknowledge() {
    if (this.context.state !== "running") return;
    const presented = this.context.getOutputTimestamp?.().contextTime;
    if (!Number.isFinite(presented)) return;
    while (this.queue.length && this.queue[0].start < presented) {
      const span = this.queue[0];
      const played = Math.floor(span.offset + (Math.min(span.end, presented) - span.start) * 1000);
      if (played > span.ack) {
        this.send({type: "playback.ack", response_id: span.response, item_id: span.item,
          played_ms: played, committed_ms: played});
        span.ack = played;
      }
      if (presented < span.end) break;
      this.queue.shift().node.disconnect();
    }
  }

  speaking() {
    if (this.muted || this.context.state !== "running") return false;
    const time = this.context.getOutputTimestamp?.().contextTime;
    const span = this.queue.find(item => item.start <= time && time < item.end);
    if (!span) return false;
    const buffer = span.node.buffer, samples = buffer.getChannelData(0);
    const end = Math.min(samples.length, Math.floor((time - span.start) * buffer.sampleRate));
    const start = Math.max(0, end - Math.round(buffer.sampleRate * .02));
    let squares = 0;
    for (let index = start; index < end; index++) squares += samples[index] ** 2;
    return end > start && squares / (end - start) > .012 ** 2;
  }

  setMuted(muted) {
    if (muted) {
      this.clear();
      this.cancelled.add(this.response);
    }
    this.muted = muted;
  }

  clear() {
    this.acknowledge();
    for (const span of this.queue) {
      this.cancelled.add(span.response);
      span.node.stop();
      span.node.disconnect();
    }
    this.queue = [];
    this.tail = 0;
  }

  event(event) {
    const response = event.response_id || event.response?.id;
    const epoch = event.metadata?.epoch ?? event.epoch;
    if (Number.isInteger(epoch)) {
      if (epoch < this.epoch) return;
      if (epoch > this.epoch) { this.clear(); this.epoch = epoch; }
    }
    if (event.type === "output_audio_buffer.cleared" || event.type === "barge_in" ||
        (event.type === "turn.signal" && event.event === "barge_in") ||
        (event.type === "response.done" && ["failed", "cancelled"].includes(event.response?.status))) {
      this.cancelled.add(response || this.response);
      this.clear();
      return;
    }
    if (!["response.audio.delta", "response.output_audio.delta"].includes(event.type) ||
        !response || this.cancelled.has(response)) return;
    if (this.muted) { this.cancelled.add(response); return; }
    const rate = event.sample_rate_hz ?? 24000;
    if (!Number.isInteger(rate) || rate < 8000 || rate > 96000 || (event.format ?? "pcm16") !== "pcm16" ||
        typeof event.delta !== "string" || event.delta.length > 1048576 ||
        this.tail - this.context.currentTime > 10) throw Error("语音输出格式或积压超限");
    const bytes = Uint8Array.from(atob(event.delta), char => char.charCodeAt(0));
    if (!bytes.length) return;
    const pcm = new Int16Array(bytes.buffer);
    const buffer = this.context.createBuffer(1, pcm.length, rate);
    buffer.copyToChannel(Float32Array.from(pcm, sample => sample / 32768), 0);
    const node = this.context.createBufferSource();
    node.buffer = buffer;
    node.connect(this.context.destination);
    const start = Math.max(this.context.currentTime, this.tail);
    this.tail = start + buffer.duration;
    if (response !== this.response) { this.response = response; this.offset = 0; }
    this.queue.push({node, response, item: event.item_id || `item_${response}`, offset: this.offset,
      start, end: this.tail, ack: -1});
    this.offset += buffer.duration * 1000;
    node.start(start);
  }
}
