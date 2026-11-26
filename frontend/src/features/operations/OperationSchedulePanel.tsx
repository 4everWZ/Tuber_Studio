import { useMemo, useState, type FormEvent } from "react";
import { StatusPill, type StatusPillTone } from "@/shared";
import styles from "./operations.module.css";
import type {
  AutomationStatus,
  OperationScheduleCreateInput,
  OperationScheduleItem,
  OperationScheduleUpdateInput,
} from "./types";

const WEEKDAYS: ReadonlyArray<readonly [number, string]> = [
  [0, "一"],
  [1, "二"],
  [2, "三"],
  [3, "四"],
  [4, "五"],
  [5, "六"],
  [6, "日"],
];

const SCHEDULE_STATUS_META: Record<string, { label: string; tone: StatusPillTone }> = {
  active: { label: "运行中", tone: "good" },
  scheduled: { label: "待执行", tone: "blue" },
  paused: { label: "已暂停", tone: "amber" },
  completed: { label: "已完成", tone: "neutral" },
  cancelled: { label: "已取消", tone: "neutral" },
};

export function formatRemainingMinutes(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || seconds <= 0) return "不足 1 分钟";
  const mins = Math.floor(seconds / 60);
  if (mins >= 60) {
    const hrs = Math.floor(mins / 60);
    const rem = mins % 60;
    return rem > 0 ? `约 ${hrs} 小时 ${rem} 分钟` : `约 ${hrs} 小时`;
  }
  return mins >= 1 ? `约 ${mins} 分钟` : `约 ${Math.ceil(seconds)} 秒`;
}

