import { useRef, useState } from "react";
import { safeMessage } from "@/shared/lib/errors";
import styles from "./operations.module.css";
import type { AutomationStatus, BilibiliAccountSnapshot } from "./types";

const MATRIX_ACTIONS = [
  {
    name: "matrix_top_level",
    legend: "在视频下发表评论",
    enabledKey: "top_level_enabled",
    autoKey: "top_level_auto_publish",
    labels: ["禁止", "先审草稿", "自动发布"] as const,
  },
  {
    name: "matrix_reply",
    legend: "回复已有评论",
    enabledKey: "reply_enabled",
    autoKey: "reply_auto_publish",
    labels: ["禁止", "先审草稿", "自动发布"] as const,
  },
  {
    name: "matrix_comment_like",
    legend: "评论点赞",
    enabledKey: "comment_like_enabled",
    autoKey: "comment_like_auto_publish",
    labels: ["禁止", "需要审核", "自动点赞"] as const,
  },
  {
    name: "matrix_video_like",
    legend: "视频点赞",
    enabledKey: "video_like_enabled",
    autoKey: "video_like_auto_publish",
    labels: ["禁止", "需要审核", "自动点赞"] as const,
  },
  {
    name: "matrix_dynamic_like",
    legend: "动态点赞",
    enabledKey: "dynamic_like_enabled",
    autoKey: "dynamic_like_auto_publish",
    labels: ["禁止", "需要审核", "自动点赞"] as const,
  },
  {
    name: "matrix_video_fav",
    legend: "视频收藏",
    enabledKey: "video_fav_enabled",
    autoKey: "video_fav_auto_publish",
    labels: ["禁止", "需要审核", "自动收藏"] as const,
  },
] as const;

export function deriveAutomaticSendingWarning(
  status: AutomationStatus | null,
  account: BilibiliAccountSnapshot | null
): string {
  if (!status) return "";
  const settings = status.settings;
  const hasAutomatic =
    MATRIX_ACTIONS.some(
      ({ enabledKey, autoKey }) => Boolean(settings?.[enabledKey] && settings?.[autoKey])
    ) ||
    status.permissions?.private_messages === "auto" ||
    status.permissions?.dynamics === "auto";

  if (!hasAutomatic || status.automatic_sending_ready !== false) {
    return "";
  }
  const blockers = (status.blockers || [])
    .map((value) => (typeof value === "string" ? value : value?.message || ""))
    .filter(Boolean);
  const configured = account
    ? Boolean(account.configured?.sessdata && account.configured?.csrf)
    : status.effective?.comments?.credentials_configured;

  if (blockers.length > 0) {
    return `尚未就绪：${blockers.join("；")}`;
  }
  if (configured === false) {
    return "请先登录 B 站账号。";
  }
  if (status.startup_check === "checking") {
    return "正在核验账号与模型连接…";
  }
  if (status.state === "off" && status.startup_check === "pending") {
    return "当前未开启自主运营，开启时会自动核验账号与模型连接。";
  }
  return "等待账号或模型连接。";
}

export interface PermissionMatrixProps {
  status: AutomationStatus | null;
  account: BilibiliAccountSnapshot | null;
  onSaveChange: (change: Record<string, boolean>) => Promise<unknown>;
  onRefresh: () => Promise<unknown>;
}

export function PermissionMatrix({
  status,
  onSaveChange,
  onRefresh,
}: PermissionMatrixProps) {
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState("");
  const savingRef = useRef(false);

  const settings = status?.settings;
  const disabled = saving || !settings;
  const visualAvailable = status?.vision?.images;

  async function applyChange(change: Record<string, boolean>) {
    if (savingRef.current || !settings) return;
    savingRef.current = true;
    setSaving(true);
    setFeedback("正在保存…");
    try {
      await onSaveChange(change);
      setFeedback("已保存");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setFeedback(`保存未确认，请核对当前选项。 ${safeMessage(msg)}`);
    } finally {
      try {
        await onRefresh();
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    }
  }

  return (
    <article
      className="win-group-box"
      id="operations-permission-matrix"
      data-workspace-anchor=""
      style={{ marginTop: 16 }}
    >
      <div className="panel-heading">
        <div className="win-group-title" style={{ margin: 0 }}>
          角色权限
        </div>
        <a href="#chat">返回对话</a>
      </div>
      <nav className="actions" aria-label="其他角色权限">
        <a href="#message-policy-settings">私信收取与回复</a>
        <a href="#dynamic-policy-settings">发布动态</a>
        <a href="#daily-memory-card">私聊记忆</a>
      </nav>
      <p className="hint">选择后立即保存。运营按当前账号和已经开启的动作执行。</p>

      <fieldset id="matrix-fields" className={styles.matrixGroup} disabled={disabled}>
        <legend>刷视频与评论互动</legend>
        <label className="check-field">
          <input
            id="matrix-video-enabled"
            type="checkbox"
            checked={settings?.discovery_enabled === true}
            onChange={(event) => void applyChange({ discovery_enabled: event.target.checked })}
          />{" "}
          允许后台刷视频
        </label>

        <div className={styles.matrixActionGrid}>
          {MATRIX_ACTIONS.map((action) => {
            const isEnabled = Boolean(settings?.[action.enabledKey]);
            const isAuto = Boolean(settings?.[action.autoKey]);
            const modeValue = !isEnabled ? "0" : isAuto ? "2" : "1";
            return (
              <fieldset key={action.name} className={styles.matrixActionCard}>
                <legend>{action.legend}</legend>
                <div className={styles.matrixRadioGroup}>
                  {(["0", "1", "2"] as const).map((value, idx) => (
                    <label key={value}>
                      <input
                        type="radio"
                        name={action.name}
                        value={value}
                        checked={modeValue === value}
                        onChange={() =>
                          void applyChange({
                            [action.enabledKey]: value !== "0",
                            [action.autoKey]: value === "2",
                          })
                        }
                      />{" "}
                      {action.labels[idx]}
                    </label>
                  ))}
                </div>
              </fieldset>
            );
          })}
        </div>
      </fieldset>

      <p id="matrix-save-feedback" className="hint" role="status">
        {feedback}
      </p>
      {typeof visualAvailable === "boolean" && (
        <div id="matrix-visual-status" className="hint">
          {visualAvailable ? "观看时读取字幕并抽样画面" : "当前视觉服务未就绪"}
        </div>
      )}
      <a href="#accounts">查看平台登录状态</a>
    </article>
  );
}
