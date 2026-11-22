import { ApiError } from "@/shared/api/client";
import { streamNdjson } from "@/shared/api/sse";

interface MemoryWatchRecord {
  state?: string;
  guard?: string;
}

/**
 * Arm the owner's memory revocation NDJSON stream before opening media transports.
 * Resolves with the active `guard` token on the first valid record and continues
 * monitoring subsequent heartbeats with a 3500ms inactivity timeout.
 */
export async function guardCall(
  signal: AbortSignal,
  use: Record<string, unknown>,
  invalidate: (reason: string) => void
): Promise<string | undefined> {
  const localAbort = new AbortController();
  const onParentAbort = () => localAbort.abort();
  if (signal.aborted) {
    localAbort.abort();
  } else {
    signal.addEventListener("abort", onParentAbort, { once: true });
  }

  return new Promise<string | undefined>((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const clearWatchdog = () => {
      if (timer !== undefined) {
        clearTimeout(timer);
        timer = undefined;
      }
    };

    const armWatchdog = () => {
      clearWatchdog();
      timer = setTimeout(() => {
        localAbort.abort();
        if (!signal.aborted) {
          invalidate("closed");
        }
      }, 3500);
    };

    void streamNdjson<MemoryWatchRecord, Record<string, unknown>>(
      "/api/v1/agent/private-call/memory-watch",
      {
        method: "POST",
        body: use,
        signal: localAbort.signal,
        inactivityTimeoutMs: 3500,
        onRecord(record) {
          if (!settled) {
            settled = true;
            if (record.state !== "valid") {
              localAbort.abort();
              invalidate(record.state || "closed");
              resolve(undefined);
              return;
            }
            if (!record.guard) {
              localAbort.abort();
              reject(new Error("记忆订阅无效"));
              return;
            }
            armWatchdog();
            resolve(record.guard);
            return;
          }
          armWatchdog();
          if (record.state !== "valid") {
            clearWatchdog();
            localAbort.abort();
            invalidate(record.state || "closed");
          }
        },
      }
    ).then(
      () => {
        clearWatchdog();
        signal.removeEventListener("abort", onParentAbort);
        if (!settled) {
          settled = true;
          reject(new Error("订阅已断开"));
        } else if (!signal.aborted && !localAbort.signal.aborted) {
          invalidate("closed");
        }
      },
      (error: unknown) => {
        clearWatchdog();
        signal.removeEventListener("abort", onParentAbort);
        if (!settled) {
          settled = true;
          if (error instanceof ApiError && error.status === 409) {
            invalidate("changed");
            resolve(undefined);
            return;
          }
          reject(error instanceof ApiError ? new Error("无法订阅记忆更新") : error);
        } else if (!signal.aborted && !localAbort.signal.aborted) {
          invalidate("closed");
        }
      }
    );
  });
}
