import type { components, paths } from "./schema";

export type ApiPaths = paths;
export type ApiComponents = components;
export type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];

export type HttpMethod = "get" | "put" | "post" | "delete" | "patch";

export type PathsWithMethod<M extends HttpMethod> = {
  [P in keyof paths]: paths[P][M] extends Record<string, unknown> ? P : never;
}[keyof paths];

type OperationOf<P extends keyof paths, M extends HttpMethod> = NonNullable<paths[P][M]>;

export type OperationQuery<P extends keyof paths, M extends HttpMethod> =
  OperationOf<P, M> extends { parameters: { query?: infer Q } }
    ? Exclude<Q, never | undefined>
    : never;

export type OperationPathParams<P extends keyof paths, M extends HttpMethod> =
  OperationOf<P, M> extends { parameters: { path?: infer PP } }
    ? Exclude<PP, never | undefined>
    : never;

export type OperationRequestBody<P extends keyof paths, M extends HttpMethod> =
  OperationOf<P, M> extends { requestBody?: { content: { "application/json": infer B } } }
    ? B
    : OperationOf<P, M> extends { requestBody: { content: { "application/json": infer B } } }
      ? B
      : unknown;

export type OperationJsonResponse<P extends keyof paths, M extends HttpMethod> =
  OperationOf<P, M> extends { responses: { 200: { content: { "application/json": infer R } } } }
    ? R
    : OperationOf<P, M> extends { responses: { 201: { content: { "application/json": infer R } } } }
      ? R
      : unknown;

export type UnauthorizedHandler = () => void;

let registeredUnauthorizedHandler: UnauthorizedHandler | null = null;

/** Register the workspace-wide 401 handler (typically owned by AppShell). */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  registeredUnauthorizedHandler = handler;
}

export function notifyUnauthorized(override?: UnauthorizedHandler | null): void {
  if (typeof override === "function") {
    override();
    return;
  }
  registeredUnauthorizedHandler?.();
}

/**
 * Resolve an application-relative path against `<base href="...">` without double-prefixing.
 * Absolute URLs (`https://...`, `//...`) and non-slash strings are returned unchanged.
 */
