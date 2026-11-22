import type { OperationCapabilityStatus } from "@/shared/ui/CapabilityStrip";

export interface ChatSourceEntry {
  kind?: string;
  state?: string;
  author?: string | null;
  uid?: string | number | null;
  part?: number | null;
  start?: number | null;
  text?: string | null;
  original_text?: string | null;
  published_at?: string | null;
  href?: string | null;
}

export interface ChatSourceGroup {
  title: string;
  entries: ChatSourceEntry[];
}

export interface OpenedArtifactRef {
  task_id: string;
  task_revision: number;
  edition: number;
}

export interface ChatCard {
  type: string;
  data: Record<string, unknown>;
}

export interface ChatTurn {
  request_id: string;
  message?: string;
  assistant_only?: boolean;
  hidden?: boolean;
  character_name?: string | null;
  reply_text?: string;
  reply_format?: "markdown" | "plain" | string;
  reply_sections?: string[];
  sources?: ChatSourceGroup[];
  card?: ChatCard | null;
  memory_dependent?: boolean;
  task_reference?: string | null;
  task_delivery?: {
    task_id: string;
    revision: number;
    status: string;
    step_id?: string | null;
  } | null;
  tool_receipt?: {
    group?: string;
    values?: Record<string, unknown>;
  } | null;
  stopped?: boolean;
}

export interface PetAssetRef {
  asset_id: string;
  title?: string | null;
  preview_url?: string | null;
  animated?: boolean;
  manifest_owned?: boolean;
  source?: {
    kind?: string | null;
    author?: string | null;
    url?: string | null;
    revision?: string | null;
    note?: string | null;
  } | null;
  license?: {
    name?: string | null;
    url?: string | null;
    note?: string | null;
  } | null;
}

export interface PetSelectionState {
  character_id: string;
  revision: number;
  selected: PetAssetRef | null;
  items?: PetAssetRef[];
}

export interface PetCharacterInfo {
  character_id: string;
  character_version?: number | string | null;
  avatar?: string | null;
  avatar_still?: string | null;
  pet?: PetSelectionState | null;
}

export interface ChatHistoryResponse {
  character: string;
  avatar?: string | null;
  avatar_still?: string | null;
  pet?: PetSelectionState | null;
  character_id?: string;
  character_version?: number | string | null;
  account_id?: string | null;
  conversation_id?: string | null;
  turns: ChatTurn[];
  next_cursor?: string | null;
}

export interface VideoRangeInput {
  part: number;
  start: number;
  end: number;
}

export interface VideoSelectionInput {
  bvid: string;
  ranges: VideoRangeInput[];
}

export interface ChatSubmissionPayload {
  message: string;
  intent?: {
    action: string;
    topic?: string;
    reference?: string;
  };
  video_selection?: VideoSelectionInput;
  opened_artifact?: OpenedArtifactRef;
}

export interface ChatRequestPayload extends ChatSubmissionPayload {
  request_id: string;
  conversation_id?: string;
}

export interface AgentTaskStep {
  id: string;
  tool: string;
  arguments?: Record<string, unknown>;
  revision?: number;
  result?: Record<string, unknown> | null;
  approval?: Record<string, unknown> | null;
}

export interface CreativeGoalRun {
  id: string;
  goal: string;
  status: string;
  revision: number;
}

export interface CreativeGoal {
  id: string;
  title: string;
  closed: boolean;
  runs: CreativeGoalRun[];
  stop_scope?: Record<string, number>;
}

export interface CreativeEdition {
  task_revision: number;
  edition: number;
  author?: string;
  author_name?: string;
  body: string;
}

export interface AgentTask {
  id: string;
  goal: string;
  status: string;
  revision: number;
  due?: number;
  updated?: number;
  message?: string;
  pause_requested?: boolean;
  can_control?: boolean;
  attention?: number | null;
  delivered_in_chat?: boolean;
  steps: AgentTaskStep[];
  creative_goal?: CreativeGoal | null;
  artifact?: CreativeEdition | null;
}

