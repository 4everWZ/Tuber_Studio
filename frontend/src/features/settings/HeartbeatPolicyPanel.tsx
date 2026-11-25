import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiRequest } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import { StatusPill } from "@/shared/ui/StatusPill";

export interface HeartbeatPolicyData {
  enabled: boolean;
  tick_interval_seconds: number;
  pulse_cooldown_seconds: number;
  event_cooldown_seconds: number;
  max_pulses_per_day: number;
  max_unreplied_pulses: number;
  hot_topic_dedup_seconds: number;
  quiet_hours_start: string;
  quiet_hours_end: string;
  curiosity_research_enabled: boolean;
  curiosity_max_queries_per_tick: number;
  surface_to_chat: boolean;
}

export interface HeartbeatSuppressionEntry {
  at: number;
  reason: string;
  dedup_key: string;
  kind: string;
  detail?: string;
}

export interface HeartbeatPendingSummary {
  dynamics_pending_review: number;
  dm_pending: number;
  inbox_pending_events: number;
  uncertain_writes: number;
}

export interface HeartbeatStatusData {
  enabled: boolean;
  running: boolean;
  closed?: boolean;
  in_quiet_hours: boolean;
  unreplied_pulses: number;
  pending_summary: HeartbeatPendingSummary;
  recent_suppressions: HeartbeatSuppressionEntry[];
  last_tick_at?: number | null;
  recent_pulse_count?: number;
}

export interface HeartbeatEndpointResponse {
  policy?: Partial<HeartbeatPolicyData>;
  status?: Partial<HeartbeatStatusData>;
}

const DEFAULT_POLICY: HeartbeatPolicyData = {
  enabled: true,
  tick_interval_seconds: 300,
  pulse_cooldown_seconds: 10800,
  event_cooldown_seconds: 900,
  max_pulses_per_day: 3,
  max_unreplied_pulses: 1,
  hot_topic_dedup_seconds: 604800,
  quiet_hours_start: "23:00",
  quiet_hours_end: "08:30",
  curiosity_research_enabled: true,
  curiosity_max_queries_per_tick: 2,
  surface_to_chat: true,
};

const DEFAULT_PENDING: HeartbeatPendingSummary = {
  dynamics_pending_review: 0,
  dm_pending: 0,
  inbox_pending_events: 0,
  uncertain_writes: 0,
};

const SUPPRESSION_REASON_LABELS: Record<string, string> = {
  persisted_dedup: "7天持久去重",
  unread_cap: "未回脉冲上限",
  quiet_hours: "勿扰时段拦截",
  pulse_cooldown: "脉冲冷却中",
  daily_pulse_limit: "今日脉冲上限",
  no_state_delta: "存量无新增变化",
  deduplicated: "重复指纹",
  cooldown: "事件冷却",
};

function formatSuppressionTime(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return "";
  try {
    return new Date(epochSeconds * 1000).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return "";
  }
}

