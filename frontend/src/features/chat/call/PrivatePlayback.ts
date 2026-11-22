export interface PlaybackSpan {
  node: AudioBufferSourceNode;
  response: string;
  item: string;
  offset: number;
  start: number;
  end: number;
  ack: number;
}

export interface RealtimeAudioEvent {
  type?: string;
  response_id?: string;
  response?: { id?: string; status?: string };
  metadata?: { epoch?: number };
  epoch?: number;
  item_id?: string;
  sample_rate_hz?: number;
  format?: string;
  delta?: string;
  [key: string]: unknown;
}

export interface CallPlaybackController {
  muted: boolean;
  readonly queue: PlaybackSpan[];
  acknowledge: () => void;
  speaking: () => boolean;
  setMuted: (muted: boolean) => void;
  clear: () => void;
  event: (event: RealtimeAudioEvent) => void;
  diagnose?: (action: string, cause: string, code?: string | number | null) => void;
  gateway?: (event: Record<string, unknown>) => Promise<void> | void;
  close?: () => void;
}

export class PrivatePlayback implements CallPlaybackController {
  private readonly context: AudioContext;
  private readonly send: (event: Record<string, unknown>) => void;
  readonly queue: PlaybackSpan[] = [];
  private readonly cancelled = new Set<string | null>();
  private epoch = -1;
  private tail = 0;
  private response: string | null = null;
  private offset = 0;
  muted = false;

  constructor(context: AudioContext, send: (event: Record<string, unknown>) => void) {
    this.context = context;
    this.send = send;
  }

  acknowledge(): void {
    if (this.context.state !== "running") return;
    const presented = this.context.getOutputTimestamp?.().contextTime;
    if (typeof presented !== "number" || !Number.isFinite(presented)) return;

    while (this.queue.length && this.queue[0].start < presented) {
      const span = this.queue[0];
      const played = Math.floor(span.offset + (Math.min(span.end, presented) - span.start) * 1000);
      if (played > span.ack) {
        this.send({
          type: "playback.ack",
          response_id: span.response,
          item_id: span.item,
          played_ms: played,
          committed_ms: played,
        });
        span.ack = played;
      }
      if (presented < span.end) break;
      this.queue.shift()?.node.disconnect();
    }
  }

  speaking(): boolean {
    if (this.muted || this.context.state !== "running") return false;
    const time = this.context.getOutputTimestamp?.().contextTime;
    if (typeof time !== "number" || !Number.isFinite(time)) return false;
    const span = this.queue.find((item) => item.start <= time && time < item.end);
    if (!span || !span.node.buffer) return false;
    const buffer = span.node.buffer;
    const samples = buffer.getChannelData(0);
    const end = Math.min(samples.length, Math.floor((time - span.start) * buffer.sampleRate));
    const start = Math.max(0, end - Math.round(buffer.sampleRate * 0.02));
    let squares = 0;
    for (let index = start; index < end; index++) {
      squares += samples[index] ** 2;
    }
    return end > start && squares / (end - start) > 0.012 ** 2;
  }

  setMuted(muted: boolean): void {
    if (muted) {
      this.clear();
      this.cancelled.add(this.response);
    }
    this.muted = muted;
  }

  clear(): void {
    this.acknowledge();
    for (const span of this.queue) {
      this.cancelled.add(span.response);
      span.node.stop();
      span.node.disconnect();
    }
    this.queue.length = 0;
    this.tail = 0;
  }

  event(event: RealtimeAudioEvent): void {
    const response = event.response_id || event.response?.id || null;
    const epoch = event.metadata?.epoch ?? event.epoch;
    if (Number.isInteger(epoch) && typeof epoch === "number") {
      if (epoch < this.epoch) return;
      if (epoch > this.epoch) {
        this.clear();
        this.epoch = epoch;
      }
    }
    if (
      event.type === "output_audio_buffer.cleared" ||
      event.type === "barge_in" ||
      (event.type === "turn.signal" && event.event === "barge_in") ||
      (event.type === "response.done" &&
        typeof event.response?.status === "string" &&
        ["failed", "cancelled"].includes(event.response.status))
    ) {
      this.cancelled.add(response || this.response);
      this.clear();
      return;
    }
    if (
      (event.type !== "response.audio.delta" && event.type !== "response.output_audio.delta") ||
      !response ||
      this.cancelled.has(response)
    ) {
      return;
    }
    if (this.muted) {
      this.cancelled.add(response);
      return;
    }

    const rate = event.sample_rate_hz ?? 24000;
    if (
      !Number.isInteger(rate) ||
      rate < 8000 ||
      rate > 96000 ||
      (event.format ?? "pcm16") !== "pcm16" ||
      typeof event.delta !== "string" ||
      event.delta.length > 1048576 ||
      this.tail - this.context.currentTime > 10
    ) {
      throw new Error("语音输出格式或积压超限");
    }

    const bytes = Uint8Array.from(atob(event.delta), (char) => char.charCodeAt(0));
    if (!bytes.length) return;
    const pcm = new Int16Array(bytes.buffer);
    const buffer = this.context.createBuffer(1, pcm.length, rate);
    buffer.copyToChannel(
      Float32Array.from(pcm, (sample) => sample / 32768),
      0
    );
    const node = this.context.createBufferSource();
    node.buffer = buffer;
    node.connect(this.context.destination);
    const start = Math.max(this.context.currentTime, this.tail);
    this.tail = start + buffer.duration;
    if (response !== this.response) {
      this.response = response;
      this.offset = 0;
    }
    this.queue.push({
      node,
      response,
      item: event.item_id || `item_${response}`,
      offset: this.offset,
      start,
      end: this.tail,
      ack: -1,
    });
    this.offset += buffer.duration * 1000;
    node.start(start);
  }
}
