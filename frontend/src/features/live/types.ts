export type SceneItemKind =
  | "avatar"
  | "title"
  | "now"
  | "lyrics"
  | "queue"
  | "chat"
  | "gifts"
  | "decoration";

export type SceneBackground = "ocean" | "transparent";

export interface SceneItem {
  id: string;
  kind: SceneItemKind;
  x: number;
  y: number;
  width: number;
  height: number;
  z: number;
  visible: boolean;
  locked: boolean;
  text: string;
}

export interface SceneData {
  width: number;
  height: number;
  title: string;
  background: SceneBackground;
  items: SceneItem[];
}

export interface LiveAsset {
  id?: string;
  url?: string;
  path?: string;
  still_url?: string | null;
}

export interface Live2DMotion {
  group: string;
  index: number;
}

export interface Live2DManifest {
  status?: string;
  message?: string;
  model: string;
  scripts: string[];
  mouth_parameter: string;
  mouth_gain?: number;
  motions: Record<string, Live2DMotion>;
  expressions: Record<string, string>;
  license?: string;
}

export interface SongQuality {
  format: string;
  bitrate?: number;
  bits_per_sample?: number;
  sample_rate?: number;
}

export interface SongLyricsLine {
  time: number;
  text: string;
}

export interface SongItem {
  id: string;
  title: string;
  artist?: string;
  audio?: string;
  vocal?: string | null;
  accompaniment?: string | null;
  license_note?: string;
  live_allowed?: boolean;
  commercial_allowed?: boolean;
  lyrics?: SongLyricsLine[];
  cover?: string | null;
  source?: string;
  quality?: SongQuality | null;
}

export interface PlaybackMediaSnapshot {
  mode: "direct" | "sing" | string;
  song?: SongItem | null;
  requester?: string | null;
}

export interface PlaybackSnapshot {
  song?: PlaybackMediaSnapshot | null;
  media?: PlaybackMediaSnapshot | null;
  text?: string | null;
  animation_id?: string | null;
  emotion?: string | null;
  position?: number;
  duration?: number;
  started?: boolean;
  paused?: boolean;
  mouth?: number;
  visual_warning?: string | null;
}

export interface LiveQueueItem {
  id: string;
  type: string;
  text?: string | null;
  requester?: string | null;
  media?: PlaybackMediaSnapshot | null;
  preparation?: [string, string | null] | null;
}

export interface LiveEventItem {
  id?: string | number;
  type: string;
  display_name?: string;
  requester?: string;
  text?: string;
  status?: string;
  reason?: string | null;
  received_at?: number;
}

export interface LiveDialogueItem {
  id: string;
  event_id?: string | number;
  type: string;
  requester?: string;
  status: string;
  text?: string | null;
  song_title?: string | null;
  error?: string | null;
  created_at?: number;
}

export interface LiveCurrentJob {
  id?: string;
  type?: string;
  status?: string;
  requester?: string | null;
  error?: string | null;
  song?: {
    title?: string;
    artist?: string;
    mode?: string;
  } | null;
}

export interface LivePollData {
  id?: string;
  topic: string;
  options: string[];
  counts?: Record<string, number>;
  total_votes?: number;
  complete?: boolean;
  result?: string;
}

export interface LiveViewData {
  scene: SceneData;
  queue: LiveQueueItem[];
  events: LiveEventItem[];
  character?: {
    id?: string;
    version?: number | string;
    name?: string;
  } | null;
  performance?: {
    pets_enabled?: boolean;
    avatar_enabled?: boolean;
  } | null;
  lip_sync?: {
    provider?: string;
    status?: string;
    renderer?: string;
  } | null;
  assets?: LiveAsset[];
  live2d?: Live2DManifest | null;
  playback?: PlaybackSnapshot | null;
  state: string;
  muted?: boolean;
  server_time?: number;
  overlay_connected?: boolean;
  activities?: LivePollData[];
  poll?: LivePollData | null;
  weather_attribution?: boolean;
}

export interface LiveStudioData extends LiveViewData {
  session_id?: string | null;
  chat?: LiveEventItem[];
  dialogue?: LiveDialogueItem[];
  current?: LiveCurrentJob | null;
  last_spoken?: {
    text?: string | null;
  } | null;
  timings?: Record<string, unknown>;
  error?: string | null;
  live_enabled?: boolean;
  inference_transitioning?: boolean;
}

export interface LiveMusicSettings {
  default_mode: "direct" | "sing";
  volume: number;
  queue_limit: number;
  cooldown_seconds: number;
  request_ttl_seconds?: number;
}

export interface LiveSingingStatus {
  available: boolean;
  reason: string;
}

export interface LiveMusicData {
  settings: LiveMusicSettings;
  songs: SongItem[];
  singing: LiveSingingStatus;
}

export interface OnlineSongItem {
  id: string;
  title: string;
  artist: string;
  album?: string | null;
  duration: number;
  cover?: string | null;
}

