import { useEffect, useSyncExternalStore } from "react";
import { apiGet, apiPost } from "@/shared/api/client";
import { errorMessage, safeMessage, subscribeWorkspaceIssues } from "@/shared/lib/errors";
import type { DiscoveryRecordsResponse, IssueRecordItem } from "./types";

export interface UnifiedIssueEntry {
  key: string;
  severity: "warning" | "error";
  unread: number;
  text: string;
  time: number;
  href: string | null;
  observation?: {
    id: string;
    updated_at: number;
    occurrences: number;
  };
}

interface IssueStoreSnapshot {
  entries: UnifiedIssueEntry[];
  loading: boolean;
  error: string | null;
  records: DiscoveryRecordsResponse | null;
}

const MAX_LOCAL_ISSUES = 50;

class IssueLogStoreImpl {
  private localEntries = new Map<string, UnifiedIssueEntry>();
  private remoteEntries: UnifiedIssueEntry[] = [];
  private records: DiscoveryRecordsResponse | null = null;
  private loading = false;
  private error: string | null = null;
  private requestSeq = 0;
  private listeners = new Set<() => void>();
  private snapshot: IssueStoreSnapshot = {
    entries: [],
    loading: false,
    error: null,
    records: null,
  };
  private unsubWorkspaceIssue: (() => void) | null = null;

  constructor() {
    this.ensureSubscription();
  }

  private ensureSubscription(): void {
    if (this.unsubWorkspaceIssue) return;
    this.unsubWorkspaceIssue = subscribeWorkspaceIssues((payload) => {
      const href = payload.source || (typeof window !== "undefined" ? window.location.hash || "#automation" : "#automation");
      this.recordLocal(payload.message, href);
    });
  }

  private rebuildSnapshot(): void {
    const merged = [...this.localEntries.values(), ...this.remoteEntries].sort(
      (a, b) => b.time - a.time
    );
    this.snapshot = {
      entries: merged,
      loading: this.loading,
      error: this.error,
      records: this.records,
    };
  }

  private notify(): void {
    this.rebuildSnapshot();
    for (const listener of this.listeners) {
      listener();
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.ensureSubscription();
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): IssueStoreSnapshot => this.snapshot;

  unreadCount(): number {
    return this.snapshot.entries.filter((item) => item.unread > 0).length;
  }

  recordLocal(message: string, href = "#automation"): void {
    const text = safeMessage(message);
    if (!text) return;
    const key = JSON.stringify([text, href]);
    if (this.localEntries.has(key)) return;
    this.localEntries.set(key, {
      key: `local:${key}`,
      severity: "error",
      unread: 1,
      text,
      time: Date.now() / 1000,
      href,
    });
    if (this.localEntries.size > MAX_LOCAL_ISSUES) {
      const oldestKey = this.localEntries.keys().next().value;
      if (oldestKey !== undefined) {
        this.localEntries.delete(oldestKey);
      }
    }
    this.notify();
  }

  private mapRemoteIssue(item: IssueRecordItem): UnifiedIssueEntry {
    const severity =
      item.severity || (["retry_wait", "recovered"].includes(item.status || "") ? "warning" : "error");
    const unread = item.unread_occurrences ?? item.occurrences ?? 1;
    const totalOccurrences = item.total_occurrences ?? item.occurrences ?? 1;
    const text = `${severity === "warning" ? "告警" : "错误"} · ${item.bvid ? `${item.bvid} · ` : ""}${safeMessage(item.error)}${(item.occurrences ?? 1) > 1 ? `（记录 ${item.occurrences} 次）` : ""}`;
    const href =
      item.href ||
      (item.round_id ? `#automation/round/${item.round_id}${item.bvid ? `/${item.id}` : ""}` : null);
    return {
      key: `remote:${item.id}:${item.updated_at}:${totalOccurrences}`,
      severity,
      unread,
      text,
      time: item.updated_at,
      href,
      observation: item.id
        ? {
            id: item.id,
            updated_at: item.updated_at,
            occurrences: totalOccurrences,
          }
        : undefined,
    };
  }

  applyRecords(data: DiscoveryRecordsResponse): void {
    this.records = data;
    const raw = (data.issue_history?.length ? data.issue_history : data.issues) || [];
    this.remoteEntries = raw.map((item) => this.mapRemoteIssue(item));
    this.error = null;
    this.notify();
  }

  async refresh(): Promise<DiscoveryRecordsResponse | null> {
    const current = ++this.requestSeq;
    this.loading = true;
    this.notify();
    try {
      const data = await apiGet<"/api/v1/operations/discovery/records", DiscoveryRecordsResponse>(
        "/api/v1/operations/discovery/records"
      );
      if (current !== this.requestSeq) return null;
      this.loading = false;
      this.applyRecords(data);
      return data;
    } catch (err) {
      if (current !== this.requestSeq) return null;
      this.loading = false;
      this.error = errorMessage(err);
      this.recordLocal(this.error, "#issues");
      this.notify();
      return null;
    }
  }

  async acknowledgeOne(observation: { id: string; updated_at: number; occurrences: number }): Promise<void> {
    ++this.requestSeq;
    const data = await apiPost<
      "/api/v1/operations/issues/acknowledge",
      DiscoveryRecordsResponse,
      Array<{ id: string; updated_at: number; occurrences: number }>
    >("/api/v1/operations/issues/acknowledge", {
      body: [observation],
    });
    ++this.requestSeq;
    this.applyRecords(data);
  }

  async clearAll(): Promise<void> {
    ++this.requestSeq;
    const acknowledgedLocalKeys = new Set(this.localEntries.keys());
    const data = await apiPost<
      "/api/v1/operations/issues/clear",
      DiscoveryRecordsResponse & { records?: DiscoveryRecordsResponse },
      never
    >("/api/v1/operations/issues/clear");
    ++this.requestSeq;
    for (const key of acknowledgedLocalKeys) {
      this.localEntries.delete(key);
    }
    this.applyRecords(data.records || data);
  }

  reset(): void {
    this.localEntries.clear();
    this.remoteEntries = [];
    this.records = null;
    this.loading = false;
    this.error = null;
    this.requestSeq = 0;
    this.notify();
  }
}

export const issueLogStore = new IssueLogStoreImpl();

const stableRefresh = () => issueLogStore.refresh();
const stableAcknowledgeOne = (obs: { id: string; updated_at: number; occurrences: number }) =>
  issueLogStore.acknowledgeOne(obs);
const stableClearAll = () => issueLogStore.clearAll();
const stableRecordLocal = (message: string, href?: string) =>
  issueLogStore.recordLocal(message, href);

export function useIssueLog() {
  const state = useSyncExternalStore(
    issueLogStore.subscribe,
    issueLogStore.getSnapshot,
    issueLogStore.getSnapshot
  );

  useEffect(() => {
    // Ensure subscription stays active while mounted
  }, []);

  return {
    ...state,
    unreadCount: state.entries.filter((item) => item.unread > 0).length,
    refresh: stableRefresh,
    acknowledgeOne: stableAcknowledgeOne,
    clearAll: stableClearAll,
    recordLocal: stableRecordLocal,
  };
}
