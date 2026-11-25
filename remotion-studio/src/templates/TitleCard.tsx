import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, type AquaSceneProp } from "../types";

export const TitleCard: React.FC<{ scene: AquaSceneProp }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];

  const titleEnter = spring({ frame, fps, config: { damping: 16, stiffness: 120 } });
  const subEnter = spring({ frame: Math.max(0, frame - 8), fps, config: { damping: 18, stiffness: 110 } });
  const lineWidth = interpolate(titleEnter, [0, 1], [0, 180]);

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        alignItems: "flex-start",
        padding: "0 48px 120px",
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 10,
          padding: "8px 18px",
          borderRadius: 999,
          backgroundColor: "rgba(56, 189, 248, 0.14)",
          border: "1px solid rgba(56, 189, 248, 0.42)",
          color: "#7dd3fc",
          fontSize: 20,
          fontWeight: 600,
          marginBottom: 26,
          opacity: titleEnter,
        }}
      >
        <span>TITLE CARD</span>
        {vp.citation_label ? <span style={{ opacity: 0.78 }}>· {vp.citation_label}</span> : null}
      </div>

      <h1
        style={{
          margin: 0,
          fontSize: 78,
          lineHeight: 1.15,
          fontWeight: 800,
          color: "#f8fafc",
          letterSpacing: -0.5,
          maxWidth: "88%",
          transform: `translateY(${interpolate(titleEnter, [0, 1], [32, 0])}px)`,
          opacity: titleEnter,
        }}
      >
        {scene.heading}
      </h1>

      <div
        style={{
          height: 6,
          width: lineWidth,
          borderRadius: 999,
          marginTop: 26,
          marginBottom: 26,
          background: "linear-gradient(90deg, #38bdf8, #22d3ee, #818cf8)",
        }}
      />

      {vp.subtitle ? (
        <p
          style={{
            margin: 0,
            fontSize: 34,
            lineHeight: 1.45,
            color: "#cbd5e1",
            maxWidth: "82%",
            transform: `translateY(${interpolate(subEnter, [0, 1], [22, 0])}px)`,
            opacity: subEnter,
          }}
        >
          {vp.subtitle}
        </p>
      ) : null}

      {bullets.length > 0 ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginTop: 34 }}>
          {bullets.map((bullet, idx) => {
            const chip = spring({
              frame: Math.max(0, frame - 14 - idx * 5),
              fps,
              config: { damping: 16, stiffness: 140 },
            });
            return (
              <div
                key={`${bullet}-${idx}`}
                style={{
                  padding: "12px 22px",
                  borderRadius: 14,
                  backgroundColor: "rgba(30, 41, 59, 0.8)",
                  border: "1px solid rgba(148, 163, 184, 0.25)",
                  color: "#e2e8f0",
                  fontSize: 22,
                  fontWeight: 500,
                  opacity: chip,
                  transform: `translateY(${interpolate(chip, [0, 1], [14, 0])}px)`,
                }}
              >
                ✦ {bullet}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
};
