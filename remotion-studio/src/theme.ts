import type {
  AquaSceneProp,
  AvatarLayout,
  DiagramTone,
  SceneTheme,
} from "./types";

export interface ThemePalette {
  id: SceneTheme;
  isDark: boolean;
  bgBase: string;
  ink: string;
  inkSoft: string;
  muted: string;
  surface: string;
  surfaceElevated: string;
  surfaceMuted: string;
  border: string;
  borderStrong: string;
  accent: string;
  accentSoft: string;
  secondary: string;
  secondarySoft: string;
  highlight: string;
  highlightSoft: string;
  danger: string;
  dangerSoft: string;
  cardShadow: string;
  pillText: string;
  subtitleBg: string;
  subtitleBorder: string;
  subtitleInk: string;
}

const DARK_TECH_PALETTE: ThemePalette = {
  id: "dark_tech",
  isDark: true,
  bgBase: "#070d1a",
  ink: "#f1f5f9",
  inkSoft: "#cbd5e1",
  muted: "#94a3b8",
  surface: "rgba(15, 23, 42, 0.82)",
  surfaceElevated: "rgba(22, 33, 58, 0.92)",
  surfaceMuted: "rgba(30, 41, 59, 0.65)",
  border: "rgba(56, 189, 248, 0.24)",
  borderStrong: "rgba(56, 189, 248, 0.55)",
  accent: "#38bdf8",
  accentSoft: "rgba(56, 189, 248, 0.16)",
  secondary: "#2dd4bf",
  secondarySoft: "rgba(45, 212, 191, 0.16)",
  highlight: "#fbbf24",
  highlightSoft: "rgba(251, 191, 36, 0.18)",
  danger: "#fb7185",
  dangerSoft: "rgba(251, 113, 133, 0.18)",
  cardShadow: "0 20px 50px rgba(2, 6, 23, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.08)",
  pillText: "#e0f2fe",
  subtitleBg: "rgba(9, 14, 28, 0.82)",
  subtitleBorder: "rgba(56, 189, 248, 0.3)",
  subtitleInk: "#f8fafc",
};

const AQUA_LIGHT_PALETTE: ThemePalette = {
  id: "aqua_light",
  isDark: false,
  bgBase: "#f3f9ff",
  ink: "#0f223d",
  inkSoft: "#334e6f",
  muted: "#647d9c",
  surface: "rgba(255, 255, 255, 0.88)",
  surfaceElevated: "rgba(255, 255, 255, 0.96)",
  surfaceMuted: "rgba(226, 240, 253, 0.72)",
  border: "rgba(15, 34, 61, 0.12)",
  borderStrong: "rgba(14, 165, 233, 0.45)",
  accent: "#0284c7",
  accentSoft: "rgba(14, 165, 233, 0.12)",
  secondary: "#0d9488",
  secondarySoft: "rgba(20, 184, 166, 0.13)",
  highlight: "#d97706",
  highlightSoft: "rgba(253, 224, 71, 0.38)",
  danger: "#e11d48",
  dangerSoft: "rgba(251, 113, 133, 0.16)",
  cardShadow: "0 18px 42px rgba(15, 34, 61, 0.08), 0 2px 10px rgba(14, 165, 233, 0.06)",
  pillText: "#0c4a6e",
  subtitleBg: "rgba(255, 255, 255, 0.9)",
  subtitleBorder: "rgba(15, 34, 61, 0.14)",
  subtitleInk: "#0f223d",
};

const CINEMA_PALETTE: ThemePalette = {
  id: "cinema",
  isDark: true,
  bgBase: "#090a0f",
  ink: "#fafafa",
  inkSoft: "#d4d4d8",
  muted: "#a1a1aa",
  surface: "rgba(20, 20, 26, 0.84)",
  surfaceElevated: "rgba(28, 28, 36, 0.94)",
  surfaceMuted: "rgba(39, 39, 48, 0.72)",
  border: "rgba(255, 255, 255, 0.14)",
  borderStrong: "rgba(245, 158, 11, 0.55)",
  accent: "#f59e0b",
  accentSoft: "rgba(245, 158, 11, 0.16)",
  secondary: "#38bdf8",
  secondarySoft: "rgba(56, 189, 248, 0.15)",
  highlight: "#fde047",
  highlightSoft: "rgba(253, 224, 71, 0.18)",
  danger: "#fb7185",
  dangerSoft: "rgba(251, 113, 133, 0.18)",
  cardShadow: "0 24px 60px rgba(0, 0, 0, 0.65), inset 0 1px 0 rgba(255, 255, 255, 0.08)",
  pillText: "#fef3c7",
  subtitleBg: "rgba(10, 10, 14, 0.84)",
  subtitleBorder: "rgba(255, 255, 255, 0.18)",
  subtitleInk: "#ffffff",
};

export function resolveSceneTheme(theme?: string): SceneTheme {
  if (theme === "dark_tech" || theme === "aqua_light" || theme === "cinema") {
    return theme;
  }
  return "aqua_light";
}

export function getThemePalette(theme?: string): ThemePalette {
  const resolved = resolveSceneTheme(theme);
  if (resolved === "dark_tech") {
    return DARK_TECH_PALETTE;
  }
  if (resolved === "cinema") {
    return CINEMA_PALETTE;
  }
  return AQUA_LIGHT_PALETTE;
}

export function resolveAvatarLayout(
  scene?: AquaSceneProp,
  includeAvatar = true
): AvatarLayout {
  if (!includeAvatar) {
    return "hidden";
  }
  const explicit = scene?.avatar_layout;
  if (
    explicit === "corner_float" ||
    explicit === "compact" ||
    explicit === "featured" ||
    explicit === "hidden"
  ) {
    return explicit;
  }
  const tpl = scene?.template;
  if (tpl === "ChapterBumper" || tpl === "KineticStatement") {
    return "hidden";
  }
  if (tpl === "CodeWalkthrough" || tpl === "ArchitectureFlow" || tpl === "UIShowcase") {
    return "compact";
  }
  return "corner_float";
}

export function getAvatarRightClearance(layout: AvatarLayout): number {
  switch (layout) {
    case "featured":
      return 286;
    case "corner_float":
      return 228;
    case "compact":
      return 164;
    case "hidden":
    default:
      return 48;
  }
}

export function getToneAccent(
  tone: DiagramTone | undefined,
  palette: ThemePalette
): { stroke: string; bg: string; text: string } {
  switch (tone) {
    case "mint":
      return {
        stroke: palette.secondary,
        bg: palette.secondarySoft,
        text: palette.isDark ? "#5eead4" : "#0f766e",
      };
    case "amber":
      return {
        stroke: palette.highlight,
        bg: palette.highlightSoft,
        text: palette.isDark ? "#fcd34d" : "#b45309",
      };
    case "coral":
      return {
        stroke: palette.danger,
        bg: palette.dangerSoft,
        text: palette.isDark ? "#fda4af" : "#be123c",
      };
    case "slate":
      return {
        stroke: palette.muted,
        bg: palette.surfaceMuted,
        text: palette.inkSoft,
      };
    case "sky":
    default:
      return {
        stroke: palette.accent,
        bg: palette.accentSoft,
        text: palette.isDark ? "#7dd3fc" : "#0369a1",
      };
  }
}
