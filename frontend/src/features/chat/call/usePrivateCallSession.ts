import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiGet, apiPost, apiRequest, studioPath } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import {
  createRequestId,
  type CallOrigin,
  type ChatTurn,
  type PrivateCallAvailability,
  type PrivateCallAvatarsResponse,
  type PrivateCallTicket,
  type VoiceOrigin,
} from "../types";
import { callTranscription, type CallTranscriptionController } from "./callTranscription";
import { FinalUtterances, type RealtimeConversationEvent } from "./FinalUtterances";
import { guardCall } from "./guardCall";
import { PortraitPlayback, type PortraitUiState } from "./PortraitPlayback";
import { PrivatePlayback, type CallPlaybackController, type RealtimeAudioEvent } from "./PrivatePlayback";
import type { TranscriptRecord, TranscriptReviewConfig } from "./TranscriptReviewPanel";
import { useCallWork } from "./useCallWork";

interface ActiveCallState {
  abort: AbortController;
  utterances: FinalUtterances;
  ephemeral: boolean;
  scope: string;
  callContext: CallOrigin | null;
  ready: boolean;
  invalidated: boolean;
  speaking: boolean;
  lastVoice: number;
  pendingAudio: number;
  audioQueue: Promise<void>;
  sending: boolean;
  workRequest: {
    request_id: string;
    message: string;
    call_scope: string;
    call_context: CallOrigin | null;
  } | null;
  guard?: string;
  context?: AudioContext;
  stream?: MediaStream;
  capture?: AudioWorkletNode;
  source?: MediaStreamAudioSourceNode;
  playback?: CallPlaybackController;
  socket?: WebSocket;
  transcription?: CallTranscriptionController;
  deadline?: ReturnType<typeof setTimeout>;
  receipts?: ReturnType<typeof setInterval>;
  heartbeat?: ReturnType<typeof setInterval>;
  send: (event: Record<string, unknown>) => void;
}

export interface UsePrivateCallSessionOptions {
  active: boolean;
  characterKey: string;
  latestTurnRequestId?: string;
  onChatChanged: () => void;
}

const INITIAL_PORTRAIT_UI: PortraitUiState = {
  videoVisible: false,
  caption: "",
  smoothingSupported: false,
  smoothingVisible: false,
  smoothingChecked: false,
  interruptVisible: false,
};

function canRunVoiceCall(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof navigator !== "undefined" &&
    Boolean(window.isSecureContext) &&
    typeof navigator.mediaDevices?.getUserMedia === "function" &&
    typeof window.AudioWorkletNode === "function" &&
    typeof window.AudioContext === "function"
  );
}

async function fetchVoiceReference(signal: AbortSignal): Promise<{ data: string; sha256: string }> {
  let blob: Blob;
  try {
    blob = await apiRequest<Blob>("/api/v1/agent/private-call/reference", {
      signal,
      cache: "no-store",
      responseType: "blob",
    });
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new Error("暂时读不到参考录音");
  }
  const buffer = await blob.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", buffer);
  const sha256 = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  return { data, sha256 };
}

