import { useSyncExternalStore } from "react";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "@/shared/api/client";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import type {
  AutomationStatus,
  BilibiliAccountSnapshot,
  DiscoveryStatus,
  InboxPipelineStatus,
  InboxRecordsResponse,
  OperationClaim,
  OperationScheduleCloseoutRun,
  OperationScheduleCreateInput,
  OperationScheduleItem,
  OperationScheduleUpdateInput,
  QuotaSnapshot,
} from "./types";

export interface OperationsStoreState {
  status: AutomationStatus | null;
  account: BilibiliAccountSnapshot | null;
  discovery: DiscoveryStatus | null;
  quotas: QuotaSnapshot | null;
  inboxRecords: InboxRecordsResponse | null;
  claims: OperationClaim[];
  controlPending: boolean;
  statusError: string | null;
}

const INITIAL_STATE: OperationsStoreState = {
  status: null,
  account: null,
  discovery: null,
  quotas: null,
  inboxRecords: null,
  claims: [],
  controlPending: false,
  statusError: null,
};

function isStaleStatus(
  next: AutomationStatus,
  prev: AutomationStatus | null
): boolean {
  if (!prev) return false;
  if (
    typeof next.started_at === "number" &&
    typeof prev.started_at === "number" &&
    next.started_at < prev.started_at
  ) {
    return true;
  }
  if (
    next.started_at === prev.started_at &&
    typeof next.status_sequence === "number" &&
    typeof prev.status_sequence === "number" &&
    next.status_sequence < prev.status_sequence
  ) {
    return true;
  }
  return false;
}

class OperationsStoreImpl {
  private state: OperationsStoreState = { ...INITIAL_STATE };
  private listeners = new Set<() => void>();
  private statusRequestSeq = 0;
  private statusRequestedAt = 0;
  private controlRequestSeq = 0;
  private discoveryRequestSeq = 0;
  private inboxRequestSeq = 0;
  private quotasRequestSeq = 0;
  private claimsRequestSeq = 0;
  private accountRequestSeq = 0;

  private update(patch: Partial<OperationsStoreState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) {
      listener();
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): OperationsStoreState => this.state;

  applyAutomationStatus(next: AutomationStatus | null | undefined): boolean {
    if (!next) return false;
    if (isStaleStatus(next, this.state.status)) {
      return false;
    }
    const patch: Partial<OperationsStoreState> = {
      status: next,
      statusError: null,
    };
    if (Array.isArray(next.claims)) {
      patch.claims = next.claims;
    }
    if (next.discovery && typeof next.discovery === "object") {
      patch.discovery = { ...this.state.discovery, ...next.discovery };
    }
    this.update(patch);
    return true;
  }

  applyLiveStatus(
    automation:
      | {
          state?: string;
          enabled?: boolean;
          emergency?: boolean;
          epoch?: number;
          inbox_enabled?: boolean;
          discovery_enabled?: boolean;
          comment_cooldown_until?: number | null;
          active_schedule?: OperationScheduleItem | null;
        }
      | null
      | undefined
  ): void {
    if (!automation) return;
    const prev = this.state.status;
    if (!prev) {
      void this.refreshStatus();
      return;
    }
    const prevPolicy = prev.policy || {};
    const prevSchedId = prev.active_schedule?.id ?? null;
    const nextSchedId = automation.active_schedule?.id ?? null;
    const changed =
      prev.state !== automation.state ||
      Boolean(prevPolicy.enabled) !== Boolean(automation.enabled) ||
      Boolean(prevPolicy.emergency) !== Boolean(automation.emergency) ||
      (automation.epoch != null && prevPolicy.epoch !== automation.epoch) ||
      Boolean(prev.inbox?.enabled) !== Boolean(automation.inbox_enabled) ||
      Boolean(prev.discovery?.enabled) !== Boolean(automation.discovery_enabled) ||
      (prev.comment_cooldown_until ?? null) !== (automation.comment_cooldown_until ?? null) ||
      prevSchedId !== nextSchedId;
    if (!changed) return;
    const next: AutomationStatus = {
      ...prev,
      state: automation.state ?? prev.state,
      comment_cooldown_until: automation.comment_cooldown_until ?? null,
      active_schedule:
        automation.active_schedule !== undefined ? automation.active_schedule : prev.active_schedule,
      policy: {
        ...prevPolicy,
        enabled: automation.enabled ?? prevPolicy.enabled,
        emergency: automation.emergency ?? prevPolicy.emergency,
        epoch: automation.epoch ?? prevPolicy.epoch,
      },
      inbox: prev.inbox ? { ...prev.inbox, enabled: automation.inbox_enabled } : prev.inbox,
      discovery: prev.discovery
        ? { ...prev.discovery, enabled: automation.discovery_enabled }
        : prev.discovery,
    };
    this.update({ status: next });
    if (automation.epoch != null && prevPolicy.epoch !== automation.epoch) {
      void this.refreshStatus();
    }
  }

