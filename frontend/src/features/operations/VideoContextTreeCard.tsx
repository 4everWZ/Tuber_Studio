import { useState } from "react";
import { safeMessage } from "@/shared/lib/errors";
import { illustrationStatus, illustrationUrl } from "@/shared/lib/operationImage";
import styles from "./operations.module.css";
import type {
  ActivityItem,
  BrowsingIntent,
  BrowsingTask,
  CommentRecord,
  QueueDetail,
  QueueRow,
} from "./types";

export interface FloorTurnEntry {
  id: string;
  author: string;
  role: "viewer" | "uploader" | "owner_takeover" | "aqua" | "draft";
  roleLabel: string;
  text: string;
  isTarget?: boolean;
}

export interface FloorBranchNode {
  branchKey: string;
  rootId: string | null;
  isTopLevel: boolean;
  title: string;
  hasOwnerTakeover: boolean;
  turns: FloorTurnEntry[];
  rows: QueueRow[];
}

export interface VideoBaseMeta {
  bvid: string;
  title: string;
  videoUrl: string;
  uploaderName?: string;
  uploaderUid?: string;
  uploaderUrl?: string;
  coverageBadges: string[];
  firstImpression?: string;
}

export interface VideoContextTreeGroup {
  video: VideoBaseMeta;
  branches: FloorBranchNode[];
}

export interface VideoContextTreeData {
  videos: VideoContextTreeGroup[];
  orphans: QueueRow[];
}

const SOURCE_LABELS: Record<string, string> = {
  manual: "手动",
  interaction: "公开互动",
  discovery: "自主浏览",
};

const QUEUE_STATUS_LABELS: Record<string, string> = {
  pending_review: "待审核",
  approved: "已审核 · 待提交",
  publish_uncertain: "待核对",
  publishing: "提交中",
  published: "平台已受理",
  rejected: "已拒绝",
  copied: "已复制",
  operator_replied: "人工接管已回复",
  private_handoff: "已转私信",
  pending: "排队中",
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  partial: "部分完成",
  cancelled: "已取消",
  send_queued: "已授权待发",
  send_waiting: "等额度或恢复",
  send_blocked: "发布被拒／待处理",
  rejected_by_platform: "平台已拒绝",
  source_changed: "原楼已失效或变化",
  unverified: "原楼暂时无法核验",
  ready: "可排队",
};

const MATERIAL_MODE_LABELS: Record<string, string> = {
  subtitle_visual: "字幕 + 画面帧",
  subtitle: "字幕",
  visual: "画面帧",
  audio: "ASR 语音转写",
  unavailable: "取材失败",
};

const COVERAGE_LABELS: Record<string, string> = {
  all_available_cc: "全分P可用字幕",
  partial: "部分分P取材",
  sampled_visual: "抽样画面帧",
};

const READ_STATE_LABELS: Record<string, string> = {
  complete: "完整阅读",
  fully_read: "完整阅读",
  partial: "部分阅读",
  sampled_visual: "抽样画面",
  sampled_audio: "ASR 语音",
  comments_only: "仅评论区",
  viewed: "已观看",
};

function keyOf(row: { channel: string; id: string | number }): string {
  return `${row.channel}/${row.id}`;
}

function safeOwnerName(task: BrowsingTask | undefined): { name?: string; uid?: string } {
  if (!task) return {};
  const meta = (task.metadata || {}) as Record<string, unknown>;
  const payload = (task.payload || {}) as Record<string, unknown>;
  const rawOwner = meta.owner ?? payload.owner ?? (meta.author as unknown);
  let name = "";
  let uid = meta.owner_uid != null ? String(meta.owner_uid) : "";
  if (typeof rawOwner === "string") {
    name = rawOwner.trim();
  } else if (rawOwner && typeof rawOwner === "object") {
    const rec = rawOwner as { name?: unknown; mid?: unknown; uid?: unknown };
    if (typeof rec.name === "string") name = rec.name.trim();
    if (!uid && (rec.mid != null || rec.uid != null)) uid = String(rec.mid ?? rec.uid);
  }
  return {
    name: name || undefined,
    uid: uid && /^\d+$/.test(uid) ? uid : undefined,
  };
}

