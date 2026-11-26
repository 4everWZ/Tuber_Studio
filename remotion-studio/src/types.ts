export type TemplateId =
  | "TitleCard"
  | "CodeWalkthrough"
  | "ChartCompare"
  | "MemeCutaway"
  | "QuoteSpotlight"
  | "TimelineSummary"
  | "ChapterBumper"
  | "ArchitectureFlow"
  | "UIShowcase"
  | "KineticStatement";

export type SceneTheme = "dark_tech" | "aqua_light" | "cinema";

export type SceneTransition =
  | "cut"
  | "fade"
  | "slide_left"
  | "wipe_right"
  | "zoom_punch"
  | "flash";

export type AvatarLayout = "corner_float" | "compact" | "featured" | "hidden";

export type CameraMotion =
  | "static"
  | "push_in"
  | "pull_out"
  | "pan_right"
  | "focus_target";

export type LineSfx = "none" | "pop" | "whoosh" | "chime" | "chapter_hit";

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

export type DiagramTone = "sky" | "mint" | "amber" | "coral" | "slate";

export interface DiagramNode {
  id: string;
  label: string;
  sublabel?: string;
  lane?: string;
  tone?: DiagramTone;
}

export interface DiagramEdge {
  source: string;
  target: string;
  label?: string;
}

export interface CodeStep {
  title?: string;
  code_snippet: string;
  highlight_lines?: number[];
}

export interface UiFocusRegion {
  x: number;
  y: number;
  zoom?: number;
  label?: string;
  line_index?: number;
}

export interface SceneVisualProps {
  subtitle?: string;
  bullets?: string[];
  code_snippet?: string;
  language?: string;
  chart_items?: ChartPoint[];
  meme_asset_id?: string | null;
  citation_label?: string;
  uploaded_image_file?: string | null;
  chapter_number?: string | number;
  stat_callout?: string;
  diagram_nodes?: DiagramNode[];
  diagram_edges?: DiagramEdge[];
  code_steps?: CodeStep[];
  ui_focus_regions?: UiFocusRegion[];
  uploaded_video_file?: string | null;
}

export interface AquaSceneProp {
  scene_id: string;
  template: TemplateId | string;
  heading: string;
  start_frame: number;
  duration_frames: number;
  visual_props: SceneVisualProps;
  avatar_cue?: AvatarCue | string;
  chapter_title?: string;
  theme?: SceneTheme | string;
  transition?: SceneTransition | string;
  camera_motion?: CameraMotion | string;
  avatar_layout?: AvatarLayout | string;
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
  reveal_step?: number;
  visual_focus?: string;
  sfx?: LineSfx | string;
  line_index_in_scene?: number;
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
  audioEnvelope?: number[];
  sfxFiles?: Record<string, string>;
  duration_preset?: string;
}

export interface SceneTemplateProps {
  scene: AquaSceneProp;
  activeCue?: AquaCueProp;
  sceneCues?: AquaCueProp[];
  activeLineIndex?: number;
  memeAssets?: Record<string, VisualAssetEntry>;
  avatarLayout?: AvatarLayout;
  includeAvatar?: boolean;
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
