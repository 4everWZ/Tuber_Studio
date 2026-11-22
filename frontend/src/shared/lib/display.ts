const STATUS_LABELS: Record<string, string> = {
  stopped: "已停止",
  live: "运行中",
  paused: "已暂停",
  degraded: "降级运行",
  ready: "就绪",
  connecting: "连接中",
  error: "错误",
  queued: "排队中",
  generating: "生成台词",
  synthesizing: "合成语音",
  ready_to_play: "等待播放器",
  playing: "播放中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
  filtered: "已过滤",
  duplicate: "重复事件",
  dropped: "已丢弃",
  selected: "已选中",
  private_handoff: "已转私信",
  received: "已收到",
  skipped: "已跳过",
  pending: "等待中",
  running: "处理中",
  pending_review: "待审核",
  approved: "已审核",
  rejected: "已退回",
  copied: "已复制",
  published: "已发布",
  publishing: "发布中",
  publish_uncertain: "发布结果待核对",
  publish_failed: "发布失败",
  question: "问题",
  suggestion: "建议 / 反馈",
  interaction: "适合互动",
  spam: "垃圾",
  general: "一般评论",
  chat: "弹幕",
  gift: "礼物",
  song: "点歌",
  sfx: "预录音效",
  disconnect: "断线",
  mock: "离线演示",
  invalidated: "已失效",
  invalid: "生成无效",
  deferred: "本次已暂缓",
  post: "生成动态",
  wait: "等待",
  role_story: "虚构角色故事",
  checkpoint: "进度已保存",
  needs_material: "等待补充材料",
  deciding: "Agent 处理中",
  awaiting_review: "待审核",
  needs_reconciliation: "发送结果待核对",
  waiting: "等待下一步",
  abandoned: "已暂缓",
};

const GOOD_BADGES = new Set(["completed", "approved", "published", "live", "ready", "copied"]);
const BAD_BADGES = new Set([
  "failed",
  "invalid",
  "error",
  "cancelled",
  "rejected",
  "degraded",
  "publish_uncertain",
  "publish_failed",
]);

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  return STATUS_LABELS[value] || value;
}

export function display(value: unknown): string {
  if (value == null) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function list<T = unknown>(data: unknown): T[] {
  if (Array.isArray(data)) return data as T[];
  if (typeof data === "object" && data !== null) {
    const rec = data as { items?: unknown; results?: unknown };
    if (Array.isArray(rec.items)) return rec.items as T[];
    if (Array.isArray(rec.results)) return rec.results as T[];
  }
  return [];
}

export function time(value: number | string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function badgeTone(value: string | null | undefined): "good" | "bad" | "neutral" {
  if (!value) return "neutral";
  if (GOOD_BADGES.has(value)) return "good";
  if (BAD_BADGES.has(value)) return "bad";
  return "neutral";
}

export function dateTime(value: number | string | null | undefined): string {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? display(value) : date.toISOString().replace("T", " ").slice(0, 19);
}

export function tokenCount(value: number | null | undefined): string {
  return typeof value === "number" ? `${value.toLocaleString("en-US")} tokens` : "—";
}

export function compactTokenCount(count: number | null | undefined): string {
  if (count == null || Number.isNaN(count)) return "—";
  if (count < 1000) return `${count} tokens`;
  if (count < 1000000) return `${(count / 1000).toFixed(1)}K tokens`;
  return `${(count / 1000000).toFixed(2)}M tokens`;
}

export function occupancyText(
  context: { configured_tokens?: number | null; active_tokens?: number | null } | null | undefined
): string {
  if (!context?.configured_tokens || context.active_tokens == null) return "—";
  return `${Math.round((context.active_tokens / context.configured_tokens) * 100)}%`;
}
