import { CapabilityStrip } from "@/shared";
import { safeMessage } from "@/shared/lib/errors";
import styles from "./operations.module.css";
import type { AutomationBacklog, AutomationStatus } from "./types";

const STATE_LABELS: Record<string, string> = {
  starting: "正在检查启动条件",
  draining: "正在收尾当前动作",
  stopping: "正在停止自主运营",
  blocked: "自主运营受阻",
  running: "自主运营中",
  off: "自主运营已停止",
  emergency: "已紧急停止",
};

export function automationHomeStateLabel(status: AutomationStatus | null | undefined): string {
  if (!status) return "正在读取运营状态…";
  if (status.policy?.emergency) return STATE_LABELS.emergency;
  if (status.state && STATE_LABELS[status.state]) return STATE_LABELS[status.state];
  return status.policy?.enabled === true ? STATE_LABELS.running : STATE_LABELS.off;
}

function formatBacklogSummary(backlog: AutomationBacklog, sendError?: string | null): {
  heading: string;
  detail: string;
} {
  const seconds = backlog.oldest_seconds ?? 0;
  const age = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  const heading = [
    `待回访 ${backlog.total_heads} 楼 · 可执行 ${backlog.ready_heads} 楼`,
    `最老等待 ${age}`,
    backlog.discovery_paused ? "新视频与旧页暂缓" : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const review = backlog.review || {};
  const flow = backlog.last_round_flow || {};
  const changes = ([key, label]: [string, string]) =>
    flow[key]
      ? `${label}新增 ${flow[key].added}／结清 ${flow[key].settled}／净变化 ${flow[key].net}`
      : "";
  const uncertainTotal = Object.values(backlog.uncertain || {}).reduce((sum, count) => sum + count, 0);

  const detail = [
    `等额度或其他条件 ${backlog.waiting_comments} 楼`,
    `已授权待发 ${backlog.sends.queued}（等待 ${backlog.sends.deferred}）`,
    `点赞与点踩待执行 ${backlog.sends.reactions}`,
    `发布被拒／待处理 ${backlog.sends.rejected}`,
    backlog.video_work
      ? `视频待办 ${backlog.video_work.pending} 项 · 关联 ${backlog.video_work.history} 条批次记录`
      : "",
    backlog.comment_hourly_resume_at
      ? `评论小时额度预计 ${new Date(backlog.comment_hourly_resume_at * 1000).toLocaleString("zh-CN")} 恢复`
      : "",
    backlog.catching_up ? "正在清理积压" : "",
    `待审核 评论 ${review.comments ?? 0}／私信 ${review.messages ?? 0}／动态 ${review.dynamics ?? 0}`,
    `待核对 ${uncertainTotal}`,
    `楼头水位 ${backlog.low_heads ?? "—"}–${backlog.high_heads ?? "—"}`,
    changes(["revisit", "本轮回访"]),
    changes(["send", "本轮发送队列"]),
    safeMessage(sendError),
  ]
    .filter(Boolean)
    .join(" · ");

  return { heading, detail };
}

export interface OperationsQuickBarProps {
  status: AutomationStatus | null;
  controlPending?: boolean;
  onToggleMaster?: () => void;
  showToggleButton?: boolean;
}

export function OperationsQuickBar({
  status,
  controlPending = false,
  onToggleMaster,
  showToggleButton = true,
}: OperationsQuickBarProps) {
  const policyEnabled = status?.policy?.enabled === true;
  const emergency = Boolean(status?.policy?.emergency);
  const transitioning = Boolean(status?.state && ["starting", "draining", "stopping"].includes(status.state));

  const activeRounds = status?.active_rounds ?? (status?.active_round ? [status.active_round] : []);
  const hasActiveRound = activeRounds.length > 0;
  const roundNames = activeRounds.map((round) =>
    round.config?.interaction_only ? "互动收件箱" : round.preview ? "只读预览" : "刷视频"
  );
  const roundSummary = hasActiveRound
    ? `当前轮次 · ${roundNames.join(" · ")}`
    : status?.waiting_round
      ? `视频轮次已保存 · 待续跑${status.waiting_round.remaining ? ` · 剩余 ${status.waiting_round.remaining} 项` : ""}`
      : "暂无正在执行的轮次";

  const backlog = status?.backlog;
  const backlogText = backlog ? formatBacklogSummary(backlog, status?.send_error) : null;

  return (
    <section id="operation-overview" className={styles.operationOverview}>
      <div className="panel-heading">
        <p id="op-home-state" role="status">
          {automationHomeStateLabel(status)}
        </p>
        {showToggleButton && onToggleMaster && (
          <button
            id="op-toggle-auto"
            type="button"
            className="primary"
            disabled={!status || emergency || transitioning || controlPending}
            aria-busy={controlPending}
            onClick={onToggleMaster}
          >
            {policyEnabled ? "停止自主运营" : "开始自主运营"}
          </button>
        )}
      </div>

      <CapabilityStrip status={status} showLegend showBlocker />

      <p id="op-active-rounds" className="hint" role="status">
        {roundSummary}
      </p>

      {backlogText && (
        <details id="op-backlog" className="hint">
          <summary id="op-backlog-heading">{backlogText.heading}</summary>
          <p id="op-backlog-detail">{backlogText.detail}</p>
        </details>
      )}
    </section>
  );
}
