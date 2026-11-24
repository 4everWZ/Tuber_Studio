import { studioPath } from "@/shared/api/client";
import type { SystemSettings, SystemStatus } from "@/shared/hooks/useAppSession";
import { webCharacterName } from "@/shared/lib/errors";
import styles from "@/shared/styles/workbench.module.css";
import { ServiceStatusCapsule } from "./ServiceStatusCapsule";
import { SidebarToggleGlyph } from "./Sidebar";

export interface AutomationSummary {
  state?: string;
  policy?: {
    enabled?: boolean;
    emergency?: boolean;
  } | null;
}

export interface BilibiliAccountSummary {
  state?: string;
  profile?: {
    name?: string | null;
    avatar_url?: string | null;
  } | null;
}

export interface TopStatusBarProps {
  menuOpen: boolean;
  onToggleMenu: () => void;
  sidebarCollapsed?: boolean;
  onExpandSidebar?: () => void;
  status: SystemStatus | null;
  settings: SystemSettings | null;
  automation: AutomationSummary | null;
  account: BilibiliAccountSummary | null;
  unreadIssuesCount: number;
}

const AUTOMATION_LABELS: Record<string, string> = {
  starting: "启动检查中",
  draining: "收尾中",
  stopping: "停止中",
  blocked: "暂需关注",
  running: "自主运营中",
  off: "自主运营已暂停",
  emergency: "已紧急停止",
};

const ACCOUNT_STATE_LABELS: Record<string, string> = {
  not_configured: "未配置",
  unverified: "待验证",
  logged_in: "已登录",
  expired: "已过期",
  restricted: "受限",
  error: "异常",
};

export function automationStateLabel(automation: AutomationSummary | null | undefined): string {
  if (!automation) return "自主运营 · 同步中";
  if (automation.policy?.emergency) return AUTOMATION_LABELS.emergency;
  if (automation.state && AUTOMATION_LABELS[automation.state]) {
    return AUTOMATION_LABELS[automation.state];
  }
  return automation.policy?.enabled === true ? AUTOMATION_LABELS.running : AUTOMATION_LABELS.off;
}

export function TopStatusBar({
  menuOpen,
  onToggleMenu,
  sidebarCollapsed = false,
  onExpandSidebar,
  status,
  settings,
  automation,
  account,
  unreadIssuesCount,
}: TopStatusBarProps) {
  const charName = webCharacterName(status?.character) || "Aqua";
  const autoText = automationStateLabel(automation);
  const autoRunning = automation?.state === "running" && !automation.policy?.emergency;
  const profile = account?.profile;
  const accountName = profile?.name || (account?.state === "not_configured" ? "未绑定 B站" : "B站账号");
  const accountState = account?.state ? ACCOUNT_STATE_LABELS[account.state] || account.state : "";

  return (
    <header className={`topbar win-command-bar ${styles.topbar}`} data-chat-hide="">
      <div className={`command-bar-left ${styles.topbarLeft}`}>
        {sidebarCollapsed && onExpandSidebar && (
          <button
            type="button"
            id="expand-workspace-sidebar"
            className={styles.expandSidebarBtn}
            aria-label="展开侧边栏"
            aria-expanded={false}
            aria-controls="workspace-sidebar"
            title="展开侧边栏"
            onClick={onExpandSidebar}
          >
            <SidebarToggleGlyph />
          </button>
        )}

        <button
          type="button"
          id="workspace-menu"
          className={`btn-command-toggle ${styles.menuButton}`}
          aria-controls="workspace-sidebar"
          aria-expanded={menuOpen}
          onClick={onToggleMenu}
        >
          <span aria-hidden="true">☰</span> 导航
        </button>

        <a
          href="#characters"
          id="workspace-character"
          className={`win-badge-pill ${styles.badgePill} ${styles.characterPill}`}
          title="查看当前角色设定"
          data-chat-hide=""
        >
          <span className="pill-text">{charName}</span>
        </a>

        <ServiceStatusCapsule
          status={status}
          settings={settings}
          className={`win-badge-pill ${styles.badgePill}`}
          dotClassName={`status-dot ${styles.statusDot}`}
          dotGoodClassName={`good ${styles.statusDotGood}`}
          dotBadClassName={`bad ${styles.statusDotBad}`}
        />
      </div>

      <div className={`command-bar-right ${styles.topbarRight}`} data-chat-hide="">
        <a
          href="#automation"
          id="discovery-top-state"
          className={`win-quick-status ${styles.quickStatus}`}
          title="自主运营当前状态"
        >
          <span
            className={`status-indicator ${styles.statusIndicator}${autoRunning ? ` good ${styles.statusIndicatorGood}` : ""}`}
            aria-hidden="true"
          />
          {autoText}
        </a>

        <a
          href="#issues"
          id="workspace-issues"
          className={`win-quick-alert ${styles.quickAlert}`}
          title="查看告警日志"
        >
          {unreadIssuesCount > 0 ? `告警与错误 · ${unreadIssuesCount}` : "告警与错误"}
        </a>

        <a href="#accounts" id="top-account-badge" className="account-badge" aria-label="B站账号">
          <span className="account-avatar">
            {profile?.avatar_url ? (
              <img
                src={studioPath("/api/v1/operations/account/bilibili/avatar")}
                alt=""
                referrerPolicy="no-referrer"
              />
            ) : (
              <span aria-hidden="true">{profile?.name ? Array.from(profile.name)[0] : "B站"}</span>
            )}
          </span>
          <span className="account-badge-name">{accountName}</span>
          {accountState && (
            <span className="account-state" data-state={account?.state || "unknown"}>
              {accountState}
            </span>
          )}
        </a>
      </div>
    </header>
  );
}
