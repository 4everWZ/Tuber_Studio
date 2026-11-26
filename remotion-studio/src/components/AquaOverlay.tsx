import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { getThemePalette } from "../theme";
import {
  CJK_FONT_STACK,
  type AvatarLayout,
  type VisualAssetEntry,
} from "../types";
import { getEnvelopeLevel } from "../utils/beatSync";
import { AnimatedSticker } from "./AnimatedSticker";

const CUE_META: Record<
  string,
  { label: string; state: string; altState: string; fallbackEmoji: string; ringColor: string }
> = {
  idle: { label: "摸鱼待机", state: "hum", altState: "idle", fallbackEmoji: "🐋", ringColor: "#38bdf8" },
  cheerful: { label: "开心开麦", state: "dance", altState: "cheerful", fallbackEmoji: "✨", ringColor: "#fbbf24" },
  curious: { label: "好奇探头", state: "turn", altState: "confused", fallbackEmoji: "🔍", ringColor: "#818cf8" },
  proud: { label: "得意晃尾巴", state: "tail", altState: "gift", fallbackEmoji: "🌟", ringColor: "#2dd4bf" },
  thinking: { label: "呆毛转圈", state: "token", altState: "confused", fallbackEmoji: "💭", ringColor: "#f59e0b" },
  surprised: { label: "震惊吃鲸", state: "jump", altState: "angry", fallbackEmoji: "⚡", ringColor: "#fb7185" },
  calm: { label: "淡定吃白饭", state: "meal", altState: "eat", fallbackEmoji: "🍚", ringColor: "#38bdf8" },
};

export interface AquaOverlayProps {
  cue?: string;
  speaking?: boolean;
  includeAvatar?: boolean;
  avatarLayout?: AvatarLayout;
  avatarAssets?: Record<string, VisualAssetEntry>;
  audioEnvelope?: number[];
  theme?: string;
}

export const AquaOverlay: React.FC<AquaOverlayProps> = ({
  cue = "idle",
  speaking = false,
  includeAvatar = true,
  avatarLayout = "corner_float",
  avatarAssets,
  audioEnvelope,
  theme,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  if (!includeAvatar || avatarLayout === "hidden") {
    return null;
  }

  const palette = getThemePalette(theme);
  const meta = CUE_META[cue] ?? CUE_META.idle;
  const preferredState =
    speaking && (cue === "idle" || cue === "calm") ? "talking" : meta.state;
  const chosenAsset =
    avatarAssets?.[preferredState] ||
    avatarAssets?.[meta.altState] ||
    avatarAssets?.talking ||
    avatarAssets?.idle ||
    undefined;

  const env = getEnvelopeLevel(audioEnvelope, frame, speaking);
  const reactiveBoost = speaking ? env : env * 0.35;

  const size =
    avatarLayout === "featured" ? 236 : avatarLayout === "compact" ? 132 : 186;
  const bottomOffset = avatarLayout === "compact" ? 20 : 22;
  const rightOffset = avatarLayout === "compact" ? 22 : 28;

  const enter = spring({
    frame,
    fps,
    config: { damping: 16, stiffness: 135 },
  });

  const floatY = Math.sin(frame * 0.085) * 4 - reactiveBoost * 8;
  const scaleX = 1 + reactiveBoost * 0.032;
  const scaleY = 1 - reactiveBoost * 0.024;
  const ringScale = 1 + reactiveBoost * 0.14;
  const ringOpacity = speaking ? 0.38 + reactiveBoost * 0.52 : 0.22;

  const radius = size / 2;
  const strokeWidth = avatarLayout === "compact" ? 2.5 : 3.5;
  const ringR = radius - 6;
  const circumference = 2 * Math.PI * ringR;
  const dashArc = circumference * (0.28 + reactiveBoost * 0.65);

  return (
    <div
      style={{
        position: "absolute",
        bottom: bottomOffset,
        right: rightOffset,
        width: size,
        height: size + (avatarLayout === "compact" ? 6 : 26),
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "flex-end",
        pointerEvents: "none",
        zIndex: 8,
        fontFamily: CJK_FONT_STACK,
        opacity: enter,
        transform: `translateY(${interpolate(enter, [0, 1], [18, 0]).toFixed(1)}px)`,
      }}
    >
      <div
        style={{
          position: "relative",
          width: size,
          height: size,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {/* Subtle radial ambient glow */}
        <div
          style={{
            position: "absolute",
            inset: 6,
            borderRadius: "50%",
            background: `radial-gradient(circle, ${meta.ringColor}33 0%, ${meta.ringColor}0d 55%, transparent 72%)`,
            transform: `scale(${ringScale.toFixed(3)})`,
          }}
        />

        {/* Speech-reactive SVG energy ring */}
        <svg
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
          style={{
            position: "absolute",
            inset: 0,
            transform: `rotate(${((frame * (speaking ? 1.8 : 0.4)) % 360).toFixed(1)}deg) scale(${(1 + reactiveBoost * 0.05).toFixed(3)})`,
            opacity: ringOpacity,
          }}
        >
          <circle
            cx={radius}
            cy={radius}
            r={ringR}
            fill="none"
            stroke={palette.isDark ? "rgba(255,255,255,0.12)" : "rgba(15,34,61,0.09)"}
            strokeWidth={1.5}
          />
          <circle
            cx={radius}
            cy={radius}
            r={ringR}
            fill="none"
            stroke={meta.ringColor}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeDasharray={`${dashArc.toFixed(1)} ${circumference.toFixed(1)}`}
          />
        </svg>

        {/* Borderless floating Aqua cutout */}
        <div
          style={{
            width: Math.round(size * 0.84),
            height: Math.round(size * 0.84),
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            transform: `translateY(${floatY.toFixed(2)}px) scale(${scaleX.toFixed(3)}, ${scaleY.toFixed(3)})`,
            filter: palette.isDark
              ? `drop-shadow(0 10px 22px rgba(0, 0, 0, 0.55)) drop-shadow(0 0 14px ${meta.ringColor}44)`
              : "drop-shadow(0 10px 20px rgba(15, 34, 61, 0.18))",
          }}
        >
          {chosenAsset ? (
            <AnimatedSticker asset={chosenAsset} alt={`Aqua ${meta.label}`} />
          ) : (
            <span style={{ fontSize: Math.round(size * 0.42) }}>{meta.fallbackEmoji}</span>
          )}
        </div>
      </div>

      {avatarLayout !== "compact" ? (
        <div
          style={{
            marginTop: -6,
            padding: "4px 12px",
            borderRadius: 999,
            backgroundColor: palette.subtitleBg,
            border: `1px solid ${palette.subtitleBorder}`,
            backdropFilter: "blur(8px)",
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            boxShadow: "0 4px 12px rgba(0, 0, 0, 0.16)",
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              backgroundColor: speaking ? meta.ringColor : palette.muted,
              boxShadow: speaking ? `0 0 8px ${meta.ringColor}` : "none",
            }}
          />
          <span
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: palette.subtitleInk,
              letterSpacing: 0.3,
            }}
          >
            {meta.label}
          </span>
        </div>
      ) : null}
    </div>
  );
};
