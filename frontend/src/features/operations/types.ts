import type { ContinuationRecord } from "@/shared";
import type { MessagePolicyState, PipelineSource } from "@/shared/operations/types";

export type {
  PipelineSource,
  BilibiliAccountSnapshot,
  ReadGateDiscoveryBackoff,
  ReadGateStatus,
  DiscoveryAttentionItem,
  DiscoverySourceConfig,
  DailyWatchStatus,
  DiscoveryStatus,
  InboundCounts,
  InboxPipelineStatus,
  AutomationBacklog,
  OperationClaim,
  AutomationPolicySettings,
  AutomationStatus,
  QuotaSnapshot,
  InboxEventItem,
  InboxRecordsResponse,
  BrowsingRound,
  MessagePolicyState,
  DynamicPolicy,
} from "@/shared/operations/types";

export type ActivitySourceFilter = "all" | PipelineSource;
export type ActivityKindFilter = "all" | "videos" | "comments" | "messages" | "dynamics";
export type CommentActivityFilter = "replied" | "read" | "pending_review" | "all";

export interface ActivityVideoReply {
  id?: string;
  kind?: string;
  recipient_name?: string;
  status: string;
  text?: string;
}

export interface ActivityItem {
  id: string;
  kind?: "video" | "comment" | "message" | "dynamic" | string;
  source_pipeline?: PipelineSource | string;
  source?: PipelineSource | string;
  title?: string;
  bvid?: string | null;
  video_title?: string | null;
  local_id?: string | number | null;
  status?: string;
  read_state?: string;
  fully_read?: boolean;
  watched?: boolean;
  has_main_reply?: boolean;
  main_replies?: number;
  comment_replies?: number;
  main_decision?: { action?: string } | null;
  replies?: ActivityVideoReply[];
  comment_kind?: "top_level" | "reply" | string | null;
  reply_kind?: "top_level" | "reply" | string | null;
  comment_outcome?: string | null;
  direction?: string | null;
  contact_type?: string | null;
  outreach_status?: string | null;
  contact_state?: string | null;
  recipient_name?: string | null;
  video_preference?: string | null;
  preference_reason?: string | null;
  reason?: string | null;
  error?: string | null;
  occurrences?: number;
  round_id?: string | null;
  href?: string | null;
  source_href?: string | null;
  sent_href?: string | null;
  source_text?: string | null;
  text?: string | null;
  content?: string | null;
  account_reply_text?: string | null;
  confirmed_reply_text?: string | null;
  own_resource?: boolean | null;
  own_thread?: boolean | null;
  thread?: {
    resource?: string | null;
    root_id?: string | null;
  } | null;
  updated_at?: number;
}

export interface ActivityResponse {
  day: string;
  timezone?: string;
  days?: string[];
  kind?: string;
  source?: string;
  comment_filter?: string;
  page: number;
  pages?: number;
  total: number;
  counts?: Record<string, number>;
  summary?: Record<string, number>;
  items: ActivityItem[];
}

export interface IssueRecordItem {
  id: string;
  round_id?: string | null;
  bvid?: string | null;
  error: string;
  status?: string;
  severity?: "warning" | "error";
  updated_at: number;
  occurrences?: number;
  total_occurrences?: number;
  unread_occurrences?: number;
  href?: string | null;
}

export interface DiscoveryPublishedItem {
  id: string;
  kind?: string;
  bvid?: string;
  text?: string;
  receipt_id?: string | number | null;
  updated_at: number;
}

export interface DiscoveryRecordsResponse {
  sent?: DiscoveryPublishedItem[];
  issues?: IssueRecordItem[];
  issue_history?: IssueRecordItem[];
}

export interface EvidenceSegment {
  id?: string;
  cid?: string | number | null;
  start?: number | null;
  time?: number | null;
  timestamp?: number | null;
  text?: string | null;
  observation?: string | null;
  content?: string | null;
}

export interface BrowsingTask {
  id: string;
  round_id?: string;
  bvid?: string;
  title?: string;
  status: string;
  material_mode?: string | null;
  coverage?: string | null;
  cache_status?: string | null;
  created_at?: number;
  updated_at?: number;
  reason?: string | null;
  retry_kind?: string | null;
  explanation?: string | null;
  error?: string | null;
  error_kind?: string | null;
  visual_error?: string | null;
  consecutive_failures?: number;
  autonomous?: boolean;
  intent_id?: string | null;
  reply_intent_ids?: string[];
  metadata?: { title?: string; [key: string]: unknown } | null;
  subtitle_tracks?: Array<{
    cid?: string | number;
    language?: string;
    is_ai?: boolean | null;
    track_id?: string | null;
  }>;
  missing_parts?: unknown[];
  evidence?: EvidenceSegment[];
  decisions?: Array<{ explanation?: string | null; reason?: string | null }>;
  payload?: Record<string, unknown> | null;
  continuation?: ContinuationRecord | null;
}

