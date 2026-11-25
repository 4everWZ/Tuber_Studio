import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, type AquaSceneProp } from "../types";

const REACTION_ICON: Record<string, string> = {
  cheerful: "😆",
  curious: "🧐",
  proud: "😎",
  thinking: "🤔",
  surprised: "🤯",
  calm: "😌",
  idle: "🐋",
};

export const MemeCutaway: React.FC<{ scene: AquaSceneProp }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];

  const pop = spring({ frame, fps, config: { damping: 11, stiffness: 170 } });
  const wobble = Math.sin(frame * 0.16) * 2.2;
  const icon = REACTION_ICON[scene.avatar_cue || "idle"] ?? "🐋";

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        padding: "0 48px 130px",
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          width: "84%",
          borderRadius: 28,
          padding: "46px 56px",
          background: "linear-gradient(135deg, rgba(30, 41, 59, 0.92), rgba(15, 23, 42, 0.95))",
          border: "2px solid rgba(244, 114, 182, 0.55)",
          boxShadow: "0 24px 64px rgba(244, 114, 182, 0.18)",
          display: "flex",
          alignItems: "center",
          gap: 44,
          transform: `scale(${interpolate(pop, [0, 1], [0.85, 1])})`,
          opacity: pop,
        }}
      >
        <div
          style={{
            width: 176,
            height: 176,
            flexShrink: 0,
            borderRadius: 32,
            background: "radial-gradient(circle at 35% 30%, rgba(244, 114, 182, 0.35), rgba(56, 189, 248, 0.18))",
            border: "2px solid rgba(56, 189, 248, 0.45)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            transform: `rotate(${wobble}deg)`,
          }}
        >
          <span style={{ fontSize: 82 }}>{icon}</span>
          {vp.meme_asset_id ? (
            <span style={{ fontSize: 13, color: "#94a3b8", marginTop: 6 }}>{vp.meme_asset_id}</span>
          ) : null}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18, flex: 1 }}>
          <span
            style={{
              alignSelf: "flex-start",
              padding: "6px 14px",
              borderRadius: 999,
              backgroundColor: "rgba(244, 114, 182, 0.18)",
              color: "#f9a8d4",
              fontSize: 17,
              fontWeight: 700,
            }}
          >
            AQUA 吐槽插播
          </span>
          <div style={{ fontSize: 54, fontWeight: 800, color: "#f8fafc", lineHeight: 1.2 }}>
            {scene.heading}
          </div>
          {vp.subtitle ? (
            <div style={{ fontSize: 30, color: "#cbd5e1", lineHeight: 1.4 }}>{vp.subtitle}</div>
          ) : null}
          {bullets.length > 0 ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 6 }}>
              {bullets.map((b, i) => (
                <span
                  key={`${b}-${i}`}
                  style={{
                    padding: "8px 16px",
                    borderRadius: 12,
                    backgroundColor: "rgba(15, 23, 42, 0.72)",
                    border: "1px solid rgba(148, 163, 184, 0.25)",
                    color: "#e2e8f0",
                    fontSize: 22,
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