export function usePrivateCallSession({
  active,
  characterKey,
  latestTurnRequestId,
  onChatChanged,
}: UsePrivateCallSessionOptions) {
  const [availability, setAvailability] = useState<PrivateCallAvailability | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [temporary, setTemporary] = useState(false);
  const [topic, setTopic] = useState("");
  const [optionsOpen, setOptionsOpen] = useState(false);

  const [inCall, setInCall] = useState(false);
  const [callReady, setCallReady] = useState(false);
  const [callEphemeral, setCallEphemeral] = useState(false);
  const [statusText, setStatusText] = useState("尚未开始通话");
  const [muted, setMuted] = useState(false);
  const [showMute, setShowMute] = useState(false);

  const [avatarProfilesOffered, setAvatarProfilesOffered] = useState(false);
  const [avatarSelection, setAvatarSelection] = useState<unknown>(null);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [portraitActive, setPortraitActive] = useState(false);
  const [portraitUi, setPortraitUi] = useState<PortraitUiState>(INITIAL_PORTRAIT_UI);

  const [transcriptRecords, setTranscriptRecords] = useState<TranscriptRecord[]>([]);
  const [reviewConfig, setReviewConfig] = useState<TranscriptReviewConfig | null>(null);

  const [workInput, setWorkInput] = useState("");
  const [workSending, setWorkSending] = useState(false);
  const [workAnswer, setWorkAnswer] = useState<ChatTurn | null>(null);
  const [workError, setWorkError] = useState("");

  const currentRef = useRef<ActiveCallState | null>(null);
  const dialogOpenRef = useRef(false);
  dialogOpenRef.current = dialogOpen;
  const activeRef = useRef(active);
  activeRef.current = active;
  const latestTurnRef = useRef(latestTurnRequestId);
  latestTurnRef.current = latestTurnRequestId;
  const selectedProfileRef = useRef(selectedProfile);
  selectedProfileRef.current = selectedProfile;
  const avatarSelectionRef = useRef(avatarSelection);
  avatarSelectionRef.current = avatarSelection;
  const avatarRevisionRef = useRef(0);
  const recordSeqRef = useRef(0);
  const videoElRef = useRef<HTMLVideoElement | null>(null);

  const callWork = useCallWork();

  const checkAvailability = useCallback(async () => {
    if (!activeRef.current || !canRunVoiceCall()) {
      setAvailability(null);
      return;
    }
    try {
      const state = await apiGet<"/api/v1/agent/private-call", PrivateCallAvailability>(
        "/api/v1/agent/private-call"
      );
      if (activeRef.current) {
        setAvailability(state);
      }
    } catch {
      setAvailability(null);
    }
  }, []);

  const finish = useCallback(
    (call: ActiveCallState | null, cause: string, message: string, code: string | number | null = null) => {
      if (!call || call !== currentRef.current) return;
      currentRef.current = null;
      setInCall(false);
      setCallReady(false);
      setWorkInput("");
      setWorkAnswer(null);
      setWorkError("");
      setWorkSending(false);

      call.playback?.diagnose?.("call_end", cause, code);

      setShowMute(false);
      setMuted(false);
      setTranscriptRecords([]);
      setReviewConfig(null);
      setPortraitActive(false);
      setPortraitUi(INITIAL_PORTRAIT_UI);

      call.abort.abort();
      clearTimeout(call.deadline);
      clearInterval(call.receipts);
      clearInterval(call.heartbeat);

      if (call.capture) {
        call.capture.port.onmessage = null;
        call.capture.port.close();
        call.capture.disconnect();
      }
      call.source?.disconnect();
      call.stream?.getTracks().forEach((track) => track.stop());
      call.playback?.clear();
      call.send({ type: "session.close" });
      if (call.socket) {
        call.socket.onmessage = null;
        call.socket.onopen = null;
        call.socket.onclose = null;
        call.socket.onerror = null;
        call.socket.close();
      }
      if (call.context) {
        void call.context.close().catch(() => {});
      }
      setStatusText(message);
    },
    []
  );

  // Availability check on active/character change
  const prevCharKeyRef = useRef(characterKey);
  useEffect(() => {
    if (prevCharKeyRef.current !== characterKey) {
      prevCharKeyRef.current = characterKey;
      if (currentRef.current) {
        finish(currentRef.current, "character_changed", "角色已切换，可重新拨打。");
      }
    }
    if (active) {
      void checkAvailability();
    } else if (currentRef.current) {
      finish(currentRef.current, "navigation", "通话已结束");
      setDialogOpen(false);
    }
  }, [active, characterKey, checkAvailability, finish]);

  // Query gated avatars ONLY when dialog opens (and RTCPeerConnection is supported)
  useEffect(() => {
    const version = ++avatarRevisionRef.current;
    setAvatarSelection(null);
    setAvatarProfilesOffered(false);
    setSelectedProfile("");

    if (!dialogOpen || typeof window === "undefined" || !window.RTCPeerConnection) {
      return;
    }

    void (async () => {
      try {
        const data = await apiGet<"/api/v1/agent/private-call/avatars", PrivateCallAvatarsResponse>(
          "/api/v1/agent/private-call/avatars"
        );
        if (
          avatarRevisionRef.current !== version ||
          !Array.isArray(data.profiles) ||
          !data.profiles.includes("original") ||
          !data.profiles.includes("q")
        ) {
          return;
        }
        setAvatarSelection(data.selection ?? null);
        setAvatarProfilesOffered(true);
      } catch {
        // Voice calls remain available independently.
      }
    })();
  }, [dialogOpen]);

  // Pagehide / hashchange cleanup
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onPageHide = () => {
      finish(currentRef.current, "page_hide", "通话已结束");
      setDialogOpen(false);
    };
    const onHashChange = () => {
      if (!["", "#chat"].includes(window.location.hash)) {
        finish(currentRef.current, "navigation", "通话已结束");
        setDialogOpen(false);
      }
    };
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("hashchange", onHashChange);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("hashchange", onHashChange);
    };
  }, [finish]);

  // Unmount cleanup
  useEffect(() => {
    return () => {
      finish(currentRef.current, "unmount", "通话已结束");
    };
  }, [finish]);

  const addTranscript = useCallback((text: string, origin: VoiceOrigin | null = null) => {
    const id = ++recordSeqRef.current;
    setTranscriptRecords((prev) => [...prev, { id, text, origin }]);
  }, []);

  const begin = useCallback(
    async (
      previous: { scope: string; muted: boolean; turn?: string } | null = null
    ): Promise<void> => {
      if (currentRef.current) return;

      const call: ActiveCallState = {
        abort: new AbortController(),
        utterances: new FinalUtterances(),
        ephemeral: false,
        scope: "",
        callContext: null,
        ready: false,
        invalidated: false,
        speaking: false,
        lastVoice: 0,
        pendingAudio: 0,
        audioQueue: Promise.resolve(),
        sending: false,
        workRequest: null,
        send(event: Record<string, unknown>) {
          if (this.invalidated && event.type === "playback.ack") return;
          if (this.socket?.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify(event));
          }
        },
      };

      currentRef.current = call;
      setInCall(true);
      setCallReady(false);
      setStatusText("正在读取音色…");
      call.deadline = setTimeout(
        () => finish(call, "voice_reference_timeout", "参考录音读取超时"),
        60000
      );

      try {
        const audioCtx = new AudioContext({ sampleRate: 16000 });
        call.context = audioCtx;
        await audioCtx.resume();
        if (currentRef.current !== call) return;

        const ephemeral = temporary;
        call.ephemeral = ephemeral;
        setCallEphemeral(ephemeral);

        const subject = ephemeral ? "" : topic.trim();
        const turn =
          !ephemeral && !subject
            ? previous
              ? previous.turn
              : latestTurnRef.current
            : undefined;

        const reference = await fetchVoiceReference(call.abort.signal);
        if (currentRef.current !== call) return;

        setStatusText("正在连接私聊语音…");
        const config = await apiPost<
          "/api/v1/agent/private-call/ticket",
          PrivateCallTicket,
          { temporary: boolean; topic?: string; topic_turn?: string }
        >("/api/v1/agent/private-call/ticket", {
          signal: call.abort.signal,
          body: {
            temporary: ephemeral,
            ...(subject ? { topic: subject } : turn ? { topic_turn: turn } : {}),
          },
        });
        if (currentRef.current !== call) return;

        if (reference.sha256 !== config.voice.reference_sha256) {
          finish(call, "voice_reference_changed", "音色已变化，请重新拨打。");
          return;
        }
        if (previous && config.call_scope !== previous.scope) {
          finish(call, "scope_changed", "角色或授权已变化，请重新开始通话");
          return;
        }

        call.scope = config.call_scope;
        call.callContext = config.voice_context ?? null;

        const use = config.memory_use
          ? config.topic_source
            ? { ...config.memory_use, topic_source: config.topic_source }
            : config.memory_use
          : null;

        const invalidate = (reason: string) => {
          if (currentRef.current !== call) return;
          call.invalidated = true;
          const wasMuted = Boolean(call.playback?.muted);
          call.send({ type: "response.cancel" });
          call.send({ type: "output_audio_buffer.clear" });
          finish(
            call,
            reason === "changed" ? "memory_changed" : "memory_guard_closed",
            "通话记忆已变化，正在重新连接"
          );
          if (reason === "changed" && dialogOpenRef.current && activeRef.current) {
            void begin({ scope: config.call_scope, muted: wasMuted, turn });
          } else {
            setStatusText("记忆或授权暂时无法核对，通话已结束");
          }
        };

        const validateMemory = async (): Promise<boolean> => {
          if (!use) return true;
          try {
            if (config.topic_source) {
              await apiPost<"/api/v1/agent/private-call/memory-validate", unknown, typeof use>(
                "/api/v1/agent/private-call/memory-validate",
                { body: use, signal: call.abort.signal }
              );
            } else {
              await apiPost<"/api/v1/agent/memory/validate", unknown, typeof use>(
                "/api/v1/agent/memory/validate",
                { body: use, signal: call.abort.signal }
              );
            }
            return currentRef.current === call;
          } catch (error) {
            invalidate(error instanceof ApiError && error.status === 409 ? "changed" : "closed");
            return false;
          }
        };

        if (use) {
          call.guard = await guardCall(call.abort.signal, use, invalidate);
          if (currentRef.current !== call) return;
        }

        const url = new URL(config.socket);
        url.search = new URLSearchParams({
          ticket: config.ticket,
          duplex: "1",
          autostart: "0",
        }).toString();

        const hasWorkContext = !ephemeral && Boolean(config.voice_context);
        if (hasWorkContext) {
          url.searchParams.set("input_transcription", "explicit");
        }

        const profileChoice = selectedProfileRef.current;
        const hasAvatarChoice = Boolean(avatarSelectionRef.current && profileChoice);
        let playback: CallPlaybackController;
        if (hasAvatarChoice) {
          url.searchParams.set("avatar", "1");
          setPortraitActive(true);
          playback = new PortraitPlayback(
            {
              context: audioCtx,
              guard: call.guard,
              get socket() {
                return call.socket ?? null;
              },
              abort: call.abort,
              send: (ev) => call.send(ev),
            },
            profileChoice,
            avatarSelectionRef.current,
            () => videoElRef.current,
            (patch) => setPortraitUi((prev) => ({ ...prev, ...patch }))
          );
        } else {
          setPortraitActive(false);
          playback = new PrivatePlayback(audioCtx, (event) => call.send(event));
        }

        call.playback = playback;
        if (previous?.muted) {
          playback.setMuted(true);
          setMuted(true);
        }

        if (!ephemeral) {
          if (config.voice_context) {
            callWork.attach({
              context: config.voice_context,
              audioContext: audioCtx,
              playback,
              abort: call.abort,
              get speaking() {
                return call.speaking;
              },
              get lastVoice() {
                return call.lastVoice;
              },
              get pendingAudio() {
                return call.pendingAudio;
              },
              get transcriptionPending() {
                return Boolean(call.transcription?.pending);
              },
            });
          }
          setReviewConfig({
            memory_citations: config.memory_citations,
            correction_targets: config.correction_targets,
            memory_use: config.memory_use,
          });
        }

        const captureMic = async (identity: string, voiceName?: string) => {
          setStatusText("等待麦克风授权…");
          await audioCtx.audioWorklet.addModule(
            studioPath("/static/worklets/private-call-capture.js")
          );
          if (currentRef.current !== call) return;
          const stream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
            video: false,
          });
          if (currentRef.current !== call) {
            stream.getTracks().forEach((track) => track.stop());
            return;
          }
          call.stream = stream;
          clearTimeout(call.deadline);
          const audioTrack = stream.getAudioTracks()[0];
          if (audioTrack) {
            audioTrack.onended = () => finish(call, "microphone_ended", "麦克风已断开");
          }
          const captureNode = new AudioWorkletNode(audioCtx, "private-call-capture");
          const sourceNode = audioCtx.createMediaStreamSource(stream);
          call.capture = captureNode;
          call.source = sourceNode;
          sourceNode.connect(captureNode);
          captureNode.connect(audioCtx.destination);

          captureNode.port.onmessage = ({ data }: MessageEvent<unknown>) => {
            if (currentRef.current !== call) return;
            if (
              typeof data === "object" &&
              data !== null &&
              "speaking" in data &&
              typeof (data as { speaking?: unknown }).speaking === "boolean"
            ) {
              const speaking = (data as { speaking: boolean }).speaking;
              call.speaking = speaking;
              call.lastVoice = performance.now();
              if (speaking) callWork.interrupt();
              return;
            }
            if (!(data instanceof ArrayBuffer)) return;
            if ((call.socket?.bufferedAmount ?? 0) > 128000) {
              finish(call, "realtime_send_backlog", "网络积压，通话已结束");
              return;
            }
            call.transcription?.pcm(data);
            const audio = btoa(String.fromCharCode(...new Uint8Array(data)));
            call.send({
              type: "input_audio_buffer.append",
              format: "pcm16",
              sample_rate_hz: 16000,
              audio,
            });
          };

          setStatusText(
            `正在与${identity}私聊 · 麦克风已开启${voiceName ? ` · ${voiceName}` : ""}`
          );
        };

        const socket = new WebSocket(url);
        call.socket = socket;
        clearTimeout(call.deadline);
        call.deadline = setTimeout(
          () => finish(call, "realtime_handshake_timeout", "语音握手超时"),
          30000
        );
        setShowMute(true);

        socket.onopen = () => {
          call.send({
            type: "session.update",
            session: {
              instructions: config.instructions,
              modalities: ["audio", "text"],
              input_audio_format: "pcm16",
              output_audio_format: "pcm16",
              playback_commit_policy: "ack_only",
              extra_body: { auto_response: true },
              ref_audio: reference.data,
            },
          });
        };

        socket.onerror = () => finish(call, "realtime_ws_error", "无法连接私聊语音服务");
        socket.onclose = (ev) =>
          finish(call, "realtime_ws_close", "私聊连接已断开", ev.code);

        socket.onmessage = async ({ data }) => {
          if (currentRef.current !== call) return;
          try {
            const event = JSON.parse(String(data)) as RealtimeAudioEvent & {
              error?: { type?: string; code?: string };
              session?: {
                capabilities?: {
                  implementation_level?: string;
                  chunk_period_ms?: number;
                  gateway_input_transcription?: {
                    mode?: string;
                    receipt?: string;
                    transcript_receipt?: string;
                    sample_rate_hz?: number;
                    max_seconds?: number;
                  };
                };
              };
            };

            if (event.type === "error") {
              if (
                call.ready &&
                event.error?.type === "invalid_request_error" &&
                event.error.code === "response_not_active"
              ) {
                call.playback?.diagnose?.("response_error", "realtime_error_event", event.error.code);
                return;
              }
              finish(
                call,
                "realtime_error_event",
                "语音协议或设备不可用，通话已结束",
                event.error?.code ?? null
              );
              return;
            }

            if (event.type === "session.created" && !call.ready) {
              const caps = event.session?.capabilities;
              if (
                caps?.implementation_level !== "model_native_duplex" ||
                (caps.chunk_period_ms ?? 1000) !== 1000
              ) {
                throw new Error("未协商到支持的原生双工语音");
              }
              clearTimeout(call.deadline);
              call.deadline = setTimeout(
                () => finish(call, "microphone_timeout", "麦克风授权超时，通话已结束"),
                60000
              );
              call.ready = true;
              setCallReady(true);

              const asr = caps.gateway_input_transcription;
              if (
                hasWorkContext &&
                config.voice_context &&
                asr?.mode === "explicit_once" &&
                asr.receipt === "hmac-sha256-v1" &&
                asr.sample_rate_hz === 16000 &&
                Number.isInteger(asr.max_seconds) &&
                (asr.max_seconds ?? 0) > 0
              ) {
                call.transcription = callTranscription(
                  (ev) => call.send(ev),
                  config.voice_context,
                  asr.max_seconds!,
                  (text, origin) => {
                    addTranscript(text, origin);
                    if (asr.transcript_receipt === "hmac-sha256-v1" && origin.transcript_receipt) {
                      callWork.acceptOrigin(origin);
                    }
                  }
                );
              }

              call.receipts = setInterval(() => call.playback?.acknowledge(), 200);
              call.heartbeat = setInterval(() => call.send({ type: "session.heartbeat" }), 20000);
              void call.playback?.gateway?.(event);
              await captureMic(config.character, config.voice?.name);
            } else if (call.stream) {
              call.transcription?.event(event);
              const text = !ephemeral ? call.utterances.accept(event as RealtimeConversationEvent) : undefined;
              if (text) addTranscript(text);

              const isAudio =
                event.type === "response.audio.delta" ||
                event.type === "response.output_audio.delta";
              if (isAudio) callWork.interrupt();

              if (use && isAudio) {
                call.pendingAudio += 1;
                if (call.pendingAudio > 8) {
                  finish(call, "memory_backlog", "记忆核对积压，通话已结束");
                  return;
                }
                call.audioQueue = call.audioQueue
                  .then(async () => {
                    if (currentRef.current === call && (await validateMemory())) {
                      call.playback?.event(event);
                    }
                  })
                  .catch(() => finish(call, "playback_error", "语音播放失败，通话已结束"))
                  .finally(() => {
                    call.pendingAudio -= 1;
                  });
              } else {
                call.playback?.event(event);
              }
            }
          } catch (error) {
            const name = error instanceof Error ? error.name : "";
            finish(
              call,
              "realtime_message_or_device_error",
              name === "NotAllowedError"
                ? "未获麦克风权限，通话已结束"
                : name === "NotFoundError"
                  ? "未找到麦克风"
                  : "语音协议或设备不可用，通话已结束"
            );
          }
        };
      } catch (error) {
        finish(
          call,
          "call_setup_error",
          error instanceof ApiError && error.status ? errorMessage(error) : "无法建立私聊语音连接"
        );
      }
    },
    [addTranscript, callWork, finish, temporary, topic]
  );

  const openDialog = useCallback(() => {
    setOptionsOpen(false);
    setTopic("");
    setStatusText("尚未开始通话");
    setDialogOpen(true);
  }, []);

  const endOrCloseDialog = useCallback(() => {
    finish(currentRef.current, "hangup_button", "通话已结束");
    setDialogOpen(false);
  }, [finish]);

  const handleDialogNativeClose = useCallback(() => {
    finish(currentRef.current, "dialog_closed", "通话已结束");
    setDialogOpen(false);
  }, [finish]);

  const toggleMute = useCallback(() => {
    const playback = currentRef.current?.playback;
    if (!playback) return;
    const nextMuted = !playback.muted;
    playback.setMuted(nextMuted);
    if (nextMuted) callWork.stopReading();
    setMuted(nextMuted);
  }, [callWork]);

  const setSmoothing = useCallback((checked: boolean) => {
    const playback = currentRef.current?.playback;
    if (playback instanceof PortraitPlayback) {
      playback.setSmoothing(checked);
    }
  }, []);

  const interruptAnswer = useCallback(() => {
    const playback = currentRef.current?.playback;
    if (playback instanceof PortraitPlayback) {
      playback.interruptAnswer();
    }
  }, []);

  const submitWork = useCallback(async () => {
    const call = currentRef.current;
    const message = workInput.trim();
    if (!message || !call?.ready || call.ephemeral || call.invalidated || call.sending) {
      return;
    }
    callWork.stopReading();
    call.sending = true;
    setWorkSending(true);
    const request =
      call.workRequest?.message === message
        ? call.workRequest
        : {
            request_id: createRequestId(),
            message,
            call_scope: call.scope,
            call_context: call.callContext,
          };
    call.workRequest = request;
    setWorkAnswer(null);
    setWorkError("正在处理…");
    try {
      const reply = await apiPost<"/api/v1/agent/chat", ChatTurn, typeof request>(
        "/api/v1/agent/chat",
        { body: request }
      );
      onChatChanged();
      if (currentRef.current === call) {
        if (reply.card?.type === "agent_task") callWork.refresh();
        setWorkError("");
        setWorkAnswer(reply);
        setWorkInput((prev) => (prev.trim() === message ? "" : prev));
        call.workRequest = null;
      }
    } catch (error) {
      if (currentRef.current === call) {
        setWorkAnswer(null);
        setWorkError(errorMessage(error));
      }
    } finally {
      call.sending = false;
      setWorkSending(false);
    }
  }, [callWork, onChatChanged, workInput]);

  return {
    available: Boolean(active && availability?.configured && canRunVoiceCall()),
    availability,
    dialogOpen,
    temporary,
    setTemporary,
    topic,
    setTopic,
    optionsOpen,
    setOptionsOpen,
    inCall,
    callReady,
    callEphemeral,
    statusText,
    muted,
    showMute,
    avatarProfilesOffered,
    selectedProfile,
    setSelectedProfile,
    portraitActive,
    portraitUi,
    videoElRef,
    transcriptRecords,
    reviewConfig,
    callWork,
    workInput,
    setWorkInput,
    workSending,
    workAnswer,
    workError,
    openDialog,
    startCall: () => void begin(null),
    endOrCloseDialog,
    handleDialogNativeClose,
    toggleMute,
    setSmoothing,
    interruptAnswer,
    submitWork,
  };
}
