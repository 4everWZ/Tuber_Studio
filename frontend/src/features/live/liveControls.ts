import type { LiveQueueItem, SongLyricsLine, SongQuality } from "./types";

export type LiveControlAction =
  | "start"
  | "pause"
  | "resume"
  | "stop"
  | "mute"
  | "unmute"
  | "skip"
  | "clear";

export interface LiveActionStatusInput {
  state?: string | null;
  muted?: boolean | null;
  current_job?: unknown;
  queue_length?: number | null;
}

export function liveActionDisabled(
  action: LiveControlAction,
  status: LiveActionStatusInput = {},
  { enabled = false }: { enabled?: boolean } = {}
): boolean {
  const state = status.state || "stopped";
  if (!enabled && !(action === "stop" && state !== "stopped")) {
    return true;
  }
  switch (action) {
    case "start":
      return state !== "stopped";
    case "pause":
      return !["live", "degraded"].includes(state);
    case "resume":
      return !["paused", "degraded"].includes(state);
    case "stop":
      return state === "stopped";
    case "mute":
      return Boolean(status.muted);
    case "unmute":
      return !status.muted;
    case "skip":
      return !status.current_job;
    case "clear":
      return !status.queue_length;
    default:
      return false;
  }
}

export function moveSongInQueue(
  queue: readonly LiveQueueItem[],
  id: string,
  direction: -1 | 1
): string[] | null {
  const order = queue.map((item) => item.id);
  const songIndices = queue.flatMap((item, index) => (item.type === "song" ? [index] : []));
  const index = songIndices.findIndex((position) => order[position] === id);
  const target = index + direction;
  if (index < 0 || ![-1, 1].includes(direction) || target < 0 || target >= songIndices.length) {
    return null;
  }
  const fromPos = songIndices[index];
  const toPos = songIndices[target];
  [order[fromPos], order[toPos]] = [order[toPos], order[fromPos]];
  return order;
}

/** Allow external song covers only from `.music.126.net` over HTTP(S), normalized to HTTPS. */
export function sanitizeCoverUrl(rawCover: string | null | undefined): string | null {
  if (!rawCover) return null;
  try {
    const url = new URL(rawCover);
    const host = url.hostname.toLowerCase();
    if (
      host.endsWith(".music.126.net") &&
      host.length > ".music.126.net".length &&
      (url.protocol === "http:" || url.protocol === "https:")
    ) {
      url.protocol = "https:";
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

export function parseLrcLyrics(text: string): SongLyricsLine[] {
  if (!text.trim()) return [];
  const lines = text.split(/\r?\n/).filter((line) => line.trim());
  const parsed: SongLyricsLine[] = [];
  for (const line of lines) {
    if (/^\[(ti|ar|al|by|offset):/.test(line)) continue;
    const matches = [...line.matchAll(/\[(\d+):(\d{2}(?:\.\d{1,3})?)\]/g)];
    if (!matches.length) {
      throw new Error("歌词请使用带时间戳的LRC格式");
    }
    const lyric = line.replace(/\[[^\]]*\]/g, "").trim();
    for (const match of matches) {
      parsed.push({
        time: Number(match[1]) * 60 + Number(match[2]),
        text: lyric,
      });
    }
  }
  return parsed.sort((a, b) => a.time - b.time);
}

export function formatElapsed(seconds: number | null | undefined): string {
  const total = Math.floor(Math.max(0, Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function formatSongQuality(quality: SongQuality | null | undefined): string {
  if (!quality) return "";
  if (quality.format === "flac") {
    return `FLAC ${quality.bits_per_sample}-bit / ${(quality.sample_rate ?? 0) / 1000} kHz`;
  }
  if (quality.bitrate) {
    return `${(quality.format || "音频").toUpperCase()} ${Math.round(quality.bitrate / 1000)} kbps`;
  }
  return quality.format ? quality.format.toUpperCase() : "";
}

export function buildSilentWavBlob(): Blob {
  const bytes = new Uint8Array(844);
  const data = new DataView(bytes.buffer);
  const write = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) {
      bytes[offset + i] = text.charCodeAt(i);
    }
  };
  write(0, "RIFF");
  data.setUint32(4, 836, true);
  write(8, "WAVEfmt ");
  data.setUint32(16, 16, true);
  data.setUint16(20, 1, true);
  data.setUint16(22, 1, true);
  data.setUint32(24, 8000, true);
  data.setUint32(28, 8000, true);
  data.setUint16(32, 1, true);
  data.setUint16(34, 8, true);
  write(36, "data");
  data.setUint32(40, 800, true);
  bytes.fill(128, 44);
  return new Blob([bytes], { type: "audio/wav" });
}
