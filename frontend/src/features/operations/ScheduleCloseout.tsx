import { useMemo, useState } from "react";
import { StatusPill, type StatusPillTone } from "@/shared";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import type {
  OperationScheduleCloseout,
  OperationScheduleCloseoutRun,
  OperationScheduleItem,
} from "@/shared/operations/types";
import styles from "./operations.module.css";

const MODE_LABELS = {
  dynamic: { off: "关闭", review: "生成待审动态", automatic: "自动发布动态" },
  video: {
    off: "关闭", manual: "手动准备", automatic_review: "自动制片 · 待审核",
    automatic_publish: "自动制片 · 自动投稿",
  },
} satisfies {
  dynamic: Record<OperationScheduleCloseout["dynamic_mode"], string>;
  video: Record<OperationScheduleCloseout["video_mode"], string>;
};

const DYNAMIC_STATES: Record<string, [label: string, tone: StatusPillTone]> = {
  off: ["关闭", "neutral"], pending: ["等候生成", "blue"], review: ["待审核", "amber"],
  published: ["已发布", "good"], failed: ["失败", "bad"], uncertain: ["发布待核对", "amber"],
  rejected: ["已拒绝", "neutral"], cancelled: ["已取消", "neutral"],
};

const VIDEO_STATES: Record<string, [label: string, tone: StatusPillTone]> = {
  off: ["关闭", "neutral"], scheduled: ["等候制片", "blue"], manual: ["等待手动准备", "blue"],
  preparing: ["正在准备", "blue"], ready: ["已出片", "good"], submitted: ["已提交 · 等待平台", "blue"],
  published: ["已公开", "good"], window_missed: ["时段已结束", "amber"], cancelled: ["已取消", "neutral"],
  render_failed: ["制片失败", "bad"], publish_failed: ["投稿失败", "bad"],
  publish_uncertain: ["投稿待核对", "amber"],
};

export const CLOSEOUT_OFF: OperationScheduleCloseout = { dynamic_mode: "off", video_mode: "off" };

export function closeoutHasWork(closeout: OperationScheduleCloseout | null | undefined) {
  return Boolean(closeout && (closeout.dynamic_mode !== "off" || closeout.video_mode !== "off"));
}

export function closeoutModeSummary(closeout: OperationScheduleCloseout) {
  return `动态 ${MODE_LABELS.dynamic[closeout.dynamic_mode]} · 视频 ${MODE_LABELS.video[closeout.video_mode]}`;
}

export function closeoutTimeError(closeout: OperationScheduleCloseout) {
  if (closeout.video_mode === "off") return "";
  if (!closeout.video_start || !closeout.video_end) return "请填写制片开始和结束时间。";
  return closeout.video_start === closeout.video_end ? "制片开始和结束时间必须不同。" : "";
}

export function closeoutRequest(closeout: OperationScheduleCloseout, timezone: string) {
  return {
    dynamic_mode: closeout.dynamic_mode,
    video_mode: closeout.video_mode,
    video_start: closeout.video_mode === "off" ? null : closeout.video_start,
    video_end: closeout.video_mode === "off" ? null : closeout.video_end,
    timezone: closeout.timezone || timezone,
  };
}

export function needsCloseoutFollowup(run: OperationScheduleCloseoutRun) {
  return isCloseoutRunning(run) || ["review", "failed", "uncertain"].includes(run.dynamic_status) ||
    canPrepareVideo(run) || ["ready", "publish_failed"].includes(run.video_status);
}

export function isCloseoutRunning(run: OperationScheduleCloseoutRun) {
  return run.dynamic_status === "pending" || ["scheduled", "preparing", "submitted", "publish_uncertain"].includes(run.video_status);
}

function canPrepareVideo(run: OperationScheduleCloseoutRun) {
  return run.video_status === "manual" || run.video_status === "render_failed" ||
    (run.video_status === "window_missed" && !run.video_job_id);
}

function closeoutTimeFormatter(timeZone: string) {
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone, month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format;
}

