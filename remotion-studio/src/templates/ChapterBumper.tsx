import React from "react";
import { Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { getThemePalette } from "../theme";
import { CJK_FONT_STACK, MONO_FONT_STACK, type SceneTemplateProps } from "../types";

export const ChapterBumper: React.FC<SceneTemplateProps> = ({ scene, activeCue }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};

  const chapterNumRaw = vp.chapter_number ?? "";
  const chapterNum =
    typeof chapterNumRaw === "number"
      ? String(chapterNumRaw).padStart(2, "0")
      : String(chapterNumRaw).trim();

  const enter = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 120, mass: 0.8 },
  });
  const barProgress = interpolate(frame, [4, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  const subEnter = spring({
    frame: Math.max(0, frame - 8),
    fps,
    config: { damping: 16, stiffness: 130 },
  });

  const ringRotate = frame * 0.45;
  const ringCounterRotate = -frame * 0.32;
  const streakShift = interpolate(frame, [0, Math.max(1, scene.duration_frames)], [-180, 220]);

  return (
    <div
      style={{
        position: "relative",
        flex: 1,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "40px 88px 110px",
        fontFamily: CJK_FONT_STACK,
        overflow: "hidden",
      }}
    >
      {/* Animated SVG geometric motion backdrop */}
      <svg
        width="920"
        height="920"
        viewBox="0 0 920 920"
        style={{
          position: "absolute",
          left: "50%",
          top: "47%",
          transform: "translate(-50%, -50%)",
          pointerEvents: "none",
          opacity: interpolate(enter, [0, 1], [0, 0.75]),
        }}
      >
        <g transform={`translate(460 460) rotate(${ringRotate.toFixed(1)})`}>
          <circle
            r={330}
            fill="none"
            stroke={palette.accent}
            strokeOpacity={0.22}
            strokeWidth={1.5}
            strokeDasharray="18 14 4 14"
          />
          <circle cx={330} cy={0} r={5} fill={palette.accent} fillOpacity={0.7} />
          <circle cx={-330} cy={0} r={4} fill={palette.secondary} fillOpacity={0.7} />
        </g>
        <g transform={`translate(460 460) rotate(${ringCounterRotate.toFixed(1)})`}>
          <circle
            r={245}
            fill="none"
            stroke={palette.secondary}
            strokeOpacity={0.24}
            strokeWidth={2}
            strokeDasharray="60 32"
          />
          <polygon
            points="0,-245 8,-231 -8,-231"
            fill={palette.highlight}
            fillOpacity={0.75}
          />
        </g>
      </svg>

      {/* Diagonal geometric light streak */}
      <div
        style={{
          position: "absolute",
          top: "24%",
          left: "50%",
          width: 680,
          height: 2,
          background: `linear-gradient(90deg, transparent, ${palette.accent}, ${palette.secondary}, transparent)`,
          opacity: 0.45,
          transform: `translateX(calc(-50% + ${streakShift.toFixed(1)}px)) rotate(-14deg)`,
          pointerEvents: "none",
        }}
      />

      {/* Giant subtle chapter watermark */}
      {chapterNum ? (
        <div
          style={{
            position: "absolute",
            fontFamily: MONO_FONT_STACK,
            fontSize: 320,
            fontWeight: 900,
            lineHeight: 1,
            color: palette.isDark ? "rgba(255, 255, 255, 0.04)" : "rgba(15, 34, 61, 0.045)",
            letterSpacing: -12,
            transform: `scale(${interpolate(enter, [0, 1], [1.14, 1]).toFixed(3)})`,
            userSelect: "none",
            pointerEvents: "none",
          }}
        >
          {chapterNum}
        </div>
      ) : null}

      {/* Chapter badge */}
      <div
        style={{
          position: "relative",
          zIndex: 2,
          display: "inline-flex",
          alignItems: "center",
          gap: 12,
          padding: "9px 22px",
          borderRadius: 999,
          backgroundColor: palette.accentSoft,
          border: `1.5px solid ${palette.borderStrong}`,
          marginBottom: 26,
          opacity: enter,
          transform: `translateY(${interpolate(enter, [0, 1], [20, 0]).toFixed(1)}px)`,
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            backgroundColor: palette.accent,
            boxShadow: `0 0 12px ${palette.accent}`,
          }}
        />
        <span
          style={{
            fontFamily: MONO_FONT_STACK,
            fontSize: 18,
            fontWeight: 800,
            letterSpacing: 2.2,
            color: palette.pillText,
            textTransform: "uppercase",
          }}
        >
          {chapterNum ? `CHAPTER ${chapterNum}` : "CHAPTER"}
          {scene.chapter_title ? ` · ${scene.chapter_title}` : ""}
        </span>
      </div>

      {/* Main chapter heading */}
      <h1
        style={{
          position: "relative",
          zIndex: 2,
          margin: 0,
          maxWidth: 1280,
          textAlign: "center",
          fontSize: 78,
          fontWeight: 900,
          lineHeight: 1.15,
          letterSpacing: -1.2,
          color: palette.ink,
          opacity: enter,
          transform: `translateY(${interpolate(enter, [0, 1], [28, 0]).toFixed(1)}px)`,
          textShadow: palette.isDark ? "0 10px 36px rgba(0, 0, 0, 0.5)" : "none",
        }}
      >
        {scene.heading}
      </h1>

      {/* Animated gradient divider bar */}
      <div
        style={{
          position: "relative",
          zIndex: 2,
          width: 420,
          height: 6,
          borderRadius: 999,
          marginTop: 28,
          marginBottom: 24,
          backgroundColor: palette.surfaceMuted,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${(barProgress * 100).toFixed(1)}%`,
            height: "100%",
            borderRadius: 999,
            background: `linear-gradient(90deg, ${palette.accent}, ${palette.secondary}, ${palette.highlight})`,
            boxShadow: `0 0 16px ${palette.accent}`,
          }}
        />
      </div>

      {/* Subtitle / active spoken line */}
      {(vp.subtitle || activeCue?.text) ? (
        <p
          style={{
            position: "relative",
            zIndex: 2,
            margin: 0,
            maxWidth: 960,
            textAlign: "center",
            fontSize: 30,
            fontWeight: 600,
            lineHeight: 1.45,
            color: palette.inkSoft,
            opacity: subEnter,
            transform: `translateY(${interpolate(subEnter, [0, 1], [16, 0]).toFixed(1)}px)`,
          }}
        >
          {vp.subtitle || activeCue?.text}
        </p>
      ) : null}
    </div>
  );
};
