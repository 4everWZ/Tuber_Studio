import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, type AquaSceneProp } from "../types";

export const QuoteSpotlight: React.FC<{ scene: AquaSceneProp }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];
  const cardEnter = spring({ frame, fps, config: { damping: 16, stiffness: 120 } });

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        gap: 28,
        padding: "0 48px 130px",
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div style={{ fontSize: 48, fontWeight: 800, color: "#f8fafc" }}>{scene.heading}</div>

      <div
        style={{
          position: "relative",
          borderRadius: 24,
          padding: "40px 48px",
          background: "linear-gradient(135deg, rgba(15, 23, 42, 0.88), rgba(30, 41, 59, 0.78))",
          borderLeft: "8px solid #38bdf8",
          borderTop: "1px solid rgba(148, 163, 184, 0.22)",
          borderRight: "1px solid rgba(148, 163, 184, 0.22)",
          borderBottom: "1px solid rgba(148, 163, 184, 0.22)",
          opacity: cardEnter,
          transform: `translateY(${interpolate(cardEnter, [0, 1], [24, 0])}px)`,
        }}
      >
        <div
          style={{
            position: "absolute",
            top: 8,
            right: 32,
            fontSize: 120,
            lineHeight: 1,
            color: "rgba(56, 189, 248, 0.16)",
            fontWeight: 900,
          }}
        >
          “
        </div>
        <div style={{ fontSize: 36, lineHeight: 1.5, fontWeight: 600, color: "#f1f5f9", maxWidth: "92%" }}>
          {vp.subtitle || scene.heading}
        </div>
        {vp.citation_label ? (
          <div style={{ marginTop: 20, fontSize: 20, color: "#7dd3fc", fontWeight: 500 }}>
            — 来源：{vp.citation_label}
          </div>
        ) : null}
      </div>

      {bullets.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 18 }}>
          {bullets.map((bullet, idx) => {
            const enter = spring({
              frame: Math.max(0, frame - 10 - idx * 6),
              fps,
              config: { damping: 16, stiffness: 135 },
            });
            return (
              <div
                key={`${bullet}-${idx}`}
                style={{
                  padding: "20px 24px",
                  borderRadius: 16,
                  backgroundColor: "rgba(30, 41, 59, 0.72)",
                  border: "1px solid rgba(148, 163, 184, 0.22)",
                  color: "#e2e8f0",
                  fontSize: 24,
                  lineHeight: 1.4,
                  opacity: enter,
                  transform: `translateY(${interpolate(enter, [0, 1], [16, 0])}px)`,
                }}
              >
                <span style={{ color: "#22d3ee", fontWeight: 700, marginRight: 10 }}>▸</span>
                {bullet}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
};
