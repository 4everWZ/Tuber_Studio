import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, MONO_FONT_STACK, type AquaSceneProp } from "../types";

const BAR_GRADIENTS = [
  "linear-gradient(90deg, #38bdf8, #22d3ee)",
  "linear-gradient(90deg, #818cf8, #c084fc)",
  "linear-gradient(90deg, #34d399, #2dd4bf)",
  "linear-gradient(90deg, #fbbf24, #fb923c)",
  "linear-gradient(90deg, #f472b6, #fb7185)",
];

export const ChartCompare: React.FC<{ scene: AquaSceneProp }> = ({ scene }) => {
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
        gap: 28,
        padding: "0 32px 130px",
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div>
        <div style={{ fontSize: 50, fontWeight: 800, color: "#f8fafc" }}>{scene.heading}</div>
        {vp.subtitle ? (
          <div style={{ fontSize: 25, color: "#94a3b8", marginTop: 8 }}>{vp.subtitle}</div>
        ) : null}
      </div>

      <div
        style={{
          flex: 1,
          borderRadius: 22,
          padding: "32px 36px",
          backgroundColor: "rgba(15, 23, 42, 0.78)",
          border: "1px solid rgba(148, 163, 184, 0.24)",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 24,
        }}
      >
        {items.length > 0 ? (
          items.map((item, idx) => {
            const grow = spring({
              frame: Math.max(0, frame - idx * 6),
              fps,
              config: { damping: 16, stiffness: 110 },
            });
            const ratio = Math.min(1, Math.abs(Number(item.value) || 0) / maxAbs);
            const widthPct = Math.max(8, Math.round(ratio * 100 * grow));
            const animatedValue = interpolate(grow, [0, 1], [0, Number(item.value) || 0]);
            const formatted =
              Number.isInteger(item.value) ? Math.round(animatedValue).toString() : animatedValue.toFixed(1);

            return (
              <div key={`${item.label}-${idx}`} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <span style={{ fontSize: 26, fontWeight: 600, color: "#e2e8f0" }}>{item.label}</span>
                  <span
                    style={{
                      fontFamily: MONO_FONT_STACK,
                      fontSize: 28,
                      fontWeight: 700,
                      color: "#7dd3fc",
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
                    backgroundColor: "rgba(51, 65, 85, 0.55)",
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${widthPct}%`,
                      borderRadius: 999,
                      background: BAR_GRADIENTS[idx % BAR_GRADIENTS.length],
                      boxShadow: "0 0 18px rgba(56, 189, 248, 0.35)",
                    }}
                  />
                </div>
              </div>
            );
          })
        ) : (
          <div style={{ fontSize: 28, color: "#94a3b8", textAlign: "center" }}>
            {bullets.join(" · ") || "暂无对比指标项"}
          </div>
        )}
      </div>

      {vp.citation_label ? (
        <div style={{ fontSize: 18, color: "#94a3b8", textAlign: "right" }}>
          数据来源：{vp.citation_label}
        </div>
      ) : null}
    </div>
  );
};
