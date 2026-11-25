import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/shared/api/client";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import { humanizeStatus } from "@/shared/lib/formatters";
import { contactLabel, contactReason, contactStatus } from "./messageLabels";
import styles from "./operations.module.css";
import {
  extractBrowsingIntentId,
  formatVisibilityDetail,
  getVisibilityMeta,
  sentLinkLabel,
  verifyIntentVisibility,
} from "./visibility";
import type {
  ActivityItem,
  ActivityKindFilter,
  ActivityResponse,
  ActivitySourceFilter,
  CommentActivityFilter,
} from "./types";

const PAGE_SIZE = 10;

const READ_LABELS: Record<string, string> = {
  complete: "完整阅读",
  fully_read: "完整阅读",
  full: "完整阅读",
  partial: "部分阅读",
  sampled_visual: "抽样画面",
  sampled_audio: "抽样语音",
  title_only: "仅看标题",
  comments_only: "只看评论",
  not_read: "未阅读",
  none: "未阅读",
  new: "尚未观看",
  started: "已开始观看",
  reading: "阅读中",
  viewed: "已看过",
};

const STATUS_LABELS: Record<string, string> = {
  read: "仅读",
  published: "已发送",
  publish_uncertain: "待核对",
  automatic_skipped: "未发送",
  failed: "发送失败",
  pending_review: "待审核",
  approved: "已保存",
  copied: "已复制",
  rejected: "已拒绝",
  private_handoff: "已转私信",
  received: "收到来信",
  queued: "排队生成中",
};

const COMMENT_OUTCOME_LABELS: Record<string, string> = {
  new_reply: "本轮新回",
  previous_reply: "此前已回",
  same_account: "同账号已回 · 归属未确认",
  pending_verification: "待核对",
};

const VIDEO_PREFERENCE_LABELS: Record<string, string> = {
  like: "喜欢",
  not_interested: "不感兴趣",
  neutral: "一般",
};

const SOURCE_BADGE_MAP: Record<string, { label: string; toneClass: string }> = {
  inbox: { label: "收件箱消息 inbox", toneClass: "blue" },
  discovery: { label: "主动探索 discovery", toneClass: "" },
  manual: { label: "手动 manual", toneClass: "amber" },
};

export function mainReplySummary(item: Partial<ActivityItem>): string {
  if ((item.main_replies ?? 0) > 0) return `今日已发主评 ${item.main_replies} 条`;
  if (item.has_main_reply) return "已有主评";
  const draft = item.replies?.find((reply) => reply.kind === "top_level");
  const labels: Record<string, string> = {
    pending_review: "主评待审核",
    approved: "主评已审核，待发布",
    publishing: "主评提交中",
    publish_uncertain: "主评结果待核对",
    failed: "主评发送失败",
    rejected: "主评已拒绝",
    copied: "主评已复制",
  };
  if (draft?.status && labels[draft.status]) return labels[draft.status];
  const action = item.main_decision?.action;
  if (action === "skip" || action === "finish") return "本次不发主评";
  if (action === "wait") return "主评等待继续";
  if (action === "reply_public") return "已决定主评，等待生成";
  if (item.read_state === "comments_only") return "只逛评论，未生成主评";
  return "主评尚未决定";
}

function buildCountsText(
  data: ActivityResponse | null,
  kind: ActivityKindFilter,
  roundId: string | null
): string {
  if (!data) return "";
  const counts = data.counts || data.summary || {};
  const parts =
    kind === "videos"
      ? [
          `当日视频 ${counts.videos ?? data.total ?? 0}`,
          `看过 ${counts.watched ?? 0}`,
          `回复 ${counts.replies ?? 0}`,
          `跳过 ${counts.skipped ?? 0}`,
        ]
      : kind === "comments"
        ? [
            `${roundId ? "本轮当日新发" : "当日新发"} 主评 ${counts.sent_top_level ?? 0}`,
            `楼内回复 ${counts.sent_replies ?? 0}`,
          ]
        : [
            `当日总计 ${counts.total ?? data.total ?? 0}`,
            `已发送 ${counts.sent ?? 0}`,
            `待审核 ${counts.pending ?? 0}`,
          ];

  if ((counts.visibility_issues ?? 0) > 0) {
    parts.push(`可见异常 ${counts.visibility_issues}`);
  }
  if ((counts.failed ?? 0) > 0) {
    parts.push(`异常 ${counts.failed}`);
  }
  return parts.join(" · ");
}

