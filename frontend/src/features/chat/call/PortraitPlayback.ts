import { ApiError, apiGet, apiPost, apiRequest } from "@/shared/api/client";
import { PrivatePlayback, type CallPlaybackController, type RealtimeAudioEvent } from "./PrivatePlayback";

const AVATARS_BASE = "/api/v1/agent/private-call/avatars";
const ICE_GATHER_TIMEOUT = Symbol("ice_gather_timeout");

const REALTIME_CODES = new Set([
  "bad_event",
  "bad_audio",
  "invalid_json",
  "unknown_event",
  "event_too_large",
  "session_closed",
  "unknown_session",
  "response_not_active",
  "response_already_active",
  "playback_ack_too_late",
  "playback_item_not_found",
  "playback_item_mismatch",
  "item_not_found",
  "input_audio_buffer_empty",
  "input_backpressure",
  "output_backpressure",
  "resource_exhausted",
  "internal_error",
  "engine_error",
  "commit_aborted",
  "runtime_append_failed",
  "runtime_append_task_failed",
  "runtime_abort_failed",
  "runtime_signal_failed",
  "runtime_data_plane_stream_failed",
  "runtime_data_plane_text_without_audio",
  "runtime_touch_failed",
]);

function releasePath(path: string | null | undefined): Promise<void> {
  if (!path) return Promise.resolve();
  return apiRequest(path, { method: "DELETE", keepalive: true })
    .then(() => undefined)
    .catch(() => undefined);
}

export interface PortraitUiState {
  videoVisible: boolean;
  caption: string;
  smoothingSupported: boolean;
  smoothingVisible: boolean;
  smoothingChecked: boolean;
  interruptVisible: boolean;
}

export interface PortraitCallHandle {
  readonly context: AudioContext;
  readonly guard?: string;
  readonly socket: WebSocket | null;
  readonly abort: AbortController;
  send: (event: Record<string, unknown>) => void;
}

interface AvatarSpanEvent {
  type?: string;
  generation?: number;
  response_id?: string;
  source_start_ms?: number;
  start_ms?: number;
  end_ms?: number;
  text?: string;
}

/**
 * WebRTC WHEP live avatar playback with graceful voice-only fallback to `PrivatePlayback`.
 * Only instantiated when `/api/v1/agent/private-call/avatars` offers profiles AND the owner selects one.
 */
export class PortraitPlayback implements CallPlaybackController {
  private readonly call: PortraitCallHandle;
  private readonly profile: string;
  private readonly selection: unknown;
  private readonly getVideo: () => HTMLVideoElement | null;
  private readonly onUiChange: (patch: Partial<PortraitUiState>) => void;
  private readonly fallback: PrivatePlayback;

  muted = false;
  private spans: AvatarSpanEvent[] = [];
  private subtitles: AvatarSpanEvent[] = [];
  private version = 0;
  private generation = 0;
  private phase = "attach";
  private attaching = false;
  private closed = false;
  private failed = false;
  private epoch = -1;
  private response: string | null = null;
  private blocked: string | null = null;
  private item: string | null = null;
  private received = 0;
  private lastAck = 0;
  private path: string | null = null;
  private location: string | null = null;
  private origin = 0;
  private lastTimeline = -1;
  private audibleStart: number | null = null;
  private peer: RTCPeerConnection | null = null;
  private events: WebSocket | null = null;
  private deadline: ReturnType<typeof setTimeout> | undefined;
  private reconnect: ReturnType<typeof setTimeout> | undefined;
  private heartbeat: ReturnType<typeof setInterval> | undefined;
  private stopGather: (() => void) | null = null;
  private smoothing = false;
  private captionText = "正在连接实时形象…";

  constructor(
    call: PortraitCallHandle,
    profile: string,
    selection: unknown,
    getVideo: () => HTMLVideoElement | null,
    onUiChange: (patch: Partial<PortraitUiState>) => void
  ) {
    this.call = call;
    this.profile = profile;
    this.selection = selection;
    this.getVideo = getVideo;
    this.onUiChange = onUiChange;
    this.fallback = new PrivatePlayback(call.context, (event) => call.send(event));

    const smoothingSupported =
      typeof window !== "undefined" &&
      "jitterBufferTarget" in (window.RTCRtpReceiver?.prototype || {});

    this.onUiChange({
      videoVisible: false,
      caption: this.captionText,
      smoothingSupported,
      smoothingVisible: false,
      smoothingChecked: false,
      interruptVisible: true,
    });

    this.deadline = setTimeout(() => this.fail("attach_timeout"), 30000);
    call.abort.signal.addEventListener("abort", () => this.close(), { once: true });
  }

