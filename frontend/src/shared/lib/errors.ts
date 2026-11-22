export interface NoticePayload {
  message: string;
  error?: boolean;
  timestamp: number;
}

export interface WorkspaceIssuePayload {
  message: string;
  source?: string;
}

export interface CharacterLike {
  id?: string | null;
  display_name?: string | null;
  name?: string | null;
  version?: number | string | null;
  voice_profile?: string | null;
}

/** Mask credential values in URLs/queries/errors before presenting them in UI or logs. */
export function safeMessage(message: unknown): string {
  if (message == null || message === "") return "";
  return String(message).replace(
    /((?:sessdata|bili_jct|token|api[_-]?key)=)[^&;\s]+/gi,
    "$1[已隐藏]"
  );
}

export function webCharacterName(character: CharacterLike | null | undefined): string {
  return character?.display_name || character?.name || character?.id || "";
}

export function errorMessage(error: unknown, fallback = "请求处理失败"): string {
  if (error instanceof Error && error.message) return safeMessage(error.message);
  if (typeof error === "string" && error) return safeMessage(error);
  if (typeof error === "object" && error !== null && "message" in error) {
    const msg = (error as { message?: unknown }).message;
    if (typeof msg === "string" && msg) return safeMessage(msg);
  }
  return fallback;
}

const NOTICE_EVENT = "tuber:notice";
const ISSUE_EVENT = "workspace-issue";

/**
 * Emit a workspace notice banner. When `error` is true, also dispatches a cancelable
 * `workspace-issue` event so an active issue log can capture it (matching `core.js`).
 */
export function emitNotice(message: string, error = false, source?: string): void {
  const sanitized = safeMessage(message);
  if (error && typeof document !== "undefined") {
    const issueEvent = new CustomEvent<WorkspaceIssuePayload>(ISSUE_EVENT, {
      detail: { message: sanitized, source },
      cancelable: true,
    });
    if (!document.dispatchEvent(issueEvent)) {
      return;
    }
  }
  if (typeof document !== "undefined") {
    document.dispatchEvent(
      new CustomEvent<NoticePayload>(NOTICE_EVENT, {
        detail: { message: sanitized, error, timestamp: Date.now() },
      })
    );
  }
}

export function emitWorkspaceIssue(message: string, source?: string): void {
  const sanitized = safeMessage(message);
  if (!sanitized || typeof document === "undefined") return;
  document.dispatchEvent(
    new CustomEvent<WorkspaceIssuePayload>(ISSUE_EVENT, {
      detail: { message: sanitized, source },
      cancelable: true,
    })
  );
}

export function subscribeNotices(listener: (payload: NoticePayload) => void): () => void {
  if (typeof document === "undefined") return () => {};
  const handler = (event: Event) => {
    const custom = event as CustomEvent<NoticePayload>;
    if (custom.detail) listener(custom.detail);
  };
  document.addEventListener(NOTICE_EVENT, handler);
  return () => document.removeEventListener(NOTICE_EVENT, handler);
}

export function subscribeWorkspaceIssues(
  listener: (payload: WorkspaceIssuePayload, event: CustomEvent<WorkspaceIssuePayload>) => void
): () => void {
  if (typeof document === "undefined") return () => {};
  const handler = (event: Event) => {
    const custom = event as CustomEvent<WorkspaceIssuePayload>;
    if (custom.detail) listener(custom.detail, custom);
  };
  document.addEventListener(ISSUE_EVENT, handler);
  return () => document.removeEventListener(ISSUE_EVENT, handler);
}