export interface OnlineCandidateGroup {
  token: string;
  requester: string;
  songs: OnlineSongItem[];
}

export type OnlineSongSource = "bodian" | "migu";

export interface OnlineSongSettings {
  enabled: boolean;
  usage: "broadcast" | "local_test";
  sources: OnlineSongSource[];
  quality: "flac_preferred" | "high_bitrate";
  fallback_bitrate: number;
  allow_lower_quality: boolean;
  cache_limit_mib: number;
  max_download_mib: number;
  max_duration_seconds: number;
  license_note: string;
  live_allowed: boolean;
  commercial_allowed: boolean;
}

export interface OnlineComponentStatus {
  installed: boolean;
  running: boolean;
  error?: string | null;
  migu_configured?: boolean;
}

export interface OnlineSongsStatus {
  settings: OnlineSongSettings;
  component: OnlineComponentStatus;
  cache_bytes: number;
  candidates: OnlineCandidateGroup[];
}

export interface WeatherLocation {
  name: string;
  latitude: number;
  longitude: number;
}

export interface LiveOptionsData {
  timezone: string | null;
  web_fetch: boolean;
  weather: WeatherLocation | null;
  idle: boolean;
  idle_text: string;
}

export interface CacheCategoryStats {
  bytes: number;
  limit_bytes: number;
  reclaimable_bytes: number;
  protected_bytes: number;
  writing?: boolean;
}

export interface CacheStatusData {
  categories: Record<string, CacheCategoryStats | undefined>;
  settings: Record<string, number>;
  result?: {
    freed_bytes: number;
    removed_files: number;
  };
}

export interface SongRequestRule {
  mode: "free" | "gift_only" | "gift_or_free" | string;
  min_coin: number;
  gift_names: string[];
  danmaku_command?: string;
}

export interface DrawAndGuessRule {
  enabled: boolean;
  trigger_mode: "gift_only" | "danmaku_or_gift" | "host_only" | string;
  min_coin: number;
  gift_names: string[];
  danmaku_command?: string;
  guess_mode?: string;
  round_duration_sec: number;
  cooldown_sec?: number;
}

export interface InteractiveRulesData {
  song_request?: SongRequestRule;
  draw_and_guess?: DrawAndGuessRule;
  game_livestream?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GameStatusData {
  active_game_id?: string;
  owner?: "operator" | "studio" | string;
  active_status?: {
    display_name?: string;
    summary?: string;
    current_activity?: string;
    [key: string]: unknown;
  };
}

export interface DrawGuessStroke {
  type: "path" | "circle" | "ellipse" | string;
  d?: string;
  fill?: string;
  stroke?: string;
  width?: number;
  cx?: number;
  cy?: number;
  r?: number;
  rx?: number;
  ry?: number;
}

export interface DrawGuessStatusData {
  active?: boolean;
  state?: string;
  remaining?: number;
  category?: string;
  hint?: string;
  word?: string;
  winner?: string | null;
  trigger_user?: string;
  revealed_strokes?: DrawGuessStroke[];
  total_strokes?: number;
  character?: string;
}

export interface StardewFarmSnapshot {
  save?: string;
  location?: string;
  x?: number;
  y?: number;
  season?: string;
  day?: number;
  time?: string;
  stamina?: number;
  money?: number;
  slot: number;
  inventory: Array<{ item?: string | null } | null>;
  front?: { key: string } | null;
}

export interface StardewDeviceState {
  session: string;
  epoch: number;
  owner: "operator" | "studio" | string;
  can_act?: boolean;
  farm?: StardewFarmSnapshot | null;
}

export interface StardewDeviceStatus {
  configured: boolean;
  online: boolean;
  state: StardewDeviceState | null;
}

export interface StardewActionReceipt {
  status: string;
  reason?: string;
  error?: string;
}

export interface OverlayPlayMessage {
  type: "play";
  job_id: string;
  playback_token: string;
  audio_ref?: string;
  volume?: number;
  media?: PlaybackMediaSnapshot | null;
  text?: string | null;
  animation_id?: string | null;
  emotion?: string | null;
  duration?: number;
  activity?: {
    step: number;
    envelope: number[];
  } | null;
}

export type OverlayWsMessage =
  | { type: "hello" }
  | { type: "standby"; reason?: string }
  | OverlayPlayMessage
  | { type: "stop"; job_id?: string }
  | { type: "character" }
  | { type: "media-control"; job_id: string; paused: boolean };

export interface OverlayAudioActivity {
  step: number;
  envelope: number[];
}

export interface OverlayActiveJob extends OverlayPlayMessage {
  generation: number;
  started: boolean;
}

export interface BilibiliAccountSnapshot {
  state?: string;
  checked_at?: number;
  error?: string | null;
  configured?: {
    sessdata?: boolean;
    csrf?: boolean;
  } | null;
  profile?: {
    uid?: string | number | null;
    name?: string | null;
    coins?: number | null;
    fans?: number | null;
    avatar_url?: string | null;
  } | null;
}
