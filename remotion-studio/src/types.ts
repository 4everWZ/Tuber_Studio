export type TemplateId =
  | "TitleCard"
  | "CodeWalkthrough"
  | "ChartCompare"
  | "MemeCutaway"
  | "QuoteSpotlight"
  | "TimelineSummary";

export type AvatarCue =
  | "idle"
  | "cheerful"
  | "curious"
  | "proud"
  | "thinking"
  | "surprised"
  | "calm";

export interface ChartPoint {
  label: string;
  value: number;
  unit?: string;
}

export interface SceneVisualProps {
  subtitle?: string;
  bullets?: string[];
  code_snippet?: string;
  language?: string;
  chart_items?: ChartPoint[];
  meme_asset_id?: string | null;
  citation_label?: string;
}

export interface AquaSceneProp {
  scene_id: string;
  template: TemplateId | string;
  heading: string;
  start_frame: number;
  duration_frames: number;
  visual_props: SceneVisualProps;
  avatar_cue?: AvatarCue | string;
}

export interface AquaCueProp {
  line_id: string;
  scene_id?: string;
  text: string;
  start_frame: number;
  duration_frames: number;
  start_seconds?: number;
  end_seconds?: number;
  audio_file?: string;
  avatar_cue?: AvatarCue | string;
  highlight_code?: string | null;
}

export interface AquaVideoProps extends Record<string, unknown> {
  title: string;
  fps: number;
  width?: number;
  height?: number;
  totalFrames: number;
  audioFile?: string;
  includeAvatar?: boolean;
  scenes: AquaSceneProp[];
  cues: AquaCueProp[];
}

export const CJK_FONT_STACK =
  '"PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Noto Sans CJK SC", "Segoe UI", system-ui, sans-serif';

export const MONO_FONT_STACK =
  '"JetBrains Mono", "Cascadia Code", "Fira Code", Consolas, "Courier New", monospace';
