import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AQUA_THEME, CJK_FONT_STACK, type VisualAssetEntry } from "../types";
import { AnimatedSticker } from "./AnimatedSticker";

const CUE_META: Record<string, { label: string; badgeBg: string; badgeInk: string; state: string; altState: string; fallbackEmoji: string }> = {
  idle: { label: "本鲸摸鱼中", badgeBg: "#e0f2fe", badgeInk: "#0369a1", state: "hum", altState: "idle", fallbackEmoji: "🐋" },
  cheerful: { label: "开心开麦", badgeBg: "#fef08a", badgeInk: "#92400e", state: "dance", altState: "cheerful", fallbackEmoji: "✨" },
  curious: { label: "好奇探头", badgeBg: "#e0e7ff", badgeInk: "#3730a3", state: "turn", altState: "confused", fallbackEmoji: "🔍" },
  proud: { label: "得意晃尾巴", badgeBg: "#ccfbf1", badgeInk: "#0f766e", state: "tail", altState: "gift", fallbackEmoji: "🌟" },
  thinking: { label: "呆毛转圈", badgeBg: "#fef3c7", badgeInk: "#92400e", state: "token", altState: "confused", fallbackEmoji: "💭" },
  surprised: { label: "震惊吃鲸", badgeBg: "#ffe4e6", badgeInk: "#be123c", state: "jump", altState: "angry", fallbackEmoji: "⚡" },
  calm: { label: "淡定吃白饭", badgeBg: "#e0f2fe", badgeInk: "#0369a1", state: "meal", altState: "eat", fallbackEmoji: "🍚" },
};

export interface AquaBadgeProps {
  cue?: string;
  speaking?: boolean;
  includeAvatar?: boolean;
  avatarAssets?: Record<string, VisualAssetEntry>;
}

export const AquaBadge: React.FC<AquaBadgeProps> = ({
  cue = "idle",
  speaking = false,
  includeAvatar = true,
  avatarAssets,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const meta = CUE_META[cue] ?? CUE_META.idle;
  const preferredState =
    speaking && (cue === "idle" || cue === "calm") ? "talking" : meta.state;
  const chosenAsset =
    avatarAssets?.[preferredState] ||
    avatarAssets?.[meta.altState] ||
    avatarAssets?.talking ||
    avatarAssets?.idle ||
    undefined;

  const talkBounce = speaking ? Math.abs(Math.sin(frame * 0.35)) : 0;
  const squashX = speaking ? 1 + talkBounce * 0.025 : 1;
  const stretchY = speaking ? 1 - talkBounce * 0.02 : 1;
  const hopY = speaking ? -talkBounce * 6 : Math.sin(frame * 0.08) * 2;
  const enter = spring({ frame, fps, config: { damping: 16, stiffness: 120 } });

  if (!includeAvatar) {
    return null;
  }

  return (
    <div
      style={{
        position: "absolute",
        bottom: 22,
        right: 26,
        width: 248,
        height: 264,
        borderRadius: 28,
        backgroundColor: "rgba(255, 253, 247, 0.92)",
        border: `3px solid ${AQUA_THEME.border}`,
        boxShadow: AQUA_THEME.shadowLg,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        zIndex: 8,
        fontFamily: CJK_FONT_STACK,
        opacity: enter,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "10px 14px",
          background: "linear-gradient(90deg, #e0f2fe, #fff8e8)",
          borderBottom: `2px solid rgba(27, 53, 84, 0.14)`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: "50%",
              backgroundColor: speaking ? AQUA_THEME.coral : AQUA_THEME.mint,
              display: "inline-block",
            }}
          />
          <span style={{ fontSize: 15, fontWeight: 800, color: AQUA_THEME.ink }}>Aqua 小窗</span>
        </div>
        <span
          style={{
            padding: "2px 9px",
            borderRadius: 999,
            backgroundColor: meta.badgeBg,
            color: meta.badgeInk,
            fontSize: 12,
            fontWeight: 700,
          }}
        >
          {meta.label}
        </span>
      </div>

      <div
        style={{
          flex: 1,
          position: "relative",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "8px 12px 12px",
          background:
            "radial-gradient(circle at 50% 62%, rgba(14, 165, 233, 0.14), rgba(255, 253, 247, 0.4) 72%)",
        }}
      >
        <div
          style={{
            width: 186,
            height: 186,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            transform: `translateY(${hopY.toFixed(2)}px) scale(${squashX.toFixed(3)}, ${stretchY.toFixed(3)})`,
          }}
        >
          {chosenAsset ? (
            <AnimatedSticker asset={chosenAsset} alt={`Aqua ${meta.label}`} />
          ) : (
            <span style={{ fontSize: 82 }}>{meta.fallbackEmoji}</span>
          )}
        </div>

        <div
          style={{
            position: "absolute",
            bottom: 10,
            right: 14,
            display: "flex",
            alignItems: "flex-end",
            gap: 3,
            height: 18,
            padding: "4px 8px",
            borderRadius: 999,
            backgroundColor: "rgba(255, 255, 255, 0.85)",
            border: "1px solid rgba(27, 53, 84, 0.16)",
          }}
        >
          {[0, 1, 2, 3].map((bar) => {
            const wave = speaking
              ? 0.35 + 0.65 * Math.abs(Math.sin((frame + bar * 4) * 0.34))
              : 0.22;
            return (
              <div
                key={bar}
                style={{
                  width: 4,
                  height: Math.round(4 + wave * 12),
                  borderRadius: 2,
                  backgroundColor: speaking ? AQUA_THEME.sky : AQUA_THEME.muted,
                }}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
};
