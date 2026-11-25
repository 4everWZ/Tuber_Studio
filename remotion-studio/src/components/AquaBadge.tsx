import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK } from "../types";

const CUE_META: Record<string, { label: string; color: string; glow: string; emoji: string }> = {
  idle: { label: "Aqua", color: "#38bdf8", glow: "rgba(56,189,248,0.35)", emoji: "🐋" },
  cheerful: { label: "开心解说", color: "#22d3ee", glow: "rgba(34,211,238,0.45)", emoji: "✨" },
  curious: { label: "好奇探究", color: "#818cf8", glow: "rgba(129,140,248,0.42)", emoji: "🔍" },
  proud: { label: "得意总结", color: "#34d399", glow: "rgba(52,211,153,0.42)", emoji: "🌟" },
  thinking: { label: "认真思考", color: "#fbbf24", glow: "rgba(251,191,36,0.38)", emoji: "💭" },
  surprised: { label: "惊喜发现", color: "#f472b6", glow: "rgba(244,114,182,0.45)", emoji: "⚡" },
  calm: { label: "沉稳陈述", color: "#60a5fa", glow: "rgba(96,165,250,0.35)", emoji: "🌊" },
};

export interface AquaBadgeProps {
  cue?: string;
  speaking?: boolean;
  includeAvatar?: boolean;
}

export const AquaBadge: React.FC<AquaBadgeProps> = ({
  cue = "idle",
  speaking = false,
  includeAvatar = false,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const meta = CUE_META[cue] ?? CUE_META.idle;
  const pulse = spring({ frame: frame % Math.max(1, fps), fps, config: { damping: 14, stiffness: 120 } });
  const scale = speaking ? interpolate(pulse, [0, 1], [1, 1.04]) : 1;

  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 14,
        padding: includeAvatar ? "10px 22px 10px 14px" : "8px 18px",
        borderRadius: 999,
        backgroundColor: "rgba(15, 23, 42, 0.82)",
        border: `1.5px solid ${meta.color}`,
        boxShadow: `0 0 24px ${meta.glow}`,
        transform: `scale(${scale})`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          width: includeAvatar ? 42 : 34,
          height: includeAvatar ? 42 : 34,
          borderRadius: "50%",
          background: `radial-gradient(circle at 30% 30%, ${meta.color}, #0f172a 85%)`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: includeAvatar ? 22 : 18,
          boxShadow: `inset 0 0 12px ${meta.glow}`,
        }}
      >
        {meta.emoji}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 18, fontWeight: 700, color: "#f8fafc", letterSpacing: 0.4 }}>
          Aqua
        </span>
        <span style={{ fontSize: 13, fontWeight: 500, color: meta.color }}>{meta.label}</span>
      </div>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 3, height: 22, marginLeft: 4 }}>
        {[0, 1, 2, 3].map((bar) => {
          const wave = speaking
            ? 0.35 + 0.65 * Math.abs(Math.sin((frame + bar * 5) * 0.32))
            : 0.25;
          return (
            <div
              key={bar}
              style={{
                width: 4,
                height: Math.round(6 + wave * 16),
                borderRadius: 2,
                backgroundColor: meta.color,
                opacity: speaking ? 0.95 : 0.45,
              }}
            />
          );
        })}
      </div>
    </div>
  );
};
