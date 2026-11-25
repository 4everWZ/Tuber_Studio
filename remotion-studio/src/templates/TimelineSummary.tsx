import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, type AquaSceneProp } from "../types";

export const TimelineSummary: React.FC<{ scene: AquaSceneProp }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const steps = vp.bullets && vp.bullets.length > 0 ? vp.bullets : [vp.subtitle || scene.heading];

  const lineProgress = interpolate(
    spring({ frame, fps, config: { damping: 20, stiffness: 90 } }),
    [0, 1],
    [0, 100]
  );

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
      <div>
        <div style={{ fontSize: 50, fontWeight: 800, color: "#f8fafc" }}>{scene.heading}</div>
        {vp.subtitle ? (
          <div style={{ fontSize: 25, color: "#94a3b8", marginTop: 8 }}>{vp.subtitle}</div>
        ) : null}
      </div>

      <div style={{ position: "relative", display: "flex", flexDirection: "column", gap: 20, paddingLeft: 28 }}>
        <div
          style={{
            position: "absolute",
            left: 19,
            top: 12,
            width: 4,
            height: `${lineProgress}%`,
            maxHeight: "calc(100% - 24px)",
            borderRadius: 999,
            background: "linear-gradient(180deg, #38bdf8, #34d399)",
          }}
        />
        {steps.map((step, idx) => {
          const itemEnter = spring({
            frame: Math.max(0, frame - idx * 7),
            fps,
            config: { damping: 16, stiffness: 130 },
          });
          return (
            <div
              key={`${step}-${idx}`}
              style={{
                position: "relative",
                display: "flex",
                alignItems: "center",
                gap: 20,
                padding: "20px 28px",
                marginLeft: 20,
                borderRadius: 18,
                backgroundColor: "rgba(15, 23, 42, 0.82)",
                border: "1px solid rgba(56, 189, 248, 0.28)",
                opacity: itemEnter,
                transform: `translateX(${interpolate(itemEnter, [0, 1], [28, 0])}px)`,
              }}
            >
              <div
                style={{
                  width: 44,
                  height: 44,
                  flexShrink: 0,
                  borderRadius: "50%",
                  background: "linear-gradient(135deg, #38bdf8, #34d399)",
                  color: "#0f172a",
                  fontSize: 22,
                  fontWeight: 800,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {idx + 1}
              </div>
              <div style={{ fontSize: 28, fontWeight: 600, color: "#f1f5f9", lineHeight: 1.35 }}>
                {step}
              </div>
            </div>
          );
        })}
      </div>

      {vp.citation_label ? (
        <div style={{ fontSize: 18, color: "#94a3b8" }}>参考来源：{vp.citation_label}</div>
      ) : null}
    </div>
  );
};
