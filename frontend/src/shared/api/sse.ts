import {
  ApiError,
  apiPost,
  notifyUnauthorized,
  parseErrorEnvelope,
  parseResponseError,
  prepareRequestInit,
  studioPath,
  type ApiRequestOptions,
  type UnauthorizedHandler,
} from "./client";

export interface SseEventFrame<TData = unknown> {
  event: string;
  data: TData;
  id?: string;
}

export interface StreamSseOptions<TBody = unknown, TDone = unknown>
  extends Omit<ApiRequestOptions<TBody>, "responseType"> {
  onEvent?: (event: string, data: unknown, frame: SseEventFrame) => void;
  /** When true (default), resolving without a `done` event raises "回复连接已中断". */
  requireDone?: boolean;
  /** When true, an `application/json` response is parsed and returned directly (e.g., batch-publish fallback). */
  allowJsonResponse?: boolean;
  _doneType?: TDone;
}

function parseSseBlock(block: string): SseEventFrame | null {
  const lines = block.split("\n");
  if (!lines.length || lines.every((line) => !line || line.startsWith(":"))) {
    return null;
  }

  let event = "message";
  let id: string | undefined;
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      const value = line.slice(5);
      dataLines.push(value.startsWith(" ") ? value.slice(1) : value);
    } else if (line.startsWith("id:")) {
      id = line.slice(3).trim();
    }
  }

  if (dataLines.length === 0) return null;
  const rawData = dataLines.join("\n");
  let data: unknown = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {
    // Keep non-JSON text payload as-is.
  }
  return { event, data, id };
}

function buildStreamEventError(payload: unknown): ApiError {
  if (typeof payload === "object" && payload !== null) {
    const rec = payload as Record<string, unknown>;
    const status = typeof rec.status === "number" ? rec.status : 500;
    const { code, message, details } = parseErrorEnvelope(status, "Stream Error", rec);
    return new ApiError({
      status,
      code,
      message,
      details,
      body: rec,
      streamEvent: true,
    });
  }
  return new ApiError({
    status: 500,
    code: "STREAM_ERROR",
    message: typeof payload === "string" && payload ? payload : "流式响应失败",
    body: payload,
    streamEvent: true,
  });
}

/**
 * Stream Server-Sent Events (`\n\n` frames, `:` keepalives, JSON `data:` blocks) over `fetch`.
 * Supports `stage`, `delta`, `progress`, `done`, `error` events and `AbortController`.
 */
export async function streamSseRequest<TDone = unknown, TBody = unknown>(
  path: string,
  options: StreamSseOptions<TBody, TDone> = {},
  onUnauthorized?: UnauthorizedHandler | null
): Promise<TDone> {
  const { onEvent, requireDone = true, allowJsonResponse = false, ...rest } = options;
  const init = prepareRequestInit(rest);
  const headers = new Headers(init.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "text/event-stream");
  }
  init.headers = headers;

  const response = await fetch(studioPath(path), init);
  if (!response.ok) {
    if (response.status === 401) {
      notifyUnauthorized(options.onUnauthorized ?? onUnauthorized);
    }
    throw await parseResponseError(response);
  }

  const contentType = response.headers.get("content-type") || "";
  if (allowJsonResponse && contentType.includes("application/json")) {
    const text = await response.text();
    return (text ? JSON.parse(text) : {}) as TDone;
  }

  if (!response.body) {
    throw new Error("回复连接已中断");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const processFrames = (): TDone | undefined => {
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const rawBlock = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const frame = parseSseBlock(rawBlock);
      if (!frame) continue;
      if (frame.event === "done") {
        return frame.data as TDone;
      }
      if (frame.event === "error") {
        throw buildStreamEventError(frame.data);
      }
      onEvent?.(frame.event, frame.data, frame);
    }
    return undefined;
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        buffer = buffer.replace(/\r\n/g, "\n");
        const completed = processFrames();
        if (completed !== undefined) return completed;
        if (requireDone) {
          throw new Error("回复连接已中断");
        }
        return undefined as TDone;
      }
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const completed = processFrames();
      if (completed !== undefined) return completed;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export type ChatStreamStage =
  | "queued"
  | "preparing"
  | "preflight"
  | "checking_model"
  | "counting"
  | "generating"
  | "working"
  | (string & {});

export interface StreamAgentChatOptions<TBody = unknown, TDone = unknown> {
  body: TBody;
  signal?: AbortSignal;
  onStage?: (stage: ChatStreamStage) => void;
  onDelta?: (delta: string) => void;
  onEvent?: (event: string, data: unknown) => void;
  onUnauthorized?: UnauthorizedHandler | null;
  _doneType?: TDone;
}

