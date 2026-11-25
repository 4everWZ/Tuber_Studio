import { apiRequest } from "@/shared/api/client";
import type { BrowsingIntent, VisibilityDetail } from "./types";

export type VisibilityTone = "good" | "amber" | "bad" | "neutral";

export interface VisibilityMeta {
  code: string;
  label: string;
  linkLabel: string;
  tone: VisibilityTone;
  badgeClass: string;
  isIssue: boolean;
}

const VISIBILITY_META: Record<string, Omit<VisibilityMeta, "code">> = {
  public_visible: {
    label: "公开可见",
    linkLabel: "查看公开回复",
    tone: "good",
    badgeClass: "good",
    isIssue: false,
  },
  visible: {
    label: "公开可见",
    linkLabel: "查看公开回复",
    tone: "good",
    badgeClass: "good",
    isIssue: false,
  },
  self_only: {
    label: "仅自己可见",
    linkLabel: "查看回复（仅自己可见）",
    tone: "amber",
    badgeClass: "amber",
    isIssue: true,
  },
  shadow_deleted: {
    label: "疑似被吞",
    linkLabel: "原回复链接（疑似被吞）",
    tone: "bad",
    badgeClass: "bad",
    isIssue: true,
  },
  not_visible: {
    label: "疑似被吞",
    linkLabel: "原回复链接（平台隐藏）",
    tone: "bad",
    badgeClass: "bad",
    isIssue: true,
  },
  not_found: {
    label: "未查到",
    linkLabel: "原回复链接（暂未查到）",
    tone: "amber",
    badgeClass: "amber",
    isIssue: false,
  },
  pending_recheck: {
    label: "待复查",
    linkLabel: "回执链接（待复查）",
    tone: "neutral",
    badgeClass: "",
    isIssue: false,
  },
  not_checked: {
    label: "未复查",
    linkLabel: "回执链接（未复查）",
    tone: "neutral",
    badgeClass: "",
    isIssue: false,
  },
  check_failed: {
    label: "复查失败",
    linkLabel: "回执链接（复查失败）",
    tone: "amber",
    badgeClass: "amber",
    isIssue: true,
  },
};

export const VISIBILITY_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(VISIBILITY_META).map(([key, meta]) => [key, meta.label])
);

export function getVisibilityMeta(visibility?: string | null): VisibilityMeta | null {
  if (!visibility) return null;
  const known = VISIBILITY_META[visibility];
  if (known) return { code: visibility, ...known };
  return {
    code: visibility,
    label: visibility,
    linkLabel: `查看回复（${visibility}）`,
    tone: "neutral",
    badgeClass: "",
    isIssue: false,
  };
}

export function sentLinkLabel(visibility?: string | null): string {
  return getVisibilityMeta(visibility)?.linkLabel ?? "回执链接（未复查）";
}

export function formatVisibilityDetail(
  detail?: VisibilityDetail | null,
  checkedAt?: number | null
): string {
  const parts: string[] = [];
  const ts = checkedAt ?? detail?.checked_at ?? null;
  if (typeof ts === "number" && Number.isFinite(ts) && ts > 0) {
    parts.push(`复查时间 ${new Date(ts * 1000).toLocaleString("zh-CN")}`);
  }
  if (typeof detail?.auth_visible === "boolean" || typeof detail?.anon_visible === "boolean") {
    const authText =
      detail.auth_visible === true ? "登录态可见" : detail.auth_visible === false ? "登录态不可见" : "登录态未知";
    const anonText =
      detail.anon_visible === true ? "匿名可见" : detail.anon_visible === false ? "匿名不可见" : "匿名未知";
    parts.push(`${authText}／${anonText}`);
  }
  if (detail?.error_code !== undefined && detail.error_code !== null) {
    parts.push(`错误码 ${detail.error_code}`);
  }
  const reason = detail?.reason || detail?.error;
  if (reason) {
    parts.push(reason);
  }
  return parts.join(" · ");
}

export function extractBrowsingIntentId(item: {
  id?: string | null;
  intent_id?: string | null;
  kind?: string | null;
  status?: string | null;
}): string | null {
  if (item.status !== "published") return null;
  if (item.intent_id) return item.intent_id;
  if (item.id && item.id.startsWith("comment/browsing/")) {
    const extracted = item.id.slice("comment/browsing/".length);
    return extracted || null;
  }
  return null;
}

export async function verifyIntentVisibility(intentId: string): Promise<BrowsingIntent> {
  return apiRequest<BrowsingIntent>("/api/v1/operations/intents/{identity}/verify-visibility", {
    method: "POST",
    pathParams: { identity: intentId },
  });
}
