import type { ContinuationRecord, OperationCapabilityStatus } from "@/shared";

export type PipelineSource = "discovery" | "inbox" | "manual";
export interface BilibiliAccountSnapshot {
  state?: string | null;
  profile?: {
    uid?: string | number | null;
    name?: string | null;
    avatar_url?: string | null;
  } | null;
  configured?: {
    sessdata?: boolean;
    csrf?: boolean;
  } | null;
  last_error?: string | null;
}

export interface ReadGateDiscoveryBackoff {
  until?: number;
  cooldown_seconds?: number;
  strikes?: number;
  source_pipeline?: string;
  reason?: string;
  http_status?: number | null;
  code?: number | null;
}

export interface ReadGateStatus {
  discovery_paused?: boolean;
  discovery_backoff?: ReadGateDiscoveryBackoff | null;
  inbox_allowed?: boolean;
  reconciliation_allowed?: boolean;
  inbox_last_restricted?: {
    at?: number;
    reason?: string;
    http_status?: number | null;
    code?: number | null;
  } | null;
}

export interface DiscoveryAttentionItem {
  message: string;
  label: string;
  href: string;
  at?: number;
}

export interface DiscoverySourceConfig {
  mode: "recommend" | "search" | "popular" | "autonomous" | "specified";
  keywords: string[];
  watch_videos: string[];
}

export interface DailyWatchStatus {
  limit: number;
  started: number;
  remaining?: number | null;
}

export interface DiscoveryStatus {
  enabled?: boolean;
  configured_enabled?: boolean;
  state?: string;
  overlay_enabled?: boolean;
  error?: string | null;
  attention?: DiscoveryAttentionItem[];
  next_at?: number | null;
  read_cooldown_until?: number | null;
  read_gate?: {
    discovery_paused?: boolean;
    discovery_backoff?: ReadGateDiscoveryBackoff | null;
    [key: string]: unknown;
  } | null;
  daily_watch?: DailyWatchStatus | null;
  active_round?: BrowsingRound | null;
  last_round?: BrowsingRound | null;
  progress?: Record<string, number>;
  sent?: number;
  current_video?: {
    id?: string;
    bvid?: string;
    status?: string;
    title?: string;
  } | null;
  summary?: {
    processed?: number;
    skipped?: number;
  } | null;
  source?: DiscoverySourceConfig | null;
  round_source?: string | null;
  account_id?: string | null;
  configured_account_id?: string | null;
  character?: {
    id?: string;
    version?: number;
    name?: string;
    display_name?: string;
  } | null;
  model?: string | null;
}

export interface InboundCounts {
  comment_reply?: number;
  mention?: number;
  own_content_comment?: number;
  dm?: number;
}

export interface InboxPipelineStatus {
  enabled?: boolean;
  configured_enabled?: boolean;
  state?: string;
  pending_events?: number;
  dm_enabled?: boolean;
  read_gate?: {
    allowed?: boolean;
    [key: string]: unknown;
  } | null;
  counts?: InboundCounts;
  messages?: MessagePolicyState | null;
}

export interface AutomationBacklog {
  total_heads: number;
  ready_heads: number;
  oldest_seconds?: number;
  high_heads?: number;
  low_heads?: number;
  discovery_paused?: boolean;
  comment_hourly_resume_at?: number | null;
  catching_up?: boolean;
  waiting_comments: number;
  sends: {
    queued: number;
    deferred: number;
    rejected: number;
    reactions: number;
  };
  review?: {
    comments?: number;
    messages?: number;
    dynamics?: number;
  };
  uncertain?: Record<string, number>;
  video_work?: {
    pending: number;
    history: number;
  } | null;
  last_round_flow?: Record<string, { added: number; settled: number; net: number }>;
}

export interface OperationClaim {
  claim_key: string;
  claim_kind: "comment" | "floor" | string;
  account_id: string;
  character_id: string;
  resource_type: string;
  resource_id: string;
  root_id: string;
  target_comment_id?: string | null;
  owner_pipeline: PipelineSource | string;
  lease_token?: string;
  lease_until: number;
  updated_at?: number;
}

export interface AutomationPolicySettings {
  discovery_enabled?: boolean;
  inbox_enabled?: boolean;
  mentions_enabled?: boolean;
  mention_reply_enabled?: boolean;
  replies_enabled?: boolean;
  top_level_enabled?: boolean;
  top_level_auto_publish?: boolean;
  reply_enabled?: boolean;
  reply_auto_publish?: boolean;
  comment_like_enabled?: boolean;
  comment_like_auto_publish?: boolean;
  video_like_enabled?: boolean;
  video_like_auto_publish?: boolean;
  dynamic_like_enabled?: boolean;
  dynamic_like_auto_publish?: boolean;
  video_fav_enabled?: boolean;
  video_fav_auto_publish?: boolean;
  daily_watch_limit?: number;
  max_replies_per_video?: number;
  max_per_hour?: number;
  max_per_day?: number;
  recipient_cooldown_seconds?: number;
  business_timezone?: string;
  interval_seconds?: number;
  notification_interval_seconds?: number;
  workers?: number;
  candidate_limit?: number;
  decision_max_tokens?: number;
  deadline_seconds?: number;
  frame_sampling?: string;
  [key: string]: unknown;
}