export interface BrowsingIntent {
  id: string;
  kind: string;
  status: string;
  bvid?: string;
  source_id?: string;
  text?: string | null;
  reason?: string | null;
  error?: string | null;
  auto_authorized?: boolean;
  round_id?: string | null;
  created_at?: number;
  updated_at?: number;
  receipt?: Record<string, unknown> | null;
  target?: {
    bvid?: string;
    title?: string;
    dynamic_id?: string;
    source_text?: string;
    recipient?: string | number;
    recipient_name?: string;
    parent?: string | number;
    image_ref?: Record<string, unknown> | null;
    [key: string]: unknown;
  } | null;
  continuation?: ContinuationRecord | null;
}

export interface CommentRelationship {
  affinity?: number | null;
  stage?: string | null;
}

export interface CommentRecord {
  id: number;
  comment_id: string;
  video_id: string;
  thread_id?: string | null;
  parent_comment_id?: string | null;
  platform?: string;
  platform_user_id?: string | null;
  display_name?: string | null;
  content: string;
  like_count?: number | null;
  category?: string | null;
  category_source?: string | null;
  relationship?: CommentRelationship | null;
  relationship_account_id?: string | null;
  continuation?: ContinuationRecord | null;
  draft?: {
    id: number | string;
    status: string;
    text?: string | null;
    comment_id?: number;
    [key: string]: unknown;
  } | null;
}

export interface CommentTaskRecord {
  id: string;
  kind?: string;
  type?: string;
  status: string;
  done?: number;
  total?: number;
  errors?: number;
  progress?: string | number;
  result?: string | number;
  completed_count?: number;
  error?: string | null;
  reason?: string | null;
  comment_ids?: number[];
  existing_drafts?: QueueRow[];
}

