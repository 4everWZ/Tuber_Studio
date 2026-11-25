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
  emphasis?: string[];
}

export interface VisualAssetEntry {
  id?: string;
  title?: string;
  file: string;
  frames?: string[];
  frameDurationMs?: number;
}

export interface AquaVideoProps extends Record<string, unknown> {
  title: string;
  fps: number;
  width?: number;
  height?: number;
  totalFrames: number;
  audioFile?: string;
  includeAvatar?: boolean;
  bgmEnabled?: boolean;
  bgmFile?: string | null;
  transitionSfxFile?: string | null;
  avatarAssets?: Record<string, VisualAssetEntry>;
  memeAssets?: Record<string, VisualAssetEntry>;
  scenes: AquaSceneProp[];
  cues: AquaCueProp[];
}

export const AQUA_THEME = {
  ink: "#162a45",
  inkSoft: "#3b5575",
  muted: "#5f7896",
  cream: "#fffdf7",
  creamTint: "#fff8e8",
  sky: "#0ea5e9",
  skySoft: "#e0f2fe",
  mint: "#14b8a6",
  mintSoft: "#ccfbf1",
  yellow: "#fde047",
  amber: "#f59e0b",
  coral: "#fb7185",
  coralSoft: "#ffe4e6",
  border: "#1b3554",
  shadow: "6px 6px 0 rgba(27, 53, 84, 0.14)",
  shadowLg: "8px 8px 0 rgba(27, 53, 84, 0.16)",
};

export const CJK_FONT_STACK =
  '"PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Noto Sans CJK SC", "WenQuanYi Zen Hei", "Segoe UI", system-ui, sans-serif';

export const MONO_FONT_STACK =
  '"JetBrains Mono", "Cascadia Code", "Fira Code", Consolas, "Courier New", monospace';