  async refreshStatus({ background = false }: { background?: boolean } = {}): Promise<AutomationStatus | null> {
    const now = Date.now();
    if (background && now - this.statusRequestedAt < 8000) {
      return this.state.status;
    }
    this.statusRequestedAt = now;
    const seq = ++this.statusRequestSeq;
    try {
      const next = await apiGet<"/api/v1/operations/automation", AutomationStatus>(
        "/api/v1/operations/automation"
      );
      if (seq !== this.statusRequestSeq) return this.state.status;
      this.applyAutomationStatus(next);
      return next;
    } catch (err) {
      if (seq === this.statusRequestSeq) {
        this.update({ statusError: errorMessage(err) });
      }
      return null;
    }
  }

  async refreshAccount(): Promise<BilibiliAccountSnapshot | null> {
    const seq = ++this.accountRequestSeq;
    try {
      const account = await apiGet<"/api/v1/operations/account/bilibili", BilibiliAccountSnapshot>(
        "/api/v1/operations/account/bilibili"
      );
      if (seq === this.accountRequestSeq) {
        this.update({ account });
      }
      return account;
    } catch {
      return null;
    }
  }

  async refreshDiscovery(): Promise<DiscoveryStatus | null> {
    const seq = ++this.discoveryRequestSeq;
    try {
      const discovery = await apiGet<"/api/v1/operations/discovery", DiscoveryStatus>(
        "/api/v1/operations/discovery"
      );
      if (seq === this.discoveryRequestSeq) {
        this.update({ discovery });
      }
      return discovery;
    } catch {
      if (seq === this.discoveryRequestSeq) {
        this.update({
          discovery: {
            ...(this.state.discovery || {}),
            state: "unavailable",
          },
        });
      }
      return null;
    }
  }

  async refreshQuotas(): Promise<QuotaSnapshot | null> {
    const seq = ++this.quotasRequestSeq;
    try {
      const quotas = await apiGet<"/api/v1/operations/control/quotas", QuotaSnapshot>(
        "/api/v1/operations/control/quotas"
      );
      if (seq === this.quotasRequestSeq) {
        this.update({ quotas });
      }
      return quotas;
    } catch {
      return null;
    }
  }

  async refreshInbox(): Promise<InboxRecordsResponse | null> {
    const seq = ++this.inboxRequestSeq;
    try {
      const inboxRecords = await apiGet<"/api/v1/operations/inbox", InboxRecordsResponse>(
        "/api/v1/operations/inbox"
      );
      if (seq === this.inboxRequestSeq) {
        this.update({ inboxRecords });
      }
      return inboxRecords;
    } catch {
      return null;
    }
  }

  async refreshClaims(): Promise<OperationClaim[]> {
    const seq = ++this.claimsRequestSeq;
    try {
      const response = await apiGet<"/api/v1/operations/claims", { items?: OperationClaim[] }>(
        "/api/v1/operations/claims"
      );
      const claims = Array.isArray(response.items) ? response.items : [];
      if (seq === this.claimsRequestSeq) {
        this.update({ claims });
      }
      return claims;
    } catch {
      return this.state.claims;
    }
  }

  async refreshDashboard(): Promise<void> {
    await Promise.allSettled([
      this.refreshStatus(),
      this.refreshAccount(),
      this.refreshDiscovery(),
      this.refreshQuotas(),
      this.refreshInbox(),
      this.refreshClaims(),
    ]);
  }

  private async runControlCommand(
    work: () => Promise<AutomationStatus>,
    successMessage: string
  ): Promise<AutomationStatus | null> {
    if (this.state.controlPending) return null;
    const command = ++this.controlRequestSeq;
    ++this.statusRequestSeq;
    this.update({ controlPending: true });
    try {
      const next = await work();
      if (command !== this.controlRequestSeq) return null;
      ++this.statusRequestSeq;
      if (this.applyAutomationStatus(next)) {
        emitNotice(next.state === "draining" ? "当前批次收尾后停止。" : successMessage);
      }
      return next;
    } catch (err) {
      if (command !== this.controlRequestSeq) return null;
      emitNotice(errorMessage(err), true);
      await this.refreshStatus();
      return null;
    } finally {
      if (command === this.controlRequestSeq) {
        this.update({ controlPending: false });
      }
    }
  }

  async toggleMasterAutomation(enabled?: boolean): Promise<AutomationStatus | null> {
    const target = enabled ?? this.state.status?.policy?.enabled !== true;
    return this.runControlCommand(
      () =>
        apiPost<"/api/v1/operations/automation/enable", AutomationStatus, { enabled: boolean }>(
          "/api/v1/operations/automation/enable",
          { body: { enabled: target } }
        ),
      target ? "自主运营已开启。" : "自主运营已停止。"
    );
  }

