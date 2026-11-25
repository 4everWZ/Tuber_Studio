import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, MONO_FONT_STACK, type AquaCueProp, type AquaSceneProp } from "../types";

export interface CodeWalkthroughProps {
  scene: AquaSceneProp;
  activeCue?: AquaCueProp;
}

export const CodeWalkthrough: React.FC<CodeWalkthroughProps> = ({ scene, activeCue }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const rawCode = vp.code_snippet?.trim() || "// Code walkthrough snippet";
  const codeLines = rawCode.split("\n").slice(0, 18);
  const bullets = vp.bullets ?? [];
  const language = vp.language || "typescript";
  const highlightTarget = (activeCue?.highlight_code || "").trim().toLowerCase();

  const windowEnter = spring({ frame, fps, config: { damping: 16, stiffness: 130 } });
  const autoFocusLine = Math.floor(
    interpolate(frame, [0, Math.max(1, scene.duration_frames)], [0, Math.max(1, codeLines.length)], {
      extrapolateRight: "clamp",
    })
  );

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        gap: 24,
        padding: "0 24px 130px",
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
        <div>
          <div style={{ fontSize: 48, fontWeight: 800, color: "#f8fafc" }}>{scene.heading}</div>
          {vp.subtitle ? (
            <div style={{ fontSize: 24, color: "#94a3b8", marginTop: 6 }}>{vp.subtitle}</div>
          ) : null}
        </div>
        <span
          style={{
            padding: "8px 16px",
            borderRadius: 10,
            backgroundColor: "rgba(56, 189, 248, 0.16)",
            border: "1px solid rgba(56, 189, 248, 0.4)",
            color: "#7dd3fc",
            fontFamily: MONO_FONT_STACK,
            fontSize: 18,
            fontWeight: 700,
            textTransform: "uppercase",
          }}
        >
          {language}
        </span>
      </div>

      <div style={{ display: "flex", gap: 28, flex: 1, minHeight: 0 }}>
        <div
          style={{
            flex: bullets.length > 0 ? 1.55 : 1,
            borderRadius: 20,
            backgroundColor: "#0b1220",
            border: "1px solid rgba(148, 163, 184, 0.25)",
            boxShadow: "0 24px 54px rgba(2, 6, 23, 0.65)",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            opacity: windowEnter,
            transform: `translateY(${interpolate(windowEnter, [0, 1], [24, 0])}px)`,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "14px 20px",
              backgroundColor: "rgba(30, 41, 59, 0.85)",
              borderBottom: "1px solid rgba(148, 163, 184, 0.16)",
            }}
          >
            <div style={{ display: "flex", gap: 8 }}>
              <span style={{ width: 12, height: 12, borderRadius: "50%", backgroundColor: "#f87171" }} />
              <span style={{ width: 12, height: 12, borderRadius: "50%", backgroundColor: "#fbbf24" }} />
              <span style={{ width: 12, height: 12, borderRadius: "50%", backgroundColor: "#34d399" }} />
            </div>
            <span style={{ fontFamily: MONO_FONT_STACK, fontSize: 16, color: "#94a3b8" }}>
              {vp.citation_label || `snippet.${language}`}
            </span>
          </div>

          <div style={{ padding: "18px 0", fontFamily: MONO_FONT_STACK, fontSize: 22, lineHeight: 1.55 }}>
            {codeLines.map((line, idx) => {
              const lineEnter = spring({
                frame: Math.max(0, frame - idx * 2),
                fps,
                config: { damping: 20, stiffness: 160 },
              });
              const isHighlighted = highlightTarget
                ? line.toLowerCase().includes(highlightTarget)
                : idx === autoFocusLine;
              return (
                <div
                  key={`${idx}-${line.slice(0, 16)}`}
                  style={{
                    display: "flex",
                    alignItems: "baseline",
                    padding: "4px 24px",
                    backgroundColor: isHighlighted ? "rgba(56, 189, 248, 0.18)" : "transparent",
                    borderLeft: isHighlighted ? "4px solid #38bdf8" : "4px solid transparent",
                    opacity: lineEnter,
                  }}
                >
                  <span
                    style={{
                      width: 44,
                      flexShrink: 0,
                      color: isHighlighted ? "#7dd3fc" : "#475569",
                      userSelect: "none",
                      fontSize: 18,
                    }}
                  >
                    {idx + 1}
                  </span>
                  <span
                    style={{
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      color: isHighlighted ? "#f8fafc" : "#cbd5e1",
                      fontWeight: isHighlighted ? 600 : 400,
                    }}
                  >
                    {line || " "}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {bullets.length > 0 ? (
          <div style={{ flex: 0.75, display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ fontSize: 20, fontWeight: 700, color: "#7dd3fc", letterSpacing: 0.6 }}>
              关键要点
            </div>
            {bullets.map((item, idx) => {
              const itemEnter = spring({
                frame: Math.max(0, frame - 8 - idx * 6),
                fps,
                config: { damping: 17, stiffness: 140 },
              });
              return (
                <div
                  key={`${item}-${idx}`}
                  style={{
                    padding: "18px 20px",
                    borderRadius: 16,
                    backgroundColor: "rgba(30, 41, 59, 0.72)",
                    border: "1px solid rgba(56, 189, 248, 0.25)",
                    color: "#e2e8f0",
                    fontSize: 23,
                    lineHeight: 1.4,
                    opacity: itemEnter,
                    transform: `translateX(${interpolate(itemEnter, [0, 1], [24, 0])}px)`,
                  }}
                >
                  <span style={{ color: "#38bdf8", fontWeight: 700, marginRight: 10 }}>
                    0{idx + 1}
                  </span>
                  {item}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
};
