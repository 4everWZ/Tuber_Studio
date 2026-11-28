import type {
  ColdDirectorStatusData,
  DrawGuessStatusData,
  LiveShowsStatusData,
  OvertimeStatusData,
  RoastRadioStatusData,
  SceneData,
  TurtleSoupStatusData,
} from "./types";

export type ObsLayoutId = "chat" | "game";

export interface SceneLayoutState {
  active_layout: ObsLayoutId;
  layout_locked: boolean;
}

export interface SceneProfilesResponse {
  active_layout: ObsLayoutId;
  layout_locked: boolean;
  profiles: Record<ObsLayoutId, SceneData>;
  defaults: Record<ObsLayoutId, SceneData>;
}

export interface LivePollState {
  id: string;
  platform?: string;
  room_id?: string;
  topic: string;
  options: string[];
  counts?: Record<string, number>;
  total_votes?: number;
  started_at?: number;
  duration?: number;
  remaining_seconds?: number;
  phase?: string;
  fallback_label?: string;
}

export type RundownSegmentKind =
  | "chat"
  | "draw_and_guess"
  | "turtle_soup"
  | "roast_radio"
  | "song_party"
  | "stardew_valley"
  | "pvz";

export interface RundownSegment {
  id: string;
  title: string;
  kind: RundownSegmentKind;
  duration_minutes: number;
  obs_layout: ObsLayoutId;
  auto_start_show: boolean;
  graceful_finish: boolean;
  settings: Record<string, unknown>;
}

export interface ChatEntryConfig {
  opening_mode: "chat_opening" | "direct_rundown" | "manual";
  opening_chat_minutes: number;
  poll_duration_seconds: number;
  min_votes: number;
  include_continue_chat: boolean;
  avoid_repeat: boolean;
  allow_chat_vote_trigger: boolean;
  vote_trigger_threshold: number;
  allow_chat_extend: boolean;
  chat_adaptive_enabled: boolean;
  chat_hot_threshold_per_2m: number;
}

export interface RundownPlan {
  name: string;
  enabled_games: string[];
  hold_on_last: boolean;
  auto_switch_layout: boolean;
  chat_entry: ChatEntryConfig;
  segments: RundownSegment[];
}

export interface RundownRuntimeState {
  active: boolean;
  paused: boolean;
  status: string;
  current_index: number;
  current_segment?: RundownSegment | null;
  next_segment?: RundownSegment | null;
  remaining_seconds: number;
  waiting_graceful_finish?: boolean;
  hot_chat_prompt?: boolean;
  last_transition_line?: string;
}

export interface RundownPresetItem {
  id: string;
  name: string;
  description: string;
  plan: RundownPlan;
}

export interface RundownGameCatalogItem {
  kind: string;
  label: string;
  default_layout: ObsLayoutId;
  description: string;
}

export interface RundownStatusResponse {
  plan: RundownPlan;
  state: RundownRuntimeState;
  presets: RundownPresetItem[];
  game_catalog: RundownGameCatalogItem[];
}

export interface LiveFineSettings {
  cold_director: Record<string, number>;
  overtime: Record<string, number>;
  draw_and_guess: Record<string, unknown>;
  turtle_soup: Record<string, unknown>;
}

export interface ExtendedLiveShowsStatusData extends LiveShowsStatusData {
  poll?: LivePollState | null;
  rundown?: RundownRuntimeState | null;
  scene_layout?: SceneLayoutState | null;
}

export interface ShowStageLayerProps {
  shows?: ExtendedLiveShowsStatusData | null;
  drawGuess?: DrawGuessStatusData | null;
  drawGuessVisible?: boolean;
  turtleSoup?: TurtleSoupStatusData | null;
  roastRadio?: RoastRadioStatusData | null;
  poll?: LivePollState | null;
  rundown?: RundownRuntimeState | null;
  character?: LiveShowsStatusData["character"];
  previewStandby?: boolean;
  compact?: boolean;
  className?: string;
}

export interface OvertimeHudProps {
  shows?: ExtendedLiveShowsStatusData | null;
  overtime?: OvertimeStatusData | null;
  coldDirector?: ColdDirectorStatusData | null;
  rundown?: RundownRuntimeState | null;
  compact?: boolean;
  className?: string;
}