export interface OperationsActivityFeedProps {
  initialKind?: ActivityKindFilter;
  initialSource?: ActivitySourceFilter;
  roundId?: string | null;
  refreshTick?: number;
}

export function OperationsActivityFeed({
  initialKind = "all",
  initialSource = "all",
  roundId = null,
  refreshTick = 0,
}: OperationsActivityFeedProps) {
  const [kind, setKind] = useState<ActivityKindFilter>(initialKind);
  const [sourceFilter, setSourceFilter] = useState<ActivitySourceFilter>(initialSource);
  const [commentFilter, setCommentFilter] = useState<CommentActivityFilter>("replied");
  const [day, setDay] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<ActivityResponse | null>(null);
  const [feedback, setFeedback] = useState("正在读取活动记录…");
  const [loading, setLoading] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());
  const [recheckingIds, setRecheckingIds] = useState<Set<string>>(() => new Set());

  const requestSeqRef = useRef(0);
  const displayedQueryRef = useRef("");
  const prevRoundRef = useRef<string | null>(roundId);

  useEffect(() => {
    setKind(initialKind);
    setPage(1);
  }, [initialKind]);

  useEffect(() => {
    if (prevRoundRef.current !== roundId) {
      prevRoundRef.current = roundId;
      if (roundId) {
        setCommentFilter("replied");
        setKind("comments");
      }
      setPage(1);
    }
  }, [roundId]);

  const loadActivity = useCallback(
    async (
      targetPage = page,
      targetKind = kind,
      targetSource = sourceFilter,
      targetCommentFilter = commentFilter,
      targetDay = day,
      targetRoundId = roundId
    ) => {
      const query: Record<string, string | number> = {
        page: targetPage,
        page_size: PAGE_SIZE,
        kind: targetKind,
        source: targetSource,
      };
      if (targetRoundId) query.round_id = targetRoundId;
      else if (targetDay) query.day = targetDay;
      if (targetKind === "comments") query.comment_filter = targetCommentFilter;

      const queryKey = JSON.stringify(query);
      const seq = ++requestSeqRef.current;

      if (displayedQueryRef.current !== queryKey) {
        setLoading(true);
        setFeedback("正在读取活动记录…");
      }

      try {
        const result = await apiGet<"/api/v1/operations/activity", ActivityResponse>(
          "/api/v1/operations/activity",
          { query }
        );
        if (seq !== requestSeqRef.current) return;

        if (
          targetKind === "comments" &&
          (result.comment_filter !== targetCommentFilter ||
            !Number.isInteger(result.counts?.sent_top_level) ||
            !Number.isInteger(result.counts?.sent_replies))
        ) {
          setFeedback("当前工作台后端未应用评论筛选。更新并重启工作台后再刷新。");
          setLoading(false);
          return;
        }

        displayedQueryRef.current = queryKey;
        setData(result);
        setPage(result.page || targetPage);
        if (!targetRoundId && result.day && targetDay !== result.day && targetDay === "") {
          // Keep date input showing backend day cleanly without retriggering load
          setDay(result.day);
        }
        setFeedback("");
      } catch (err) {
        if (seq === requestSeqRef.current) {
          setFeedback(`记录读取失败：${errorMessage(err)}`);
        }
      } finally {
        if (seq === requestSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [page, kind, sourceFilter, commentFilter, day, roundId]
  );

  useEffect(() => {
    void loadActivity(page, kind, sourceFilter, commentFilter, day, roundId);
  }, [page, kind, sourceFilter, commentFilter, day, roundId, refreshTick, loadActivity]);

  const handleRecheckVisibility = useCallback(
    async (intentId: string) => {
      setRecheckingIds((prev) => {
        const next = new Set(prev);
        next.add(intentId);
        return next;
      });
      try {
        await verifyIntentVisibility(intentId);
        await loadActivity(page, kind, sourceFilter, commentFilter, day, roundId);
      } catch (err) {
        setFeedback(`可见性复查失败：${errorMessage(err)}`);
      } finally {
        setRecheckingIds((prev) => {
          const next = new Set(prev);
          next.delete(intentId);
          return next;
        });
      }
    },
    [page, kind, sourceFilter, commentFilter, day, roundId, loadActivity]
  );

  const totalItems = data?.total ?? 0;
  const totalPages = data?.pages || Math.ceil(totalItems / PAGE_SIZE) || 1;
  const currentPage = data?.page || page;
  const items = data?.items || [];
  const countsText = buildCountsText(data, kind, roundId);

  return (
    <section id="operation-activity-history" className={`panel ${styles.activitySection}`}>
      <div className="panel-heading">
        <h2>运营活动</h2>
        <div className="actions">
          <label className={styles.dayLabel}>
            日期{" "}
            <input
              id="discovery-history-day"
              type="date"
              value={day}
              disabled={Boolean(roundId)}
              onChange={(event) => {
                setDay(event.target.value);
                setPage(1);
              }}
            />
          </label>
          <button
            id="discovery-history-today"
            type="button"
            disabled={Boolean(roundId)}
            onClick={() => {
              setDay("");
              setPage(1);
              void loadActivity(1, kind, sourceFilter, commentFilter, "", roundId);
            }}
          >
            今天
          </button>
          <button
            id="discovery-history-refresh"
            type="button"
            onClick={() => void loadActivity()}
          >
            刷新
          </button>
        </div>
      </div>

      {/* Stage 2 Source Filter */}
      <div className={styles.filterRow}>
        <div
          id="operation-activity-sources"
          className={styles.filterGroup}
          role="group"
          aria-label="来源管道"
        >
          {(
            [
              ["all", "全部来源"],
              ["discovery", "主动探索 discovery"],
              ["inbox", "收件箱消息 inbox"],
              ["manual", "手动 manual"],
            ] as const
          ).map(([val, label]) => (
            <button
              key={val}
              type="button"
              data-activity-source={val}
              aria-pressed={sourceFilter === val}
              onClick={() => {
                setSourceFilter(val);
                setPage(1);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Kind Filter */}
      <div className={styles.filterRow}>
        <div
          id="operation-activity-kinds"
          className={styles.filterGroup}
          role="group"
          aria-label="活动类型"
        >
          {(
            [
              ["all", "全部"],
              ["videos", "视频"],
              ["comments", "评论"],
              ["messages", "私信"],
              ["dynamics", "动态"],
            ] as const
          ).map(([val, label]) => (
            <button
              key={val}
              type="button"
              data-activity-kind={val}
              aria-pressed={kind === val}
              onClick={() => {
                setKind(val);
                setPage(1);
              }}
            >
              {label}
            </button>
          ))}
          <a href="#automation/issues" className="button quiet">
            异常
          </a>
        </div>

        {kind === "comments" && (
          <label id="comment-activity-filter-control" className={styles.dayLabel}>
            评论结果
            <select
              id="comment-activity-filter"
              value={commentFilter}
              onChange={(event) => {
                setCommentFilter(event.target.value as CommentActivityFilter);
                setPage(1);
              }}
            >
              <option value="replied">回复</option>
              <option value="read">仅读</option>
              <option value="pending_review">待审核</option>
              <option value="all">全部</option>
            </select>
          </label>
        )}
      </div>

      {countsText && (
        <p id="discovery-history-counts" className="hint">
          {countsText}
        </p>
      )}
      {feedback && (
        <p
          id="discovery-history-feedback"
          className={loading ? "hint" : "error-text"}
          role="status"
        >
          {feedback}
        </p>
      )}

      <div id="discovery-activity-list">
        {items.length === 0 ? (
          <p className="hint">{loading ? "正在读取活动记录…" : "这一天还没有匹配的运营活动。"}</p>
        ) : (
          items.map((item) => {
            const itemKind = item.kind || "video";
            const rowKey = `${data?.day || day}/${kind}/${item.id || `${itemKind}/${item.bvid || item.local_id}`}`;
            const names: Record<string, string> = {
              comment:
                item.reply_kind === "top_level" || item.comment_kind === "top_level"
                  ? "视频主评"
                  : "评论回复",
              message: contactLabel(item),
              dynamic: "动态",
            };
            const label =
              itemKind === "video"
                ? item.title || item.bvid || "视频"
                : item.title || [names[itemKind], item.recipient_name].filter(Boolean).join(" · ");

            const video = itemKind === "comment" ? item.bvid : null;
            const dynamicId =
              itemKind === "comment" && !video && item.thread?.resource?.startsWith("dynamic:")
                ? item.thread.resource.slice("dynamic:".length)
                : null;
            const commentHref = itemKind === "comment" ? item.source_href || item.sent_href : null;
            const href =
              commentHref ||
              (video
                ? `https://www.bilibili.com/video/${encodeURIComponent(video)}`
                : dynamicId
                  ? `https://t.bilibili.com/${encodeURIComponent(dynamicId)}`
                  : item.href ||
                    (itemKind === "video" && item.bvid
                      ? `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}`
                      : null));
            const titleText = video ? item.video_title || video : dynamicId ? "原动态" : label;
            const isExternal = Boolean(href && /^https?:\/\//i.test(href));

            const readText =
              item.fully_read === true
                ? "完整阅读"
                : item.fully_read === false &&
                    ["complete", "full", "fully_read"].includes(item.read_state || "")
                  ? "已看过"
                  : (item.read_state && READ_LABELS[item.read_state]) ||
                    (item.watched ? "已看过" : "未阅读");
            const resultText =
              (item.comment_outcome && COMMENT_OUTCOME_LABELS[item.comment_outcome]) ||
              (itemKind === "message" ? contactStatus(item) : undefined) ||
              (item.status && STATUS_LABELS[item.status]) ||
              humanizeStatus(item.status || "running").label;
            const statusText = itemKind === "video" ? readText : resultText;

            const pipeline = item.source_pipeline || item.source || "";
            const sourceBadge = pipeline ? SOURCE_BADGE_MAP[pipeline] : undefined;

            const preference = item.video_preference
              ? VIDEO_PREFERENCE_LABELS[item.video_preference]
              : undefined;
            const reasons = [
              preference
                ? `偏好：${preference}${item.preference_reason ? ` · ${item.preference_reason}` : ""}`
                : "",
              item.reason && (!preference || item.reason !== item.preference_reason)
                ? itemKind === "message"
                  ? contactReason(item.reason)
                  : item.reason
                : "",
            ]
              .filter(Boolean)
              .join("；");

            const ownership =
              itemKind === "comment"
                ? [
                    item.own_resource === true ? (item.bvid ? "自有投稿" : "自有动态") : "",
                    item.own_thread === true
                      ? "号主的楼"
                      : item.own_thread === false
                        ? "观众的楼"
                        : "",
                  ].filter(Boolean)
                : [];

            const commentType = item.comment_kind || item.reply_kind;
            const commentTypeBadge =
              commentType === "top_level"
                ? "主评"
                : commentType === "reply"
                  ? "楼内回复"
                  : "评论楼";

            const bodyText =
              item.account_reply_text ||
              item.confirmed_reply_text ||
              item.text ||
              item.content ||
              item.source_text ||
              (item.status === "private_handoff"
                ? "在私信工作台查看"
                : item.status === "pending_review" || item.status === "queued"
                  ? "尚未生成回复正文"
                  : "正文未保留");

            const isDetailsOpen = expandedKeys.has(rowKey);
            const visMeta =
              itemKind === "comment"
                ? getVisibilityMeta(item.visibility) ||
                  (item.sent_href ? getVisibilityMeta("not_checked") : null)
                : null;
            const visDetailText =
              itemKind === "comment"
                ? formatVisibilityDetail(item.visibility_detail, item.visibility_checked_at)
                : "";
            const verifyIntentId = itemKind === "comment" ? extractBrowsingIntentId(item) : null;
            const isRechecking = Boolean(verifyIntentId && recheckingIds.has(verifyIntentId));

            return (
              <article
                key={rowKey}
                className={styles.activityRow}
                data-activity-key={rowKey}
                data-source-pipeline={pipeline || undefined}
              >
                <div className={styles.activityHeading}>
                  {href ? (
                    <a
                      href={href}
                      data-activity-control="title"
                      target={isExternal ? "_blank" : undefined}
                      rel={isExternal ? "noreferrer" : undefined}
                    >
                      {titleText}
                    </a>
                  ) : (
                    <strong data-activity-control="title">{titleText}</strong>
                  )}

                  <span className={styles.activityBadges}>
                    {sourceBadge && (
                      <span
                        className={`badge ${sourceBadge.toneClass}`.trim()}
                        data-source-badge={pipeline}
                      >
                        {sourceBadge.label}
                      </span>
                    )}
                    {itemKind === "comment" && <span className="badge">{commentTypeBadge}</span>}
                    <span className="badge">{statusText}</span>
                    {visMeta && (
                      <span
                        className={`badge ${visMeta.badgeClass}`.trim()}
                        data-visibility-badge={visMeta.code}
                        data-visibility-tone={visMeta.tone}
                      >
                        {visMeta.label}
                      </span>
                    )}
                  </span>
                </div>

                {reasons && <p className={styles.activityReason}>{safeMessage(reasons)}</p>}

                <div className={styles.activityReplies}>
                  {itemKind === "video" ? (
                    <>
                      <span className="hint">
                        {mainReplySummary(item)}
                        {(item.comment_replies ?? 0) > 0
                          ? ` · 今日跟帖 ${item.comment_replies} 条`
                          : ""}
                      </span>
                      {kind === "videos" &&
                        (item.replies || []).map((reply, idx) => {
                          const outcome = STATUS_LABELS[reply.status] || reply.status;
                          const replyVis = getVisibilityMeta(reply.visibility);
                          const target =
                            reply.recipient_name ||
                            (reply.kind === "top_level" ? "视频评论区" : "原评论作者");
                          return (
                            <div key={reply.id || idx} className={styles.replyRow}>
                              <span className="hint">
                                回复 {target} · {outcome}
                                {replyVis ? ` · ${replyVis.label}` : ""}
                              </span>
                              <p>{reply.text || "正文未保留"}</p>
                            </div>
                          );
                        })}
                    </>
                  ) : (
                    <>
                      <p>{bodyText}</p>
                      {itemKind === "comment" && (
                        <>
                          {ownership.length > 0 && (
                            <small className="hint">{ownership.join(" · ")}</small>
                          )}
                          {item.source_href && (
                            <>
                              {" "}
                              <a
                                href={item.source_href}
                                className="win-link-btn"
                                target="_blank"
                                rel="noreferrer"
                              >
                                原评论
                              </a>
                            </>
                          )}
                          {item.sent_href && (
                            <>
                              {" "}
                              <a
                                href={item.sent_href}
                                className="win-link-btn"
                                data-sent-link-visibility={item.visibility || "not_checked"}
                                target="_blank"
                                rel="noreferrer"
                              >
                                {sentLinkLabel(item.visibility)}
                              </a>
                            </>
                          )}
                          {verifyIntentId && (
                            <>
                              {" "}
                              <button
                                type="button"
                                className="win-link-btn"
                                data-activity-control="recheck-visibility"
                                disabled={isRechecking}
                                onClick={() => void handleRecheckVisibility(verifyIntentId)}
                              >
                                {isRechecking ? "复查中…" : "复查可见性"}
                              </button>
                            </>
                          )}
                        </>
                      )}
                    </>
                  )}
                </div>

                <details
                  className={styles.activityDetails}
                  open={isDetailsOpen}
                  onToggle={(event) => {
                    const nextOpen = event.currentTarget.open;
                    setExpandedKeys((prev) => {
                      if (prev.has(rowKey) === nextOpen) return prev;
                      const next = new Set(prev);
                      if (nextOpen) next.add(rowKey);
                      else next.delete(rowKey);
                      return next;
                    });
                  }}
                >
                  <summary data-activity-control="summary">处理详情</summary>
                  {itemKind === "comment" && (video || dynamicId) && (
                    <p>
                      {item.href?.startsWith("#comments/") ? (
                        <a href={item.href}>{label}</a>
                      ) : (
                        <span>{label}</span>
                      )}
                    </p>
                  )}
                  {visMeta && (
                    <p className={visMeta.isIssue ? "error-text" : "hint"} data-visibility-detail="">
                      平台可见性：{visMeta.label}
                      {visDetailText ? ` · ${safeMessage(visDetailText)}` : ""}
                    </p>
                  )}
                  {reasons && <p>{safeMessage(reasons)}</p>}
                  <p className="hint">
                    {item.bvid || names[itemKind] || "活动"} · {resultText}
                    {item.updated_at
                      ? ` · ${new Date(item.updated_at * 1000).toLocaleString("zh-CN")}`
                      : ""}
                  </p>
                  {item.error && (
                    <p className="error-text">
                      {safeMessage(item.error)}
                      {(item.occurrences ?? 0) > 1 ? `（记录 ${item.occurrences} 次）` : ""}
                    </p>
                  )}
                  {item.round_id && (
                    <a
                      href={`#automation/round/${encodeURIComponent(item.round_id)}`}
                      data-activity-control="source"
                    >
                      查看来源记录
                    </a>
                  )}
                </details>
              </article>
            );
          })
        )}
      </div>

      <div className={`actions ${styles.historyPagination}`}>
        <button
          id="discovery-history-prev"
          type="button"
          disabled={currentPage <= 1}
          onClick={() => setPage(Math.max(1, currentPage - 1))}
        >
          上一页
        </button>
        <span id="discovery-history-page" className="hint">
          第 {currentPage} / {totalPages} 页 · 共 {totalItems} 条记录
        </span>
        <button
          id="discovery-history-next"
          type="button"
          disabled={currentPage >= totalPages}
          onClick={() => setPage(currentPage + 1)}
        >
          下一页
        </button>
      </div>
    </section>
  );
}
