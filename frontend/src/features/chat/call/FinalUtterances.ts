export interface RealtimeInputContentPart {
  type?: string;
  transcript?: string | null;
}

export interface RealtimeConversationEvent {
  type?: string;
  item_id?: string;
  content_index?: number;
  transcript?: string;
  item?: {
    id?: string;
    type?: string;
    role?: string;
    content?: RealtimeInputContentPart[];
  };
}

/**
 * Tracks committed user audio items from Realtime API events and emits each
 * finalized user transcript at most once.
 */
export class FinalUtterances {
  private items = new Map<string, RealtimeInputContentPart[]>();
  private committed = new Set<string>();
  private rejected = new Set<string>();
  private seen = new Set<string>();

  accept(event: RealtimeConversationEvent): string | undefined {
    if (event.type === "input_audio_buffer.committed") {
      if (typeof event.item_id === "string") {
        this.committed.add(event.item_id);
      }
      return undefined;
    }
    if (
      event.type === "conversation.item.added" ||
      event.type === "conversation.item.created" ||
      event.type === "conversation.item.done"
    ) {
      const item = event.item;
      if (
        typeof item?.id === "string" &&
        item.type === "message" &&
        Array.isArray(item.content)
      ) {
        if (item.role !== "user") {
          this.rejected.add(item.id);
        } else {
          this.items.set(item.id, item.content);
        }
      }
      return undefined;
    }

    const itemId = event.item_id;
    const contentIndex = event.content_index;
    if (typeof itemId !== "string" || typeof contentIndex !== "number" || !Number.isInteger(contentIndex)) {
      return undefined;
    }
    const part = this.items.get(itemId)?.[contentIndex];
    if (
      event.type !== "conversation.item.input_audio_transcription.completed" ||
      this.rejected.has(itemId) ||
      !this.committed.has(itemId) ||
      part?.type !== "input_audio" ||
      typeof event.transcript !== "string" ||
      !event.transcript.trim()
    ) {
      return undefined;
    }
    if (part.transcript && part.transcript !== event.transcript) {
      return undefined;
    }
    const key = JSON.stringify([itemId, contentIndex]);
    if (this.seen.has(key)) {
      return undefined;
    }
    this.seen.add(key);
    return event.transcript.trim();
  }
}
