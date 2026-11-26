import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AnimatedSticker } from "../components/AnimatedSticker";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

export const TitleCard: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  memeAssets,
  avatarLayout = "corner_float",
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];

  const cardEnter = spring({ frame, fps, config: { damping: 15, stiffness: 120 } });
  const subEnter = spring({ frame: Math.max(0, frame - 6), fps, config: { damping: 17, stiffness: 115 } });
  const lineWidth = interpolate(cardEnter, [0, 1], [0, 260]);
  const memeEntry = vp.meme_asset_id ? memeAssets?.[vp.meme_asset_id] : undefined;

  const effectiveLayout = includeAvatar ? avatarLayout : "hidden";
  const rightClearance = getAvatarRightClearance(effectiveLayout);

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    Math.max(1, bullets.length),
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: `24px ${rightClearance}px 132px 48px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          position: "relative",
          width: "100%",
          borderRadius: 28,
          backgroundColor: palette.surface,
          border: `1.5px solid ${palette.border}`,
          boxShadow: palette.cardShadow,
          backdropFilter: "blur(12px)",
          padding: "46px 52px 44px",
          opacity: cardEnter,
          transform: `translateY(${interpolate(cardEnter, [0, 1], [22, 0]).toFixed(1)}px)`,
        }}
      >
        {/* Top subtle accent bar */}
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 44,
            right: 44,
            height: 3,
            borderRadius: 999,
            background: `linear-gradient(90deg, ${palette.accent}, ${palette.secondary}, transparent)`,
            opacity: 0.85,
          }}
        />

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 28 }}>
          <div style={{ flex: 1 }}>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 10,
                padding: "6px 18px",
                borderRadius: 999,
                backgroundColor: palette.accentSoft,
                border: `1px solid ${palette.borderStrong}`,
                color: palette.pillText,
                fontSize: 17,
                fontWeight: 800,
                marginBottom: 20,
              }}
            >
              <span>🐋 {scene.chapter_title || "FEATURED STORY"}</span>
              {vp.citation_label ? (
                <span style={{ color: palette.inkSoft, fontWeight: 700 }}>· {vp.citation_label}</span>
              ) : null}
            </div>

            <h1
              style={{
                margin: 0,
                fontSize: 64,
                lineHeight: 1.16,
                fontWeight: 900,
                color: palette.ink,
                letterSpacing: -0.6,
              }}
            >
              {scene.heading}
            </h1>

            <div
              style={{
                height: 6,
                width: lineWidth,
                borderRadius: 999,
                marginTop: 20,
                marginBottom: 20,
                background: `linear-gradient(90deg, ${palette.accent}, ${palette.secondary}, ${palette.highlight})`,
              }}
            />

            {vp.subtitle ? (
              <p
                style={{
                  margin: 0,
                  fontSize: 29,
                  lineHeight: 1.46,
                  fontWeight: 600,
                  color: palette.inkSoft,
                  opacity: subEnter,
                  transform: `translateY(${interpolate(subEnter, [0, 1], [14, 0]).toFixed(1)}px)`,
                }}
              >
                {vp.subtitle}
              </p>
            ) : null}
          </div>

          {memeEntry ? (
            <div
              style={{
                width: 178,
                height: 178,
                flexShrink: 0,
                borderRadius: 24,
                padding: 12,
                backgroundColor: palette.surfaceElevated,
                border: `1.5px solid ${palette.borderStrong}`,
                boxShadow: palette.cardShadow,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                transform: `translateY(${(Math.sin(frame * 0.09) * 5).toFixed(1)}px) rotate(${(Math.sin(frame * 0.08) * 2.2).toFixed(2)}deg)`,
              }}
            >
              <div style={{ width: 138, height: 138 }}>
                <AnimatedSticker asset={memeEntry} alt={memeEntry.title || "Aqua 表情贴纸"} />
              </div>
            </div>
          ) : null}
        </div>

        {bullets.length > 0 ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginTop: 28 }}>
            {bullets.map((bullet, idx) => {
              const unlocked = idx <= revealStep;
              const isCurrent = idx === revealStep;
              const chip = spring({
                frame: unlocked ? Math.max(0, frame - idx * 4) : 0,
                fps,
                config: { damping: 15, stiffness: 155 },
              });
              return (
                <div
                  key={`${bullet}-${idx}`}
                  style={{
                    padding: "10px 20px",
                    borderRadius: 14,
                    backgroundColor: isCurrent ? palette.accentSoft : palette.surfaceMuted,
                    border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                    color: isCurrent ? palette.ink : palette.inkSoft,
                    fontSize: 21,
                    fontWeight: isCurrent ? 800 : 700,
                    opacity: unlocked ? interpolate(chip, [0, 1], [0, 1]) : 0.35,
                    transform: `translateY(${interpolate(chip, [0, 1], [10, 0]).toFixed(1)}px) scale(${isCurrent ? 1.03 : 1})`,
                  }}
                >
                  <span style={{ color: palette.accent, marginRight: 8 }}>✦</span>
                  {bullet}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
};
