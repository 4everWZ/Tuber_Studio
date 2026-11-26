import React from "react";
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

export const QuoteSpotlight: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  avatarLayout = "corner_float",
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];
  const cardEnter = spring({ frame, fps, config: { damping: 16, stiffness: 120 } });

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    Math.max(1, bullets.length),
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  const effectiveLayout = includeAvatar ? avatarLayout : "hidden";
  const rightClearance = getAvatarRightClearance(effectiveLayout);

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        gap: 18,
        padding: `16px ${rightClearance}px 128px 48px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div>
        <div
          style={{
            display: "inline-block",
            padding: "4px 14px",
            borderRadius: 999,
            backgroundColor: palette.highlightSoft,
            border: `1px solid ${palette.highlight}`,
            fontSize: 14,
            fontWeight: 800,
            color: palette.isDark ? "#fde68a" : palette.ink,
            letterSpacing: 0.8,
            marginBottom: 8,
          }}
        >
          KEY INSIGHT · 核心观点
        </div>
        <div style={{ fontSize: 44, fontWeight: 900, color: palette.ink }}>{scene.heading}</div>
      </div>

      <div
        style={{
          position: "relative",
          borderRadius: 24,
          padding: "32px 38px",
          backgroundColor: palette.surface,
          border: `1.5px solid ${palette.border}`,
          borderLeft: `6px solid ${palette.accent}`,
          boxShadow: palette.cardShadow,
          backdropFilter: "blur(10px)",
          opacity: cardEnter,
          transform: `translateY(${interpolate(cardEnter, [0, 1], [18, 0]).toFixed(1)}px)`,
        }}
      >
        <div
          style={{
            position: "absolute",
            top: 2,
            right: 26,
            fontSize: 96,
            lineHeight: 1,
            color: palette.accentSoft,
            fontWeight: 900,
            pointerEvents: "none",
          }}
        >
          “
        </div>
        <div style={{ fontSize: 32, lineHeight: 1.48, fontWeight: 800, color: palette.ink, maxWidth: "92%" }}>
          {vp.subtitle || scene.heading}
        </div>
        {vp.citation_label ? (
          <div style={{ marginTop: 14, fontSize: 18, color: palette.accent, fontWeight: 700 }}>
            — {vp.citation_label}
          </div>
        ) : null}
      </div>

      {vp.uploaded_image_file ? (
        <div
          style={{
            borderRadius: 18,
            padding: 10,
            backgroundColor: palette.surface,
            border: `1px solid ${palette.border}`,
            boxShadow: palette.cardShadow,
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            maxHeight: 240,
            overflow: "hidden",
          }}
        >
          <Img
            src={staticFile(vp.uploaded_image_file)}
            alt={scene.heading}
            style={{ width: "100%", maxHeight: 214, objectFit: "contain", borderRadius: 10 }}
          />
        </div>
      ) : null}

      {bullets.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 14 }}>
          {bullets.map((bullet, idx) => {
            const unlocked = idx <= revealStep;
            const isCurrent = idx === revealStep;
            const enter = spring({
              frame: Math.max(0, frame - idx * 4),
              fps,
              config: { damping: 16, stiffness: 145 },
            });
            const targetOpacity = isCurrent
              ? 1
              : unlocked
                ? palette.isDark
                  ? 0.8
                  : 0.88
                : palette.isDark
                  ? 0.58
                  : 0.72;
            return (
              <div
                key={`${bullet}-${idx}`}
                style={{
                  padding: "16px 20px",
                  borderRadius: 16,
                  backgroundColor: isCurrent ? palette.surfaceElevated : palette.surface,
                  border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                  boxShadow: isCurrent ? `0 8px 26px ${palette.accent}24` : palette.cardShadow,
                  color: isCurrent ? palette.ink : palette.inkSoft,
                  fontSize: 21,
                  fontWeight: isCurrent ? 800 : 600,
                  lineHeight: 1.42,
                  opacity: interpolate(enter, [0, 1], [0, targetOpacity]),
                  transform: `translateY(${interpolate(enter, [0, 1], [12, 0]).toFixed(1)}px) scale(${isCurrent ? 1.02 : 1})`,
                }}
              >
                <span style={{ color: palette.accent, fontWeight: 900, marginRight: 8 }}>▸</span>
                {bullet}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
};
