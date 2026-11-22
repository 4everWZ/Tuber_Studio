import { describe, expect, it } from "vitest";
import { FinalUtterances } from "./FinalUtterances";
import { callTranscription } from "./callTranscription";

describe("FinalUtterances & callTranscription", () => {
  it("handoff requires a committed user audio part and a unique final transcript", () => {
    const utterances = new FinalUtterances();
    const finalEvent = {
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "voice",
      content_index: 0,
      transcript: "准备提纲",
    };

    expect(utterances.accept(finalEvent)).toBeUndefined();

    const item = {
      type: "message",
      id: "assistant-voice",
      role: "assistant",
      content: [{ type: "input_audio" }],
    };
    utterances.accept({ type: "conversation.item.added", item });
    utterances.accept({ type: "input_audio_buffer.committed", item_id: "voice" });
    expect(utterances.accept(finalEvent)).toBeUndefined();

    utterances.accept({ type: "input_audio_buffer.committed", item_id: item.id });
    expect(utterances.accept({ ...finalEvent, item_id: item.id })).toBeUndefined();

    utterances.accept({
      type: "conversation.item.created",
      item: { ...item, id: "voice", role: "user" },
    });

    for (const type of [
      "response.output_audio_transcript.done",
      "conversation.item.input_audio_transcription.delta",
      "response.output_text.done",
    ]) {
      expect(utterances.accept({ ...finalEvent, type })).toBeUndefined();
    }

    expect(utterances.accept({ ...finalEvent, content_index: 1 })).toBeUndefined();
    expect(utterances.accept(finalEvent)).toBe("准备提纲");
    expect(utterances.accept(finalEvent)).toBeUndefined();
  });

  it("microphone slices request only a complete signed transcription", () => {
    const sent: Array<Record<string, unknown>> = [];
    const accepted: Array<{ text: string; origin: Record<string, unknown> }> = [];
    const prefix = "gateway.input_transcription.";
    const pcm = (value: number) => new Int16Array(16000).fill(value).buffer;

    const mic = callTranscription(
      (event) => sent.push(event),
      { ticket_hash: "call", scope_signature: "sig-1" },
      2,
      (text, origin) => accepted.push({ text, origin })
    );

    const capture = "mic-1";
    const reply = {
      type: `${prefix}completed`,
      capture_id: capture,
      transcript: "整理私有便笺",
      source: "explicit_user_audio",
      samples: 32000,
      receipt: "a".repeat(64),
    };

    mic.pcm(pcm(4));
    expect(sent).toEqual([]);

    mic.pcm(pcm(3000));
    mic.event({ type: `${prefix}started`, capture_id: capture });
    mic.pcm(pcm(3000));
    mic.event(reply);
    expect(accepted).toEqual([]);

    mic.pcm(pcm(0));
    expect(sent).toEqual([
      { type: `${prefix}start` },
      { type: `${prefix}finish`, capture_id: capture },
    ]);

    mic.event({ ...reply, type: `${prefix}delta` });
    mic.event({ type: "response.output_audio_transcript.done", transcript: "助手的话" });
    mic.event({ ...reply, capture_id: "other" });
    mic.event(reply);
    mic.event(reply);

    expect(accepted).toHaveLength(1);
    expect(accepted[0].origin.capture_id).toBe(capture);

    mic.pcm(pcm(3000));
    mic.event({ type: `${prefix}started`, capture_id: "overlong" });
    mic.pcm(pcm(3000));
    mic.pcm(pcm(3000));

    expect(sent.at(-1)).toEqual({ type: `${prefix}cancel`, capture_id: "overlong" });
    expect(mic.pending).toBe(false);
  });
});
