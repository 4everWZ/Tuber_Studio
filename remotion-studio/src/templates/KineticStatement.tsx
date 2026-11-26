import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { getThemePalette } from "../theme";
import { CJK_FONT_STACK, MONO_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

export const KineticStatement: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};

  const phrases =
    vp.bullets && vp.bullets.length > 0
      ? vp.bullets
      : vp.subtitle
        ? vp.subtitle.split(/[，。；|·]/).map((s) => s.trim()).filter(Boolean)
        : [];

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    Math.max(1, phrases.length),
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  const heroEnter = spring({
    frame,
    fps,
    config: { damping: 12, stiffness: 150, mass: 0.8 },
  });
  const statPop = spring({
    frame: Math.max(0, frame - 4),
    fps,
    config: { damping: 11, stiffness: 170, mass: 0.7 },
  });

  const statText = (vp.stat_callout || "").trim();
  const numericMatch = statText.match(/^([+-]?\d+(?:\.\d+)?)(.*)$/);

  let renderedStat = statText;
  if (numericMatch) {
    const rawNum = Number(numericMatch[1]);
    const suffix = numericMatch[2] || "";
    const hasDecimal = numericMatch[1].includes(".");
    const animated = interpolate(statPop, [0, 1], [0, rawNum]);
    renderedStat = `${hasDecimal ? animated.toFixed(1) : Math.round(animated)}${suffix}`;
  }

  return (
    <div
      style={{
        position: "relative",
        flex: 1,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        alignItems: "center",
        padding: "44px 86px 120px",
        fontFamily: CJK_FONT_STACK,
        overflow: "hidden",
      }}
    >
      {/* Subtle kinetic backdrop watermark */}
      <div
        style={{
          position: "absolute",
          fontSize: 180,
          fontWeight: 900,
          color: palette.isDark ? "rgba(255,255,255,0.03)" : "rgba(15,34,61,0.035)",
          letterSpacing: -4,
          whiteSpace: "nowrap",
          transform: `translateX(${Math.sin(frame * 0.02) * 32}px)`,
          pointerEvents: "none",
          userSelect: "none",
        }}
      >
        {statText || scene.heading}
      </div>

      {/* Hero Stat Callout */}
      {statText ? (
        <div
          style={{
            position: "relative",
            zIndex: 2,
            marginBottom: 18,
            padding: "10px 36px",
            borderRadius: 24,
            background: `linear-gradient(135deg, ${palette.accentSoft}, ${palette.secondarySoft})`,
            border: `1.5px solid ${palette.borderStrong}`,
            boxShadow: `0 18px 48px ${palette.accent}26`,
            transform: `scale(${interpolate(statPop, [0, 1], [0.76, 1]).toFixed(3)})`,
            opacity: statPop,
          }}
        >
          <span
            style={{
              fontFamily: MONO_FONT_STACK,
              fontSize: 108,
              fontWeight: 900,
              lineHeight: 1.05,
              letterSpacing: -2,
              background: `linear-gradient(90deg, ${palette.accent}, ${palette.highlight})`,
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
            }}
          >
            {renderedStat}
          </span>
        </div>
      ) : null}

      {/* Main kinetic statement */}
      <h1
        style={{
          position: "relative",
          zIndex: 2,
          margin: 0,
          maxWidth: 1320,
          textAlign: "center",
          fontSize: statText ? 60 : 74,
          fontWeight: 900,
          lineHeight: 1.18,
          letterSpacing: -1,
          color: palette.ink,
          opacity: heroEnter,
          transform: `translateY(${interpolate(heroEnter, [0, 1], [24, 0]).toFixed(1)}px)`,
        }}
      >
        {scene.heading}
      </h1>

      {/* Subtitle if not split into phrases */}
      {vp.subtitle && vp.bullets && vp.bullets.length > 0 ? (
        <p
          style={{
            position: "relative",
            zIndex: 2,
            margin: "16px 0 0",
            maxWidth: 960,
            textAlign: "center",
            fontSize: 28,
            fontWeight: 600,
            color: palette.inkSoft,
            opacity: heroEnter,
          }}
        >
          {vp.subtitle}
        </p>
      ) : null}

      {/* Staggered key phrase pills synced to revealStep */}
      {phrases.length > 0 ? (
        <div
          style={{
            position: "relative",
            zIndex: 2,
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "center",
            gap: 14,
            marginTop: 32,
            maxWidth: 1180,
          }}
        >
          {phrases.map((phrase, idx) => {
            const unlocked = idx <= revealStep;
            const isCurrent = idx === revealStep;
            const pop = spring({
              frame: unlocked ? Math.max(0, frame - idx * 4) : 0,
              fps,
              config: { damping: 14, stiffness: 165 },
            });
            return (
              <div
                key={`${phrase}-${idx}`}
                style={{
                  padding: "12px 24px",
                  borderRadius: 16,
                  backgroundColor: isCurrent ? palette.accentSoft : palette.surface,
                  border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                  boxShadow: isCurrent ? `0 8px 26px ${palette.accent}2a` : palette.cardShadow,
                  color: isCurrent ? palette.ink : palette.inkSoft,
                  fontSize: 25,
                  fontWeight: isCurrent ? 800 : 700,
                  opacity: unlocked ? interpolate(pop, [0, 1], [0, isCurrent ? 1 : 0.78]) : 0.22,
                  transform: `translateY(${interpolate(pop, [0, 1], [14, 0]).toFixed(1)}px) scale(${isCurrent ? 1.04 : 1})`,
                }}
              >
                {phrase}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
};