  async toggleDiscoveryEnabled(enabled: boolean): Promise<AutomationStatus | null> {
    return this.runControlCommand(
      () =>
        apiPost<"/api/v1/operations/discovery/enable", AutomationStatus, { enabled: boolean }>(
          "/api/v1/operations/discovery/enable",
          { body: { enabled } }
        ),
      enabled ? "主动探索已开启。" : "主动探索已停用。"
    );
  }

  async toggleInboxEnabled(enabled: boolean): Promise<AutomationStatus | null> {
    return this.runControlCommand(
      () =>
        apiPost<"/api/v1/operations/inbox/enable", AutomationStatus, { enabled: boolean }>(
          "/api/v1/operations/inbox/enable",
          { body: { enabled } }
        ),
      enabled ? "消息响应（Inbox）已开启。" : "消息响应（Inbox）已停用。"
    );
  }

  async stopAutomation(emergency = false): Promise<AutomationStatus | null> {
    return this.runControlCommand(
      () =>
        apiPost<"/api/v1/operations/automation/stop", AutomationStatus, { emergency: boolean }>(
          "/api/v1/operations/automation/stop",
          { body: { emergency } }
        ),
      emergency ? "已紧急停止所有自动收发。" : "已停止。"
    );
  }

  async resetEmergencyStop(): Promise<AutomationStatus | null> {
    return this.runControlCommand(
      () =>
        apiPost<"/api/v1/operations/automation/reset-stop", AutomationStatus, never>(
          "/api/v1/operations/automation/reset-stop"
        ),
      "已解除紧急停止，可手动开始。"
    );
  }

  async saveAutomationSettings(patch: Record<string, unknown>): Promise<AutomationStatus> {
    const next = await apiPut<"/api/v1/operations/automation/settings", AutomationStatus, Record<string, unknown>>(
      "/api/v1/operations/automation/settings",
      { body: patch }
    );
    ++this.statusRequestSeq;
    this.applyAutomationStatus(next);
    return next;
  }

  async saveDiscoverySettings(patch: Record<string, unknown>): Promise<DiscoveryStatus> {
    const next = await apiPut<"/api/v1/operations/discovery/settings", DiscoveryStatus, Record<string, unknown>>(
      "/api/v1/operations/discovery/settings",
      { body: patch }
    );
    ++this.discoveryRequestSeq;
    this.update({ discovery: next });
    return next;
  }

  async saveInboxSettings(patch: Record<string, unknown>): Promise<InboxPipelineStatus> {
    const next = await apiPut<"/api/v1/operations/inbox/settings", InboxPipelineStatus, Record<string, unknown>>(
      "/api/v1/operations/inbox/settings",
      { body: patch }
    );
    await this.refreshStatus();
    return next;
  }

  async saveQuotaControls(patch: {
    direct_quota_reserve?: number;
    max_per_hour?: number;
    max_per_day?: number;
    recipient_cooldown_seconds?: number;
  }): Promise<QuotaSnapshot> {
    const next = await apiPut<
      "/api/v1/operations/control/quotas",
      QuotaSnapshot,
      typeof patch
    >("/api/v1/operations/control/quotas", { body: patch });
    ++this.quotasRequestSeq;
    this.update({ quotas: next });
    await this.refreshStatus();
    return next;
  }

  async createOperationSchedule(
    input: OperationScheduleCreateInput
  ): Promise<OperationScheduleItem | null> {
    const res = await this.runControlCommand(async () => {
      const response = await apiPost<
        "/api/v1/operations/automation/schedules",
        { schedule: OperationScheduleItem; status: AutomationStatus },
        OperationScheduleCreateInput
      >("/api/v1/operations/automation/schedules", { body: input });
      return response.status;
    }, input.kind === "duration" ? "定时自主运营已开启。" : "定时运营计划已保存。");
    return res?.active_schedule ?? res?.schedules?.[0] ?? null;
  }

  async updateOperationSchedule(
    scheduleId: string,
    patch: OperationScheduleUpdateInput
  ): Promise<AutomationStatus | null> {
    return this.runControlCommand(async () => {
      const response = await apiPatch<
        "/api/v1/operations/automation/schedules/{schedule_id}",
        { schedule: OperationScheduleItem; status: AutomationStatus },
        OperationScheduleUpdateInput
      >("/api/v1/operations/automation/schedules/{schedule_id}", {
        pathParams: { schedule_id: scheduleId },
        body: patch,
      });
      return response.status;
    }, patch.action === "cancel" ? "已取消定时运营计划。" : "定时计划状态已更新。");
  }

