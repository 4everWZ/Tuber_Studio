export interface HumanizedKind {
  label: string;
  icon: string;
  type: string;
}

export interface HumanizedStatus {
  label: string;
  className: string;
  priority: number;
}

export interface RoundReport {
  id: string;
  title: string;
  dateStr: string;
  status: string;
  statusInfo: HumanizedStatus;
  interactionOnly: boolean;
  narrative: string;
  metrics: {
    reads: number;
    modelCalls: number;
    tokens: string;
    drafts: number | undefined;
  };
  raw: Record<string, unknown>;
}

export function parseEpoch(val: string | number | null | undefined): number {
  if (!val) return 0;
  if (typeof val === "number") {
    return val > 1e11 ? val : val * 1000;
  }
  const d = new Date(val);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime();
}

export function formatRelativeTime(val: string | number | null | undefined): string {
  const time = parseEpoch(val);
  if (!time) return "未知时间";
  const now = Date.now();
  const diff = Math.floor((now - time) / 1000);

  if (diff < 30) return "刚刚";
  if (diff < 60) return `${diff} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;

  const d = new Date(time);
  const today = new Date();
  const isYesterday =
    d.getDate() === today.getDate() - 1 &&
    d.getMonth() === today.getMonth() &&
    d.getFullYear() === today.getFullYear();

  const pad = (n: number) => String(n).padStart(2, "0");
  const timeStr = `${pad(d.getHours())}:${pad(d.getMinutes())}`;

  if (isYesterday) return `昨天 ${timeStr}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${timeStr}`;
}

export function humanizeKind(kind: string | null | undefined): HumanizedKind {
  switch (kind) {
    case "top_level":
      return { label: "💡 顶层主评", icon: "💡", type: "top_level" };
    case "reply_public":
      return { label: "💬 楼内跟帖", icon: "💬", type: "reply_public" };
    case "video_like":
      return { label: "👍 视频点赞", icon: "👍", type: "video_like" };
    case "comment_like":
      return { label: "👍 评论点赞", icon: "👍", type: "comment_like" };
    case "dynamic_like":
      return { label: "👍 动态点赞", icon: "👍", type: "dynamic_like" };
    case "video_fav":
      return { label: "⭐ 视频收藏", icon: "⭐", type: "video_fav" };
    case "private_handoff":
      return { label: "✉️ 深度转私信", icon: "✉️", type: "private_handoff" };
    case "mention":
      return { label: "🔔 艾特与提及", icon: "🔔", type: "mention" };
    default:
      return { label: "📝 互动草稿", icon: "📝", type: "draft" };
  }
}

export function humanizeStatus(
  status: string | null | undefined,
  materialMode?: string | null
): HumanizedStatus {
  if (materialMode === "unavailable") {
    return { label: "取材失败", className: "status-slate", priority: 9 };
  }
  switch (status) {
    case "pending_review":
      return { label: "⏳ 待审核", className: "status-amber", priority: 1 };
    case "approved":
      return { label: "✓ 已审核通过", className: "status-emerald", priority: 2 };
    case "published":
      return { label: "平台已受理", className: "status-blue", priority: 3 };
    case "rejected":
      return { label: "✕ 已放弃", className: "status-slate", priority: 4 };
    case "publish_uncertain":
      return { label: "⚠️ 发布核验中", className: "status-amber", priority: 5 };
    case "running":
      return { label: "运行中", className: "status-emerald", priority: 0 };
    case "queued":
    case "discovered":
      return { label: "排队中", className: "status-slate", priority: 0 };
    case "claimed":
    case "preparing":
      return { label: "取材中", className: "status-emerald", priority: 0 };
    case "evidence_ready":
      return { label: "材料就绪", className: "status-emerald", priority: 0 };
    case "completed":
      return { label: "🟢 正常完成", className: "status-emerald", priority: 10 };
    case "partial":
      return { label: "🟡 部分完成", className: "status-amber", priority: 11 };
    case "cancelled":
      return { label: "⚪ 已停止", className: "status-slate", priority: 12 };
    case "failed":
      return { label: "🔴 发生异常", className: "status-danger", priority: 13 };
    case "invalid":
      return { label: "生成无效", className: "status-danger", priority: 13 };
    case "awaiting_review":
      return { label: "候选待人工审核", className: "status-amber", priority: 1 };
    case "deferred":
      return { label: "本次已暂缓", className: "status-slate", priority: 9 };
    case "selected":
      return { label: "✨ 已生成互动", className: "status-blue", priority: 2 };
    case "viewed_without_comment":
      return { label: "看过未发", className: "status-slate", priority: 8 };
    case "comments_checked":
      return { label: "已检查评论", className: "status-slate", priority: 8 };
    case "paused":
      return { label: "已暂停，进度保留", className: "status-slate", priority: 9 };
    case "later":
      return { label: "稍后处理", className: "status-slate", priority: 9 };
    case "deferred_daily":
      return { label: "等待可用名额", className: "status-slate", priority: 9 };
    case "recovered":
      return { label: "已恢复", className: "status-slate", priority: 10 };
    case "checkpoint":
      return { label: "💾 进度已保存", className: "status-slate", priority: 9 };
    case "retry_wait":
      return { label: "已记录失败", className: "status-amber", priority: 9 };
    case "no_subtitles":
      return { label: "无可用字幕", className: "status-slate", priority: 14 };
    case "skipped":
      return { label: "已跳过", className: "status-slate", priority: 14 };
    default:
      return { label: status || "未知状态", className: "status-slate", priority: 99 };
  }
}

const REASON_MAP: Record<string, string> = {
  light_interaction: "讨论值得参与",
  specific_detail: "深入呼应视频中的核心观点与亮点细节",
  private_details: "涉及更深话题，引导至私信交流",
  no_subtitles: "视频无可用字幕，已跳过",
  visual_unavailable: "当前无法读取视频画面",
  material_unavailable: "取材失败，暂无字幕或画面",
  platform_error: "B站接口网络波动或频控限制",
  spam: "过滤掉广告营销或低俗刷屏发言",
  short_comment: "评论内容过短，缺少展开互动的价值",
  duplicate_intent: "同源互动已有草稿或已发表，绝不重复发言",
  already_commented: "当前角色已在此视频留言",
  round_budget: "达到本轮资源上限，进度已保存，下一轮继续",
  daily_watch_limit: "今日观看次数已用完，继续处理评论",
  daily_reply_limit: "该视频今日回复数已用完，草稿保留到次日",
  send_limit: "等待发送名额",
};

export function humanizeReason(reason: string | null | undefined): string {
  if (!reason) return "暂无原因记录";
  return REASON_MAP[reason] || reason;
}

const SCHEDULING_REASONS = new Set([
  "round_budget",
  "queued",
  "waiting",
  "checkpoint",
  "paused",
  "next_batch",
  "daily_watch_limit",
  "daily_reply_limit",
  "send_limit",
]);

const TASK_STAGE_REASONS: Record<string, string> = {
  discovered: "视频已找到，等待读取字幕与画面。",
  claimed: "已分配处理任务，正在准备材料。",
  preparing: "正在读取字幕与画面，内容判断尚未完成。",
  evidence_ready: "材料已准备，等待生成内容判断。",
  checkpoint: "处理进度已保存，尚未生成完整的内容判断。",
  paused: "任务已暂停，保留进度等待继续。",
  queued: "任务已排队，尚未开始读取内容。",
  running: "任务正在处理，内容判断尚未完成。",
  cancelled: "任务已停止；此历史记录未保存内容判断。",
  selected: "已生成回应；此历史记录未保存具体原因。",
  viewed_without_comment: "已看过且未生成回复；此历史记录未保存具体原因。",
  comments_checked: "已检查这条视频的新增评论。",
  deferred_daily: "程序进度：等待可用名额，当前进度已保留。",
  invalid: "本次生成未形成有效完整候选；原始输出保留供检查。",
  awaiting_review: "完整候选已保留，存在正文诊断，等待人工审核。",
  deferred: "模型已决定暂缓本次行动。",
};

export interface TaskReasonInput {
  status?: string;
  reason?: string | null;
  retry_kind?: string | null;
  explanation?: string | null;
  error?: string | null;
  error_kind?: string | null;
  decisions?: Array<{ explanation?: string | null; reason?: string | null }>;
  payload?: {
    reason?: string | null;
    retry_kind?: string | null;
    explanation?: string | null;
    decisions?: Array<{ explanation?: string | null; reason?: string | null }>;
  } | null;
}

export function humanizeTaskReason(task: TaskReasonInput): string {
  const payload = task.payload || {};
  const rawReason = task.reason || payload.reason;
  const scheduling =
    task.retry_kind ||
    payload.retry_kind ||
    (rawReason && SCHEDULING_REASONS.has(rawReason) ? rawReason : null);
  const decisions = task.decisions || payload.decisions || [];
  const reasons = [
    task.explanation,
    task.reason,
    payload.explanation,
    payload.reason,
    ...decisions.map((value) => value.explanation || value.reason),
  ]
    .filter((value): value is string => typeof value === "string" && value.trim().length > 0)
    .map((value) => value.trim());

  const unique = [...new Set(reasons)].filter((value) => value !== scheduling);
  const meaningful = unique.filter((value) => !/^[a-z][a-z0-9_]*$/.test(value));
  let content = meaningful.join("；");
  if (!content && unique.length) {
    content = unique.map((value) => `${humanizeReason(value)}（历史记录仅保存此标记）`).join("；");
  }
  if (scheduling) {
    const progress = `程序进度：${humanizeReason(scheduling)}`;
    return content ? `${content} · ${progress}` : progress;
  }
  if (content) return content;
  if (task.error || task.error_kind) {
    return `处理未完成：${task.error || humanizeReason(task.error_kind)}`;
  }
  return (task.status ? TASK_STAGE_REASONS[task.status] : undefined) || "此历史记录未保存具体原因，可查看来源与处理详情。";
}

function parseJsonRecord(raw: unknown): Record<string, unknown> {
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }
  return typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
}

export function humanizeRoundReport(round: Record<string, unknown>): RoundReport {
  const status = typeof round.status === "string" ? round.status : "";
  const statusInfo = humanizeStatus(status);
  const dateStr = formatRelativeTime(round.created_at as string | number | undefined);
  const budget = parseJsonRecord(round.budget);
  const config = parseJsonRecord(round.config);
  const metrics = parseJsonRecord(round.metrics);

  const reads = Number(budget.reads ?? 0);
  const modelCalls = Number(budget.model_calls ?? 0);
  const inputTokens = Number(budget.input_tokens ?? 0);
  const outputTokens = Number(budget.output_tokens ?? 0);
  const totalTokens = inputTokens + outputTokens;
  const interactionOnly = config.interaction_only === true;
  const kind = interactionOnly
    ? "互动收件箱"
    : round.preview
      ? "只读预览"
      : config.discovery_id || config.autonomous_batch
        ? "自主刷视频"
        : "自主运营";
  const title = `${kind} · ${dateStr}`;
  const viewed = metrics.viewed;
  const drafts = typeof round.public_drafts === "number" ? round.public_drafts : undefined;
  const narrative = [
    !interactionOnly && viewed != null ? `已读 ${String(viewed)} 个视频的材料` : "",
    drafts ? `生成评论 ${drafts} 条` : "",
    round.public_published ? `确认发布 ${String(round.public_published)} 条` : "",
    round.public_uncertain ? `待核对 ${String(round.public_uncertain)} 条` : "",
    typeof round.error === "string" ? round.error : typeof metrics.source_error === "string" ? metrics.source_error : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const tokenFormatted = totalTokens > 1000 ? `${(totalTokens / 1000).toFixed(1)}k` : `${totalTokens}`;

  return {
    id: String(round.id ?? ""),
    title,
    dateStr,
    status,
    statusInfo,
    interactionOnly,
    narrative,
    metrics: {
      reads,
      modelCalls,
      tokens: tokenFormatted,
      drafts,
    },
    raw: round,
  };
}
