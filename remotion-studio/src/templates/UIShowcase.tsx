import React from "react";
import {
  Img,
  OffthreadVideo,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import {
  CJK_FONT_STACK,
  MONO_FONT_STACK,
  type SceneTemplateProps,
  type UiFocusRegion,
} from "../types";
import { getRevealStep } from "../utils/beatSync";

function normalizeCoord(raw: number | undefined, fallback: number): number {
  if (typeof raw !== "number" || !Number.isFinite(raw)) {
    return fallback;
  }
  const pct = raw >= 0 && raw <= 1 ? raw * 100 : raw;
  return Math.min(94, Math.max(6, pct));
}

export const UIShowcase: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  avatarLayout = "compact",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};
  const regions: UiFocusRegion[] =
    vp.ui_focus_regions && vp.ui_focus_regions.length > 0
      ? vp.ui_focus_regions
      : [
          { x: 0.36, y: 0.34, zoom: 1.18, label: vp.bullets?.[0] || "核心交互区" },
          { x: 0.68, y: 0.62, zoom: 1.24, label: vp.bullets?.[1] || "状态与反馈" },
        ];

  let activeIdx = getRevealStep(
    activeCue,
    activeLineIndex,
    regions.length,
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  const byLine = regions.findIndex((r) => r.line_index === activeLineIndex);
  if (byLine >= 0) {
    activeIdx = byLine;
  }
  const focusLabel = (activeCue?.visual_focus || "").trim().toLowerCase();
  if (focusLabel) {
    const matched = regions.findIndex((r) =>
      (r.label || "").toLowerCase().includes(focusLabel)
    );
    if (matched >= 0) {
      activeIdx = matched;
    }
  }

  const targetRegion = regions[activeIdx] ?? regions[0];
  const prevRegion = regions[Math.max(0, activeIdx - 1)] ?? targetRegion;

  const cueLocal = activeCue ? Math.max(0, frame - activeCue.start_frame) : frame;
  const moveSpring = spring({
    frame: cueLocal,
    fps,
    config: { damping: 18, stiffness: 110, mass: 0.85 },
  });

  const targetX = normalizeCoord(targetRegion.x, 50);
  const targetY = normalizeCoord(targetRegion.y, 50);
  const prevX = normalizeCoord(prevRegion.x, targetX);
  const prevY = normalizeCoord(prevRegion.y, targetY);

  const curX = interpolate(moveSpring, [0, 1], [prevX, targetX]);
  const curY = interpolate(moveSpring, [0, 1], [prevY, targetY]);
  const targetZoom = Math.min(2.2, Math.max(1.0, targetRegion.zoom ?? 1.25));
  const prevZoom = Math.min(2.2, Math.max(1.0, prevRegion.zoom ?? 1.12));
  const curZoom = interpolate(moveSpring, [0, 1], [prevZoom, targetZoom]);

  const rippleProgress = (cueLocal % 36) / 36;
  const rippleScale = interpolate(rippleProgress, [0, 1], [0.4, 2.3]);
  const rippleOpacity = interpolate(rippleProgress, [0, 0.2, 1], [0, 0.75, 0]);

  const windowEnter = spring({
    frame,
    fps,
    config: { damping: 16, stiffness: 130 },
  });
  const rightClearance = getAvatarRightClearance(avatarLayout);
  const hasVideo = Boolean(vp.uploaded_video_file);
  const hasImage = Boolean(vp.uploaded_image_file);

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        gap: 14,
        padding: `18px ${rightClearance}px 124px 44px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16 }}>
        <div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "4px 14px",
              borderRadius: 999,
              backgroundColor: palette.secondarySoft,
              border: `1px solid ${palette.borderStrong}`,
              fontSize: 14,
              fontWeight: 800,
              color: palette.pillText,
              letterSpacing: 1,
              marginBottom: 8,
            }}
          >
            UI SHOWCASE · 界面演示
          </div>
          <div style={{ fontSize: 40, fontWeight: 900, color: palette.ink, lineHeight: 1.15 }}>
            {scene.heading}
          </div>
        </div>
        {regions.length > 0 ? (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            {regions.map((reg, idx) => {
              const active = idx === activeIdx;
              return (
                <div
                  key={`${reg.label || idx}-${idx}`}
                  style={{
                    padding: "5px 12px",
                    borderRadius: 999,
                    backgroundColor: active ? palette.accentSoft : palette.surface,
                    border: `1px solid ${active ? palette.accent : palette.border}`,
                    color: active ? palette.pillText : palette.muted,
                    fontSize: 14,
                    fontWeight: active ? 800 : 600,
                  }}
                >
                  {idx + 1}. {reg.label || `区域 ${idx + 1}`}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>

      {/* Sleek macOS Browser Frame */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          borderRadius: 22,
          backgroundColor: palette.surfaceElevated,
          border: `1px solid ${palette.border}`,
          boxShadow: palette.cardShadow,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          opacity: windowEnter,
          transform: `translateY(${interpolate(windowEnter, [0, 1], [18, 0]).toFixed(1)}px)`,
        }}
      >
        {/* Browser Chrome */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "11px 18px",
            backgroundColor: palette.isDark ? "rgba(15, 23, 42, 0.9)" : "rgba(241, 245, 249, 0.92)",
            borderBottom: `1px solid ${palette.border}`,
          }}
        >
          <div style={{ display: "flex", gap: 8 }}>
            <span style={{ width: 11, height: 11, borderRadius: "50%", backgroundColor: "#fb7185" }} />
            <span style={{ width: 11, height: 11, borderRadius: "50%", backgroundColor: "#fbbf24" }} />
            <span style={{ width: 11, height: 11, borderRadius: "50%", backgroundColor: "#34d399" }} />
          </div>
          <div
            style={{
              padding: "4px 18px",
              borderRadius: 8,
              backgroundColor: palette.surfaceMuted,
              border: `1px solid ${palette.border}`,
              fontFamily: MONO_FONT_STACK,
              fontSize: 13,
              fontWeight: 600,
              color: palette.muted,
            }}
          >
            {vp.citation_label || "localhost:3000 / Tuber Studio"}
          </div>
          <div style={{ fontSize: 13, fontFamily: MONO_FONT_STACK, color: palette.accent, fontWeight: 700 }}>
            {curZoom.toFixed(2)}x
          </div>
        </div>

        {/* Viewport with virtual camera zoom/pan + gliding cursor */}
        <div style={{ position: "relative", flex: 1, minHeight: 0, overflow: "hidden" }}>
          <div
            style={{
              position: "relative",
              width: "100%",
              height: "100%",
              transformOrigin: `${curX.toFixed(2)}% ${curY.toFixed(2)}%`,
              transform: `scale(${curZoom.toFixed(4)})`,
              backgroundColor: palette.isDark ? "#0b1220" : "#f8fafc",
            }}
          >
            {hasVideo && vp.uploaded_video_file ? (
              <OffthreadVideo
                src={staticFile(vp.uploaded_video_file)}
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
              />
            ) : hasImage && vp.uploaded_image_file ? (
              <Img
                src={staticFile(vp.uploaded_image_file)}
                alt={scene.heading}
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
              />
            ) : (
              /* Graceful wireframe placeholder when media is not attached */
              <div
                style={{
                  width: "100%",
                  height: "100%",
                  display: "grid",
                  gridTemplateColumns: "240px 1fr",
                  gap: 18,
                  padding: 24,
                }}
              >
                <div
                  style={{
                    borderRadius: 16,
                    backgroundColor: palette.surface,
                    border: `1px solid ${palette.border}`,
                    padding: 18,
                    display: "flex",
                    flexDirection: "column",
                    gap: 12,
                  }}
                >
                  <div style={{ height: 18, width: "65%", borderRadius: 6, backgroundColor: palette.accentSoft }} />
                  {[0, 1, 2, 3].map((i) => (
                    <div
                      key={i}
                      style={{
                        height: 36,
                        borderRadius: 10,
                        backgroundColor: i === activeIdx % 4 ? palette.accentSoft : palette.surfaceMuted,
                        border: i === activeIdx % 4 ? `1px solid ${palette.accent}` : "none",
                      }}
                    />
                  ))}
                </div>
                <div
                  style={{
                    borderRadius: 16,
                    backgroundColor: palette.surface,
                    border: `1px solid ${palette.border}`,
                    padding: 24,
                    display: "flex",
                    flexDirection: "column",
                    justifyContent: "space-between",
                  }}
                >
                  <div style={{ fontSize: 28, fontWeight: 800, color: palette.ink }}>
                    {vp.subtitle || scene.heading}
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                    {(vp.bullets && vp.bullets.length > 0 ? vp.bullets : ["实时状态看板", "交互预览区"]).slice(0, 4).map((b, i) => (
                      <div
                        key={i}
                        style={{
                          padding: 18,
                          borderRadius: 14,
                          backgroundColor: palette.surfaceMuted,
                          border: `1px solid ${palette.border}`,
                          fontSize: 19,
                          fontWeight: 700,
                          color: palette.inkSoft,
                        }}
                      >
                        {b}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* Click ripple + Gliding SVG cursor anchored to target coord */}
            <div
              style={{
                position: "absolute",
                left: `${curX.toFixed(2)}%`,
                top: `${curY.toFixed(2)}%`,
                pointerEvents: "none",
                transform: `scale(${(1 / Math.max(1, curZoom * 0.85)).toFixed(3)})`,
              }}
            >
              <div
                style={{
                  position: "absolute",
                  left: -18,
                  top: -18,
                  width: 36,
                  height: 36,
                  borderRadius: "50%",
                  border: `2.5px solid ${palette.accent}`,
                  opacity: rippleOpacity,
                  transform: `scale(${rippleScale.toFixed(3)})`,
                }}
              />
              <svg width="34" height="34" viewBox="0 0 28 28" style={{ filter: "drop-shadow(0 4px 10px rgba(0,0,0,0.45))" }}>
                <path
                  d="M4 3 L23 13 L14 16 L11 25 Z"
                  fill="#ffffff"
                  stroke="#0f172a"
                  strokeWidth="2"
                  strokeLinejoin="round"
                />
              </svg>
              {targetRegion.label ? (
                <div
                  style={{
                    position: "absolute",
                    left: 26,
                    top: 16,
                    whiteSpace: "nowrap",
                    padding: "6px 14px",
                    borderRadius: 10,
                    backgroundColor: palette.subtitleBg,
                    border: `1px solid ${palette.borderStrong}`,
                    color: palette.subtitleInk,
                    fontSize: 16,
                    fontWeight: 800,
                    boxShadow: "0 8px 20px rgba(0,0,0,0.28)",
                  }}
                >
                  {targetRegion.label}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