export interface AgentTaskActivityResponse {
  tasks: AgentTask[];
  goals?: Array<{ id: string; title: string; closed: boolean }>;
  attention?: Array<{ id: string; goal: string; status: string }>;
}

export interface DynamicApprovalPost {
  post_id: number;
  text: string;
  image_hashes?: string[];
  payload?: {
    mentions?: Array<{ uid: string | number; name: string }>;
  };
}

export interface DynamicApprovalExcluded {
  post_id: number;
  reason: string;
}

export interface DynamicPublicationApprovalData {
  kind: "dynamic_publication";
  target: string;
  expires_at: number;
  carried_ids?: number[];
  posts: DynamicApprovalPost[];
  excluded?: DynamicApprovalExcluded[];
}

export interface BrowserControlItem {
  index: number;
  kind: string;
  label?: string;
}

export interface BrowserSessionState {
  mode: "ungranted" | "ended" | "owner" | "agent";
  revision: number;
  version?: number;
  position: number;
  sources: string[];
  view?: {
    image?: string;
    text?: string;
    controls?: BrowserControlItem[];
  };
}

export interface GoalEventState {
  source?: string;
  account?: string | null;
  character?: string | null;
  epoch?: number | null;
  closed?: boolean;
  enabled: boolean;
  sender_uid?: string | null;
  after_reply?: string | null;
  revision: number;
}

export interface GoalCommentEventState {
  account?: string | null;
  character?: string | null;
  epoch?: number | null;
  closed?: boolean;
  enabled: boolean;
  resource?: string | null;
  root_id?: string | null;
  revision: number;
}

export interface GoalFollowupState {
  epoch?: number | null;
  enabled: boolean;
  next_due?: number | null;
  schedule?: {
    cadence?: "daily" | "weekly";
    weekday?: number | null;
    local_time?: string;
    timezone?: string;
    states?: string[];
    pages?: string[];
  } | null;
}

export interface AutomationControlStatus extends OperationCapabilityStatus {
  account_id?: string | null;
  character?: { id?: string; version?: number | string } | null;
  settings?: Record<string, unknown> | null;
  base?: Record<string, Record<string, unknown>> | null;
}

export interface CallOrigin {
  ticket_hash: string;
  scope_signature: string;
}

export interface VoiceOrigin extends CallOrigin {
  capture_id: string;
  machine_text: string;
  receipt: string;
  samples?: number;
  transcript_receipt?: string;
  [key: string]: unknown;
}

export interface PrivateCallAvailability {
  configured: boolean;
  character?: string;
  recordings?: Record<string, string>;
}

export interface PrivateCallAvatarsResponse {
  profiles: string[];
  selection?: unknown;
}

export interface MemoryCitationItem {
  id: number | string;
  note: string;
  revision: number;
}

export interface PrivateCallTicket {
  socket: string;
  ticket: string;
  call_scope: string;
  character: string;
  voice: {
    id: string;
    name?: string;
    reference_sha256: string;
  };
  voice_context?: CallOrigin | null;
  memory_use?: {
    ids: Array<number | string>;
    stamp: string;
    topic_source?: Record<string, unknown>;
  } | null;
  topic_source?: Record<string, unknown> | null;
  memory_citations?: MemoryCitationItem[];
  correction_targets?: Array<number | string>;
  avatar?: {
    still?: string | null;
    speaking?: string | null;
    fallback?: string | null;
  } | null;
  instructions: string;
}

export interface PrivateCallWorkTask {
  id: string;
  goal: string;
  revision: number;
  status: string;
  message?: string | null;
  updated?: number;
}

export function createRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

export function isPlatformLink(value: unknown): value is string {
  return typeof value === "string" && /^https:\/\/(?:www\.|t\.)bilibili\.com\//.test(value);
}

export function safeHttpsUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password) {
      return url.href;
    }
  } catch {
    // Invalid URLs stay non-clickable.
  }
  return null;
}