function deriveCoverageBadges(
  task: BrowsingTask | undefined,
  activityItem: ActivityItem | undefined
): string[] {
  const badges: string[] = [];
  const push = (label: string | undefined) => {
    if (label && !badges.includes(label)) badges.push(label);
  };

  if (task) {
    const tracks = task.subtitle_tracks || [];
    const hasHumanCc = tracks.some((t) => t.is_ai === false);
    const hasAiCc = tracks.some((t) => t.is_ai === true);
    if (hasHumanCc) push("CC 字幕");
    else if (hasAiCc) push("AI 字幕");

    if (task.material_mode && MATERIAL_MODE_LABELS[task.material_mode]) {
      push(MATERIAL_MODE_LABELS[task.material_mode]);
    } else if (tracks.length > 0) {
      push("字幕");
    }

    const hasFrames = (task.evidence || []).some((seg) => seg.id?.startsWith("frame:"));
    if (hasFrames) push("画面帧");

    if (task.coverage && COVERAGE_LABELS[task.coverage]) {
      push(COVERAGE_LABELS[task.coverage]);
    }
  }

  if (activityItem) {
    if (activityItem.fully_read) push("完整阅读");
    else if (activityItem.read_state && READ_STATE_LABELS[activityItem.read_state]) {
      push(READ_STATE_LABELS[activityItem.read_state]);
    } else if (activityItem.watched && badges.length === 0) {
      push("已观看");
    }
  }

  return badges;
}

function deriveFirstImpression(
  task: BrowsingTask | undefined,
  activityItem: ActivityItem | undefined
): string | undefined {
  const candidates = [
    task?.explanation,
    task?.decisions?.[0]?.explanation,
    activityItem?.preference_reason,
    activityItem?.reason,
    task?.reason,
  ];
  for (const cand of candidates) {
    const cleaned = typeof cand === "string" ? cand.trim() : "";
    if (cleaned) return cleaned;
  }
  return undefined;
}

interface SourceDiscussionNode {
  comment_id?: string | number;
  author_uid?: string | number;
  author_name?: string;
  text?: string;
}

interface AccountReplyNode {
  comment_id?: string | number;
  author_uid?: string | number;
  author_name?: string;
  text?: string;
}

export interface BuildTreeParams {
  rows: QueueRow[];
  tasks?: BrowsingTask[];
  intents?: BrowsingIntent[];
  activities?: ActivityItem[];
  comments?: CommentRecord[];
  detailsByKey?: Record<string, QueueDetail>;
}