export function HeartbeatPolicyPanel() {
  const [loaded, setLoaded] = useState(false);
  const [form, setForm] = useState<HeartbeatPolicyData>(DEFAULT_POLICY);
  const [status, setStatus] = useState<HeartbeatStatusData | null>(null);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState(false);
  const [resultText, setResultText] = useState("");
  const [isError, setIsError] = useState(false);

  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useUnsavedGuard("heartbeat-policy-panel", dirty);

  const applyResponse = useCallback((data: HeartbeatEndpointResponse, overwriteForm: boolean) => {
    if (data.policy && (overwriteForm || !dirtyRef.current)) {
      setForm({ ...DEFAULT_POLICY, ...data.policy });
    }
    if (data.status) {
      setStatus({
        enabled: Boolean(data.status.enabled ?? data.policy?.enabled ?? true),
        running: Boolean(data.status.running),
        closed: Boolean(data.status.closed),
        in_quiet_hours: Boolean(data.status.in_quiet_hours),
        unreplied_pulses: Number(data.status.unreplied_pulses ?? 0),
        pending_summary: { ...DEFAULT_PENDING, ...(data.status.pending_summary ?? {}) },
        recent_suppressions: Array.isArray(data.status.recent_suppressions)
          ? data.status.recent_suppressions
          : [],
        last_tick_at: data.status.last_tick_at ?? null,
        recent_pulse_count: data.status.recent_pulse_count ?? 0,
      });
    }
    setLoaded(true);
  }, []);

  const refresh = useCallback(async () => {
    setPending(true);
    try {
      const data = await apiRequest<HeartbeatEndpointResponse>("/api/v1/system/heartbeat", {
        method: "GET",
      });
      applyResponse(data, true);
      setResultText("");
      setIsError(false);
    } catch (error) {
      setLoaded(true);
      setResultText(errorMessage(error));
      setIsError(true);
    } finally {
      setPending(false);
    }
  }, [applyResponse]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function updateField<K extends keyof HeartbeatPolicyData>(key: K, value: HeartbeatPolicyData[K]) {
    dirtyRef.current = true;
    setDirty(true);
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setResultText("");
    setIsError(false);
    try {
      const payload = {
        enabled: form.enabled,
        pulse_cooldown_seconds: Math.max(0, Number(form.pulse_cooldown_seconds) || 0),
        max_pulses_per_day: Math.max(0, Math.trunc(Number(form.max_pulses_per_day) || 0)),
        max_unreplied_pulses: Math.max(0, Math.trunc(Number(form.max_unreplied_pulses) || 0)),
        quiet_hours_start: form.quiet_hours_start.trim(),
        quiet_hours_end: form.quiet_hours_end.trim(),
        curiosity_research_enabled: form.curiosity_research_enabled,
      };
      const updated = await apiRequest<HeartbeatEndpointResponse, typeof payload>(
        "/api/v1/system/heartbeat",
        {
          method: "PUT",
          body: payload,
        }
      );
      dirtyRef.current = false;
      setDirty(false);
      applyResponse(updated, true);
      setResultText("心跳策略已保存");
    } catch (error) {
      setResultText(errorMessage(error));
      setIsError(true);
    } finally {
      setPending(false);
    }
  }

  const pendingSummary = status?.pending_summary ?? DEFAULT_PENDING;
  const suppressions = status?.recent_suppressions ?? [];

  return (
    <fieldset className="win-group-box" id="heartbeat-policy-panel" disabled={!loaded || pending}>
      <legend>主动脉冲与勿扰策略</legend>

      <div className="actions" style={{ flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
        <StatusPill
          id="heartbeat-running-pill"
          label={status?.running ? "运行中" : "已暂停"}
          tone={status?.running ? "good" : "neutral"}
        />
        <StatusPill
          id="heartbeat-quiet-pill"
          label={status?.in_quiet_hours ? "勿扰时段中" : "正常时段"}
          tone={status?.in_quiet_hours ? "amber" : "neutral"}
        />
        <StatusPill
          id="heartbeat-unreplied-pill"
          label={`未回脉冲 ${status?.unreplied_pulses ?? 0}`}
          tone={(status?.unreplied_pulses ?? 0) > 0 ? "blue" : "neutral"}
        />
        <StatusPill
          id="heartbeat-badge-dynamics"
          label={`待审动态 ${pendingSummary.dynamics_pending_review}`}
          tone={pendingSummary.dynamics_pending_review > 0 ? "blue" : "neutral"}
        />
        <StatusPill
          id="heartbeat-badge-dm"
          label={`待处理私信 ${pendingSummary.dm_pending}`}
          tone={pendingSummary.dm_pending > 0 ? "blue" : "neutral"}
        />
        <StatusPill
          id="heartbeat-badge-inbox"
          label={`待看互动 ${pendingSummary.inbox_pending_events}`}
          tone={pendingSummary.inbox_pending_events > 0 ? "blue" : "neutral"}
        />
        <StatusPill
          id="heartbeat-badge-uncertain"
          label={`待核对发送 ${pendingSummary.uncertain_writes}`}
          tone={pendingSummary.uncertain_writes > 0 ? "bad" : "neutral"}
        />
      </div>

      <form id="heartbeat-policy-form" onSubmit={(event) => void handleSubmit(event)}>
        <div className="checks">
          <label>
            <input
              type="checkbox"
              name="enabled"
              checked={form.enabled}
              onChange={(event) => updateField("enabled", event.target.checked)}
            />
            {" 启用常驻心跳脉冲"}
          </label>
          <label>
            <input
              type="checkbox"
              name="curiosity_research_enabled"
              checked={form.curiosity_research_enabled}
              onChange={(event) => updateField("curiosity_research_enabled", event.target.checked)}
            />
            {" 启用热门话题公开联网好奇心查证"}
          </label>
        </div>

        <div className="field-grid" style={{ marginTop: 10 }}>
          <label>
            脉冲冷却（秒，0 为不限）
            <input
              type="number"
              name="pulse_cooldown_seconds"
              min={0}
              max={86400}
              step={60}
              value={form.pulse_cooldown_seconds}
              onChange={(event) => updateField("pulse_cooldown_seconds", Number(event.target.value))}
            />
          </label>
          <label>
            每日非紧急脉冲上限（0 为不限）
            <input
              type="number"
              name="max_pulses_per_day"
              min={0}
              max={200}
              step={1}
              value={form.max_pulses_per_day}
              onChange={(event) => updateField("max_pulses_per_day", Number(event.target.value))}
            />
          </label>
          <label>
            连续未回复上限（0 为不限）
            <input
              type="number"
              name="max_unreplied_pulses"
              min={0}
              max={50}
              step={1}
              value={form.max_unreplied_pulses}
              onChange={(event) => updateField("max_unreplied_pulses", Number(event.target.value))}
            />
          </label>
          <label>
            勿扰开始时间（HH:MM，留空关闭）
            <input
              type="text"
              name="quiet_hours_start"
              placeholder="23:00"
              value={form.quiet_hours_start}
              onChange={(event) => updateField("quiet_hours_start", event.target.value)}
            />
          </label>
          <label>
            勿扰结束时间（HH:MM，留空关闭）
            <input
              type="text"
              name="quiet_hours_end"
              placeholder="08:30"
              value={form.quiet_hours_end}
              onChange={(event) => updateField("quiet_hours_end", event.target.value)}
            />
          </label>
        </div>

        <p className="hint" style={{ marginTop: 8 }}>
          默认冷却 3 小时（10800 秒）、每天最多 3 条、连续未回 1 条暂停打扰；紧急异常（停机、凭证失效、412 冷却、冻结或待核对发送）会穿透冷却与勿扰提醒你。
        </p>

        <div className="actions" style={{ marginTop: 8 }}>
          <button
            type="submit"
            id="heartbeat-policy-save"
            className="win-btn"
            disabled={!loaded || pending || !dirty}
          >
            保存心跳策略
          </button>
          <button
            type="button"
            id="heartbeat-policy-refresh"
            className="win-btn"
            disabled={pending}
            onClick={() => void refresh()}
          >
            刷新状态
          </button>
        </div>
      </form>

      <p
        id="heartbeat-policy-result"
        className={isError ? "hint error-text" : "hint"}
        role="status"
      >
        {resultText}
      </p>

      <div className="win-subgroup-box" style={{ marginTop: 10 }}>
        <div className="win-subgroup-title">最近防刷屏抑制记录</div>
        {suppressions.length === 0 ? (
          <p className="hint" id="heartbeat-suppressions-empty">
            暂无抑制记录。
          </p>
        ) : (
          <ul id="heartbeat-suppressions-list" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {suppressions.slice(0, 8).map((entry, idx) => (
              <li key={`${entry.dedup_key}-${entry.at}-${idx}`} className="hint">
                {formatSuppressionTime(entry.at) ? `[${formatSuppressionTime(entry.at)}] ` : ""}
                <strong>{SUPPRESSION_REASON_LABELS[entry.reason] ?? entry.reason}</strong>
                {" · "}
                {entry.kind}
                {entry.dedup_key ? ` (${entry.dedup_key})` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>
    </fieldset>
  );
}
