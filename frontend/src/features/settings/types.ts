import type { ApiComponents } from "@/shared/api/client";

export interface OmniProviderConfig {
  base_url: string;
  model: string;
  responses_model: string;
  operations_max_tokens: number;
  enable_thinking: boolean;
  video_options?: {
    max_frames?: number;
    frame_strategy?: string;
  } | null;
  responses_options?: {
    parallel_reads?: number;
    max_output_tokens?: number;
  } | null;
}

export interface ProviderCredentialsState {
  omni?: boolean;
  [key: string]: unknown;
}

export interface ProviderSettingsPayload {
  omni: {
    base_url: string;
    model: string;
    responses_model: string;
    operations_max_tokens: number;
    enable_thinking: boolean;
    video_options: { max_frames: number };
    responses_options: {
      parallel_reads: number;
      max_output_tokens: number;
    };
  };
  credentials?: { omni: string };
  clear_credentials?: string[];
}

export interface ProviderModelCardStatus {
  available: boolean;
  context_tokens?: number | null;
  error?: string | null;
}

export interface ProviderCheckResponse {
  foreground?: ProviderModelCardStatus;
  background?: ProviderModelCardStatus;
}

export interface ProviderModelsResponse {
  models?: string[];
  error?: string | null;
}

export type IllustrationPolicy = ApiComponents["schemas"]["IllustrationPolicy"];

export interface ArtworkVariantInfo {
  available: boolean;
  source?: string | null;
  sha256?: string | null;
  preview_url?: string | null;
  thumbnail_url?: string | null;
}

export interface ArtworkReferencesResponse {
  character_id: string;
  variants: Partial<Record<"q" | "mature", ArtworkVariantInfo>>;
}

export interface CacheCategoryStats {
  bytes: number;
  files?: number;
  limit_bytes: number;
  reclaimable_bytes: number;
  protected_bytes: number;
  writing?: boolean;
  unlimited?: boolean;
  max_items?: number;
}

export type CachePolicy = ApiComponents["schemas"]["CachePolicy"] & {
  meme_inbox_limit_mib?: number;
  meme_inbox_max_items?: number;
};

export type CacheCategoryKey =
  | "video"
  | "music"
  | "speech"
  | "browsing"
  | "temporary"
  | "assets"
  | "meme_inbox";

export interface CacheSummaryStats {
  bytes: number;
  files: number;
  protected_bytes: number;
  reclaimable_bytes: number;
  limit_bytes: number;
  unlimited?: boolean;
}

export interface CacheStatusResponse {
  categories: Partial<Record<CacheCategoryKey, CacheCategoryStats>>;
  settings: CachePolicy;
  summary?: CacheSummaryStats;
  result?: {
    freed_bytes: number;
    removed_files: number;
    protected_bytes?: number;
  };
}

export interface RetentionPolicyResponse {
  days: number | null;
  enabled: boolean;
  last_run?: number | string | null;
  next_run?: number | string | null;
  last_result?: unknown;
  error?: string | null;
}

export interface RetentionPreviewResponse {
  cutoff: string | null;
  counts?: Record<string, number>;
}

export interface RetentionCleanupResponse {
  counts?: Record<string, number>;
  [key: string]: unknown;
}

export interface BilibiliAccountProfile {
  uid?: string | null;
  name?: string | null;
  coins?: number | null;
  fans?: number | null;
  bio?: string | null;
  avatar_url?: string | null;
}

export interface BilibiliAccountStatus {
  state: string;
  profile?: BilibiliAccountProfile | null;
  checked_at?: number | null;
  attempted_at?: number | null;
  error?: string | null;
  warnings?: string[];
  configured?: {
    sessdata?: boolean;
    csrf?: boolean;
  };
  renewal?: {
    state?: string;
    checked_at?: number | null;
    renewed_at?: number | null;
  };
  verification_stale?: boolean;
}

export interface BilibiliLoginSessionResponse {
  id: string;
  expires_in: number;
}

export interface BilibiliLoginPollResponse {
  state: string;
  account?: BilibiliAccountStatus;
}

export interface VoiceOptionSummary {
  id: string;
  name?: string;
}

/** Resolve display name for a character using `display_name || id`. */
export function characterDisplayName(
  character: { id?: string | null; display_name?: string | null } | null | undefined
): string {
  return character?.display_name || character?.id || "";
}

/** Build the single-workstation Omni settings payload, rejecting conflicting key+clear flags. */
export function buildOmniSettingsPayload(fields: {
  baseUrl: string;
  model: string;
  responsesModel: string;
  operationsMaxTokens: number;
  enableThinking: boolean;
  maxFrames: number;
  parallelReads: number;
  maxOutputTokens: number;
  key?: string;
  clearKey?: boolean;
}): ProviderSettingsPayload {
  const omni: ProviderSettingsPayload["omni"] = {
    base_url: fields.baseUrl.trim(),
    model: fields.model.trim(),
    responses_model: fields.responsesModel.trim(),
    operations_max_tokens: fields.operationsMaxTokens,
    enable_thinking: fields.enableThinking,
    video_options: { max_frames: fields.maxFrames },
    responses_options: {
      parallel_reads: fields.parallelReads,
      max_output_tokens: fields.maxOutputTokens,
    },
  };
  const key = fields.key ?? "";
  const clear = Boolean(fields.clearKey);
  if (key && clear) {
    throw new Error("填写新凭据或清除已有凭据，请选择一项。");
  }
  if (key) return { omni, credentials: { omni: key } };
  if (clear) return { omni, clear_credentials: ["omni"] };
  return { omni };
}

/** Build and validate a manual retention preview/cleanup request payload. */
export function buildRetentionRequest(
  policy: RetentionPolicyResponse | null,
  dirty: boolean,
  selectedDate: string
): { before?: string } {
  if (dirty) {
    throw new Error("请先保存留存策略，再预览清理范围。");
  }
  if (!selectedDate) {
    if (!policy || policy.days == null || policy.enabled === false) {
      throw new Error("当前不自动清理；手动清理请指定截至日期。");
    }
    return {};
  }
  const cutoff = new Date(`${selectedDate}T00:00:00.000Z`);
  if (Number.isNaN(cutoff.getTime())) {
    throw new Error("请选择有效的截至日期。");
  }
  cutoff.setUTCDate(cutoff.getUTCDate() + 1);
  if (cutoff.getTime() > Date.now()) {
    throw new Error("截至日期必须是已结束的 UTC 日期；未来截止时间不能清理。");
  }
  return { before: cutoff.toISOString() };
}

export function formatCacheBytes(value: number): string {
  if (value >= 1_073_741_824) return `${(value / 1_073_741_824).toFixed(2)} GiB`;
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value} B`;
}
