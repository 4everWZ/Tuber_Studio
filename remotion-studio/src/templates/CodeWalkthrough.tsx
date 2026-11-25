import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AnimatedSticker } from "../components/AnimatedSticker";
import { AQUA_THEME, CJK_FONT_STACK, MONO_FONT_STACK, type AquaCueProp, type AquaSceneProp, type VisualAssetEntry } from "../types";

export interface CodeWalkthroughProps {
  scene: AquaSceneProp;
  activeCue?: AquaCueProp;
  memeAssets?: Record<string, VisualAssetEntry>;
  includeAvatar?: boolean;
}

export const CodeWalkthrough: React.FC<CodeWalkthroughProps> = ({
  scene,
  activeCue,
  memeAssets,
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const vp = scene.visual_props || {};
  const rawCode = vp.code_snippet?.trim() || "// Code walkthrough snippet";
  const codeLines = rawCode.split("\n").slice(0, 16);
  const bullets = vp.bullets ?? [];
  const language = vp.language || "python";
  const highlightTarget = (activeCue?.highlight_code || "").trim().toLowerCase();
  const memeEntry = vp.meme_asset_id ? memeAssets?.[vp.meme_asset_id] : undefined;

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
        gap: 18,
        padding: `0 ${includeAvatar ? 286 : 32}px 146px 24px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16 }}>
        <div>
          <div
            style={{
              display: "inline-block",
              padding: "4px 14px",
              borderRadius: 999,
              backgroundColor: AQUA_THEME.mintSoft,
              border: `2px solid ${AQUA_THEME.border}`,
              fontSize: 16,
              fontWeight: 800,
              color: AQUA_THEME.ink,
              marginBottom: 8,
            }}
          >
            桌面小窗 · 拆代码
          </div>
          <div style={{ fontSize: 44, fontWeight: 900, color: AQUA_THEME.ink, lineHeight: 1.15 }}>
            {scene.heading}
          </div>
          {vp.subtitle ? (
            <div style={{ fontSize: 22, fontWeight: 600, color: AQUA_THEME.inkSoft, marginTop: 4 }}>
              {vp.subtitle}
            </div>
          ) : null}
        </div>
        <span
          style={{
            padding: "7px 16px",
            borderRadius: 12,
            backgroundColor: AQUA_THEME.yellow,
            border: `2px solid ${AQUA_THEME.border}`,
            color: AQUA_THEME.ink,
            fontFamily: MONO_FONT_STACK,
            fontSize: 17,
            fontWeight: 800,
            textTransform: "uppercase",
            boxShadow: "3px 3px 0 rgba(27, 53, 84, 0.14)",
          }}
        >
          {language}
        </span>
      </div>

      <div style={{ display: "flex", gap: 22, flex: 1, minHeight: 0 }}>
        <div
          style={{
            flex: bullets.length > 0 ? 1.5 : 1,
            borderRadius: 24,
            backgroundColor: "#17253b",
            border: `3px solid ${AQUA_THEME.border}`,
            boxShadow: AQUA_THEME.shadowLg,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            opacity: windowEnter,
            transform: `translateY(${interpolate(windowEnter, [0, 1], [20, 0])}px)`,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "12px 18px",
              background: "linear-gradient(90deg, #e0f2fe, #fef9c3)",
              borderBottom: `2.5px solid ${AQUA_THEME.border}`,
            }}
          >
            <div style={{ display: "flex", gap: 8 }}>
              <span style={{ width: 13, height: 13, borderRadius: "50%", backgroundColor: AQUA_THEME.coral, border: `1.5px solid ${AQUA_THEME.border}` }} />
              <span style={{ width: 13, height: 13, borderRadius: "50%", backgroundColor: AQUA_THEME.yellow, border: `1.5px solid ${AQUA_THEME.border}` }} />
              <span style={{ width: 13, height: 13, borderRadius: "50%", backgroundColor: AQUA_THEME.mint, border: `1.5px solid ${AQUA_THEME.border}` }} />
            </div>
            <span style={{ fontFamily: MONO_FONT_STACK, fontSize: 16, fontWeight: 700, color: AQUA_THEME.ink }}>
              {vp.citation_label || `aqua_note.${language}`}
            </span>
          </div>

          <div style={{ padding: "14px 0", fontFamily: MONO_FONT_STACK, fontSize: 21, lineHeight: 1.52 }}>
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
                    padding: "3px 20px",
                    backgroundColor: isHighlighted ? "rgba(56, 189, 248, 0.24)" : "transparent",
                    borderLeft: isHighlighted ? `5px solid ${AQUA_THEME.yellow}` : "5px solid transparent",
                    opacity: lineEnter,
                  }}
                >
                  <span
                    style={{
                      width: 38,
                      flexShrink: 0,
                      color: isHighlighted ? AQUA_THEME.yellow : "#64748b",
                      userSelect: "none",
                      fontSize: 17,
                      fontWeight: 700,
                    }}
                  >
                    {idx + 1}
                  </span>
                  <span
                    style={{
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      color: isHighlighted ? "#ffffff" : "#cbd5e1",
                      fontWeight: isHighlighted ? 700 : 400,
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
          <div style={{ flex: 0.78, display: "flex", flexDirection: "column", gap: 14 }}>
            <div
              style={{
                alignSelf: "flex-start",
                padding: "4px 12px",
                borderRadius: 10,
                backgroundColor: AQUA_THEME.yellow,
                border: `2px solid ${AQUA_THEME.border}`,
                fontSize: 17,
                fontWeight: 800,
                color: AQUA_THEME.ink,
              }}
            >
              📌 便利贴笔记
            </div>
            {bullets.map((item, idx) => {
              const itemEnter = spring({
                frame: Math.max(0, frame - 8 - idx * 6),
                fps,
                config: { damping: 16, stiffness: 140 },
              });
              const noteBg = idx % 2 === 0 ? "#fef9c3" : AQUA_THEME.cream;
              return (
                <div
                  key={`${item}-${idx}`}
                  style={{
                    padding: "16px 18px",
                    borderRadius: 18,
                    backgroundColor: noteBg,
                    border: `2.5px solid ${AQUA_THEME.border}`,
                    boxShadow: "4px 4px 0 rgba(27, 53, 84, 0.12)",
                    color: AQUA_THEME.ink,
                    fontSize: 22,
                    fontWeight: 700,
                    lineHeight: 1.4,
                    opacity: itemEnter,
                    transform: `translateX(${interpolate(itemEnter, [0, 1], [18, 0])}px) rotate(${idx % 2 === 0 ? -0.6 : 0.6}deg)`,
                  }}
                >
                  <span style={{ color: AQUA_THEME.sky, fontWeight: 900, marginRight: 8 }}>
                    #{idx + 1}
                  </span>
                  {item}
                </div>
              );
            })}
            {memeEntry ? (
              <div
                style={{
                  marginTop: "auto",
                  alignSelf: "flex-end",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "8px 14px",
                  borderRadius: 18,
                  backgroundColor: AQUA_THEME.cream,
                  border: `2px dashed ${AQUA_THEME.sky}`,
                  transform: `rotate(${(Math.sin(frame * 0.12) * 2).toFixed(2)}deg)`,
                }}
              >
                <div style={{ width: 88, height: 88 }}>
                  <AnimatedSticker asset={memeEntry} alt={memeEntry.title || "Aqua 贴纸"} />
                </div>
                {memeEntry.title ? (
                  <span style={{ fontSize: 16, fontWeight: 800, color: AQUA_THEME.inkSoft }}>
                    {memeEntry.title}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
};