  async deleteOperationSchedule(scheduleId: string): Promise<AutomationStatus | null> {
    return this.runControlCommand(async () => {
      const response = await apiDelete<
        "/api/v1/operations/automation/schedules/{schedule_id}",
        { deleted: string; status: AutomationStatus },
        never
      >("/api/v1/operations/automation/schedules/{schedule_id}", {
        pathParams: { schedule_id: scheduleId },
      });
      return response.status;
    }, "已删除定时运营计划。");
  }

  async prepareOperationScheduleVideo(scheduleId: string, runId: string): Promise<OperationScheduleCloseoutRun> {
    const { run } = await apiPost<
      "/api/v1/operations/automation/schedules/{schedule_id}/closeouts/{run_id}/prepare-video",
      { run: OperationScheduleCloseoutRun },
      never
    >("/api/v1/operations/automation/schedules/{schedule_id}/closeouts/{run_id}/prepare-video", {
      pathParams: { schedule_id: scheduleId, run_id: runId },
    });
    ++this.statusRequestSeq;
    const current = this.state.status;
    if (current) {
      function updateSchedule(item: OperationScheduleItem): OperationScheduleItem {
        if (item.id !== scheduleId) return item;
        const runs = item.closeout_runs ?? [];
        return {
          ...item,
          closeout_runs: runs.some((previous) => previous.id === run.id)
            ? runs.map((previous) => previous.id === run.id ? run : previous)
            : [run, ...runs],
        };
      }
      this.update({
        status: {
          ...current,
          schedules: current.schedules?.map(updateSchedule),
          active_schedule: current.active_schedule ? updateSchedule(current.active_schedule) : current.active_schedule,
        },
      });
    }
    return run;
  }

  reset(): void {
    this.state = { ...INITIAL_STATE };
    this.statusRequestSeq = 0;
    this.statusRequestedAt = 0;
    this.controlRequestSeq = 0;
    this.discoveryRequestSeq = 0;
    this.inboxRequestSeq = 0;
    this.quotasRequestSeq = 0;
    this.claimsRequestSeq = 0;
    this.accountRequestSeq = 0;
    for (const listener of this.listeners) {
      listener();
    }
  }
}

export const operationsStore = new OperationsStoreImpl();

const stableControlActions = {
  refreshStatus: (opts?: { background?: boolean }) => operationsStore.refreshStatus(opts),
  refreshAccount: () => operationsStore.refreshAccount(),
  refreshDashboard: () => operationsStore.refreshDashboard(),
  refreshDiscovery: () => operationsStore.refreshDiscovery(),
  refreshQuotas: () => operationsStore.refreshQuotas(),
  refreshInbox: () => operationsStore.refreshInbox(),
  refreshClaims: () => operationsStore.refreshClaims(),
  toggleMasterAutomation: (enabled?: boolean) => operationsStore.toggleMasterAutomation(enabled),
  toggleDiscoveryEnabled: (enabled: boolean) => operationsStore.toggleDiscoveryEnabled(enabled),
  toggleInboxEnabled: (enabled: boolean) => operationsStore.toggleInboxEnabled(enabled),
  stopAutomation: (emergency?: boolean) => operationsStore.stopAutomation(emergency),
  resetEmergencyStop: () => operationsStore.resetEmergencyStop(),
  saveAutomationSettings: (patch: Record<string, unknown>) =>
    operationsStore.saveAutomationSettings(patch),
  saveDiscoverySettings: (patch: Record<string, unknown>) =>
    operationsStore.saveDiscoverySettings(patch),
  saveInboxSettings: (patch: Record<string, unknown>) => operationsStore.saveInboxSettings(patch),
  saveQuotaControls: (patch: {
    direct_quota_reserve?: number;
    max_per_hour?: number;
    max_per_day?: number;
    recipient_cooldown_seconds?: number;
  }) => operationsStore.saveQuotaControls(patch),
  createOperationSchedule: (input: OperationScheduleCreateInput) =>
    operationsStore.createOperationSchedule(input),
  updateOperationSchedule: (scheduleId: string, patch: OperationScheduleUpdateInput) =>
    operationsStore.updateOperationSchedule(scheduleId, patch),
  deleteOperationSchedule: (scheduleId: string) => operationsStore.deleteOperationSchedule(scheduleId),
  prepareOperationScheduleVideo: (scheduleId: string, runId: string) =>
    operationsStore.prepareOperationScheduleVideo(scheduleId, runId),
};

export function useOperationsControl(): OperationsStoreState & typeof stableControlActions {
  const snapshot = useSyncExternalStore(
    operationsStore.subscribe,
    operationsStore.getSnapshot,
    operationsStore.getSnapshot
  );

  return {
    ...snapshot,
    ...stableControlActions,
  };
}