  get queue() {
    return this.fallback.queue;
  }

  private setCaption(text: string): void {
    this.captionText = text;
    this.onUiChange({ caption: text });
  }

  setSmoothing(checked: boolean): void {
    this.smoothing = checked;
    this.onUiChange({ smoothingChecked: checked });
    this.applyBufferTarget();
  }

  interruptAnswer(): void {
    this.diagnose("interrupt", "interrupt_button");
    this.block();
    void this.dropPeer();
    this.call.send({ type: "output_audio_buffer.clear" });
  }

  diagnose(action: string, cause: string, code: string | number | null = null): void {
    const peer = this.peer;
    const socket = this.call.socket;
    const diagnosticCode =
      cause === "realtime_error_event"
        ? typeof code === "string" && REALTIME_CODES.has(code)
          ? code
          : "other"
        : typeof code === "number" && Number.isInteger(code)
          ? code
          : null;

    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("studio-whep-diagnostic", {
          detail: {
            ms: performance.now(),
            action,
            cause,
            phase: this.phase,
            version: this.version,
            generation: Number.isSafeInteger(this.generation) ? this.generation : null,
            code: diagnosticCode,
            peer: peer?.connectionState ?? null,
            ice: peer?.iceConnectionState ?? null,
            gathering: peer?.iceGatheringState ?? null,
            signaling: peer?.signalingState ?? null,
            eventsWs: this.events?.readyState ?? null,
            realtimeWs: socket?.readyState ?? null,
            realtimeBufferedBytes: socket?.bufferedAmount ?? null,
          },
        })
      );
    }
  }

  private resetResponse(): void {
    this.response = null;
    this.received = 0;
    this.lastAck = 0;
    this.item = null;
  }

  private block(): void {
    this.blocked = this.response;
    this.spans = [];
    this.subtitles = [];
    this.setCaption("");
    this.fallback.clear();
  }

  async gateway(event: Record<string, unknown>): Promise<void> {
    if (this.attaching || this.closed || this.failed) return;
    this.attaching = true;
    try {
      const sourceSessionId = event.source_session_id;
      if (!event.avatar_available || typeof sourceSessionId !== "string" || !sourceSessionId) {
        this.fail("source_unavailable");
        return;
      }
      const joined = await apiPost<
        string & never,
        { handle: string; generation: number; events: string },
        { profile: string; selection: unknown; source_session_id: string; guard?: string }
      >(AVATARS_BASE as never, {
        body: {
          profile: this.profile,
          selection: this.selection,
          source_session_id: sourceSessionId,
          guard: this.call.guard,
        },
      });
      const path = `${AVATARS_BASE}/${joined.handle}`;
      if (this.closed || this.failed) {
        await releasePath(path);
        return;
      }
      this.path = path;
      this.generation = joined.generation;
      this.phase = "await_snapshot";
      const events = new WebSocket(joined.events);
      this.events = events;
      events.onmessage = (message) => {
        try {
          this.avatarEvent(JSON.parse(String(message.data)) as AvatarSpanEvent);
        } catch {
          this.fail("avatar_event_error");
        }
      };
      events.onclose = (ev) => this.fail("events_ws_close", ev.code);
      events.onerror = () => this.fail("events_ws_error");
      this.heartbeat = setInterval(() => {
        void (async () => {
          try {
            const state = await apiGet<string & never, { state?: string }>(path as never);
            if (state.state !== "active") this.fail("heartbeat_inactive");
          } catch (error) {
            this.fail("heartbeat_error", error instanceof ApiError ? error.status : null);
          }
        })();
      }, 15000);
    } catch (error) {
      this.fail("attach_error", error instanceof ApiError ? error.status : null);
    }
  }

  private avatarEvent(event: AvatarSpanEvent): void {
    if (this.closed || this.failed) return;
    if (event.type === "avatar.snapshot" || event.type === "avatar.cleared") {
      if (typeof event.generation === "number" && event.generation < this.generation) return;
      if (typeof event.generation === "number") this.generation = event.generation;
      this.spans = [];
      this.subtitles = [];
      this.setCaption("");
      this.diagnose("resubscribe", event.type);
      this.resubscribe();
    } else if (
      event.type === "avatar.failed" ||
      event.type === "avatar.closed" ||
      event.type === "avatar.resync_required"
    ) {
      this.fail(event.type);
    } else if (event.generation === this.generation) {
      if (event.type === "avatar.audio.queued") {
        this.spans = [...this.spans.slice(-119), event];
      }
      if (event.type === "avatar.subtitle") {
        this.subtitles = [...this.subtitles.slice(-119), event];
      }
    }
  }

  private dropPeer(): Promise<void> {
    this.version += 1;
    clearTimeout(this.reconnect);
    this.stopGather?.();
    const peer = this.peer;
    this.peer = null;
    peer?.close();

    const video = this.getVideo();
    if (video) {
      video.onplaying = null;
      video.onpause = null;
      video.onwaiting = null;
      video.pause();
      video.muted = true;
      const srcObj = video.srcObject as MediaStream | null;
      srcObj?.getTracks().forEach((track) => track.stop());
      video.srcObject = null;
    }
    this.onUiChange({ videoVisible: false });

    const location = this.location;
    this.location = null;
    this.lastTimeline = -1;
    this.audibleStart = null;
    return releasePath(location);
  }

  private resubscribe(): void {
    const dropped = this.dropPeer();
    const version = this.version;
    this.phase = "release_previous";
    clearTimeout(this.deadline);
    this.deadline = setTimeout(() => this.fail("subscribe_timeout"), 20000);
    this.reconnect = setTimeout(() => {
      void (async () => {
        await dropped;
        if (version !== this.version) return;
        try {
          await this.subscribe(version);
        } catch (error) {
          if (version === this.version) {
            this.fail(error === ICE_GATHER_TIMEOUT ? "ice_gather_timeout" : "subscribe_error");
          }
        }
      })();
    }, 100);
  }

  private applyBufferTarget(): void {
    for (const receiver of this.peer?.getReceivers() || []) {
      (receiver as RTCRtpReceiver & { jitterBufferTarget?: number | null }).jitterBufferTarget =
        this.smoothing ? 240 : null;
    }
  }

  private async subscribe(version: number): Promise<void> {
    this.phase = "local_offer";
    const peer = new RTCPeerConnection({ iceServers: [] });
    this.peer = peer;
    const stream = new MediaStream();
    peer.addTransceiver("audio", { direction: "recvonly" });
    peer.addTransceiver("video", { direction: "recvonly" });
    if (this.smoothing) this.applyBufferTarget();
    peer.ontrack = ({ track }) => stream.addTrack(track);
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
    if (peer.iceGatheringState !== "complete") {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(ICE_GATHER_TIMEOUT), 8000);
        this.stopGather = () => {
          clearTimeout(timer);
          resolve();
        };
        peer.onicegatheringstatechange = () => {
          if (peer.iceGatheringState === "complete") {
            clearTimeout(timer);
            resolve();
          }
        };
      });
    }
    if (version !== this.version) return;
    this.stopGather = null;

    this.phase = "whep_offer";
    let reply: Response;
    try {
      reply = await apiRequest<Response, string>(`${this.path}/whep`, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: peer.localDescription?.sdp || "",
        responseType: "response",
      });
    } catch (error) {
      if (version === this.version) {
        this.fail("whep_http_error", error instanceof ApiError ? error.status : null);
      }
      return;
    }

    const location = reply.headers.get("Location");
    if (version !== this.version) {
      await releasePath(location);
      return;
    }
    this.location = location;
    this.origin = Number(reply.headers.get("X-Avatar-Timeline-Origin-Ms"));
    this.phase = "remote_answer";
    await peer.setRemoteDescription({ type: "answer", sdp: await reply.text() });
    if (version !== this.version) return;

    const video = this.getVideo();
    if (!video) {
      this.fail("video_element_missing");
      return;
    }
    video.srcObject = stream;
    video.muted = Boolean(this.muted);
    this.onUiChange({ videoVisible: true });
    video.onplaying = () => {
      this.audibleStart = this.origin + video.currentTime * 1000;
    };
    video.onpause = () => {
      this.audibleStart = null;
    };
    video.onwaiting = () => {
      this.audibleStart = null;
    };
    this.phase = "start_playback";
    await video.play();
    if (version === this.version) {
      this.phase = "playing";
      this.diagnose("playback", "video_play");
      this.onUiChange({ smoothingVisible: true });
      clearTimeout(this.deadline);
    }
  }

  event(event: RealtimeAudioEvent): void {
    if (this.closed) return;
    const id = event.response_id || event.response?.id || null;
    const epoch = event.metadata?.epoch ?? event.epoch;
    if (Number.isInteger(epoch) && typeof epoch === "number") {
      if (epoch < this.epoch) return;
      if (epoch > this.epoch) this.block();
      this.epoch = epoch;
    }
    if (
      event.type === "barge_in" ||
      event.type === "output_audio_buffer.cleared" ||
      (event.type === "turn.signal" && event.event === "barge_in")
    ) {
      this.blocked = id || this.response;
      this.fallback.event(event);
      return;
    }
    if ((event.type === "response.audio.delta" || event.type === "response.output_audio.delta") && id) {
      if (this.response !== id) {
        this.resetResponse();
        this.response = id;
      }
      if (id === this.blocked) return;
      this.item = event.item_id || this.item || `item_${id}`;
      if (typeof event.delta === "string") {
        this.received += ((atob(event.delta).length / 2) * 1000) / (event.sample_rate_hz || 24000);
      }
      if (this.muted) this.blocked = id;
    }
    if (this.failed) {
      this.fallback.event(event);
    }
  }

  acknowledge(): void {
    if (this.failed) {
      this.fallback.acknowledge();
      return;
    }
    const video = this.getVideo();
    const stream = video?.srcObject as MediaStream | null;
    if (
      !video ||
      !this.peer ||
      this.peer.connectionState !== "connected" ||
      this.events?.readyState !== WebSocket.OPEN ||
      video.paused ||
      video.muted ||
      !video.volume ||
      video.readyState < 3 ||
      !video.currentTime ||
      !stream?.getAudioTracks().some((t) => t.readyState === "live" && !t.muted)
    ) {
      return;
    }
    const time = Math.floor(this.origin + video.currentTime * 1000);
    if (!(time > this.lastTimeline)) return;
    this.lastTimeline = time;
    this.events.send(
      JSON.stringify({ type: "playback.ack", generation: this.generation, played_ms: time })
    );

    while (this.subtitles.length && (this.subtitles[0].start_ms ?? Infinity) <= time) {
      const sub = this.subtitles.shift();
      if (sub && sub.response_id !== this.blocked && typeof sub.text === "string") {
        this.setCaption((this.captionText + sub.text).slice(-800));
      }
    }

    if (!this.response || this.blocked === this.response) return;
    let played = this.lastAck;
    for (const span of this.spans) {
      if (
        span.response_id !== this.response ||
        (span.source_start_ms ?? 0) > played + 1 ||
        this.audibleStart === null ||
        this.audibleStart > (span.start_ms ?? 0) + played - (span.source_start_ms ?? 0) + 1 ||
        (span.start_ms ?? Infinity) > time
      ) {
        continue;
      }
      played = Math.max(
        played,
        Math.floor(
          Math.min(
            this.received,
            (span.source_start_ms ?? 0) + Math.min(time, span.end_ms ?? 0) - (span.start_ms ?? 0)
          )
        )
      );
    }
    if (played > this.lastAck) {
      this.call.send({
        type: "playback.ack",
        response_id: this.response,
        item_id: this.item,
        played_ms: played,
        committed_ms: played,
      });
      this.lastAck = played;
    }
  }

  speaking(): boolean {
    return this.failed && this.fallback.speaking();
  }

  clear(): void {
    this.fallback.clear();
  }

  setMuted(value: boolean): void {
    this.muted = value;
    const video = this.getVideo();
    if (video) video.muted = value;
    if (value) this.blocked = this.response;
    this.fallback.setMuted(value);
  }

  private detach(): void {
    this.onUiChange({ smoothingVisible: false });
    clearTimeout(this.deadline);
    clearInterval(this.heartbeat);
    if (this.events) {
      this.events.onclose = null;
      this.events.onerror = null;
      this.events.close();
      this.events = null;
    }
    void this.dropPeer();
    void releasePath(this.path);
    this.path = null;
  }

  private fail(cause: string, code: string | number | null = null): void {
    if (this.failed || this.closed) return;
    this.diagnose("voice_fallback", cause, code);
    this.failed = true;
    this.blocked = this.response;
    this.detach();
    this.setCaption("实时形象不可用，继续语音通话");
  }

  close(): void {
    this.closed = true;
    this.detach();
    this.fallback.clear();
    this.onUiChange({
      videoVisible: false,
      caption: "",
      smoothingVisible: false,
      interruptVisible: false,
    });
  }
}
