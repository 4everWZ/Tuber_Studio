import React from "react";
import { spring, useCurrentFrame, useVideoConfig } from "remotion";
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
  const { fps, height } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};
  const items = vp.chart_items ?? [];
  const bullets = vp.bullets ?? [];
  const values = items.map((item) => Number(item.value) || 0);
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const span = maximum - minimum || 1;
  const zero = (-minimum / span) * 100;
  const comparable = new Set(items.map((item) => (item.unit || "").trim())).size <= 1;
  const dense = height <= 720 || items.length > 5;

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
          {comparable ? "数据对比" : "不同单位 · 原值"}
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
          minHeight: 0,
          borderRadius: 24,
          padding: dense ? "12px 24px" : "26px 34px",
          backgroundColor: palette.surface,
          border: `1.5px solid ${palette.border}`,
          boxShadow: palette.cardShadow,
          backdropFilter: "blur(10px)",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: dense ? 4 : 14,
        }}
      >
        {items.length > 0 ? (
          items.map((item, idx) => {
            const isCurrent = idx === revealStep;
            const grow = spring({
              frame: Math.max(0, frame - idx * Math.round(fps * 0.1)),
              fps,
              config: { damping: 17, stiffness: 145, overshootClamping: true },
            });
            const value = values[idx];
            const widthPct = (Math.abs(value) / span) * 100 * grow;
            const startPct = value < 0 ? zero - widthPct : zero;
            const barColor = BAR_COLORS[idx % BAR_COLORS.length];

            return (
              <div
                key={`${item.label}-${idx}`}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: dense ? 3 : 8,
                  padding: dense ? "3px 10px" : "8px 12px",
                  borderRadius: 14,
                  backgroundColor: isCurrent ? palette.surfaceElevated : "transparent",
                  border: `1px solid ${isCurrent ? barColor : "transparent"}`,
                  opacity: isCurrent ? 1 : 0.88,
                }}
              >
                <div style={{ display: "flex", gap: 12, justifyContent: "space-between", alignItems: "baseline" }}>
                  <span style={{ fontSize: dense ? 19 : 24, fontWeight: 800, color: palette.ink }}>
                    {isCurrent ? "▸ " : ""}
                    {item.label}
                  </span>
                  <span
                    style={{
                      fontFamily: MONO_FONT_STACK,
                      fontSize: dense ? 17 : 22,
                      fontWeight: 800,
                      padding: "2px 12px",
                      borderRadius: 999,
                      backgroundColor: isCurrent ? `${barColor}28` : palette.surfaceMuted,
                      border: `1px solid ${isCurrent ? barColor : palette.border}`,
                      color: palette.ink,
                      whiteSpace: "nowrap",
                    }}
                  >
                    {value}
                    {item.unit ? ` ${item.unit}` : ""}
                  </span>
                </div>
                {comparable ? (
                  <div
                    style={{
                      position: "relative",
                      height: dense ? 8 : 18,
                      borderRadius: 999,
                      backgroundColor: palette.surfaceMuted,
                      border: `1px solid ${palette.border}`,
                      overflow: "hidden",
                    }}
                  >
                    {widthPct > 0 ? (
                      <div
                        style={{
                          position: "absolute",
                          left: `${startPct}%`,
                          height: "100%",
                          width: `${widthPct}%`,
                          backgroundColor: barColor,
                        }}
                      />
                    ) : null}
                    {minimum < 0 && maximum > 0 ? (
                      <div
                        style={{
                          position: "absolute",
                          left: `${zero}%`,
                          height: "100%",
                          width: 1,
                          backgroundColor: palette.inkSoft,
                        }}
                      />
                    ) : null}
                  </div>
                ) : null}
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