function todayLocalDate(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export interface OperationSchedulePanelProps {
  status: AutomationStatus | null;
  controlPending?: boolean;
  onCreateSchedule: (input: OperationScheduleCreateInput) => Promise<unknown>;
  onUpdateSchedule: (scheduleId: string, patch: OperationScheduleUpdateInput) => Promise<unknown>;
  onDeleteSchedule: (scheduleId: string) => Promise<unknown>;
}

export function OperationSchedulePanel({
  status,
  controlPending = false,
  onCreateSchedule,
  onUpdateSchedule,
  onDeleteSchedule,
}: OperationSchedulePanelProps) {
  const [customMinutes, setCustomMinutes] = useState("60");
  const [mode, setMode] = useState<"once_window" | "daily_window">("once_window");
  const [onceDate, setOnceDate] = useState(todayLocalDate);
  const [startTime, setStartTime] = useState("20:00");
  const [endTime, setEndTime] = useState("22:00");
  const [weekdays, setWeekdays] = useState<number[]>([]);
  const [label, setLabel] = useState("");
  const [scopeDiscovery, setScopeDiscovery] = useState(true);
  const [scopeInbox, setScopeInbox] = useState(true);
  const [showHistory, setShowHistory] = useState(false);

  const emergency = Boolean(status?.policy?.emergency);
  const timezone = status?.settings?.business_timezone || "Asia/Taipei";
  const activeSchedule = status?.active_schedule ?? null;
  const schedules = status?.schedules ?? [];

  const { pendingOrActive, finished } = useMemo(() => {
    const open: OperationScheduleItem[] = [];
    const done: OperationScheduleItem[] = [];
    for (const item of schedules) {
      if (item.status === "completed" || item.status === "cancelled") {
        done.push(item);
      } else {
        open.push(item);
      }
    }
    return { pendingOrActive: open, finished: done };
  }, [schedules]);

  function toggleWeekday(day: number) {
    setWeekdays((prev) =>
      prev.includes(day) ? prev.filter((d) => d !== day) : [...prev, day].sort((a, b) => a - b)
    );
  }

  async function handleQuickDuration(minutes: number) {
    if (controlPending || emergency || !Number.isFinite(minutes) || minutes < 1) return;
    await onCreateSchedule({
      kind: "duration",
      duration_minutes: Math.min(1440, Math.round(minutes)),
      label: `限时自主运营 ${Math.min(1440, Math.round(minutes))} 分钟`,
      timezone,
      scope_discovery: scopeDiscovery,
      scope_inbox: scopeInbox,
    });
  }

  async function handleCustomDuration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = Number(customMinutes);
    if (!Number.isFinite(parsed) || parsed < 1) return;
    await handleQuickDuration(parsed);
  }

  async function handleCreateWindow(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (controlPending || (!scopeDiscovery && !scopeInbox)) return;
    if (mode === "once_window") {
      if (!onceDate || !startTime || !endTime) return;
      const startIso = `${onceDate} ${startTime}`;
      let endDateStr = onceDate;
      if (endTime < startTime) {
        const dt = new Date(`${onceDate}T00:00:00`);
        dt.setDate(dt.getDate() + 1);
        endDateStr = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(
          dt.getDate()
        ).padStart(2, "0")}`;
      }
      await onCreateSchedule({
        kind: "once_window",
        label: label.trim() || undefined,
        start_local: startIso,
        end_local: `${endDateStr} ${endTime}`,
        timezone,
        scope_discovery: scopeDiscovery,
        scope_inbox: scopeInbox,
      });
      setLabel("");
      return;
    }

    await onCreateSchedule({
      kind: "daily_window",
      label: label.trim() || undefined,
      start_time: startTime,
      end_time: endTime,
      weekdays,
      timezone,
      scope_discovery: scopeDiscovery,
      scope_inbox: scopeInbox,
    });
    setLabel("");
  }

  const visibleItems = showHistory ? [...pendingOrActive, ...finished] : pendingOrActive;

  return (
    <section
      id="operation-schedules-panel"
      className={styles.schedulePanel}
      aria-label="定时与时间段运营控制"
    >
      <div className={styles.scheduleHeader}>
        <div>
          <h2 className="win-group-title" style={{ margin: 0 }}>
            定时与时间段运营
          </h2>
          <p className="hint" style={{ margin: "2px 0 0" }}>
            支持直接对话或在此预约固定时长（如刷 1 小时视频）与每日／单次运营时间段（时区：{timezone}）。
          </p>
        </div>
        {activeSchedule && (
          <StatusPill
            label={`定时运行中 · 剩余 ${formatRemainingMinutes(activeSchedule.remaining_seconds)}`}
            tone="good"
          />
        )}
      </div>

      {activeSchedule && (
        <div id="active-schedule-banner" className={styles.scheduleBanner} role="status">
          <div>
            <strong>{activeSchedule.label}</strong>
            <span className="hint" style={{ marginLeft: 8 }}>
              预计 {activeSchedule.end_local_display || "到期"} 自动停止
            </span>
          </div>
          <button
            id="cancel-active-schedule"
            type="button"
            disabled={controlPending}
            onClick={() => void onUpdateSchedule(activeSchedule.id, { action: "cancel" })}
          >
            提前取消定时
          </button>
        </div>
      )}

      <div className={styles.quickDurationRow}>
        <span className="hint">立即定时运行：</span>
        <button
          id="schedule-quick-30m"
          type="button"
          className="win-btn"
          disabled={!status || emergency || controlPending}
          onClick={() => void handleQuickDuration(30)}
        >
          刷 30 分钟
        </button>
        <button
          id="schedule-quick-1h"
          type="button"
          className="primary"
          disabled={!status || emergency || controlPending}
          onClick={() => void handleQuickDuration(60)}
        >
          刷 1 小时
        </button>
        <button
          id="schedule-quick-2h"
          type="button"
          className="win-btn"
          disabled={!status || emergency || controlPending}
          onClick={() => void handleQuickDuration(120)}
        >
          刷 2 小时
        </button>
        <form className={styles.customDurationForm} onSubmit={handleCustomDuration}>
          <label className={styles.inlineDurationLabel}>
            <span>自定义分钟</span>
            <input
              id="schedule-custom-minutes"
              type="number"
              min={1}
              max={1440}
              value={customMinutes}
              onChange={(e) => setCustomMinutes(e.target.value)}
            />
          </label>
          <button
            id="schedule-custom-submit"
            type="submit"
            className="win-btn"
            disabled={!status || emergency || controlPending}
          >
            定时启动
          </button>
        </form>
      </div>

      <details className={styles.scheduleCreatorDetails} open={pendingOrActive.length === 0}>
        <summary>预约时间段／每日定时排班</summary>
        <form id="schedule-window-form" className={styles.scheduleCreatorForm} onSubmit={handleCreateWindow}>
          <div className={styles.scheduleModeTabs} role="tablist" aria-label="排班类型">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "once_window"}
              className={mode === "once_window" ? "primary" : "win-btn"}
              onClick={() => setMode("once_window")}
            >
              单次时间段
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "daily_window"}
              className={mode === "daily_window" ? "primary" : "win-btn"}
              onClick={() => setMode("daily_window")}
            >
              每日定时
            </button>
          </div>

          <div className={styles.scheduleFieldRow}>
            {mode === "once_window" && (
              <label>
                日期
                <input
                  id="schedule-once-date"
                  type="date"
                  required
                  value={onceDate}
                  onChange={(e) => setOnceDate(e.target.value)}
                />
              </label>
            )}
            <label>
              开始时间
              <input
                id="schedule-start-time"
                type="time"
                required
                value={startTime}
                onChange={(e) => setStartTime(e.target.value)}
              />
            </label>
            <label>
              结束时间
              <input
                id="schedule-end-time"
                type="time"
                required
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
              />
            </label>
            <label>
              计划备注（可选）
              <input
                id="schedule-label-input"
                type="text"
                maxLength={120}
                placeholder={mode === "daily_window" ? "例如：晚间刷视频时段" : "例如：今晚专题刷视频"}
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
            </label>
          </div>

          {mode === "daily_window" && (
            <div className={styles.weekdayRow} aria-label="重复星期">
              <span className="hint">星期（不选则每天执行）：</span>
              {WEEKDAYS.map(([day, title]) => {
                const selected = weekdays.includes(day);
                return (
                  <button
                    key={day}
                    type="button"
                    aria-pressed={selected}
                    className={selected ? "primary" : "win-btn"}
                    onClick={() => toggleWeekday(day)}
                  >
                    周{title}
                  </button>
                );
              })}
            </div>
          )}

          <div className={styles.scheduleScopeRow}>
            <label className={styles.scopeCheckbox}>
              <input
                id="schedule-scope-discovery"
                type="checkbox"
                checked={scopeDiscovery}
                onChange={(e) => setScopeDiscovery(e.target.checked)}
              />
              <span>包含主动探索（刷视频）</span>
            </label>
            <label className={styles.scopeCheckbox}>
              <input
                id="schedule-scope-inbox"
                type="checkbox"
                checked={scopeInbox}
                onChange={(e) => setScopeInbox(e.target.checked)}
              />
              <span>包含消息响应（通知与私信）</span>
            </label>
            <button
              id="schedule-window-submit"
              type="submit"
              className="primary"
              disabled={!status || controlPending || (!scopeDiscovery && !scopeInbox)}
            >
              {mode === "daily_window" ? "添加每日计划" : "添加时间段"}
            </button>
          </div>
        </form>
      </details>

      {visibleItems.length > 0 ? (
        <ul id="operation-schedule-list" className={styles.scheduleList}>
          {visibleItems.map((item) => {
            const meta = SCHEDULE_STATUS_META[item.status] || {
              label: item.status,
              tone: "neutral" as StatusPillTone,
            };
            const timeSummary =
              item.kind === "daily_window"
                ? `${item.weekday_labels.length ? item.weekday_labels.join("、") : "每天"} ${item.start_time}–${item.end_time}${
                    item.next_start_local_display ? ` · 下次 ${item.next_start_local_display}` : ""
                  }`
                : `${item.start_local_display || "—"} ~ ${item.end_local_display || "—"}`;
            const scopeText = [
              item.scope_discovery ? "探索刷视频" : "",
              item.scope_inbox ? "消息响应" : "",
            ]
              .filter(Boolean)
              .join(" + ");

            return (
              <li key={item.id} className={styles.scheduleItem} data-schedule-id={item.id}>
                <div className={styles.scheduleItemMain}>
                  <div className={styles.scheduleItemTitle}>
                    <StatusPill label={meta.label} tone={meta.tone} />
                    <strong>{item.label}</strong>
                    {scopeText && <span className="hint">{scopeText}</span>}
                  </div>
                  <p className="hint" style={{ margin: "4px 0 0" }}>
                    {timeSummary}
                    {item.status === "active" && item.remaining_seconds != null
                      ? ` · 剩余 ${formatRemainingMinutes(item.remaining_seconds)}`
                      : ""}
                    {item.last_error ? ` · ${item.last_error}` : ""}
                  </p>
                </div>
                <div className={styles.scheduleItemActions}>
                  {item.kind === "daily_window" && (item.status === "scheduled" || item.status === "paused") && (
                    <button
                      type="button"
                      className="win-btn"
                      disabled={controlPending}
                      onClick={() =>
                        void onUpdateSchedule(item.id, { enabled: item.status === "paused" })
                      }
                    >
                      {item.status === "paused" ? "启用" : "暂停"}
                    </button>
                  )}
                  {(item.status === "active" || item.status === "scheduled") && (
                    <button
                      type="button"
                      className="win-btn"
                      disabled={controlPending}
                      onClick={() => void onUpdateSchedule(item.id, { action: "cancel" })}
                    >
                      取消
                    </button>
                  )}
                  {item.status !== "active" && (
                    <button
                      type="button"
                      className="win-btn"
                      disabled={controlPending}
                      onClick={() => void onDeleteSchedule(item.id)}
                    >
                      删除
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p id="operation-schedule-empty" className="hint" style={{ margin: "8px 0 0" }}>
          暂无进行中或待执行的定时运营计划。
        </p>
      )}

      {finished.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <button
            type="button"
            className="win-link-btn"
            onClick={() => setShowHistory((prev) => !prev)}
          >
            {showHistory ? "收起历史定时记录" : `查看已结束定时记录（${finished.length}）`}
          </button>
        </div>
      )}
    </section>
  );
}