/**
 * Stream `POST /api/v1/agent/chat/stream`, falling back to `POST /api/v1/agent/chat`
 * only when the streaming endpoint responds with non-stream HTTP 404 or 405.
 */
export async function streamAgentChat<TDone = Record<string, unknown>, TBody = Record<string, unknown>>(
  options: StreamAgentChatOptions<TBody, TDone>
): Promise<TDone> {
  try {
    return await streamSseRequest<TDone, TBody>("/api/v1/agent/chat/stream", {
      method: "POST",
      body: options.body,
      signal: options.signal,
      onUnauthorized: options.onUnauthorized,
      onEvent(event, data) {
        if (event === "stage" && typeof data === "string") {
          options.onStage?.(data as ChatStreamStage);
        } else if (event === "delta" && typeof data === "string") {
          options.onDelta?.(data);
        }
        options.onEvent?.(event, data);
      },
    });
  } catch (error) {
    if (error instanceof ApiError && !error.streamEvent && (error.status === 404 || error.status === 405)) {
      return await apiPost<"/api/v1/agent/chat", TDone, TBody>("/api/v1/agent/chat", {
        body: options.body,
        signal: options.signal,
        onUnauthorized: options.onUnauthorized,
      });
    }
    throw error;
  }
}

export interface SubscribeChatEventsOptions {
  signal?: AbortSignal;
  onEvent: (event: string, data: unknown) => void;
  onUnauthorized?: UnauthorizedHandler | null;
}

/** Subscribe to server-pushed `GET /api/v1/agent/chat/events` SSE delivery notifications. */
export async function subscribeAgentChatEvents(options: SubscribeChatEventsOptions): Promise<void> {
  await streamSseRequest<void>("/api/v1/agent/chat/events", {
    method: "GET",
    signal: options.signal,
    requireDone: false,
    onUnauthorized: options.onUnauthorized,
    onEvent(event, data) {
      options.onEvent(event, data);
    },
  });
}

export interface BatchPublishStreamOptions<TBody = unknown, TResult = unknown> {
  body: TBody;
  signal?: AbortSignal;
  onProgress?: (item: TResult) => void;
  onUnauthorized?: UnauthorizedHandler | null;
}

/** Stream `POST /api/v1/operations/comments/workbench/batch-publish` (`progress` + `done`). */
export async function streamBatchPublish<
  TDone = { items: unknown[] },
  TBody = Record<string, unknown>,
  TProgress = unknown,
>(options: BatchPublishStreamOptions<TBody, TProgress>): Promise<TDone> {
  return streamSseRequest<TDone, TBody>("/api/v1/operations/comments/workbench/batch-publish", {
    method: "POST",
    body: options.body,
    signal: options.signal,
    allowJsonResponse: true,
    onUnauthorized: options.onUnauthorized,
    onEvent(event, data) {
      if (event === "progress") {
        options.onProgress?.(data as TProgress);
      }
    },
  });
}

export interface StreamNdjsonOptions<TBody = unknown, TRecord = unknown> {
  method?: string;
  body?: TBody;
  signal?: AbortSignal;
  inactivityTimeoutMs?: number;
  onRecord: (record: TRecord) => void | Promise<void>;
  onUnauthorized?: UnauthorizedHandler | null;
}

/** Stream line-delimited JSON (used by `/api/v1/agent/private-call/memory-watch`). */
export async function streamNdjson<TRecord = Record<string, unknown>, TBody = unknown>(
  path: string,
  options: StreamNdjsonOptions<TBody, TRecord>
): Promise<void> {
  const init = prepareRequestInit({
    method: options.method ?? "POST",
    body: options.body,
    signal: options.signal,
  });
  const response = await fetch(studioPath(path), init);
  if (!response.ok) {
    if (response.status === 401) {
      notifyUnauthorized(options.onUnauthorized);
    }
    throw await parseResponseError(response);
  }
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let timer: ReturnType<typeof setTimeout> | undefined;

  const resetTimer = () => {
    if (timer !== undefined) clearTimeout(timer);
    if (options.inactivityTimeoutMs && options.inactivityTimeoutMs > 0) {
      timer = setTimeout(() => {
        void reader.cancel(new Error("NDJSON stream inactivity timeout"));
      }, options.inactivityTimeoutMs);
    }
  };

  try {
    resetTimer();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      resetTimer();
      buffer += decoder.decode(value, { stream: true });
      let newlineIdx: number;
      while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newlineIdx).trim();
        buffer = buffer.slice(newlineIdx + 1);
        if (!line) continue;
        await options.onRecord(JSON.parse(line) as TRecord);
      }
    }
    const tail = buffer.trim();
    if (tail) {
      await options.onRecord(JSON.parse(tail) as TRecord);
    }
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
