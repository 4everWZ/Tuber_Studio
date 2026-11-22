import { useEffect, useState } from "react";
import { StatusPill } from "@/shared";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { webCharacterName } from "@/shared/lib/errors";
import type { ParsedRoute } from "@/shell/navigation";
import { DiscoveryProgressDetails } from "./DiscoveryPanel";
import { IssuesPanel } from "./IssuesPage";
import { OperationsActivityFeed } from "./OperationsActivityFeed";
import { OperationsQuickBar } from "./OperationsQuickBar";
import { useOperationsControl } from "./operationsStore";
import { Stage2PipelineCards } from "./Stage2PipelineCards";
import styles from "./operations.module.css";
import type { ActivityKindFilter } from "./types";

const ACCOUNT_STATE_LABELS: Record<string, { label: string; tone: "good" | "bad" | "amber" | "neutral" }> = {
  logged_in: { label: "已登录", tone: "good" },
  not_configured: { label: "未配置", tone: "neutral" },
  unverified: { label: "待验证", tone: "amber" },
  expired: { label: "401 已过期", tone: "bad" },
  restricted: { label: "受限", tone: "amber" },
  error: { label: "异常", tone: "bad" },
};

export function AutomationDashboardPage({ route }: { route: ParsedRoute }) {
  const { status: sysStatus } = useAppSession();
  const control = useOperationsControl();
  const [refreshTick, setRefreshTick] = useState(0);

  const showIssues = route.routeKind === "issues";
  const commentRound =
    route.routeKind === "comments" && /^[\w-]+$/.test(route.routeId || "")
      ? route.routeId || null
      : null;
  const initialKind: ActivityKindFilter =
    route.routeKind === "comments"
      ? "comments"
      : route.routeKind === "video"
        ? "videos"
        : "all";

  useEffect(() => {
    void control.refreshDashboard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.page, route.routeKind, route.routeId]);

  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void control.refreshStatus({ background: true });
      void control.refreshQuotas();
      void control.refreshInbox();
      void control.refreshClaims();
      setRefreshTick((t) => t + 1);
    }, 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const emergency = Boolean(control.status?.policy?.emergency);
  const acctState = control.account?.state || "not_configured";
  const acctMeta = ACCOUNT_STATE_LABELS[acctState] || {
    label: acctState,
    tone: "neutral" as const,
  };
  const charDisplay =
    webCharacterName(control.status?.character) ||
    webCharacterName(sysStatus?.character) ||
    "角色待确认";

  return (
    <section data-section="automation" className="win-page-view">
      <div className="page-heading win-heading">
        <h1 id="operation-page-title">自主运营</h1>
        <div className="actions">
          <a href="#automation-settings" className="button quiet">
            运营设置
          </a>
          <a href="#automation-records" className="button quiet">
            运行记录
          </a>
        </div>
      </div>

      {/* Shared Stage 2 Top Bar: Bilibili Account Status + Global Emergency Stop */}
      <div className={styles.topControlBar} role="region" aria-label="账号与全局急停控制栏">
        <div className={styles.topBarAccount}>
          <strong>当前角色：{charDisplay}</strong>
          <span>·</span>
          <span>
            B站账号：
            {control.account?.profile?.name ||
              (control.status?.account_id ? `UID ${control.status.account_id}` : "未绑定账号")}
          </span>
          <StatusPill label={acctMeta.label} tone={acctMeta.tone} />
          <a href="#accounts" className="win-link-btn">
            账号管理 →
          </a>
        </div>

        <div className={styles.topBarEmergency}>
          {!emergency ? (
            <button
              id="op-dashboard-emergency"
              type="button"
              className="danger"
              disabled={control.controlPending}
              onClick={() => void control.stopAutomation(true)}
            >
              紧急停止
            </button>
          ) : (
            <button
              id="op-dashboard-reset-emergency"
              type="button"
              className="primary"
              disabled={control.controlPending}
              onClick={() => void control.resetEmergencyStop()}
            >
              恢复急停
            </button>
          )}
        </div>
      </div>

      {emergency && (
        <p className="feature-note" role="alert">
          全局紧急停止已生效：主动探索（Discovery）与消息响应（Inbox）已同时冻结自动写入，需点击「恢复急停」后方可重新开启。
        </p>
      )}

      <OperationsQuickBar
        status={control.status}
        controlPending={control.controlPending}
        onToggleMaster={() => void control.toggleMasterAutomation()}
      />

      <Stage2PipelineCards
        status={control.status}
        account={control.account}
        discovery={control.discovery}
        quotas={control.quotas}
        inboxRecords={control.inboxRecords}
        claims={control.claims}
        controlPending={control.controlPending}
        onToggleInbox={(enabled) => control.toggleInboxEnabled(enabled)}
        onToggleDiscovery={(enabled) => control.toggleDiscoveryEnabled(enabled)}
        onSaveInboxSettings={(patch) => control.saveInboxSettings(patch)}
        onSaveDiscoverySettings={(patch) => control.saveDiscoverySettings(patch)}
        onSaveQuotas={(patch) => control.saveQuotaControls(patch)}
      />

      <DiscoveryProgressDetails discovery={control.discovery ?? control.status?.discovery ?? null} />

      {showIssues ? (
        <IssuesPanel embedded />
      ) : (
        <OperationsActivityFeed
          initialKind={initialKind}
          roundId={commentRound}
          refreshTick={refreshTick}
        />
      )}
    </section>
  );
}
