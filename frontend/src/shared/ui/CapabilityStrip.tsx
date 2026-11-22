import { safeMessage } from "../lib/errors";

export type CapabilityColor = "green" | "yellow" | "gray" | "red";

export interface OperationCapabilityItem {
  id: string;
  label: string;
  color: CapabilityColor;
  state: string;
  detail: string;
}

export interface OperationPermissionsMap {
  video_enabled?: boolean | null;
  top_level?: string | null;
  replies?: string | null;
  private_poll?: boolean | null;
  private_messages?: string | null;
  dynamics?: string | null;
  video_like?: string | null;
  comment_like?: string | null;
  dynamic_like?: string | null;
  video_fav?: string | null;
  [key: string]: unknown;
}

export interface OperationCapabilityStatus {
  state?: string | null;
  startup_check?: string | null;
  last_error?: string | null;
  blockers?: Array<string | { message?: string }>;
  policy?: {
    enabled?: boolean;
    emergency?: boolean;
    [key: string]: unknown;
  } | null;
  permissions?: OperationPermissionsMap | null;
}

const VALID_MODES = new Set(["off", "review", "auto"]);

function binaryMode(value: unknown): string | null {
  return value === true ? "auto" : value === false ? "off" : null;
}

function blockerMessage(item: string | { message?: string }): string {
  return typeof item === "string" ? safeMessage(item) : safeMessage(item?.message ?? "");
}

/**
 * Derive the 10-channel operational capability light model from backend automation status.
 * Separates saved permission modes from actual runtime state and parent blockers.
 */
export function operationCapabilities(
  status: OperationCapabilityStatus | null | undefined
): OperationCapabilityItem[] {
  const safeStatus = status ?? {};
  const permissions = safeStatus.permissions || {};
  const emergency = safeStatus.policy?.emergency === true;
  const blocked = emergency || ["blocked", "error", "failed"].includes(safeStatus.state || "");
  const running = safeStatus.policy?.enabled === true && safeStatus.state === "running" && !blocked;
  const blockerReasons = (safeStatus.blockers || []).map(blockerMessage).filter(Boolean);
  const reason = emergency
    ? "已紧急停止，请先解除紧停。"
    : blockerReasons.join("；") || safeMessage(safeStatus.last_error) || "自主运营报告受阻。";

  const channels: Array<[string, string, string | null | undefined]> = [
    ["videos", "视频", binaryMode(permissions.video_enabled)],
    ["top_level", "主评", typeof permissions.top_level === "string" ? permissions.top_level : null],
    ["comments", "评论", typeof permissions.replies === "string" ? permissions.replies : null],
    ["message_poll", "私信收取", binaryMode(permissions.private_poll)],
    ["message_reply", "私信回复", typeof permissions.private_messages === "string" ? permissions.private_messages : null],
    ["dynamics", "动态", typeof permissions.dynamics === "string" ? permissions.dynamics : null],
    ["like", "视频点赞", typeof permissions.video_like === "string" ? permissions.video_like : null],
    ["comment-like", "评论点赞", typeof permissions.comment_like === "string" ? permissions.comment_like : null],
    ["dynamic-like", "动态点赞", typeof permissions.dynamic_like === "string" ? permissions.dynamic_like : null],
    ["favorite", "收藏", typeof permissions.video_fav === "string" ? permissions.video_fav : null],
  ];

  return channels.map(([id, label, mode]) => {
    if (!mode || !VALID_MODES.has(mode)) {
      return { id, label, color: "gray", state: "待确认", detail: "尚未取得此功能的权限状态。" };
    }
    if (mode === "off") {
      return { id, label, color: "gray", state: "关闭", detail: "此功能未开放。可在运营设置中调整。" };
    }
    if (blocked) {
      return {
        id,
        label,
        color: "red",
        state: emergency ? "已紧停" : "受阻",
        detail: reason,
      };
    }
    const configured = mode === "auto" ? "已允许自动运行" : "已允许生成待审内容";
    if (safeStatus.state === "starting" || safeStatus.state === "draining") {
      const state = safeStatus.state === "starting" ? "检查中" : "收尾中";
      return { id, label, color: "gray", state, detail: `${configured}，自主运营${state}。` };
    }
    if (!running) {
      return {
        id,
        label,
        color: "gray",
        state: safeStatus.state === "stopping" ? "停止中" : "待启动",
        detail: `${configured}；${safeStatus.state === "stopping" ? "自主运营正在停止" : "自主运营尚未运行"}。`,
      };
    }
    if (mode === "review") {
      return {
        id,
        label,
        color: "yellow",
        state: "生成待审",
        detail: "自主运营已启用此功能，生成的内容需要人工审核。",
      };
    }
    return {
      id,
      label,
      color: "green",
      state: "自动运行",
      detail: "自主运营已启用此功能，按已保存设置自动执行。",
    };
  });
}

export function capabilityBlockerSummary(status: OperationCapabilityStatus | null | undefined): {
  hidden: boolean;
  isError: boolean;
  text: string;
} {
  const safeStatus = status ?? {};
  const reasons = (safeStatus.blockers || []).map(blockerMessage).filter(Boolean);
  const checking = safeStatus.startup_check === "checking";
  const failed = !checking && reasons.length > 0;
  const emergency = safeStatus.policy?.emergency === true;
  const hidden = !emergency && !failed && !checking && safeStatus.startup_check !== "pending";
  const text = emergency
    ? "已紧急停止。"
    : checking
      ? "启动检查中…"
      : failed
        ? reasons.join("；")
        : safeStatus.startup_check === "pending"
          ? "启动时核验"
          : "";
  return {
    hidden,
    isError: failed || emergency,
    text,
  };
}

export interface CapabilityStripProps {
  status: OperationCapabilityStatus | null | undefined;
  id?: string;
  className?: string;
  showLegend?: boolean;
  showBlocker?: boolean;
  legendId?: string;
  blockerId?: string;
}

export function CapabilityStrip({
  status,
  id = "op-permissions-summary",
  className,
  showLegend = false,
  showBlocker = false,
  legendId = "op-capability-legend",
  blockerId = "op-capability-blocker",
}: CapabilityStripProps) {
  const capabilities = operationCapabilities(status);
  const blocker = capabilityBlockerSummary(status);

  return (
    <>
      <div
        id={id}
        className={["capability-strip", className].filter(Boolean).join(" ")}
        role="list"
        aria-label="各功能运行状态"
        aria-describedby={showLegend ? legendId : undefined}
        aria-live="polite"
      >
        {capabilities.map((capability) => (
          <span
            key={capability.id}
            className={`capability-light is-${capability.color}`}
            data-capability={capability.id}
            data-state={capability.color}
            role="listitem"
            aria-label={`${capability.label}，${capability.state}。${capability.detail}`}
            title={capability.detail}
          >
            {capability.label} · {capability.state}
          </span>
        ))}
      </div>
      {showLegend && (
        <p id={legendId} className="capability-legend">
          绿：自动运行 · 黄：生成待审 · 灰：关闭 / 待启动 · 红：受阻
        </p>
      )}
      {showBlocker && (
        <p
          id={blockerId}
          className={`capability-blocker${blocker.isError ? " is-error" : ""}`}
          role="status"
          hidden={blocker.hidden}
        >
          {blocker.text}
        </p>
      )}
    </>
  );
}
