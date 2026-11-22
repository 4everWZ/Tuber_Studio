import { useEffect, useMemo, useState, type FormEvent } from "react";
import { StatusPill, useUnsavedGuard } from "@/shared";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import styles from "./operations.module.css";
import type {
  AutomationStatus,
  BilibiliAccountSnapshot,
  DiscoveryStatus,
  InboxPipelineStatus,
  InboxRecordsResponse,
  OperationClaim,
  QuotaSnapshot,
} from "./types";

const DISCOVERY_PHASE_LABELS: Record<string, string> = {
  off: "已停用",
  starting: "启动检查",
  discovering: "正在找视频",
  reading: "正在取材",
  judging: "判断写稿中",
  publishing: "正在发送",
  reconciling: "核对回执中",
  finishing: "收尾中",
  waiting: "轮间等待",
  blocked: "受阻",
  draining: "批次收尾中",
  stopping: "停止中",
  emergency: "已紧停冻结",
};

const INBOX_STATE_LABELS: Record<string, string> = {
  running: "运行中",
  off: "已停用",
  frozen: "已冻结（待核对）",
  emergency: "已紧停冻结",
  blocked: "受阻",
  starting: "启动检查",
  draining: "收尾中",
  stopping: "停止中",
};

export interface Stage2PipelineCardsProps {
  status: AutomationStatus | null;
  account: BilibiliAccountSnapshot | null;
  discovery: DiscoveryStatus | null;
  quotas: QuotaSnapshot | null;
  inboxRecords: InboxRecordsResponse | null;
  claims: OperationClaim[];
  controlPending: boolean;
  onToggleInbox: (enabled: boolean) => Promise<unknown>;
  onToggleDiscovery: (enabled: boolean) => Promise<unknown>;
  onSaveInboxSettings: (patch: Record<string, unknown>) => Promise<InboxPipelineStatus>;
  onSaveDiscoverySettings: (patch: Record<string, unknown>) => Promise<DiscoveryStatus>;
  onSaveQuotas: (patch: { direct_quota_reserve: number }) => Promise<QuotaSnapshot>;
}

function deriveInboundCounts(
  status: AutomationStatus | null,
  inboxRecords: InboxRecordsResponse | null
): { comment_reply: number; mention: number; own_content_comment: number; dm: number } {
  const directCounts = status?.inbox?.counts;
  if (directCounts) {
    return {
      comment_reply: Number(directCounts.comment_reply ?? 0),
      mention: Number(directCounts.mention ?? 0),
      own_content_comment: Number(directCounts.own_content_comment ?? 0),
      dm: Number(directCounts.dm ?? inboxRecords?.messages ?? inboxRecords?.private_count ?? 0),
    };
  }
  const counts = {
    comment_reply: 0,
    mention: 0,
    own_content_comment: 0,
    dm: Number(inboxRecords?.messages ?? inboxRecords?.private_count ?? 0),
  };
  for (const item of inboxRecords?.items || []) {
    if (item.kind === "comment_visit" || item.source_pipeline === "discovery") continue;
    const kind = item.inbound_kind || item.kind;
    if (kind === "comment_reply" || kind === "reply") counts.comment_reply += 1;
    else if (kind === "mention") counts.mention += 1;
    else if (kind === "own_content_comment" || kind === "own_comment" || kind === "own_dynamic") {
      counts.own_content_comment += 1;
    }
  }
  return counts;
}

