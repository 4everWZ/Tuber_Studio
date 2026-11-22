import type { CallOrigin, VoiceOrigin } from "../types";

const PREFIX = "gateway.input_transcription.";

interface CaptureSlice {
  frames: number;
  id?: string;
  boundary?: "overlong" | "quiet";
  waiting?: boolean;
}

export interface TranscriptionGatewayEvent {
  type?: string;
  capture_id?: string;
  source?: string;
  transcript?: string;
  receipt?: string;
  samples?: number;
  transcript_receipt?: string;
}

export interface CallTranscriptionController {
  pcm: (buffer: ArrayBuffer) => void;
  event: (event: TranscriptionGatewayEvent) => void;
  readonly pending: boolean;
}

function voiceInBlock(buffer: ArrayBuffer): boolean {
  const samples = new Int16Array(buffer);
  const frame = 320;
  const threshold = (32768 * 0.012) ** 2;
  for (let start = 0; start + frame <= samples.length; start += frame) {
    let squares = 0;
    for (let i = start; i < start + frame; i++) {
      squares += samples[i] ** 2;
    }
    if (squares / frame > threshold) {
      return true;
    }
  }
  return false;
}

/**
 * Bounded signed microphone slice FSM for `gateway.input_transcription.*` events.
 * Runs before forwarding PCM blocks on the duplex socket without altering stream audio.
 */
export function callTranscription(
  send: (event: Record<string, unknown>) => void,
  context: CallOrigin,
  maximum: number,
  accept: (text: string, origin: VoiceOrigin) => void
): CallTranscriptionController {
  let capture: CaptureSlice | null = null;
  let insideSpeech = false;

  const closeSlice = () => {
    if (!capture?.id) return;
    send({
      type: PREFIX + (capture.boundary === "overlong" ? "cancel" : "finish"),
      capture_id: capture.id,
    });
    if (capture.boundary === "overlong") {
      capture = null;
    } else {
      capture.waiting = true;
    }
  };

  return {
    pcm(buffer: ArrayBuffer) {
      const speaking = voiceInBlock(buffer);
      if (speaking && !insideSpeech && !capture) {
        capture = { frames: 0 };
        send({ type: `${PREFIX}start` });
      }
      insideSpeech = speaking;
      if (!capture || capture.waiting || capture.boundary) return;
      if (!speaking || capture.frames >= maximum) {
        capture.boundary = speaking ? "overlong" : "quiet";
        closeSlice();
      } else {
        capture.frames += 1;
      }
    },
    event(event: TranscriptionGatewayEvent) {
      if (typeof event.type !== "string" || !event.type.startsWith(PREFIX) || !capture) {
        return;
      }
      if (event.type === `${PREFIX}started` && !capture.id) {
        capture.id = event.capture_id;
        if (capture.boundary) {
          closeSlice();
        }
      } else if (event.capture_id === capture.id && event.type === `${PREFIX}failed`) {
        capture = null;
      } else if (
        event.capture_id === capture.id &&
        capture.waiting &&
        event.type === `${PREFIX}completed`
      ) {
        capture = null;
        if (
          event.source !== "explicit_user_audio" ||
          typeof event.transcript !== "string" ||
          !event.transcript.trim() ||
          !/^[a-f0-9]{64}$/.test(event.receipt ?? "")
        ) {
          return;
        }
        accept(event.transcript, {
          ...context,
          capture_id: event.capture_id!,
          machine_text: event.transcript,
          receipt: event.receipt!,
          samples: event.samples,
          transcript_receipt: event.transcript_receipt,
        });
      }
    },
    get pending() {
      return capture !== null;
    },
  };
}
