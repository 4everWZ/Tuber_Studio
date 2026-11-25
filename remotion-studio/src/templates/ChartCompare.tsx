import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AQUA_THEME, CJK_FONT_STACK, MONO_FONT_STACK, type AquaSceneProp } from "../types";

const BAR_COLORS = [
  "#0ea5e9",
  "#fb7185",
  "#14b8a6",
  "#f59e0b",
  "#6366f1",
];

export const ChartCompare: React.FC<{ scene: AquaSceneProp; includeAvatar?: boolean }> = ({
  scene,
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const items = vp.chart_items ?? [];
  const bullets = vp.bullets ?? [];
  const maxAbs = Math.max(1, ...items.map((item) => Math.abs(Number(item.value) || 0)));

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        gap: 20,
        padding: `0 ${includeAvatar ? 286 : 36}px 146px 28px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div>
        <div
          style={{
            display: "inline-block",
            padding: "4px 14px",
            borderRadius: 999,
            backgroundColor: AQUA_THEME.skySoft,
            border: `2px solid ${AQUA_THEME.border}`,
            fontSize: 16,
            fontWeight: 800,
            color: AQUA_THEME.ink,
            marginBottom: 8,
          }}
        >
          实测拉踩表 📊
        </div>
        <div style={{ fontSize: 46, fontWeight: 900, color: AQUA_THEME.ink }}>{scene.heading}</div>
        {vp.subtitle ? (
          <div style={{ fontSize: 23, fontWeight: 600, color: AQUA_THEME.inkSoft, marginTop: 4 }}>
            {vp.subtitle}
          </div>
        ) : null}
      </div>

      <div
        style={{
          flex: 1,
          borderRadius: 26,
          padding: "28px 34px",
          backgroundColor: AQUA_THEME.cream,
          border: `3px solid ${AQUA_THEME.border}`,
          boxShadow: AQUA_THEME.shadowLg,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 20,
        }}
      >
        {items.length > 0 ? (
          items.map((item, idx) => {
            const grow = spring({
              frame: Math.max(0, frame - idx * 6),
              fps,
              config: { damping: 15, stiffness: 115 },
            });
            const ratio = Math.min(1, Math.abs(Number(item.value) || 0) / maxAbs);
            const widthPct = Math.max(8, Math.round(ratio * 100 * grow));
            const animatedValue = interpolate(grow, [0, 1], [0, Number(item.value) || 0]);
            const formatted =
              Number.isInteger(item.value) ? Math.round(animatedValue).toString() : animatedValue.toFixed(1);

            return (
              <div key={`${item.label}-${idx}`} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <span style={{ fontSize: 25, fontWeight: 800, color: AQUA_THEME.ink }}>{item.label}</span>
                  <span
                    style={{
                      fontFamily: MONO_FONT_STACK,
                      fontSize: 24,
                      fontWeight: 800,
                      padding: "2px 12px",
                      borderRadius: 999,
                      backgroundColor: AQUA_THEME.creamTint,
                      border: `2px solid ${AQUA_THEME.border}`,
                      color: AQUA_THEME.ink,
                    }}
                  >
                    {formatted}
                    {item.unit ? ` ${item.unit}` : ""}
                  </span>
                </div>
                <div
                  style={{
                    height: 28,
                    width: "100%",
                    borderRadius: 999,
                    backgroundColor: "#e2e8f0",
                    border: `2px solid ${AQUA_THEME.border}`,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${widthPct}%`,
                      borderRadius: 999,
                      backgroundColor: BAR_COLORS[idx % BAR_COLORS.length],
                    }}
                  />
                </div>
              </div>
            );
          })
        ) : (
          <div style={{ fontSize: 28, fontWeight: 700, color: AQUA_THEME.inkSoft, textAlign: "center" }}>
            {bullets.join(" · ") || "暂无对比指标项"}
          </div>
        )}
      </div>

      {vp.citation_label ? (
        <div style={{ fontSize: 18, fontWeight: 700, color: AQUA_THEME.muted, textAlign: "right" }}>
          📎 来源便签：{vp.citation_label}
        </div>
      ) : null}
    </div>
  );
};