export function Stage2PipelineCards({
  status,
  account,
  discovery,
  quotas,
  inboxRecords,
  claims,
  controlPending,
  onToggleInbox,
  onToggleDiscovery,
  onSaveInboxSettings,
  onSaveDiscoverySettings,
  onSaveQuotas,
}: Stage2PipelineCardsProps) {
  const emergency = Boolean(status?.policy?.emergency);
  const transitioning = Boolean(status?.state && ["starting", "draining", "stopping"].includes(status.state));

  // Derive uncertain & auth status lamps
  const uncertainCount = Object.values(status?.backlog?.uncertain || {}).reduce(
    (sum, n) => sum + Number(n || 0),
    0
  );
  const hasUncertain = status?.frozen_reason === "reconciliation_pending" || uncertainCount > 0;
  const authExpired =
    account?.state === "expired" ||
    (status?.blockers || []).some((b) => {
      const msg = typeof b === "string" ? b : b?.message || "";
      return /401|登录态|过期|失效/.test(msg);
    });

  // Card 1: Inbox
  const inboxState = status?.inbox;
  const inboxEnabled = Boolean(
    inboxState?.enabled ?? (status?.policy?.enabled && status?.settings?.inbox_enabled)
  );
  const inboxPendingReview =
    Number(status?.backlog?.review?.messages ?? 0) + Number(status?.backlog?.review?.comments ?? 0);
  const inboxLastRestricted = status?.read_gate?.inbox_last_restricted;
  const inboundCounts = useMemo(
    () => deriveInboundCounts(status, inboxRecords),
    [status, inboxRecords]
  );

  const [mentionsEnabled, setMentionsEnabled] = useState(false);
  const [repliesEnabled, setRepliesEnabled] = useState(false);
  const [mentionReplyEnabled, setMentionReplyEnabled] = useState(false);
  const [notifInterval, setNotifInterval] = useState("60");
  const [inboxDirty, setInboxDirty] = useState(false);
  const [inboxSaving, setInboxSaving] = useState(false);
  const [inboxNote, setInboxNote] = useState("");

  useUnsavedGuard("stage2-inbox-settings", inboxDirty);

  useEffect(() => {
    if (inboxDirty || !status?.settings) return;
    setMentionsEnabled(Boolean(status.settings.mentions_enabled));
    setRepliesEnabled(Boolean(status.settings.replies_enabled));
    setMentionReplyEnabled(Boolean(status.settings.mention_reply_enabled));
    setNotifInterval(String(status.settings.notification_interval_seconds ?? 60));
  }, [status?.settings, inboxDirty]);

  async function handleSaveInboxSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inboxSaving) return;
    const intervalNum = Number(notifInterval);
    if (!Number.isInteger(intervalNum) || intervalNum < 60 || intervalNum > 86400) {
      setInboxNote("轮询间隔须为 60 到 86400 秒的整数。");
      return;
    }
    setInboxSaving(true);
    setInboxNote("正在保存消息响应设置…");
    try {
      await onSaveInboxSettings({
        mentions_enabled: mentionsEnabled,
        replies_enabled: repliesEnabled,
        mention_reply_enabled: mentionReplyEnabled,
        notification_interval_seconds: intervalNum,
      });
      setInboxDirty(false);
      setInboxNote("消息响应设置已保存。");
    } catch (err) {
      setInboxNote(`保存失败：${errorMessage(err)}`);
    } finally {
      setInboxSaving(false);
    }
  }

  // Card 2: Discovery
  const effectiveDiscovery = discovery ?? status?.discovery ?? null;
  const discoveryEnabled = Boolean(
    effectiveDiscovery?.enabled ?? (status?.policy?.enabled && status?.settings?.discovery_enabled)
  );
  const discoveryBackoff =
    status?.read_gate?.discovery_backoff || effectiveDiscovery?.read_gate?.discovery_backoff || null;
  const discoveryReadPaused = Boolean(
    status?.read_gate?.discovery_paused || effectiveDiscovery?.read_gate?.discovery_paused || discoveryBackoff
  );
  const discoveryDeferred = Boolean(quotas?.discovery_deferred);

  const [discoveryWatchLimit, setDiscoveryWatchLimit] = useState("0");
  const [discoveryDirty, setDiscoveryDirty] = useState(false);
  const [discoverySaving, setDiscoverySaving] = useState(false);
  const [discoveryNote, setDiscoveryNote] = useState("");

  useUnsavedGuard("stage2-discovery-quick-settings", discoveryDirty);

  useEffect(() => {
    if (discoveryDirty || !status?.settings) return;
    setDiscoveryWatchLimit(String(status.settings.daily_watch_limit ?? 0));
  }, [status?.settings, discoveryDirty]);

  async function handleSaveDiscoveryQuick(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (discoverySaving) return;
    const watchLimitNum = Number(discoveryWatchLimit);
    if (!Number.isInteger(watchLimitNum) || watchLimitNum < 0) {
      setDiscoveryNote("每日观看上限须为不小于 0 的整数（0 表示不限）。");
      return;
    }
    setDiscoverySaving(true);
    setDiscoveryNote("正在保存探索参数…");
    try {
      await onSaveDiscoverySettings({ daily_watch_limit: watchLimitNum });
      setDiscoveryDirty(false);
      setDiscoveryNote("探索参数已保存。");
    } catch (err) {
      setDiscoveryNote(`保存失败：${errorMessage(err)}`);
    } finally {
      setDiscoverySaving(false);
    }
  }

  // Direct Quota Reserve Control
  const serverReserve = quotas?.direct_quota_reserve ?? status?.policy?.direct_quota_reserve ?? 2;
  const [reserveInput, setReserveInput] = useState(String(serverReserve));
  const [reserveDirty, setReserveDirty] = useState(false);
  const [reserveSaving, setReserveSaving] = useState(false);
  const [reserveNote, setReserveNote] = useState("");

  useUnsavedGuard("stage2-quota-reserve", reserveDirty);

  useEffect(() => {
    if (reserveDirty) return;
    setReserveInput(String(serverReserve));
  }, [serverReserve, reserveDirty]);

  async function handleSaveReserve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (reserveSaving) return;
    const parsed = Number(reserveInput);
    if (reserveInput.trim() === "" || !Number.isInteger(parsed) || parsed < 0) {
      setReserveNote("预留额度须为不小于 0 的整数（0 表示不预留 / 共享额度）。");
      return;
    }
    setReserveSaving(true);
    setReserveNote("正在保存配额预留设置…");
    try {
      const updated = await onSaveQuotas({ direct_quota_reserve: parsed });
      setReserveDirty(false);
      setReserveInput(String(updated.direct_quota_reserve));
      setReserveNote(
        updated.direct_quota_reserve === 0
          ? "已保存：预留额度为 0（不预留 / 共享额度）。"
          : `已保存：为收件箱直连互动预留 ${updated.direct_quota_reserve} 条发送名额。`
      );
    } catch (err) {
      setReserveNote(`保存失败：${errorMessage(err)}`);
    } finally {
      setReserveSaving(false);
    }
  }

  const inboxEffectiveState = emergency
    ? "emergency"
    : inboxState?.state || (inboxEnabled ? "running" : "off");
  const discoveryEffectiveState = emergency
    ? "emergency"
    : effectiveDiscovery?.state || (discoveryEnabled ? "waiting" : "off");

  return (
    <section aria-label="双管道独立控制台">
      <div className={styles.pipelineGrid}>
        {/* Card 1: Inbox Pipeline */}
        <article
          id="pipeline-inbox-card"
          className={[
            styles.pipelineCard,
            inboxEffectiveState === "running" ? styles.pipelineCardActive : "",
            emergency || hasUncertain ? styles.pipelineCardFrozen : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          <div className={styles.pipelineHeader}>
            <div className={styles.pipelineTitleWrap}>
              <h2>消息响应（Inbox）</h2>
              <span className="hint">优先响应评论回复、@提及、自有投稿与动态评论、私信</span>
            </div>
            <button
              id="inbox-pipeline-toggle"
              type="button"
              role="switch"
              aria-checked={inboxEnabled}
              aria-label="消息响应（Inbox）开关"
              className={styles.switchBtn}
              disabled={!status || emergency || transitioning || controlPending}
              aria-busy={controlPending}
              onClick={() => void onToggleInbox(!inboxEnabled)}
            >
              {inboxEnabled ? "已开启 · 停用" : "已停用 · 开启"}
            </button>
          </div>

          <div className={styles.lampRow} aria-label="Inbox 状态指示灯">
            <StatusPill
              label={INBOX_STATE_LABELS[inboxEffectiveState] || inboxEffectiveState}
              tone={
                inboxEffectiveState === "running"
                  ? "good"
                  : ["emergency", "frozen", "blocked"].includes(inboxEffectiveState)
                    ? "bad"
                    : "neutral"
              }
            />
            {inboxLastRestricted ? (
              <StatusPill
                label="限流退避记录"
                tone="amber"
                title={safeMessage(inboxLastRestricted.reason)}
              />
            ) : (
              <StatusPill label="读取通道正常" tone="good" />
            )}
            {authExpired && <StatusPill label="401 登录失效" tone="bad" />}
            {inboxPendingReview > 0 && (
              <StatusPill label={`待审核 ${inboxPendingReview}`} tone="amber" />
            )}
            {hasUncertain && (
              <StatusPill label={`待核对冻结 (${uncertainCount})`} tone="bad" />
            )}
          </div>

          <dl className={styles.countsGrid} aria-label="收件箱来信统计">
            <div className={styles.countCell}>
              <dt>评论回复 (comment_reply)</dt>
              <dd data-inbound-count="comment_reply">{inboundCounts.comment_reply}</dd>
            </div>
            <div className={styles.countCell}>
              <dt>@ 提及 (mention)</dt>
              <dd data-inbound-count="mention">{inboundCounts.mention}</dd>
            </div>
            <div className={styles.countCell}>
              <dt>自有内容评论 (own_content_comment)</dt>
              <dd data-inbound-count="own_content_comment">{inboundCounts.own_content_comment}</dd>
            </div>
            <div className={styles.countCell}>
              <dt>私信 (dm)</dt>
              <dd data-inbound-count="dm">{inboundCounts.dm}</dd>
            </div>
          </dl>

          <p className="hint">
            待处理事件 {inboxState?.pending_events ?? 0} 条 · 私信通道{" "}
            {inboxState?.dm_enabled ? "已开启" : "未开启"} ·{" "}
            <a href="#messages">进入私信工作台</a> · <a href="#comments">审核评论草稿</a>
          </p>

          <details>
            <summary>收件箱响应设置</summary>
            <form onSubmit={handleSaveInboxSettings}>
              <div className="checks">
                <label>
                  <input
                    type="checkbox"
                    checked={mentionsEnabled}
                    disabled={inboxSaving}
                    onChange={(e) => {
                      setMentionsEnabled(e.target.checked);
                      setInboxDirty(true);
                    }}
                  />
                  接收 @ 提及通知
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={repliesEnabled}
                    disabled={inboxSaving}
                    onChange={(e) => {
                      setRepliesEnabled(e.target.checked);
                      setInboxDirty(true);
                    }}
                  />
                  接收评论回复通知
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={mentionReplyEnabled}
                    disabled={inboxSaving}
                    onChange={(e) => {
                      setMentionReplyEnabled(e.target.checked);
                      setInboxDirty(true);
                    }}
                  />
                  自动生成 @/回复响应
                </label>
              </div>
              <label>
                通知轮询间隔（秒，60–86400）
                <input
                  type="number"
                  min={60}
                  max={86400}
                  value={notifInterval}
                  disabled={inboxSaving}
                  onChange={(e) => {
                    setNotifInterval(e.target.value);
                    setInboxDirty(true);
                  }}
                />
              </label>
              <div className="actions" style={{ marginTop: 8 }}>
                <button type="submit" disabled={inboxSaving}>
                  保存收件箱设置
                </button>
                {inboxNote && (
                  <span className="hint" role="status">
                    {inboxNote}
                  </span>
                )}
              </div>
            </form>
          </details>
        </article>

        {/* Card 2: Discovery Pipeline */}
        <article
          id="pipeline-discovery-card"
          className={[
            styles.pipelineCard,
            discoveryEnabled && !emergency && !discoveryReadPaused && !discoveryDeferred
              ? styles.pipelineCardActive
              : "",
            emergency ? styles.pipelineCardFrozen : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          <div className={styles.pipelineHeader}>
            <div className={styles.pipelineTitleWrap}>
              <h2>主动探索（Discovery）</h2>
              <span className="hint">自主刷视频、取材、评论区巡航与主动互动</span>
            </div>
            <button
              id="discovery-pipeline-toggle"
              type="button"
              role="switch"
              aria-checked={discoveryEnabled}
              aria-label="主动探索（Discovery）开关"
              className={styles.switchBtn}
              disabled={!status || emergency || transitioning || controlPending}
              aria-busy={controlPending}
              onClick={() => void onToggleDiscovery(!discoveryEnabled)}
            >
              {discoveryEnabled ? "已开启 · 停用" : "已停用 · 开启"}
            </button>
          </div>

          <div className={styles.lampRow} aria-label="Discovery 状态指示灯">
            <StatusPill
              label={DISCOVERY_PHASE_LABELS[discoveryEffectiveState] || discoveryEffectiveState}
              tone={
                ["discovering", "reading", "judging", "publishing", "running"].includes(
                  discoveryEffectiveState
                )
                  ? "good"
                  : ["emergency", "blocked"].includes(discoveryEffectiveState)
                    ? "bad"
                    : "neutral"
              }
            />
            {discoveryReadPaused && (
              <StatusPill
                label="读频退避中"
                tone="amber"
                title={safeMessage(discoveryBackoff?.reason)}
              />
            )}
            {discoveryDeferred && (
              <StatusPill
                label="额度预留暂缓"
                tone="amber"
                title={safeMessage(quotas?.discovery_deferral_reason)}
              />
            )}
            {status?.backlog?.discovery_paused && (
              <StatusPill label="楼头积压暂缓新视频" tone="amber" />
            )}
            {authExpired && <StatusPill label="401 登录失效" tone="bad" />}
            {hasUncertain && <StatusPill label="待核对冻结" tone="bad" />}
          </div>

          {discoveryDeferred && (
            <p className="feature-note" role="status" data-discovery-deferral="">
              主动探索已为直连互动预留发送额度而暂缓：
              {quotas?.discovery_deferral_reason || "等待直连预留窗口恢复"}
              {quotas?.discovery_retry_at
                ? `（预计 ${new Date(quotas.discovery_retry_at * 1000).toLocaleString("zh-CN")} 重试）`
                : ""}
            </p>
          )}

          {discoveryReadPaused && (
            <p className="hint" role="status" data-discovery-backoff="">
              读频退避中（优先保障收件箱与回执核对）：
              {safeMessage(discoveryBackoff?.reason) || "触发平台读频限制"}
              {discoveryBackoff?.until
                ? ` · 冷却至 ${new Date(discoveryBackoff.until * 1000).toLocaleString("zh-CN")}`
                : ""}
              {discoveryBackoff?.strikes ? ` · 第 ${discoveryBackoff.strikes} 次` : ""}
            </p>
          )}

          <p className="hint">
            {effectiveDiscovery?.active_round ? (
              <>
                运行轮次{" "}
                <a href={`#automation/round/${encodeURIComponent(effectiveDiscovery.active_round.id)}`}>
                  #{effectiveDiscovery.active_round.id.slice(0, 8)}
                </a>
                {effectiveDiscovery.current_video?.title
                  ? ` · 当前视频：${effectiveDiscovery.current_video.title}`
                  : ""}
              </>
            ) : effectiveDiscovery?.last_round ? (
              <>
                最近轮次{" "}
                <a href={`#automation/round/${encodeURIComponent(effectiveDiscovery.last_round.id)}`}>
                  #{effectiveDiscovery.last_round.id.slice(0, 8)}
                </a>{" "}
                · 已处理 {effectiveDiscovery.summary?.processed ?? 0} · 已发{" "}
                {effectiveDiscovery.sent ?? 0}
              </>
            ) : (
              "暂无主动探索轮次记录。"
            )}
            {effectiveDiscovery?.daily_watch && (
              <>
                {" "}
                · 今日观看 {effectiveDiscovery.daily_watch.started} /{" "}
                {effectiveDiscovery.daily_watch.limit === 0
                  ? "不限"
                  : effectiveDiscovery.daily_watch.limit}
              </>
            )}
          </p>

          <details>
            <summary>探索管道参数</summary>
            <form onSubmit={handleSaveDiscoveryQuick}>
              <label>
                每日观看视频上限（0 表示不限）
                <input
                  type="number"
                  min={0}
                  value={discoveryWatchLimit}
                  disabled={discoverySaving}
                  onChange={(e) => {
                    setDiscoveryWatchLimit(e.target.value);
                    setDiscoveryDirty(true);
                  }}
                />
              </label>
              <div className="actions" style={{ marginTop: 8 }}>
                <button type="submit" disabled={discoverySaving}>
                  保存探索参数
                </button>
                <a href="#discovery-source-form">配置发现来源 →</a>
                {discoveryNote && (
                  <span className="hint" role="status">
                    {discoveryNote}
                  </span>
                )}
              </div>
            </form>
          </details>
        </article>
      </div>

      {/* Shared Quota Reserve Control & Active Floor Leases */}
      <section className={`panel ${styles.quotaControlPanel}`} aria-label="配额预留与租约状态">
        <div className="panel-heading">
          <div>
            <h2 className="win-group-title" style={{ marginBottom: 4 }}>
              共享配额预留与并发租约
            </h2>
            <p className="hint" style={{ margin: 0 }}>
              {quotas
                ? `小时已用 ${quotas.used_per_hour} / ${quotas.max_per_hour === 0 ? "不限" : quotas.max_per_hour}（剩余 ${quotas.remaining_per_hour ?? "不限"}） · 24小时已用 ${quotas.used_per_day} / ${quotas.max_per_day === 0 ? "不限" : quotas.max_per_day}（剩余 ${quotas.remaining_per_day ?? "不限"}）`
                : "正在读取共享写操作配额…"}
            </p>
          </div>
          <StatusPill
            label={
              serverReserve === 0
                ? "0 · 不预留 / 共享额度"
                : `已预留 ${serverReserve} 条直连额度`
            }
            tone={serverReserve === 0 ? "neutral" : "blue"}
          />
        </div>

        <form className={styles.quotaFormRow} onSubmit={handleSaveReserve}>
          <label>
            直连互动预留发送额度（direct_quota_reserve）
            <input
              id="direct-quota-reserve-input"
              type="number"
              min={0}
              step={1}
              value={reserveInput}
              disabled={reserveSaving}
              onChange={(e) => {
                setReserveInput(e.target.value);
                setReserveDirty(true);
                setReserveNote("有未保存的配额修改。");
              }}
            />
            <small className="hint">填 0 表示不预留（探索与收件箱完全共享额度）</small>
          </label>
          <button
            id="save-direct-quota-reserve"
            type="submit"
            disabled={reserveSaving}
          >
            {reserveSaving ? "正在保存…" : "保存预留额度"}
          </button>
          {reserveNote && (
            <span id="direct-quota-reserve-note" className="hint" role="status">
              {reserveNote}
            </span>
          )}
        </form>

        <div>
          <strong style={{ fontSize: "0.9rem" }}>当前楼层／评论租约（operation_claims）：</strong>
          {claims.length === 0 ? (
            <span className="hint"> 当前无活跃楼层/评论租约。</span>
          ) : (
            <div className={styles.claimsList} style={{ marginTop: 6 }}>
              {claims.map((claim) => (
                <span key={claim.claim_key} className={styles.claimChip}>
                  <StatusPill
                    label={claim.owner_pipeline}
                    tone={claim.owner_pipeline === "inbox" ? "blue" : "neutral"}
                  />
                  <span>
                    {claim.claim_kind === "floor" ? "楼租约" : "评论租约"} · {claim.resource_id} /{" "}
                    {claim.root_id}
                    {claim.target_comment_id ? ` · #${claim.target_comment_id}` : ""}
                  </span>
                  <small className="hint">
                    至 {new Date(claim.lease_until * 1000).toLocaleTimeString("zh-CN", { hour12: false })}
                  </small>
                </span>
              ))}
            </div>
          )}
        </div>
      </section>
    </section>
  );
}
