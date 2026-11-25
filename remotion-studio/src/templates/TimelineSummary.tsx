import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AQUA_THEME, CJK_FONT_STACK, type AquaSceneProp } from "../types";

export const TimelineSummary: React.FC<{ scene: AquaSceneProp; includeAvatar?: boolean }> = ({
  scene,
  includeAvatar = true,
}) => {
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
        gap: 22,
        padding: `0 ${includeAvatar ? 286 : 48}px 146px 32px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div>
        <div
          style={{
            display: "inline-block",
            padding: "4px 14px",
            borderRadius: 999,
            backgroundColor: AQUA_THEME.mintSoft,
            border: `2px solid ${AQUA_THEME.border}`,
            fontSize: 16,
            fontWeight: 800,
            color: AQUA_THEME.ink,
            marginBottom: 8,
          }}
        >
          摸鱼小结 🐟
        </div>
        <div style={{ fontSize: 46, fontWeight: 900, color: AQUA_THEME.ink }}>{scene.heading}</div>
        {vp.subtitle ? (
          <div style={{ fontSize: 23, fontWeight: 600, color: AQUA_THEME.inkSoft, marginTop: 4 }}>
            {vp.subtitle}
          </div>
        ) : null}
      </div>

      <div style={{ position: "relative", display: "flex", flexDirection: "column", gap: 16, paddingLeft: 26 }}>
        <div
          style={{
            position: "absolute",
            left: 19,
            top: 14,
            width: 5,
            height: `${lineProgress}%`,
            maxHeight: "calc(100% - 28px)",
            borderRadius: 999,
            background: `linear-gradient(180deg, ${AQUA_THEME.sky}, ${AQUA_THEME.mint}, ${AQUA_THEME.yellow})`,
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
                gap: 18,
                padding: "18px 24px",
                marginLeft: 18,
                borderRadius: 20,
                backgroundColor: AQUA_THEME.cream,
                border: `2.5px solid ${AQUA_THEME.border}`,
                boxShadow: "4px 4px 0 rgba(27, 53, 84, 0.12)",
                opacity: itemEnter,
                transform: `translateX(${interpolate(itemEnter, [0, 1], [22, 0])}px)`,
              }}
            >
              <div
                style={{
                  width: 42,
                  height: 42,
                  flexShrink: 0,
                  borderRadius: "50%",
                  backgroundColor: idx % 2 === 0 ? AQUA_THEME.sky : AQUA_THEME.yellow,
                  border: `2px solid ${AQUA_THEME.border}`,
                  color: idx % 2 === 0 ? "#ffffff" : AQUA_THEME.ink,
                  fontSize: 20,
                  fontWeight: 900,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {idx + 1}
              </div>
              <div style={{ fontSize: 26, fontWeight: 700, color: AQUA_THEME.ink, lineHeight: 1.35 }}>
                {step}
              </div>
            </div>
          );
        })}
      </div>

      {vp.citation_label ? (
        <div style={{ fontSize: 18, fontWeight: 700, color: AQUA_THEME.muted }}>
          📎 笔记来源：{vp.citation_label}
        </div>
      ) : null}
    </div>
  );
};