export function buildVideoContextTree({
  rows,
  tasks = [],
  intents = [],
  activities = [],
  comments = [],
  detailsByKey = {},
}: BuildTreeParams): VideoContextTreeData {
  const taskByBvid = new Map<string, BrowsingTask>();
  for (const task of tasks) {
    const bvid = task.bvid || (task.metadata?.bvid as string | undefined);
    if (bvid && !taskByBvid.has(bvid)) {
      taskByBvid.set(bvid, task);
    }
  }

  const activityByBvid = new Map<string, ActivityItem>();
  for (const act of activities) {
    if (act.bvid && (act.kind === "video" || !activityByBvid.has(act.bvid))) {
      activityByBvid.set(act.bvid, act);
    }
  }

  const intentById = new Map<string, BrowsingIntent>();
  for (const intent of intents) {
    intentById.set(intent.id, intent);
  }

  const commentById = new Map<number, CommentRecord>();
  const commentsByThread = new Map<string, CommentRecord[]>();
  for (const comment of comments) {
    commentById.set(comment.id, comment);
    if (comment.video_id) {
      const rootId = comment.thread_id || comment.comment_id;
      const tKey = `${comment.video_id}::${rootId}`;
      const list = commentsByThread.get(tKey) || [];
      list.push(comment);
      commentsByThread.set(tKey, list);
    }
  }

  const videoGroupsMap = new Map<string, {
    video: VideoBaseMeta;
    branchesMap: Map<string, FloorBranchNode>;
  }>();
  const orphans: QueueRow[] = [];

  for (const row of rows) {
    if (row.channel === "jobs") {
      orphans.push(row);
      continue;
    }

    const rowKey = keyOf(row);
    const detail = detailsByKey[rowKey];
    const intent = row.channel === "browsing" ? intentById.get(row.id) : undefined;
    const comment =
      row.channel === "comments" && row.comment_id != null
        ? commentById.get(row.comment_id)
        : undefined;

    const target = (detail?.target || intent?.target || {}) as Record<string, unknown>;
    const bvid =
      row.video_id ||
      detail?.video_id ||
      (typeof target.bvid === "string" ? target.bvid : undefined) ||
      intent?.bvid ||
      comment?.video_id ||
      "";

    if (!bvid || !/^BV[0-9A-Za-z]{4,}$/.test(bvid)) {
      orphans.push(row);
      continue;
    }

    let group = videoGroupsMap.get(bvid);
    if (!group) {
      const task = taskByBvid.get(bvid);
      const act = activityByBvid.get(bvid);
      const owner = safeOwnerName(task);
      const metaTitle =
        task?.title ||
        task?.metadata?.title ||
        act?.title ||
        (typeof target.title === "string" ? target.title : undefined) ||
        bvid;
      const uploaderUid =
        owner.uid ||
        (act?.thread && "resource_author_uid" in act.thread
          ? String((act.thread as { resource_author_uid?: unknown }).resource_author_uid || "")
          : undefined);

      group = {
        video: {
          bvid,
          title: metaTitle,
          videoUrl: `https://www.bilibili.com/video/${encodeURIComponent(bvid)}`,
          uploaderName: owner.name,
          uploaderUid: uploaderUid || undefined,
          uploaderUrl: uploaderUid
            ? `https://space.bilibili.com/${encodeURIComponent(uploaderUid)}`
            : undefined,
          coverageBadges: deriveCoverageBadges(task, act),
          firstImpression: deriveFirstImpression(task, act),
        },
        branchesMap: new Map(),
      };
      videoGroupsMap.set(bvid, group);
    }

    const isTopLevel = intent?.kind === "top_level";
    const resolvedRootId = isTopLevel
      ? null
      : (detail?.thread_id ||
          (target.root != null ? String(target.root) : null) ||
          comment?.thread_id ||
          comment?.comment_id ||
          (target.parent != null ? String(target.parent) : null) ||
          detail?.platform_comment_id ||
          null);

    const branchKey = isTopLevel
      ? "top_level"
      : resolvedRootId
        ? `thread:${resolvedRootId}`
        : `row:${rowKey}`;

    let branch = group.branchesMap.get(branchKey);
    if (!branch) {
      const turns: FloorTurnEntry[] = [];
      const seenTexts = new Set<string>();
      let hasOwnerTakeover = false;

      const pushTurn = (entry: FloorTurnEntry) => {
        const sig = `${entry.role}:${entry.author}:${entry.text}`;
        if (!entry.text.trim() || seenTexts.has(sig)) return;
        seenTexts.add(sig);
        if (entry.role === "owner_takeover") hasOwnerTakeover = true;
        turns.push(entry);
      };

      // 1. Imported/thread comment records in this floor
      if (resolvedRootId) {
        const threadComments = (commentsByThread.get(`${bvid}::${resolvedRootId}`) || []).slice();
        threadComments.sort((a, b) => a.id - b.id);
        for (const item of threadComments) {
          const takeover =
            item.draft?.status === "operator_replied" ||
            (Boolean(row.account_id) &&
              item.platform_user_id === row.account_id &&
              item.draft?.status !== "published");
          pushTurn({
            id: `c-${item.id}`,
            author: takeover ? "号主人工接管" : item.display_name || "评论者",
            role: takeover ? "owner_takeover" : "viewer",
            roleLabel: takeover ? "人工接管" : "评论",
            text: item.content,
            isTarget: comment?.id === item.id,
          });
        }
      }

      // 2. Source discussion snapshot from browsing intent target
      const discussion = target.source_discussion as
        | { parent?: SourceDiscussionNode; source?: SourceDiscussionNode }
        | undefined;
      if (discussion?.parent?.text) {
        const parentAuthor = discussion.parent.author_name || "楼内前序评论";
        const isTakeover =
          Boolean(row.account_id) &&
          String(discussion.parent.author_uid || "") === String(row.account_id);
        pushTurn({
          id: `disc-parent-${discussion.parent.comment_id ?? rowKey}`,
          author: isTakeover ? "号主人工接管" : parentAuthor,
          role: isTakeover ? "owner_takeover" : "viewer",
          roleLabel: isTakeover ? "人工接管" : "原楼",
          text: discussion.parent.text,
        });
      }
      if (discussion?.source?.text) {
        pushTurn({
          id: `disc-source-${discussion.source.comment_id ?? rowKey}`,
          author: discussion.source.author_name || detail?.author || "观众",
          role: "viewer",
          roleLabel: "待回复",
          text: discussion.source.text,
          isTarget: true,
        });
      }

      // 3. Account manual takeover reply observed on floor
      const accountReply = target.account_reply as AccountReplyNode | undefined;
      if (accountReply?.text) {
        pushTurn({
          id: `takeover-${accountReply.comment_id ?? rowKey}`,
          author: accountReply.author_name || "号主人工接管",
          role: "owner_takeover",
          roleLabel: "人工接管",
          text: accountReply.text,
        });
      } else if (row.status === "operator_replied") {
        hasOwnerTakeover = true;
      }

      // 4. Fallback source comment text if neither threadComments nor discussion supplied it
      const fallbackSourceText =
        detail?.source_text ||
        (typeof target.source_text === "string" ? target.source_text : "") ||
        comment?.content ||
        (!isTopLevel ? row.excerpt || "" : "");
      if (fallbackSourceText && !isTopLevel && turns.length === 0) {
        const author =
          detail?.author ||
          (typeof target.recipient_name === "string" ? target.recipient_name : "") ||
          comment?.display_name ||
          "观众";
        pushTurn({
          id: `src-${rowKey}`,
          author,
          role: "viewer",
          roleLabel: "评论",
          text: fallbackSourceText,
          isTarget: true,
        });
      }

      // 5. Drafted Aqua reply
      const draftText = detail?.text || intent?.text || comment?.draft?.text || "";
      if (draftText) {
        const isSent = row.status === "published";
        pushTurn({
          id: `draft-${rowKey}`,
          author: "Aqua",
          role: isSent ? "aqua" : "draft",
          roleLabel: isSent ? "Aqua 已发" : "Aqua 草稿",
          text: draftText,
        });
      }

      const branchTitle = isTopLevel
        ? "视频主评"
        : resolvedRootId
          ? `楼 #${resolvedRootId}`
          : "评论回复";

      branch = {
        branchKey,
        rootId: resolvedRootId,
        isTopLevel,
        title: branchTitle,
        hasOwnerTakeover,
        turns,
        rows: [],
      };
      group.branchesMap.set(branchKey, branch);
    }

    branch.rows.push(row);
  }

  const videos: VideoContextTreeGroup[] = [];
  for (const { video, branchesMap } of videoGroupsMap.values()) {
    videos.push({
      video,
      branches: [...branchesMap.values()],
    });
  }

  return { videos, orphans };
}

