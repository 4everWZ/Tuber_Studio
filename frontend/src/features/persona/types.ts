import type { ApiComponents } from "@/shared/api/client";
import type { MemeLicenseInfo, MemeSourceInfo, MemeUsageScope } from "@/shared/ui/MemePickerModal";

export interface BilibiliOwnerProfile {
  uid: string;
  name: string;
  avatar_url?: string | null;
}

export interface CharacterCore {
  identity?: string;
  background?: string;
  traits?: string[] | string;
  [key: string]: unknown;
}

export interface CharacterStyle {
  min_chars?: number;
  max_chars?: number;
  catchphrases?: string[] | string;
  instructions?: string[] | string;
  [key: string]: unknown;
}

export interface CharacterCard {
  id: string;
  name: string;
  display_name?: string;
  owner_address?: string;
  aliases?: string[];
  self_description?: string;
  speaking_style?: string;
  bilibili_owner?: BilibiliOwnerProfile | string | null;
  core?: CharacterCore;
  style?: CharacterStyle;
  voice_profile?: string;
  avatar_manifest?: string;
  avatar_url?: string | null;
  version?: number;
  persona_type?: string;
  fine_tuned_model?: string;
  compact_system_prompt?: string;
  [key: string]: unknown;
}

export interface VoiceOption {
  id: string;
  name?: string;
  provider?: string;
  reference_asset?: string | null;
  sample_text?: string | null;
  license_status?: string | null;
}

export interface VoicePreviewResponse {
  audio_ref: string;
  duration?: number | null;
  text?: string | null;
  warning?: string | null;
}

export interface PersonaAssetItem {
  id: string;
  url?: string;
  path?: string;
  kind?: string;
  type?: string;
  tag?: string;
  event_tag?: string;
  character_id?: string | null;
  license_status?: string | null;
  source?: string | null;
  source_url?: string | null;
}

export interface MemoryPreferenceState {
  enabled: boolean;
  history_enabled?: boolean | null;
  reference_chat_history?: boolean | null;
  scope: string;
}

export interface UnifiedMemoryItem {
  id: string;
  scope: "owner" | "public";
  character_id: string;
  content: string;
  source: string;
  source_key?: string;
  tags: string[];
  created_at: number;
  updated_at: number;
  active: boolean;
}

export interface UnifiedMemoryListResponse {
  character_id: string;
  items: UnifiedMemoryItem[];
  count: number;
  total_count: number;
  limit: number;
  over_limit: boolean;
  legacy_migrated_count: number;
}

export interface PurgeMigratedMemoriesResponse {
  ok: boolean;
  purged: number;
  count: number;
  limit: number;
  legacy_migrated_count: number;
}

export interface ClearUnifiedMemoriesResponse {
  ok: boolean;
  deleted: number;
  count: number;
  limit: number;
  legacy_migrated_count: number;
}

export interface OpenManageMemoriesDetail {
  scope?: "all" | "owner" | "public" | string;
  snippet?: string;
  memoryId?: string;
}

export const OPEN_MANAGE_MEMORIES_EVENT = "aqua:open-manage-memories";
export const MEMORIES_CHANGED_EVENT = "aqua:memories-changed";

export interface MemorySourceReference {
  text: string;
  href: string;
}

export interface MemoryItemSource {
  key?: string | null;
  at?: number | null;
  references?: MemorySourceReference[];
}

export interface OrganizedMemoryItem {
  id: number;
  memory_kind: string;
  headline: string;
  quote?: string | null;
  anecdote: string;
  status: string;
  channels: string[];
  source: MemoryItemSource;
  updated_at: number;
  revision?: number | null;
  scene?: string | null;
  can_share?: boolean;
  owner_only?: boolean;
  visibility?: string;
  choices?: string[];
}

export interface MemorySummaryNote {
  id: number;
  text: string;
  visibility?: string;
}

export interface MemorySummaryGroup {
  kind: string;
  notes: MemorySummaryNote[];
}

export interface HighlightPublicSource {
  bvid: string;
  thread_id?: string;
  comment_id?: string;
  quote: string;
}

export interface DailyHighlightItem {
  id: number;
  headline: string;
  anecdote: string;
  highlight_date: string;
  quote?: string | null;
  status?: string;
  tags?: string[];
  public_sources?: HighlightPublicSource[];
}

export interface DailyMemoryReport {
  highlights: DailyHighlightItem[];
  organized: OrganizedMemoryItem[];
  organization: {
    status: string;
    error?: string | null;
  };
  summary: MemorySummaryGroup[];
  summary_updated_at?: number | null;
  scope: string;
  enabled: boolean;
}

export interface MemorySummaryAskResponse {
  scope: string;
  status: "found" | "no_match" | "updated" | "proposal" | "clarify";
  items?: OrganizedMemoryItem[];
  item?: OrganizedMemoryItem;
  note?: string;
}

export interface RecentEventItem {
  title: string;
  source: {
    kind?: string;
    text: string;
    href?: string;
  };
  reply?: {
    href?: string;
  } | null;
}

export interface RecentEventsResponse {
  scope: string;
  items: RecentEventItem[];
}