export interface AutomationStatus extends OperationCapabilityStatus {
  status_sequence?: number;
  started_at?: number;
  error?: string | null;
  account_id?: string | null;
  character?: {
    id?: string;
    version?: number;
    name?: string;
    display_name?: string;
  } | null;
  policy?: {
    enabled?: boolean;
    emergency?: boolean;
    direct_quota_reserve?: number;
    max_per_hour?: number;
    max_per_day?: number;
    recipient_cooldown_seconds?: number;
    [key: string]: unknown;
  } | null;
  settings?: AutomationPolicySettings | null;
  effective_settings?: AutomationPolicySettings | null;
  effective?: {
    comments?: {
      credentials_configured?: boolean;
      [key: string]: unknown;
    };
    messages?: MessagePolicyState;
    dynamics?: DynamicPolicy;
    browsing?: AutomationPolicySettings;
  } | null;
  base?: {
    messages?: MessagePolicyState;
    dynamics?: DynamicPolicy;
    browsing?: AutomationPolicySettings;
    comments?: Record<string, unknown>;
  } | null;
  vision?: {
    images?: boolean;
    [key: string]: unknown;
  } | null;
  active_round?: BrowsingRound | null;
  active_rounds?: BrowsingRound[] | null;
  waiting_round?: {
    remaining?: number;
    [key: string]: unknown;
  } | null;
  send_error?: string | null;
  comment_cooldown_until?: number | null;
  automatic_sending_ready?: boolean;
  discovery?: DiscoveryStatus | null;
  inbox?: InboxPipelineStatus | null;
  read_gate?: ReadGateStatus | null;
  frozen_reason?: string | null;
  backlog?: AutomationBacklog | null;
  daily_watch?: DailyWatchStatus | null;
  claims?: OperationClaim[];
}

export interface QuotaSnapshot {
  account_id?: string | null;
  direct_quota_reserve: number;
  max_per_hour: number;
  max_per_day: number;
  recipient_cooldown_seconds: number;
  used_per_hour: number;
  used_per_day: number;
  remaining_per_hour: number | null;
  remaining_per_day: number | null;
  discovery_deferred: boolean;
  discovery_deferral_reason?: string | null;
  discovery_retry_at?: number | null;
}

export interface InboxEventItem {
  id: string;
  kind?: string;
  inbound_kind?: string;
  source_pipeline?: PipelineSource | string;
  status: string;
  timestamp?: number;
  created_at?: number;
  sender_name?: string;
  author_name?: string;
  source_text?: string;
  resource_title?: string;
  error?: string | null;
  next_at?: number | null;
  occurrences?: number;
  payload?: {
    author_name?: string;
    source_text?: string;
    text?: string;
    [key: string]: unknown;
  } | null;
  continuation?: ContinuationRecord | null;
}

export interface InboxRecordsResponse {
  items: InboxEventItem[];
  cursors?: Array<Record<string, unknown>>;
  messages?: number;
  private_count?: number;
  private_href?: string;
  account_id?: string;
  character_id?: string;
  updated_at?: number;
}

export interface BrowsingRound {
  id: string;
  status: string;
  preview?: boolean;
  created_at?: number;
  updated_at?: number;
  config?: {
    interaction_only?: boolean;
    autonomous_batch?: boolean;
    discovery_id?: string;
    [key: string]: unknown;
  };
  budget?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  public_drafts?: number;
  public_published?: number;
  public_uncertain?: number;
  error?: string | null;
}

export interface MessagePolicyState {
  enabled?: boolean;
  auto_poll?: boolean;
  auto_send?: boolean;
  allow_comment_handoff?: boolean;
  poll_interval_seconds?: number;
  max_per_hour?: number;
  max_per_day?: number;
  recipient_cooldown_seconds?: number;
  automation_override?: boolean;
  emergency?: boolean;
  credentials_configured?: boolean;
  last_error?: string | null;
  base?: {
    enabled?: boolean;
    auto_poll?: boolean;
    auto_send?: boolean;
    allow_comment_handoff?: boolean;
    poll_interval_seconds?: number;
    max_per_hour?: number;
    max_per_day?: number;
    recipient_cooldown_seconds?: number;
  };
}

export interface DynamicPolicy {
  enabled?: boolean;
  auto_publish?: boolean;
  evaluate_interval_seconds?: number;
  min_post_interval_seconds?: number;
  max_posts_per_day?: number;
  credentials_configured?: boolean;
  next_evaluation_at?: number | null;
  backoff_until?: number | null;
  last_error?: string | null;
  base?: {
    enabled?: boolean;
    auto_publish?: boolean;
    evaluate_interval_seconds?: number;
    min_post_interval_seconds?: number;
    max_posts_per_day?: number;
  };
}