export function studioPath(value: string): string {
  const base = globalThis.document?.querySelector?.("base")?.getAttribute?.("href") || "/";
  const prefix = base.replace(/\/$/, "");
  if (!prefix || typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
    return value;
  }
  const pathOnly = value.split(/[?#]/, 1)[0];
  if (pathOnly === prefix || value.startsWith(`${prefix}/`)) {
    return value;
  }
  return `${prefix}${value}`;
}

/** Build a WebSocket URL honoring the current origin and `<base href>` subpath prefix. */
export function studioWsUrl(path: string): string {
  const protocol = globalThis.location?.protocol === "https:" ? "wss:" : "ws:";
  const host = globalThis.location?.host || "127.0.0.1";
  return `${protocol}//${host}${studioPath(path)}`;
}

export interface ApiErrorOptions {
  status: number;
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
  body?: unknown;
  streamEvent?: boolean;
}

/** Typed HTTP / OpenAPI ErrorEnvelope error raised by `apiRequest` and SSE helpers. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | null;
  readonly body: unknown;
  readonly streamEvent: boolean;

  constructor(options: ApiErrorOptions) {
    super(options.message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.details = options.details ?? null;
    this.body = options.body;
    this.streamEvent = Boolean(options.streamEvent);
  }
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRawBody(body: unknown): body is BodyInit {
  if (typeof body === "string") return true;
  if (typeof FormData !== "undefined" && body instanceof FormData) return true;
  if (typeof Blob !== "undefined" && body instanceof Blob) return true;
  if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) return true;
  if (typeof ArrayBuffer !== "undefined" && body instanceof ArrayBuffer) return true;
  if (ArrayBuffer.isView(body)) return true;
  if (typeof ReadableStream !== "undefined" && body instanceof ReadableStream) return true;
  return false;
}

export function buildApiPath(
  template: string,
  pathParams?: Record<string, string | number>,
  query?: Record<string, unknown>
): string {
  let resolved = template;
  if (pathParams) {
    for (const [key, val] of Object.entries(pathParams)) {
      resolved = resolved.replaceAll(`{${key}}`, encodeURIComponent(String(val)));
    }
  }
  if (query) {
    const search = new URLSearchParams();
    for (const [key, val] of Object.entries(query)) {
      if (val === undefined || val === null) continue;
      if (Array.isArray(val)) {
        for (const item of val) {
          if (item !== undefined && item !== null) search.append(key, String(item));
        }
      } else {
        search.set(key, String(val));
      }
    }
    const qs = search.toString();
    if (qs) {
      resolved += (resolved.includes("?") ? "&" : "?") + qs;
    }
  }
  return resolved;
}

export function parseErrorEnvelope(
  status: number,
  statusText: string,
  rawBody: unknown
): { code: string; message: string; details: Record<string, unknown> | null } {
  const fallbackCode = `HTTP_${status}`;
  const fallbackMessage = `${status} ${statusText}`.trim() || "请求处理失败";

  if (isPlainRecord(rawBody)) {
    const hasEnvelope = typeof rawBody.code === "string" && typeof rawBody.message === "string";
    if (hasEnvelope) {
      const details = isPlainRecord(rawBody.details) ? rawBody.details : null;
      return {
        code: rawBody.code as string,
        message: (rawBody.message as string) || fallbackMessage,
        details,
      };
    }
    const rawMsg = rawBody.message ?? rawBody.detail ?? rawBody.error;
    const message =
      rawMsg == null
        ? fallbackMessage
        : typeof rawMsg === "object"
          ? JSON.stringify(rawMsg)
          : String(rawMsg);
    const details = isPlainRecord(rawBody.details) ? rawBody.details : null;
    return {
      code: typeof rawBody.code === "string" ? rawBody.code : fallbackCode,
      message: message || fallbackMessage,
      details,
    };
  }

  if (typeof rawBody === "string" && rawBody.trim()) {
    return { code: fallbackCode, message: rawBody.trim(), details: null };
  }

  return { code: fallbackCode, message: fallbackMessage, details: null };
}

export async function parseResponseError(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => "");
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { detail: text };
    }
  } else {
    parsed = {};
  }
  const { code, message, details } = parseErrorEnvelope(response.status, response.statusText, parsed);
  return new ApiError({
    status: response.status,
    code,
    message,
    details,
    body: parsed,
  });
}

export type ResponseType = "json" | "text" | "blob" | "arrayBuffer" | "response";

export interface ApiRequestOptions<TBody = unknown> extends Omit<RequestInit, "body"> {
  body?: TBody;
  pathParams?: Record<string, string | number>;
  query?: Record<string, unknown>;
  responseType?: ResponseType;
  onUnauthorized?: UnauthorizedHandler | null;
}

export function prepareRequestInit<TBody>(options: ApiRequestOptions<TBody> = {}): RequestInit {
  const { body, headers, pathParams: _p, query: _q, responseType: _r, onUnauthorized: _u, ...rest } = options;
  const resolvedHeaders = new Headers(headers);
  const config: RequestInit = {
    ...rest,
    headers: resolvedHeaders,
    credentials: rest.credentials ?? "same-origin",
  };
  if (body !== undefined) {
    if (isRawBody(body)) {
      config.body = body;
    } else {
      if (!resolvedHeaders.has("Content-Type")) {
        resolvedHeaders.set("Content-Type", "application/json");
      }
      config.body = JSON.stringify(body);
    }
  }
  return config;
}

/**
 * Core typed API request wrapper over `fetch`, honoring `<base href>` via `studioPath()`,
 * encoding JSON/FormData/Blob payloads, parsing backend `{code, message, details}` envelopes,
 * and triggering `onUnauthorized` on HTTP 401.
 */
export async function apiRequest<TResponse = unknown, TBody = unknown>(
  path: string,
  options: ApiRequestOptions<TBody> = {},
  onUnauthorized?: UnauthorizedHandler | null
): Promise<TResponse> {
  const fullPath = buildApiPath(path, options.pathParams, options.query);
  const config = prepareRequestInit(options);
  const response = await fetch(studioPath(fullPath), config);

  if (!response.ok) {
    if (response.status === 401) {
      notifyUnauthorized(options.onUnauthorized ?? onUnauthorized);
    }
    throw await parseResponseError(response);
  }

  const mode = options.responseType ?? "json";
  if (mode === "response") return response as unknown as TResponse;
  if (mode === "blob") return (await response.blob()) as unknown as TResponse;
  if (mode === "arrayBuffer") return (await response.arrayBuffer()) as unknown as TResponse;
  if (mode === "text") return (await response.text()) as unknown as TResponse;

  const text = await response.text();
  if (!text) return {} as TResponse;
  try {
    return JSON.parse(text) as TResponse;
  } catch {
    return { detail: text } as unknown as TResponse;
  }
}

export interface TypedEndpointOptions<P extends keyof paths, M extends HttpMethod, TBody>
  extends Omit<ApiRequestOptions<TBody>, "method" | "pathParams" | "query"> {
  pathParams?: OperationPathParams<P, M> extends never ? Record<string, string | number> : OperationPathParams<P, M>;
  query?: OperationQuery<P, M> extends never ? Record<string, unknown> : OperationQuery<P, M>;
}

export function apiGet<
  P extends PathsWithMethod<"get">,
  TResponse = OperationJsonResponse<P, "get">,
>(path: P, options?: TypedEndpointOptions<P, "get", never>): Promise<TResponse> {
  return apiRequest<TResponse>(path, { ...(options as ApiRequestOptions), method: "GET" });
}

export function apiPost<
  P extends PathsWithMethod<"post">,
  TResponse = OperationJsonResponse<P, "post">,
  TBody = OperationRequestBody<P, "post">,
>(path: P, options?: TypedEndpointOptions<P, "post", TBody>): Promise<TResponse> {
  return apiRequest<TResponse, TBody>(path, { ...(options as ApiRequestOptions<TBody>), method: "POST" });
}

export function apiPut<
  P extends PathsWithMethod<"put">,
  TResponse = OperationJsonResponse<P, "put">,
  TBody = OperationRequestBody<P, "put">,
>(path: P, options?: TypedEndpointOptions<P, "put", TBody>): Promise<TResponse> {
  return apiRequest<TResponse, TBody>(path, { ...(options as ApiRequestOptions<TBody>), method: "PUT" });
}

export function apiPatch<
  P extends PathsWithMethod<"patch">,
  TResponse = OperationJsonResponse<P, "patch">,
  TBody = OperationRequestBody<P, "patch">,
>(path: P, options?: TypedEndpointOptions<P, "patch", TBody>): Promise<TResponse> {
  return apiRequest<TResponse, TBody>(path, { ...(options as ApiRequestOptions<TBody>), method: "PATCH" });
}

export function apiDelete<
  P extends PathsWithMethod<"delete">,
  TResponse = OperationJsonResponse<P, "delete">,
  TBody = OperationRequestBody<P, "delete">,
>(path: P, options?: TypedEndpointOptions<P, "delete", TBody>): Promise<TResponse> {
  return apiRequest<TResponse, TBody>(path, { ...(options as ApiRequestOptions<TBody>), method: "DELETE" });
}
