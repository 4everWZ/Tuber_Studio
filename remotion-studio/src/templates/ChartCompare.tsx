import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, MONO_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

const BAR_COLORS = [
  "#38bdf8",
  "#2dd4bf",
  "#fbbf24",
  "#fb7185",
  "#818cf8",
];

export const ChartCompare: React.FC<SceneTemplateProps> = ({
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
  const items = vp.chart_items ?? [];
  const bullets = vp.bullets ?? [];
  const maxAbs = Math.max(1, ...items.map((item) => Math.abs(Number(item.value) || 0)));

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    Math.max(1, items.length),
    frame,
    scene.duration_frames,
    sceneCues.length
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
        gap: 16,
        padding: `18px ${rightClearance}px 128px 46px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div>
        <div
          style={{
            display: "inline-block",
            padding: "4px 14px",
            borderRadius: 999,
            backgroundColor: palette.accentSoft,
            border: `1px solid ${palette.borderStrong}`,
            fontSize: 14,
            fontWeight: 800,
            color: palette.pillText,
            letterSpacing: 0.8,
            marginBottom: 8,
          }}
        >
          BENCHMARK · 数据对比
        </div>
        <div style={{ fontSize: 44, fontWeight: 900, color: palette.ink }}>{scene.heading}</div>
        {vp.subtitle ? (
          <div style={{ fontSize: 22, fontWeight: 600, color: palette.inkSoft, marginTop: 4 }}>
            {vp.subtitle}
          </div>
        ) : null}
      </div>

      <div
        style={{
          flex: 1,
          borderRadius: 24,
          padding: "26px 34px",
          backgroundColor: palette.surface,
          border: `1.5px solid ${palette.border}`,
          boxShadow: palette.cardShadow,
          backdropFilter: "blur(10px)",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 18,
        }}
      >
        {items.length > 0 ? (
          items.map((item, idx) => {
            const unlocked = idx <= revealStep || frame > idx * 6;
            const isCurrent = idx === revealStep;
            const grow = spring({
              frame: unlocked ? Math.max(0, frame - idx * 5) : 0,
              fps,
              config: { damping: 15, stiffness: 120 },
            });
            const ratio = Math.min(1, Math.abs(Number(item.value) || 0) / maxAbs);
            const widthPct = Math.max(6, Math.round(ratio * 100 * grow));
            const animatedValue = interpolate(grow, [0, 1], [0, Number(item.value) || 0]);
            const formatted = Number.isInteger(item.value)
              ? Math.round(animatedValue).toString()
              : animatedValue.toFixed(1);
            const barColor = BAR_COLORS[idx % BAR_COLORS.length];

            return (
              <div
                key={`${item.label}-${idx}`}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  padding: "8px 12px",
                  borderRadius: 14,
                  backgroundColor: isCurrent ? palette.surfaceElevated : "transparent",
                  border: `1px solid ${isCurrent ? barColor : "transparent"}`,
                  opacity: isCurrent || idx <= revealStep ? 1 : 0.55,
                  transform: `scale(${isCurrent ? 1.015 : 1})`,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <span style={{ fontSize: 24, fontWeight: 800, color: palette.ink }}>
                    {isCurrent ? "▸ " : ""}
                    {item.label}
                  </span>
                  <span
                    style={{
                      fontFamily: MONO_FONT_STACK,
                      fontSize: 22,
                      fontWeight: 800,
                      padding: "2px 12px",
                      borderRadius: 999,
                      backgroundColor: isCurrent ? `${barColor}28` : palette.surfaceMuted,
                      border: `1px solid ${isCurrent ? barColor : palette.border}`,
                      color: palette.ink,
                    }}
                  >
                    {formatted}
                    {item.unit ? ` ${item.unit}` : ""}
                  </span>
                </div>
                <div
                  style={{
                    height: 24,
                    width: "100%",
                    borderRadius: 999,
                    backgroundColor: palette.surfaceMuted,
                    border: `1px solid ${palette.border}`,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${widthPct}%`,
                      borderRadius: 999,
                      background: `linear-gradient(90deg, ${barColor}, ${barColor}cc)`,
                      boxShadow: isCurrent ? `0 0 16px ${barColor}77` : "none",
                    }}
                  />
                </div>
              </div>
            );
          })
        ) : (
          <div style={{ fontSize: 26, fontWeight: 700, color: palette.inkSoft, textAlign: "center" }}>
            {bullets.join(" · ") || "暂无对比指标项"}
          </div>
        )}
      </div>

      {vp.citation_label ? (
        <div style={{ fontSize: 16, fontWeight: 700, color: palette.muted, textAlign: "right" }}>
          📎 数据来源：{vp.citation_label}
        </div>
      ) : null}
    </div>
  );
};