export interface IllustrationInfo {
  asset_id?: string | null;
  artifact_id?: string | null;
  sha256?: string | null;
  job_id?: string | null;
  status?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface MentionItem {
  uid: string | number;
  name: string;
}

export interface PendingMentionItem {
  label: string;
  account?: string;
  candidates?: Array<{ uid: string | number; name: string }>;
}

export interface QueueRow {
  channel: "comments" | "browsing" | "jobs" | string;
  id: string;
  status: string;
  source?: string;
  video_id?: string | null;
  excerpt?: string | null;
  updated_at?: number;
  comment_id?: number | null;
  can_publish?: boolean;
  send_error?: string | null;
  send_wake_at?: number | null;
  illustration?: IllustrationInfo | null;
  text?: string | null;
  error?: string | null;
  receipt?: unknown;
  source_url?: string | null;
  account_id?: string | null;
}

export interface QueueDetail extends QueueRow {
  author?: string | null;
  thread_id?: string | null;
  platform_user_id?: string | null;
  platform_comment_id?: string | null;
  character_id?: string | null;
  source_text?: string | null;
  target?: {
    bvid?: string;
    source_text?: string;
    recipient?: string | number;
    recipient_name?: string;
    parent?: string | number;
    dynamic_id?: string;
    image_ref?: Record<string, unknown> | null;
  } | null;
  send_queue?: {
    status: string;
    wake_at?: number;
    error?: string | null;
  } | null;
  image_ref?: Record<string, unknown> | null;
  illustration_choice?: string | null;
  can_attach_image?: boolean;
  image_uploads?: unknown[];
  mentions?: MentionItem[];
  pending_mentions?: PendingMentionItem[];
  continuation?: ContinuationRecord | null;
  round_id?: string | null;
  evidence_segments?: EvidenceSegment[];
  unavailable_materials?: number;
  answer_check?: unknown;
  visibility?: string | null;
  done?: number;
  total?: number;
  errors?: number;
}

export interface QueueListResponse {
  items: QueueRow[];
  total: number;
  offset: number;
  task?: CommentTaskRecord | null;
}

export interface MessageUser {
  platform: string;
  platform_user_id: string;
  display_name?: string | null;
}

export interface MessageDraft {
  id: number;
  platform: string;
  platform_user_id: string;
  display_name?: string | null;
  character_id?: string;
  account_id?: string;
  status: string;
  text?: string | null;
  source_kind?: string;
  source_id?: number;
  direction?: string;
  type?: string;
  contact_type?: string;
  contact_state?: string;
  outreach_status?: string;
  purpose?: string | null;
  reason?: string | null;
  error?: string | null;
  updated_at?: number;
  message_kind?: "text" | "image" | string;
  meme_id?: string | null;
  meme?: {
    asset_id: string;
    title?: string;
    enabled?: boolean;
    missing?: boolean;
    sha256?: string;
  } | null;
  illustration?: IllustrationInfo | null;
  illustration_choice?: string | null;
  image_ref?: Record<string, unknown> | null;
  image_uploads?: unknown[];
  delivery_steps?: unknown[];
  receipt?: unknown;
  reply_source_ids?: number[];
  trigger_source?:
    | string
    | {
        kind?: "comment" | "message" | string;
        bvid?: string;
        root?: string | number;
        comment_id?: string | number;
        ids?: number[];
        message_id?: string;
      }
    | null;
  outreach_check?: {
    previous_draft_id?: number;
    assessment?: {
      reason?: string;
      invitation_quote?: string;
    } | null;
  } | null;
  continuation?: ContinuationRecord | null;
}

export interface PrivateGenerationItem extends ContinuationRecord {
  source_title?: string | null;
}

export interface MessagesListResponse {
  account_id: string;
  character_id: string;
  policy: MessagePolicyState;
  users: MessageUser[];
  drafts: MessageDraft[];
  private_continuations?: PrivateGenerationItem[];
}

export interface InboxMessage {
  id: number;
  platform?: string;
  platform_user_id?: string;
  display_name?: string;
  content: string;
  sent_at: number;
}

export interface MessageConversationResponse {
  account_id?: string;
  character_id?: string;
  relationship?: {
    affinity?: number;
    stage?: string;
  } | null;
  messages: InboxMessage[];
  drafts: MessageDraft[];
}

export interface DynamicSourceCoordinate {
  part?: number | string;
  start?: number | string;
  href?: string;
}

export interface DynamicSourceItem {
  id?: string;
  type?: string;
  text?: string;
  source_url?: string;
  participants?: Array<{ name?: string; uid?: string | number }>;
  original_comment?: unknown;
  coordinates?: DynamicSourceCoordinate[];
}

export interface DynamicCommentCardComment {
  author?: string;
  uid?: string | number;
  reply_to?: string;
  parent_comment_id?: string | number;
  time_label?: string;
  created_at?: number | string;
  quote?: string;
  url?: string;
}

export interface DynamicCommentCard {
  comments?: DynamicCommentCardComment[];
  links?: Array<{ bvid?: string; url?: string }>;
  omitted?: number;
  missing?: number;
  page_count?: number;
}

export interface DynamicPost {
  id: number;
  account_id?: string;
  character_id?: string;
  character_version?: number;
  model?: string;
  content?: string;
  reason?: string | null;
  status: string;
  review_key?: string;
  created_at?: number;
  published_at?: number | null;
  meme_id?: string | null;
  illustration?: IllustrationInfo | null;
  illustration_choice?: string | null;
  comment_card?: DynamicCommentCard | null;
  source_comment_card?: DynamicCommentCard | null;
  sources?: DynamicSourceItem[];
  presentation?: {
    image_choice?: string;
    points?: Array<{ text: string }>;
  } | null;
  mentions?: MentionItem[];
  pending_mentions?: PendingMentionItem[];
  outgoing_links?: string[];
  image_uploads?: unknown[];
  receipt?: unknown;
  error?: string | null;
  continuation?: ContinuationRecord | null;
}

export interface DynamicDecision {
  id: number;
  created_at?: number;
  character_id?: string;
  manual?: boolean;
  action: string;
  status: string;
  reason?: string | null;
  error?: string | null;
  sources?: DynamicSourceItem[];
  continuation?: ContinuationRecord | null;
}

export interface DynamicMemory {
  id: number;
  text: string;
  character_id?: string;
  kind?: string;
  fictional?: boolean;
  public?: boolean;
  created_at?: number;
}

export interface DynamicMaterials {
  decision_id?: number | null;
  created_at?: number | null;
  sources?: DynamicSourceItem[];
}

export interface DynamicEditResult {
  request_id: string;
  platform_id?: string;
  status: "draft" | "submitting" | "pending_verification" | "applied" | string;
  error?: string | null;
}
