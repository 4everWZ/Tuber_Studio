import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

export const TimelineSummary: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  avatarLayout = "corner_float",
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};
  const steps = vp.bullets && vp.bullets.length > 0 ? vp.bullets : [vp.subtitle || scene.heading];

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    steps.length,
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  const spineTargetPct =
    steps.length <= 1 ? 100 : Math.round(((revealStep + 1) / steps.length) * 100);
  const lineProgress = interpolate(
    spring({ frame, fps, config: { damping: 20, stiffness: 90 } }),
    [0, 1],
    [0, spineTargetPct]
  );

  const effectiveLayout = includeAvatar ? avatarLayout : "hidden";
  const rightClearance = getAvatarRightClearance(effectiveLayout);

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        gap: 18,
        padding: `16px ${rightClearance}px 128px 48px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div>
        <div
          style={{
            display: "inline-block",
            padding: "4px 14px",
            borderRadius: 999,
            backgroundColor: palette.secondarySoft,
            border: `1px solid ${palette.borderStrong}`,
            fontSize: 14,
            fontWeight: 800,
            color: palette.pillText,
            letterSpacing: 0.8,
            marginBottom: 8,
          }}
        >
          TIMELINE · 重点回顾
        </div>
        <div style={{ fontSize: 44, fontWeight: 900, color: palette.ink }}>{scene.heading}</div>
        {vp.subtitle ? (
          <div style={{ fontSize: 22, fontWeight: 600, color: palette.inkSoft, marginTop: 4 }}>
            {vp.subtitle}
          </div>
        ) : null}
      </div>

      <div style={{ position: "relative", display: "flex", flexDirection: "column", gap: 14, paddingLeft: 26 }}>
        <div
          style={{
            position: "absolute",
            left: 19,
            top: 16,
            bottom: 16,
            width: 4,
            borderRadius: 999,
            backgroundColor: palette.surfaceMuted,
          }}
        />
        <div
          style={{
            position: "absolute",
            left: 19,
            top: 16,
            width: 4,
            height: `${lineProgress}%`,
            maxHeight: "calc(100% - 32px)",
            borderRadius: 999,
            background: `linear-gradient(180deg, ${palette.accent}, ${palette.secondary}, ${palette.highlight})`,
          }}
        />
        {steps.map((step, idx) => {
          const unlocked = idx <= revealStep;
          const isCurrent = idx === revealStep;
          const itemEnter = spring({
            frame: Math.max(0, frame - idx * 4),
            fps,
            config: { damping: 16, stiffness: 140 },
          });
          const targetOpacity = isCurrent
            ? 1
            : unlocked
              ? palette.isDark
                ? 0.82
                : 0.9
              : palette.isDark
                ? 0.58
                : 0.72;
          return (
            <div
              key={`${step}-${idx}`}
              style={{
                position: "relative",
                display: "flex",
                alignItems: "center",
                gap: 18,
                padding: "16px 22px",
                marginLeft: 18,
                borderRadius: 18,
                backgroundColor: isCurrent ? palette.surfaceElevated : palette.surface,
                border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                boxShadow: isCurrent ? `0 10px 28px ${palette.accent}24` : palette.cardShadow,
                opacity: interpolate(itemEnter, [0, 1], [0, targetOpacity]),
                transform: `translateX(${interpolate(itemEnter, [0, 1], [18, 0]).toFixed(1)}px) scale(${isCurrent ? 1.015 : 1})`,
              }}
            >
              <div
                style={{
                  width: 38,
                  height: 38,
                  flexShrink: 0,
                  borderRadius: "50%",
                  backgroundColor: isCurrent
                    ? palette.accent
                    : unlocked
                      ? palette.secondarySoft
                      : palette.surfaceMuted,
                  border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                  color: isCurrent ? "#ffffff" : palette.ink,
                  fontSize: 18,
                  fontWeight: 900,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {idx < revealStep ? "✓" : idx + 1}
              </div>
              <div
                style={{
                  fontSize: 24,
                  fontWeight: isCurrent ? 800 : 600,
                  color: isCurrent ? palette.ink : palette.inkSoft,
                  lineHeight: 1.38,
                }}
              >
                {step}
              </div>
            </div>
          );
        })}
      </div>

      {vp.citation_label ? (
        <div style={{ fontSize: 16, fontWeight: 700, color: palette.muted }}>
          📎 来源：{vp.citation_label}
        </div>
      ) : null}
    </div>
  );
};
