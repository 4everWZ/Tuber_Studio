import { useEffect, useState } from "react";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import { useIssueLog } from "./IssueLogStore";
import styles from "./operations.module.css";

export interface IssuesPanelProps {
  embedded?: boolean;
}

export function IssuesPanel({ embedded = false }: IssuesPanelProps) {
  const { entries, clearAll, acknowledgeOne, refresh } = useIssueLog();
  const [severity, setSeverity] = useState<"all" | "warning" | "error">("all");
  const [feedback, setFeedback] = useState("");
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [clearingAll, setClearingAll] = useState(false);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const filtered = entries.filter((item) => severity === "all" || item.severity === severity);

  async function handleClearAll() {
    if (clearingAll) return;
    setClearingAll(true);
    setFeedback("正在标为已读…");
    try {
      await clearAll();
      setFeedback("已标为已读，历史仍可查看。");
    } catch (err) {
      setFeedback(`清除失败：${errorMessage(err)}`);
    } finally {
      setClearingAll(false);
    }
  }

  async function handleAckOne(
    key: string,
    observation: { id: string; updated_at: number; occurrences: number }
  ) {
    if (busyKey) return;
    setBusyKey(key);
    try {
      await acknowledgeOne(observation);
      setFeedback("已标为已读。");
    } catch (err) {
      setFeedback(safeMessage(errorMessage(err)));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <section
      id={embedded ? "operation-activity-issues" : undefined}
      className={embedded ? `panel ${styles.activitySection}` : undefined}
    >
      <div className="panel-heading">
        <h2>{embedded ? "运营异常" : "告警记录"}</h2>
        <div className="actions">
          <label className={styles.dayLabel}>
            级别{" "}
            <select
              id={embedded ? "activity-issue-severity" : "workspace-issue-severity"}
              value={severity}
              onChange={(event) =>
                setSeverity(event.target.value as "all" | "warning" | "error")
              }
            >
              <option value="all">全部</option>
              <option value="warning">告警</option>
              <option value="error">错误</option>
            </select>
          </label>
          <button
            id={embedded ? "mark-activity-issues" : "clear-workspace-issues"}
            type="button"
            disabled={clearingAll}
            onClick={() => void handleClearAll()}
          >
            全部标为已读
          </button>
          {embedded && (
            <a href="#automation" className="button quiet">
              返回运营活动
            </a>
          )}
        </div>
      </div>

      <p
        id={embedded ? "activity-issue-feedback" : "workspace-issue-feedback"}
        className="hint"
        role="status"
      >
        {feedback}
      </p>

      <div
        id={embedded ? "activity-issue-list" : "workspace-issue-list"}
        className="win-group-box"
        aria-live="polite"
      >
        {filtered.length === 0 ? (
          <p className="hint">暂无匹配的告警或错误。</p>
        ) : (
          filtered.map((item) => (
            <article key={item.key} className={`issue-row ${styles.issueRow}`}>
              <p>{item.text}</p>
              <span className="hint">
                {item.unread > 0 ? `未读 ${item.unread} 次` : "已读"} ·{" "}
                {new Date(item.time * 1000).toLocaleString("zh-CN")}
              </span>
              {item.href && item.href !== "#issues" && (
                <a href={item.href}>打开相关位置</a>
              )}
              {item.observation && item.unread > 0 && (
                <button
                  type="button"
                  disabled={busyKey === item.key || clearingAll}
                  onClick={() => void handleAckOne(item.key, item.observation!)}
                >
                  标为已读
                </button>
              )}
            </article>
          ))
        )}
      </div>
    </section>
  );
}

export function IssuesPage() {
  return (
    <section data-section="issues" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>系统告警与诊断错误</h1>
        <div className="actions">
          <a href="#automation" className="button quiet">
            返回主控制台
          </a>
        </div>
      </div>
      <IssuesPanel />
    </section>
  );
}