export interface PersonaContextStatus {
  scope?: string | null;
  usable?: boolean;
  error?: string | null;
  configured_tokens?: number | null;
  server_tokens?: number | null;
  native_tokens?: number | null;
  active_tokens?: number | null;
  compact_count?: number | null;
  compaction_passes_last?: number | null;
  summary_budgets_last?: number[] | null;
  secondary_threshold_percent?: number | null;
  last_compaction_warning?: string | null;
  last_compaction_error?: string | null;
}

export type OrganizedMemoryChange = ApiComponents["schemas"]["OrganizedMemoryChange"];
export type MemeUploadPayload = ApiComponents["schemas"]["MemeUpload"] & {
  usage_scope?: MemeUsageScope;
};

export interface MemeInboxImageInfo {
  width?: number;
  height?: number;
  frames?: number;
  animated?: boolean;
  mime_type?: string;
}

export interface MemeInboxItem {
  item_id: string;
  sha256: string;
  dhash?: string;
  blob_name?: string;
  bytes?: number;
  width?: number;
  height?: number;
  frames?: number;
  animated?: boolean;
  mime_type?: string;
  image_info?: MemeInboxImageInfo;
  title: string;
  emotions: string[];
  tags: string[];
  description: string;
  usage_scope: MemeUsageScope;
  confidence?: number;
  ai_tagged?: boolean | number;
  status?: string;
  source?: MemeSourceInfo | null;
  license?: MemeLicenseInfo | null;
  subscription_id?: string;
  created_at?: number;
  preview_url?: string;
}

export interface MemeInboxCacheStats {
  bytes?: number;
  files?: number;
  limit_bytes?: number;
  max_items?: number;
  unlimited?: boolean;
  reclaimable_bytes?: number;
  protected_bytes?: number;
}

export interface MemeInboxResponse {
  items?: MemeInboxItem[];
  pending_count?: number;
  pending_vision_count?: number;
  cache?: MemeInboxCacheStats;
}

export type MemeSubscriptionKind = "remote_feed" | "bilibili_creator";

export interface MemeSubscription {
  source_id: string;
  kind: MemeSubscriptionKind | string;
  target_kind: string;
  name: string;
  url: string;
  enabled: boolean;
  auto_approve?: boolean;
  sync_interval_hours: number;
  license_name?: string;
  license_url?: string;
  license_note?: string;
  last_etag?: string;
  last_cursor?: string;
  last_synced_at?: number | null;
  last_status?: string;
  last_error?: string;
  created_at?: number;
  updated_at?: number;
}

export interface MemeHarvesterConfig {
  comment_harvest_enabled: boolean;
  default_sync_interval_hours: number;
  meme_inbox_limit_mib: number;
  meme_inbox_max_items: number;
}

export interface MemeSourcesResponse {
  items?: MemeSubscription[];
  sources?: MemeSubscription[];
  config?: MemeHarvesterConfig;
}

/**
 * Resolve web character label following legacy `core.js`:
 * `display_name || id`, preserving "留空显示角色 ID".
 */
export function characterDisplayName(
  character: { id?: string | null; display_name?: string | null } | null | undefined
): string {
  return character?.display_name || character?.id || "";
}

/** Strip runtime-only / legacy prototype metadata before editing or saving character JSON. */
export function editableCard(character: CharacterCard): CharacterCard {
  const {
    version: _version,
    avatar_url: _avatarUrl,
    persona_type: _personaType,
    fine_tuned_model: _fineTunedModel,
    compact_system_prompt: _compactSystemPrompt,
    ...card
  } = character;
  return card;
}

/** Extract field-level validation entries from either M3 ErrorEnvelope or legacy detail arrays. */
export function extractValidationErrors(error: unknown): Array<{ field?: string; message?: string }> {
  if (!error || typeof error !== "object") return [];
  const rec = error as { details?: unknown; body?: unknown };
  if (rec.details && typeof rec.details === "object" && "errors" in rec.details) {
    const errors = (rec.details as { errors?: unknown }).errors;
    if (Array.isArray(errors)) {
      return errors.filter((item): item is { field?: string; message?: string } => Boolean(item && typeof item === "object"));
    }
  }
  if (rec.body && typeof rec.body === "object" && "detail" in rec.body) {
    const detail = (rec.body as { detail?: unknown }).detail;
    if (Array.isArray(detail)) {
      return detail.filter((item): item is { field?: string; message?: string } => Boolean(item && typeof item === "object"));
    }
  }
  return [];
}

/** Combine multiple AbortSignals safely across modern browsers and jsdom test environments. */
export function combineAbortSignals(signals: AbortSignal[]): { signal: AbortSignal; cleanup: () => void } {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.any === "function") {
    return { signal: AbortSignal.any(signals), cleanup: () => {} };
  }
  const combined = new AbortController();
  const cleanups: Array<() => void> = [];
  for (const sig of signals) {
    if (sig.aborted) {
      combined.abort(sig.reason);
      return { signal: combined.signal, cleanup: () => {} };
    }
    const onAbort = () => combined.abort(sig.reason);
    sig.addEventListener("abort", onAbort, { once: true });
    cleanups.push(() => sig.removeEventListener("abort", onAbort));
  }
  return {
    signal: combined.signal,
    cleanup: () => {
      for (const fn of cleanups) fn();
    },
  };
}