export function CloseoutFields({ value, timezone, onChange }: {
  value: OperationScheduleCloseout;
  timezone: string;
  onChange: (value: OperationScheduleCloseout) => void;
}) {
  const timeError = closeoutTimeError(value);
  return (
    <>
      <div className={styles.scheduleFieldRow}>
        <label>
          收尾动态
          <select value={value.dynamic_mode} onChange={(event) => onChange({
            ...value, dynamic_mode: event.target.value as OperationScheduleCloseout["dynamic_mode"],
          })}>
            {Object.entries(MODE_LABELS.dynamic).map(([mode, label]) => <option key={mode} value={mode}>{label}</option>)}
          </select>
        </label>
        <label>
          收尾视频
          <select value={value.video_mode} onChange={(event) => onChange({
            ...value, video_mode: event.target.value as OperationScheduleCloseout["video_mode"],
          })}>
            {Object.entries(MODE_LABELS.video).map(([mode, label]) => <option key={mode} value={mode}>{label}</option>)}
          </select>
        </label>
        {value.video_mode !== "off" && (
          <>
            <label>
              制片开始
              <input type="time" required value={value.video_start || ""}
                onChange={(event) => onChange({ ...value, video_start: event.target.value })} />
            </label>
            <label>
              制片结束
              <input type="time" required value={value.video_end || ""}
                onChange={(event) => onChange({ ...value, video_end: event.target.value })} />
            </label>
          </>
        )}
      </div>
      {value.video_mode !== "off" && (
        <p className="hint" style={{ margin: 0 }}>制片时段独立于运营时间（{value.timezone || timezone}）。{timeError}</p>
      )}
      {(value.dynamic_mode === "automatic" || value.video_mode === "automatic_publish") && (
        <p className="hint" style={{ margin: 0 }}>自动发布仍须满足对应渠道的发布权限。</p>
      )}
    </>
  );
}

export function ScheduleCloseoutRuns({ schedule, disabled, onPrepareVideo }: {
  schedule: OperationScheduleItem;
  disabled: boolean;
  onPrepareVideo: (scheduleId: string, runId: string) => Promise<unknown>;
}) {
  const [pendingRunId, setPendingRunId] = useState("");
  const [error, setError] = useState<{ runId: string; message: string } | null>(null);
  const closeout = schedule.closeout;
  const timezone = closeout?.timezone || schedule.timezone;
  const [periodTime, videoTime] = useMemo(() => {
    const format = closeoutTimeFormatter(schedule.timezone);
    return [format, timezone === schedule.timezone ? format : closeoutTimeFormatter(timezone)];
  }, [schedule.timezone, timezone]);
  const runs = schedule.closeout_runs ?? [];

  async function prepareVideo(runId: string) {
    if (disabled || pendingRunId) return;
    setPendingRunId(runId);
    setError(null);
    try {
      await onPrepareVideo(schedule.id, runId);
    } catch (cause) {
      setError({ runId, message: errorMessage(cause) });
    } finally {
      setPendingRunId("");
    }
  }

  return (
    <>
      {closeoutHasWork(closeout) && closeout && (
        <p className="hint" style={{ margin: "4px 0 0" }}>
          收尾 · {closeoutModeSummary(closeout)}
          {closeout.video_start && closeout.video_end ? ` ${closeout.video_start}–${closeout.video_end}（${timezone}）` : ""}
        </p>
      )}
      {runs.length > 0 && (
        <details className={styles.scheduleCreatorDetails} open={runs.some(needsCloseoutFollowup)}>
          <summary>收尾记录（{runs.length}）</summary>
          <ul className={styles.scheduleList}>
            {runs.map((run) => {
              const [dynamicLabel, dynamicTone] = DYNAMIC_STATES[run.dynamic_status] || [run.dynamic_status, "neutral"];
              const [videoLabel, videoTone] = VIDEO_STATES[run.video_status] || [run.video_status, "neutral"];
              return (
                <li key={run.id}>
                  <p className="hint" style={{ margin: "4px 0" }}>
                    运营{run.period_start_at ? ` ${periodTime(run.period_start_at * 1000)}–` : "结束 "}{periodTime(run.period_end_at * 1000)}
                    {run.video_start_at && run.video_end_at
                      ? ` · 制片 ${videoTime(run.video_start_at * 1000)}–${videoTime(run.video_end_at * 1000)}` : ""}
                  </p>
                  <div className={styles.scheduleItemActions}>
                    <StatusPill label={`动态 · ${dynamicLabel}`} tone={dynamicTone} />
                    <StatusPill label={`视频 · ${videoLabel}`} tone={videoTone} />
                    {run.dynamic_id != null && <a href="#dynamics" className="win-link-btn">查看动态 #{run.dynamic_id}</a>}
                    {run.video_job_id && (
                      <a href={`#videos/${encodeURIComponent(run.video_job_id)}`} className="win-link-btn">打开视频任务</a>
                    )}
                    {canPrepareVideo(run) && (
                      <button type="button" className="win-btn" disabled={disabled || Boolean(pendingRunId)}
                        onClick={() => void prepareVideo(run.id)}>
                        {pendingRunId === run.id ? "正在准备…" : run.video_status === "render_failed"
                          ? "重试制片" : run.video_status === "window_missed" ? "手动准备视频" : "准备视频"}
                      </button>
                    )}
                  </div>
                  {run.last_error && <p className="feature-note">{safeMessage(run.last_error)}</p>}
                  {error?.runId === run.id && <p className="feature-note" role="alert">{error.message}</p>}
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </>
  );
}
