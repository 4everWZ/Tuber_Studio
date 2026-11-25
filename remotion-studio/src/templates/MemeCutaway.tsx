import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AnimatedSticker } from "../components/AnimatedSticker";
import { AQUA_THEME, CJK_FONT_STACK, type AquaSceneProp, type VisualAssetEntry } from "../types";

const REACTION_ICON: Record<string, string> = {
  cheerful: "😆",
  curious: "🧐",
  proud: "😎",
  thinking: "🤔",
  surprised: "🤯",
  calm: "😌",
  idle: "🐋",
};

export const MemeCutaway: React.FC<{
  scene: AquaSceneProp;
  memeAssets?: Record<string, VisualAssetEntry>;
  includeAvatar?: boolean;
}> = ({ scene, memeAssets, includeAvatar = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];

  const pop = spring({ frame, fps, config: { damping: 11, stiffness: 170 } });
  const wobble = Math.sin(frame * 0.16) * 2.5;
  const icon = REACTION_ICON[scene.avatar_cue || "idle"] ?? "🐋";
  const memeEntry =
    (vp.meme_asset_id && memeAssets?.[vp.meme_asset_id]) ||
    (memeAssets ? Object.values(memeAssets)[0] : undefined);

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        padding: `0 ${includeAvatar ? 286 : 48}px 146px 32px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          width: "100%",
          borderRadius: 30,
          padding: "42px 48px",
          backgroundColor: AQUA_THEME.cream,
          border: `3.5px solid ${AQUA_THEME.border}`,
          boxShadow: AQUA_THEME.shadowLg,
          display: "flex",
          alignItems: "center",
          gap: 38,
          transform: `scale(${interpolate(pop, [0, 1], [0.88, 1])})`,
          opacity: pop,
        }}
      >
        <div
          style={{
            width: 224,
            height: 224,
            flexShrink: 0,
            borderRadius: 28,
            padding: 16,
            background: "linear-gradient(145deg, #e0f2fe, #fff8e8)",
            border: `3px solid ${AQUA_THEME.border}`,
            boxShadow: "5px 5px 0 rgba(14, 165, 233, 0.28)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            transform: `rotate(${wobble.toFixed(2)}deg)`,
          }}
        >
          {memeEntry ? (
            <div style={{ width: 176, height: 176 }}>
              <AnimatedSticker asset={memeEntry} alt={memeEntry.title || vp.meme_asset_id || "鲸鱼表情包"} />
            </div>
          ) : (
            <span style={{ fontSize: 92 }}>{icon}</span>
          )}
          {memeEntry?.title ? (
            <span
              style={{
                marginTop: 4,
                padding: "2px 10px",
                borderRadius: 999,
                backgroundColor: AQUA_THEME.yellow,
                border: `1.5px solid ${AQUA_THEME.border}`,
                fontSize: 13,
                fontWeight: 800,
                color: AQUA_THEME.ink,
              }}
            >
              {memeEntry.title}
            </span>
          ) : null}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16, flex: 1 }}>
          <span
            style={{
              alignSelf: "flex-start",
              padding: "6px 16px",
              borderRadius: 999,
              backgroundColor: AQUA_THEME.coralSoft,
              border: `2px solid ${AQUA_THEME.border}`,
              color: "#be123c",
              fontSize: 18,
              fontWeight: 800,
            }}
          >
            槽点现场 💥
          </span>
          <div style={{ fontSize: 50, fontWeight: 900, color: AQUA_THEME.ink, lineHeight: 1.2 }}>
            {scene.heading}
          </div>
          {vp.subtitle ? (
            <div style={{ fontSize: 28, fontWeight: 600, color: AQUA_THEME.inkSoft, lineHeight: 1.42 }}>
              {vp.subtitle}
            </div>
          ) : null}
          {bullets.length > 0 ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 4 }}>
              {bullets.map((b, i) => (
                <span
                  key={`${b}-${i}`}
                  style={{
                    padding: "8px 16px",
                    borderRadius: 12,
                    backgroundColor: i % 2 === 0 ? "#fef9c3" : AQUA_THEME.skySoft,
                    border: `2px solid ${AQUA_THEME.border}`,
                    color: AQUA_THEME.ink,
                    fontSize: 21,
                    fontWeight: 700,
                  }}
                >
                  #{b}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};
