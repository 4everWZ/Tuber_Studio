import React from "react";
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { AQUA_THEME, CJK_FONT_STACK, type AquaSceneProp } from "../types";

export const QuoteSpotlight: React.FC<{ scene: AquaSceneProp; includeAvatar?: boolean }> = ({
  scene,
  includeAvatar = true,
}) => {
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
            backgroundColor: AQUA_THEME.yellow,
            border: `2px solid ${AQUA_THEME.border}`,
            fontSize: 16,
            fontWeight: 800,
            color: AQUA_THEME.ink,
            marginBottom: 8,
          }}
        >
          重点划起来 ✏️
        </div>
        <div style={{ fontSize: 46, fontWeight: 900, color: AQUA_THEME.ink }}>{scene.heading}</div>
      </div>

      <div
        style={{
          position: "relative",
          borderRadius: 26,
          padding: "36px 42px",
          backgroundColor: AQUA_THEME.cream,
          border: `3px solid ${AQUA_THEME.border}`,
          borderLeft: `10px solid ${AQUA_THEME.sky}`,
          boxShadow: AQUA_THEME.shadowLg,
          opacity: cardEnter,
          transform: `translateY(${interpolate(cardEnter, [0, 1], [20, 0])}px)`,
        }}
      >
        <div
          style={{
            position: "absolute",
            top: 4,
            right: 28,
            fontSize: 110,
            lineHeight: 1,
            color: "rgba(251, 113, 133, 0.28)",
            fontWeight: 900,
          }}
        >
          “
        </div>
        <div style={{ fontSize: 34, lineHeight: 1.5, fontWeight: 800, color: AQUA_THEME.ink, maxWidth: "92%" }}>
          {vp.subtitle || scene.heading}
        </div>
        {vp.citation_label ? (
          <div style={{ marginTop: 18, fontSize: 19, color: AQUA_THEME.sky, fontWeight: 700 }}>
            — 摘自：{vp.citation_label}
          </div>
        ) : null}
      </div>

      {vp.uploaded_image_file ? (
        <div
          style={{
            borderRadius: 20,
            padding: 12,
            backgroundColor: "#ffffff",
            border: `2.5px solid ${AQUA_THEME.border}`,
            boxShadow: AQUA_THEME.shadow,
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            maxHeight: 260,
            overflow: "hidden",
          }}
        >
          <Img
            src={staticFile(vp.uploaded_image_file)}
            alt={scene.heading}
            style={{ width: "100%", maxHeight: 232, objectFit: "contain", borderRadius: 12 }}
          />
        </div>
      ) : null}

      {bullets.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
          {bullets.map((bullet, idx) => {
            const enter = spring({
              frame: Math.max(0, frame - 8 - idx * 6),
              fps,
              config: { damping: 16, stiffness: 135 },
            });
            return (
              <div
                key={`${bullet}-${idx}`}
                style={{
                  padding: "18px 22px",
                  borderRadius: 18,
                  backgroundColor: idx % 2 === 0 ? AQUA_THEME.skySoft : "#fef9c3",
                  border: `2.5px solid ${AQUA_THEME.border}`,
                  boxShadow: "4px 4px 0 rgba(27, 53, 84, 0.12)",
                  color: AQUA_THEME.ink,
                  fontSize: 22,
                  fontWeight: 700,
                  lineHeight: 1.4,
                  opacity: enter,
                  transform: `translateY(${interpolate(enter, [0, 1], [14, 0])}px)`,
                }}
              >
                <span style={{ color: AQUA_THEME.coral, fontWeight: 900, marginRight: 8 }}>▸</span>
                {bullet}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
};
