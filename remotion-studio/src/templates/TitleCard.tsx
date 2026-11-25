import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AnimatedSticker } from "../components/AnimatedSticker";
import { AQUA_THEME, CJK_FONT_STACK, type AquaSceneProp, type VisualAssetEntry } from "../types";

export const TitleCard: React.FC<{
  scene: AquaSceneProp;
  memeAssets?: Record<string, VisualAssetEntry>;
  includeAvatar?: boolean;
}> = ({ scene, memeAssets, includeAvatar = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];

  const cardEnter = spring({ frame, fps, config: { damping: 15, stiffness: 120 } });
  const subEnter = spring({ frame: Math.max(0, frame - 6), fps, config: { damping: 17, stiffness: 115 } });
  const lineWidth = interpolate(cardEnter, [0, 1], [0, 220]);
  const memeEntry = vp.meme_asset_id ? memeAssets?.[vp.meme_asset_id] : undefined;

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: `0 ${includeAvatar ? 286 : 48}px 146px 32px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          position: "relative",
          width: "100%",
          borderRadius: 30,
          backgroundColor: AQUA_THEME.cream,
          border: `3px solid ${AQUA_THEME.border}`,
          boxShadow: AQUA_THEME.shadowLg,
          padding: "46px 52px 44px",
          opacity: cardEnter,
          transform: `translateY(${interpolate(cardEnter, [0, 1], [24, 0])}px)`,
        }}
      >
        <div
          style={{
            position: "absolute",
            top: -16,
            left: 56,
            width: 140,
            height: 28,
            borderRadius: 6,
            backgroundColor: "rgba(56, 189, 248, 0.55)",
            border: `2px solid ${AQUA_THEME.border}`,
            transform: "rotate(-2.2deg)",
          }}
        />
        <div
          style={{
            position: "absolute",
            top: -14,
            right: 68,
            width: 110,
            height: 26,
            borderRadius: 6,
            backgroundColor: "rgba(253, 224, 71, 0.72)",
            border: `2px solid ${AQUA_THEME.border}`,
            transform: "rotate(2.6deg)",
          }}
        />

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 24 }}>
          <div style={{ flex: 1 }}>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 10,
                padding: "7px 18px",
                borderRadius: 999,
                backgroundColor: AQUA_THEME.skySoft,
                border: `2px solid ${AQUA_THEME.border}`,
                color: AQUA_THEME.ink,
                fontSize: 19,
                fontWeight: 800,
                marginBottom: 22,
              }}
            >
              <span>本鲸开麦 🐋</span>
              {vp.citation_label ? (
                <span style={{ color: AQUA_THEME.inkSoft, fontWeight: 700 }}>· {vp.citation_label}</span>
              ) : null}
            </div>

            <h1
              style={{
                margin: 0,
                fontSize: 68,
                lineHeight: 1.18,
                fontWeight: 900,
                color: AQUA_THEME.ink,
                letterSpacing: -0.4,
              }}
            >
              {scene.heading}
            </h1>

            <div
              style={{
                height: 8,
                width: lineWidth,
                borderRadius: 999,
                marginTop: 22,
                marginBottom: 22,
                background: `linear-gradient(90deg, ${AQUA_THEME.sky}, ${AQUA_THEME.mint}, ${AQUA_THEME.yellow})`,
              }}
            />

            {vp.subtitle ? (
              <p
                style={{
                  margin: 0,
                  fontSize: 31,
                  lineHeight: 1.45,
                  fontWeight: 600,
                  color: AQUA_THEME.inkSoft,
                  opacity: subEnter,
                  transform: `translateY(${interpolate(subEnter, [0, 1], [14, 0])}px)`,
                }}
              >
                {vp.subtitle}
              </p>
            ) : null}
          </div>

          {memeEntry ? (
            <div
              style={{
                width: 184,
                height: 184,
                flexShrink: 0,
                borderRadius: 24,
                padding: 12,
                backgroundColor: AQUA_THEME.creamTint,
                border: `2.5px dashed ${AQUA_THEME.sky}`,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                transform: `rotate(${(Math.sin(frame * 0.1) * 2.5).toFixed(2)}deg)`,
              }}
            >
              <div style={{ width: 136, height: 136 }}>
                <AnimatedSticker asset={memeEntry} alt={memeEntry.title || "Aqua 表情贴纸"} />
              </div>
            </div>
          ) : null}
        </div>

        {bullets.length > 0 ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginTop: 28 }}>
            {bullets.map((bullet, idx) => {
              const chip = spring({
                frame: Math.max(0, frame - 10 - idx * 5),
                fps,
                config: { damping: 15, stiffness: 145 },
              });
              const bg = idx % 3 === 0 ? AQUA_THEME.skySoft : idx % 3 === 1 ? "#fef9c3" : AQUA_THEME.coralSoft;
              return (
                <div
                  key={`${bullet}-${idx}`}
                  style={{
                    padding: "10px 20px",
                    borderRadius: 14,
                    backgroundColor: bg,
                    border: `2px solid ${AQUA_THEME.border}`,
                    color: AQUA_THEME.ink,
                    fontSize: 22,
                    fontWeight: 700,
                    opacity: chip,
                    transform: `translateY(${interpolate(chip, [0, 1], [12, 0])}px)`,
                  }}
                >
                  ✦ {bullet}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
};
