import { apiRequest } from "@/shared/api/client";
import type { StatusPillTone } from "@/shared";

export type VideoMode =
  | "commentary"
  | "daily_vlog"
  | "watched_roundup"
  | "project_showcase"
  | "explainer"
  | "self_intro";

export type DurationPreset = "short" | "standard" | "deep_dive";
export type SceneTheme = "dark_tech" | "aqua_light" | "cinema";
export type CameraMotion = "static" | "push_in" | "pull_out" | "pan_right" | "focus_target";
export type LineSfx = "none" | "pop" | "whoosh" | "chime" | "chapter_hit";
export type ContentChannel = "comment" | "dynamic" | "video";

export interface VideoChartPoint {
  label: string;
  value: number;
  unit?: string;
}

export interface VideoDiagramNode {
  id: string;
  label: string;
  sublabel?: string;
  lane?: string;
  tone?: "sky" | "mint" | "amber" | "coral" | "slate";
}

export interface VideoDiagramEdge {
  source: string;
  target: string;
  label?: string;
}

export interface VideoCodeStep {
  title?: string;
  code_snippet: string;
  highlight_lines?: number[];
}

export interface VideoUiFocusRegion {
  x: number;
  y: number;
  zoom?: number;
  label?: string;
  line_index?: number | null;
}

export interface VideoVisualProps {
  subtitle?: string;
  bullets?: string[];
  code_snippet?: string;
  language?: string;
  chart_items?: VideoChartPoint[];
  meme_asset_id?: string | null;
  citation_label?: string;
  uploaded_image_file?: string | null;
  uploaded_video_file?: string | null;
  chapter_number?: number | string | null;
  stat_callout?: string;
  diagram_nodes?: VideoDiagramNode[];
  diagram_edges?: VideoDiagramEdge[];
  code_steps?: VideoCodeStep[];
  ui_focus_regions?: VideoUiFocusRegion[];
}

export interface VideoLineSpec {
  line_id?: string;
  text: string;
  avatar_cue?: string;
  highlight_code?: string | null;
  emphasis?: string[];
  reveal_step?: number | null;
  visual_focus?: string | null;
  sfx?: LineSfx | string;
}

export interface VideoSceneSpec {
  scene_id: string;
  template: string;
  heading: string;
  chapter_title?: string;
  theme?: SceneTheme | string;
  transition?: string;
  camera_motion?: CameraMotion | string;
  avatar_layout?: string | null;
  visual_props?: VideoVisualProps;
  lines: VideoLineSpec[];
  avatar_cue?: string;
}

export interface VideoSourceRef {
  source_id: string;
  kind: string;
  scope: string;
  title?: string;
  url?: string | null;
  bvid?: string | null;
  excerpt?: string;
  fetch_status?: "fetched" | "fallback" | "manual" | null;
  fetch_error?: string | null;
}

export interface VideoUploadItem {
  filename: string;
  rel_path: string;
  url: string;
  size_bytes: number;
  mime_type?: string;
  kind?: "image" | "video";
}

export interface VideoEditorialFit {
  recommended_channel: ContentChannel;
  fit_score: number;
  video_mode: VideoMode;
  duration_preset: DurationPreset;
  reasoning: string;
  dynamic_alternative?: string;
  key_signals?: string[];
}

export interface VideoIdeaPitch {
  idea_id: string;
  title: string;
  topic: string;
  channel: ContentChannel;
  video_mode: VideoMode;
  duration_preset: DurationPreset;
  hook: string;
  visual_angle: string;
  source_refs?: string[];
  why_this_channel: string;
}

export interface VideoVisualReviewSceneVerdict {
  scene_index: number;
  verdict: "pass" | "polish" | "fix" | string;
  note: string;
}

export interface VideoVisualReview {
  ok?: boolean;
  reviewed: boolean;
  overall_score: number;
  monotony_risk: "low" | "medium" | "high" | "unknown" | string;
  layout_issues?: string[];
  suggestions?: string[];
  scenes?: VideoVisualReviewSceneVerdict[];
}

export interface VideoScriptPlan {
  title: string;
  summary: string;
  tags: string[];
  category_tid?: number;
  video_mode?: VideoMode;
  duration_preset?: DurationPreset;
  fps: 30 | 60;
  width: 1920 | 1280;
  height: 1080 | 720;
  include_avatar: boolean;
  bgm_enabled?: boolean;
  scenes: VideoSceneSpec[];
  sources: VideoSourceRef[];
}

export interface VideoQcReport {
  passed: boolean;
  issues?: string[];
  size_bytes?: number;
  expected_duration_seconds?: number;
  probed_duration_seconds?: number;
  duration_drift_seconds?: number;
  width?: number;
  height?: number;
  loudnorm_applied?: boolean;
  cover_generated?: boolean;
  contact_sheet_generated?: boolean;
  scene_frames?: number;
  visual_review?: VideoVisualReview | null;
}

export interface VideoManifestCue {
  line_id: string;
  scene_id: string;
  text: string;
  start_frame: number;
  duration_frames: number;
  start_seconds: number;
  end_seconds: number;
  avatar_cue?: string;
}