export interface VideoContextTreeCardProps {
  tree: VideoContextTreeData;
  view: string;
  selectedKey: string | null;
  checkedDrafts: Map<string, QueueRow>;
  busy?: boolean;
  selectionLoading?: boolean;
  onSelectRow: (row: QueueRow) => void | Promise<void>;
  onToggleCheck: (row: QueueRow, checked: boolean) => void;
  onQuickApprove?: (row: QueueRow) => void | Promise<void>;
  onQuickReject?: (row: QueueRow) => void | Promise<void>;
}

export function VideoContextTreeCard({
  tree,
  view,
  selectedKey,
  checkedDrafts,
  busy = false,
  selectionLoading = false,
  onSelectRow,
  onToggleCheck,
  onQuickApprove,
  onQuickReject,
}: VideoContextTreeCardProps) {
  // Keep video bases expanded by default so branches & batch checkboxes remain immediately accessible
  const [collapsedVideos, setCollapsedVideos] = useState<Set<string>>(() => new Set());

  function toggleVideoExpand(bvid: string) {
    setCollapsedVideos((prev) => {
      const next = new Set(prev);
      if (next.has(bvid)) next.delete(bvid);
      else next.add(bvid);
      return next;
    });
  }

  function renderQueueLink(row: QueueRow) {
    const id = keyOf(row);
    const isCurrent = id === selectedKey;
    const isChecked = checkedDrafts.has(id);
    const illUrl = row.illustration ? illustrationUrl(row.channel, row.id, row.illustration) : "";
    const excerpt =
      (row.excerpt || QUEUE_STATUS_LABELS[row.status] || "评论记录").slice(0, 80) +
      ((row.excerpt?.length || 0) > 80 ? "…" : "");
    const quickEditable =
      view === "review" &&
      row.channel !== "jobs" &&
      ["pending_review", "approved"].includes(row.status);

    const linkEl = (
      <a
        href={`#comments/${view}/${id}`}
        data-queue-key={id}
        className={styles.queueRow}
        aria-current={isCurrent ? "true" : undefined}
        onClick={(event) => {
          if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
            return;
          }
          event.preventDefault();
          void onSelectRow(row);
        }}
      >
        {row.illustration &&
          (illUrl ? (
            <img src={illUrl} alt="回复配图" width={52} height={52} />
          ) : (
            <small>{illustrationStatus(row.illustration)}</small>
          ))}
        <span className="hint">
          {row.source ? SOURCE_LABELS[row.source] || row.source : "来源"} ·{" "}
          {QUEUE_STATUS_LABELS[row.status] || row.status}
        </span>
        <strong>{excerpt}</strong>
        {row.send_error && <small className="error-text">{safeMessage(row.send_error)}</small>}
        {row.status === "send_waiting" && row.send_wake_at && (
          <small className="hint">
            预计 {new Date(row.send_wake_at * 1000).toLocaleString("zh-CN")} 恢复
          </small>
        )}
      </a>
    );

    return (
      <div key={id} className={styles.branchRowWrap}>
        {view === "review" && row.channel !== "jobs" ? (
          <div className={styles.queueCheckRow} data-queue-key={id}>
            <label>
              <input
                type="checkbox"
                data-queue-key={id}
                aria-label={`选择草稿：${(row.excerpt || row.video_id || "评论草稿").slice(0, 80)}`}
                checked={isChecked}
                disabled={busy || selectionLoading}
                onChange={(e) => onToggleCheck(row, e.target.checked)}
              />
            </label>
            {linkEl}
          </div>
        ) : (
          <div>{linkEl}</div>
        )}

        {quickEditable && (onQuickApprove || onQuickReject) && (
          <div className={styles.branchQuickActions}>
            {onQuickApprove && (
              <button
                type="button"
                className={styles.branchActionBtn}
                disabled={busy}
                onClick={() => void onQuickApprove(row)}
              >
                通过
              </button>
            )}
            {onQuickReject && (
              <button
                type="button"
                className={styles.branchActionBtn}
                disabled={busy}
                onClick={() => void onQuickReject(row)}
              >
                拒绝
              </button>
            )}
            <button
              type="button"
              className={styles.branchActionBtn}
              disabled={busy}
              onClick={() => void onSelectRow(row)}
            >
              编辑详情
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div id="queue-list" className={styles.videoTreeList}>
      {tree.videos.map(({ video, branches }) => {
        const expanded = !collapsedVideos.has(video.bvid);
        return (
          <article
            key={video.bvid}
            data-video-base={video.bvid}
            className={styles.videoBaseCard}
          >
            <header className={styles.videoBaseHeader}>
              <div className={styles.videoBaseTitleRow}>
                <button
                  type="button"
                  className={styles.videoTreeToggle}
                  aria-expanded={expanded}
                  onClick={() => toggleVideoExpand(video.bvid)}
                >
                  <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
                  <strong>{video.title}</strong>
                </button>
                <a
                  href={video.videoUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="win-link-btn"
                >
                  {video.bvid} ↗
                </a>
              </div>

              <div className={styles.videoBaseMetaRow}>
                {video.uploaderName && (
                  <span className={styles.uploaderMeta}>
                    UP：
                    {video.uploaderUrl ? (
                      <a href={video.uploaderUrl} target="_blank" rel="noreferrer">
                        {video.uploaderName}
                      </a>
                    ) : (
                      video.uploaderName
                    )}
                  </span>
                )}
                {video.coverageBadges.map((badge) => (
                  <span key={badge} className={styles.coverageBadge}>
                    {badge}
                  </span>
                ))}
                <span className={styles.branchCountBadge}>{branches.length} 个楼分支</span>
              </div>

              {expanded && video.firstImpression && (
                <p className={styles.impressionSummary}>
                  <strong>初看印象：</strong>
                  {video.firstImpression}
                </p>
              )}
            </header>

            {expanded && (
              <div className={styles.floorBranchList}>
                {branches.map((branch) => (
                  <section
                    key={branch.branchKey}
                    data-floor-branch={branch.branchKey}
                    className={styles.floorBranchNode}
                  >
                    <div className={styles.floorBranchHeader}>
                      <span className={styles.floorBranchLabel}>
                        {branch.isTopLevel ? "主评分支" : branch.title}
                      </span>
                      {branch.hasOwnerTakeover && (
                        <span className={styles.takeoverBadge}>含人工接管</span>
                      )}
                    </div>

                    {branch.turns.length > 0 && (
                      <ol className={styles.floorTurnTimeline} aria-label="楼内对话时间线">
                        {branch.turns.map((turn) => (
                          <li
                            key={turn.id}
                            className={[
                              styles.floorTurnItem,
                              turn.role === "owner_takeover" ? styles.floorTurnTakeover : "",
                              turn.role === "draft" ? styles.floorTurnDraft : "",
                            ]
                              .filter(Boolean)
                              .join(" ")}
                          >
                            <span className={styles.floorTurnRole}>
                              {turn.roleLabel} · {turn.author}
                            </span>
                            <p className={styles.floorTurnText}>{turn.text}</p>
                          </li>
                        ))}
                      </ol>
                    )}

                    <div className={styles.floorBranchRows}>
                      {branch.rows.map((row) => renderQueueLink(row))}
                    </div>
                  </section>
                ))}
              </div>
            )}
          </article>
        );
      })}

      {tree.orphans.length > 0 && (
        <section className={styles.orphanFallbackSection} aria-label="其他与独立记录">
          {tree.videos.length > 0 && (
            <h3 className={styles.orphanHeading}>其他与独立记录 ({tree.orphans.length})</h3>
          )}
          {tree.orphans.map((row) => renderQueueLink(row))}
        </section>
      )}
    </div>
  );
}