export interface VideoManifest {
  job_id: string;
  fps: number;
  width: number;
  height: number;
  total_frames: number;
  total_duration_seconds: number;
  tts_mode?: string;
  cues?: VideoManifestCue[];
}

export interface VideoJobRecord {
  id: string;
  status: string;
  publish_mode: "preview_review" | "auto_publish";
  title: string;
  content_hash?: string;
  project_hash?: string;
  intent?: string;
  planner_source?: string;
  planner_error?: string | null;
  editorial_fit?: VideoEditorialFit | null;
  plan?: VideoScriptPlan | null;
  manifest?: VideoManifest | null;
  qc_report?: VideoQcReport | null;
  receipt?: { bvid?: string; aid?: string } | null;
  error?: string | null;
  render_active?: boolean;
  uploads?: VideoUploadItem[];
  media?: Partial<
    Record<
      | "video"
      | "audio"
      | "subtitles"
      | "subtitles_vtt"
      | "cover"
      | "contact_sheet"
      | "props"
      | "manifest"
      | "code",
      string
    >
  >;
  created_at: number;
  updated_at: number;
}

export interface VideoPolicy {
  publish_mode: "preview_review" | "auto_publish";
  auto_publish_enabled: boolean;
  default_fps: 30 | 60;
  default_width: 1920 | 1280;
  default_height: 1080 | 720;
  max_render_concurrency: number;
  render_timeout_seconds: number;
  upload_timeout_seconds: number;
}

export interface VideoEnvironment {
  node?: { ready: boolean };
  npx?: { ready: boolean };
  ffmpeg?: { ready: boolean };
  ffprobe?: { ready: boolean };
  remotion_studio?: { ready: boolean; dir_exists?: boolean; label?: string };
  browser?: { ready: boolean; kind?: string | null };
  biliup?: { ready: boolean; mode?: string; web_credentials?: boolean; cli_installed?: boolean };
  omni_llm?: { ready: boolean };
  omni_tts?: { ready: boolean };
}

export interface VideoCandidate {
  bvid: string;
  title?: string;
  summary?: string;
  url?: string;
}

export interface VideoListResponse {
  jobs: VideoJobRecord[];
  policy: VideoPolicy;
  environment: VideoEnvironment;
  candidates: VideoCandidate[];
}

export type SupportedUploadMime =
  | "image/png"
  | "image/jpeg"
  | "image/webp"
  | "video/mp4"
  | "video/webm";

export interface ComposerUploadItem {
  id: string;
  filename: string;
  mime_type: SupportedUploadMime;
  kind: "image" | "video";
  image_base64: string;
  preview_url: string;
  size_bytes: number;
}

export type InspectorTab = "preview" | "script" | "code";
export type CodeArtifactTab = "props" | "code" | "subtitles" | "manifest";

export const MAX_REFERENCE_URLS = 6;
export const MAX_BRIEF_LENGTH = 30000;
export const MAX_UPLOAD_FILES = 12;
export const MAX_VIDEO_UPLOAD_BYTES = 30 * 1024 * 1024;
export const UPLOAD_ACCEPT_ATTR = "image/png,image/jpeg,image/webp,video/mp4,video/webm";

export const STATUS_META: Record<string, { label: string; tone: StatusPillTone }> = {
  script_planned: { label: "已策划脚本", tone: "neutral" },
  project_ready: { label: "工程与配音就绪", tone: "blue" },
  rendering: { label: "正在渲染", tone: "blue" },
  rendered: { label: "渲染完成", tone: "good" },
  ready_for_review: { label: "待人工审阅", tone: "good" },
  qc_failed: { label: "质检未通过", tone: "amber" },
  render_failed: { label: "渲染失败", tone: "bad" },
  published: { label: "已投稿发布", tone: "good" },
  publish_uncertain: { label: "投稿回执待核对", tone: "amber" },
  publish_failed: { label: "投稿失败", tone: "bad" },
};

export const TEMPLATE_LABELS: Record<string, string> = {
  TitleCard: "开场标题卡",
  CodeWalkthrough: "代码演进",
  ChartCompare: "数据对比图",
  MemeCutaway: "趣味吐槽插播",
  QuoteSpotlight: "观点引用聚焦",
  TimelineSummary: "时间轴总结",
  ChapterBumper: "章节转场卡",
  ArchitectureFlow: "架构数据流图",
  UIShowcase: "界面特写演示",
  KineticStatement: "动态金句冲击",
};

export const VIDEO_MODE_OPTIONS: Array<{
  value: VideoMode;
  label: string;
  hint: string;
}> = [
  {
    value: "commentary",
    label: "热点吐槽 / 杂谈",
    hint: "适合单视频或热点话题锐评吐槽，节奏明快（默认短评 ~1 分钟）。",
  },
  {
    value: "daily_vlog",
    label: "日常实验 / LoRA 微调日志",
    hint: "记录 LoRA 微调、调参前后对比、日常折腾与踩坑复盘。",
  },
  {
    value: "watched_roundup",
    label: "看片串烧 / 近期盘点",
    hint: "串联多部近期看过视频做看点提炼、打分对比与吐槽总结。",
  },
  {
    value: "project_showcase",
    label: "开源项目 / 作品介绍",
    hint: "展示开源仓库架构、核心代码亮点与工坊界面特写演示。",
  },
  {
    value: "explainer",
    label: "原理拆解 / 科普",
    hint: "机制剖析、数据流图解与深入浅出技术科普讲解。",
  },
  {
    value: "self_intro",
    label: "角色 / 工坊自我介绍",
    hint: "Aqua 角色形象、人设风格与 Tuber Studio 创作工坊正式介绍。",
  },
];

export const VIDEO_MODE_LABELS: Record<VideoMode, string> = Object.fromEntries(
  VIDEO_MODE_OPTIONS.map((item) => [item.value, item.label])
) as Record<VideoMode, string>;

export const DURATION_PRESET_OPTIONS: Array<{
  value: DurationPreset;
  label: string;
  defaultScenes: number;
}> = [
  { value: "short", label: "短评 ~1分 (short)", defaultScenes: 5 },
  { value: "standard", label: "精讲 ~2-3分 (standard)", defaultScenes: 9 },
  { value: "deep_dive", label: "深度长片 ~3.5-5分 (deep_dive)", defaultScenes: 14 },
];

export const DURATION_PRESET_LABELS: Record<DurationPreset, string> = {
  short: "短评 ~1分",
  standard: "精讲 ~2-3分",
  deep_dive: "深度长片 ~3.5-5分",
};

export const DEFAULT_DURATION_BY_MODE: Record<VideoMode, DurationPreset> = {
  commentary: "short",
  daily_vlog: "standard",
  watched_roundup: "standard",
  project_showcase: "standard",
  explainer: "standard",
  self_intro: "standard",
};

export const DEFAULT_SCENES_BY_DURATION: Record<DurationPreset, number> = {
  short: 5,
  standard: 9,
  deep_dive: 14,
};

export const SCENE_THEME_LABELS: Record<string, { label: string; tone: StatusPillTone }> = {
  dark_tech: { label: "深海科技", tone: "blue" },
  aqua_light: { label: "浅海手账", tone: "good" },
  cinema: { label: "电影过场", tone: "amber" },
};

export const CAMERA_MOTION_LABELS: Record<string, string> = {
  static: "静止机位",
  push_in: "镜头推进",
  pull_out: "镜头拉远",
  pan_right: "横向平移",
  focus_target: "焦点放大",
};

export const SFX_LABELS: Record<string, string> = {
  none: "无",
  pop: "气泡弹出 (pop)",
  whoosh: "气流感 (whoosh)",
  chime: "清脆提示 (chime)",
  chapter_hit: "章节重击 (chapter_hit)",
};

export const CHANNEL_META: Record<ContentChannel, { label: string; tone: StatusPillTone }> = {
  video: { label: "适合视频", tone: "good" },
  dynamic: { label: "建议动态", tone: "amber" },
  comment: { label: "建议评论", tone: "neutral" },
};

export const FETCH_STATUS_META: Record<string, { label: string; tone: StatusPillTone }> = {
  fetched: { label: "已抓取", tone: "good" },
  fallback: { label: "抓取回退", tone: "amber" },
  manual: { label: "手动资料", tone: "neutral" },
};

export function inferUploadMimeType(file: File): SupportedUploadMime | null {
  const mime = (file.type || "").toLowerCase();
  if (
    mime === "image/png" ||
    mime === "image/jpeg" ||
    mime === "image/webp" ||
    mime === "video/mp4" ||
    mime === "video/webm"
  ) {
    return mime;
  }
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".webp")) return "image/webp";
  if (lower.endsWith(".mp4")) return "video/mp4";
  if (lower.endsWith(".webm")) return "video/webm";
  return null;
}

export function isVideoMime(mime: string): boolean {
  return mime === "video/mp4" || mime === "video/webm";
}

export function isVideoUploadItem(item: Pick<VideoUploadItem, "filename" | "mime_type" | "kind">): boolean {
  if (item.kind === "video") return true;
  if (item.mime_type && isVideoMime(item.mime_type)) return true;
  return /\.(mp4|webm)$/i.test(item.filename);
}

export function readMediaDataUrl(file: File): Promise<{ dataUrl: string; base64: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const base64 = dataUrl.includes(",") ? dataUrl.split(",", 2)[1] : dataUrl;
      if (!base64) {
        reject(new Error("读取本地媒体文件失败"));
        return;
      }
      resolve({ dataUrl, base64 });
    };
    reader.onerror = () => reject(new Error("读取本地媒体文件失败"));
    reader.readAsDataURL(file);
  });
}

export function safeHttpsUrl(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed.startsWith("https://")) return null;
  try {
    const parsed = new URL(trimmed);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export function formatBytes(bytes?: number): string {
  if (!bytes || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export async function videoRequest<T>(
  path: string,
  options: { method?: string; body?: unknown; responseType?: "json" | "text" } = {}
): Promise<T> {
  return apiRequest<T>(path, {
    method: options.method || "GET",
    body: options.body,
    responseType: options.responseType,
  });
}
