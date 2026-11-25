import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { AQUA_THEME, CJK_FONT_STACK, type AquaCueProp } from "../types";

export interface SubtitleBarProps {
  cue: AquaCueProp;
  includeAvatar?: boolean;
}

interface TextSegment {
  text: string;
  highlighted: boolean;
}

function splitByEmphasis(text: string, emphasis: string[] = []): TextSegment[] {
  const cleanKeywords = emphasis
    .map((kw) => kw.trim())
    .filter((kw) => kw.length > 0 && text.includes(kw))
    .sort((a, b) => b.length - a.length);
  if (cleanKeywords.length === 0) {
    return [{ text, highlighted: false }];
  }
  const segments: TextSegment[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    let matchedKw = "";
    let matchedIdx = text.length;
    for (const kw of cleanKeywords) {
      const idx = text.indexOf(kw, cursor);
      if (idx !== -1 && idx < matchedIdx) {
        matchedIdx = idx;
        matchedKw = kw;
      }
    }
    if (!matchedKw) {
      segments.push({ text: text.slice(cursor), highlighted: false });
      break;
    }
    if (matchedIdx > cursor) {
      segments.push({ text: text.slice(cursor, matchedIdx), highlighted: false });
    }
    segments.push({ text: matchedKw, highlighted: true });
    cursor = matchedIdx + matchedKw.length;
  }
  return segments;
}

export const SubtitleBar: React.FC<SubtitleBarProps> = ({ cue, includeAvatar = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = Math.max(0, frame - cue.start_frame);
  const progress = Math.min(1, localFrame / Math.max(1, cue.duration_frames));
  const enter = spring({
    frame: localFrame,
    fps,
    config: { damping: 16, stiffness: 190, mass: 0.65 },
  });
  const translateY = interpolate(enter, [0, 1], [16, 0]);
  const opacity = interpolate(enter, [0, 1], [0, 1]);
  const segments = splitByEmphasis(cue.text, cue.emphasis);

  return (
    <div
      style={{
        position: "absolute",
        bottom: 28,
        left: 44,
        right: includeAvatar ? 298 : 44,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
        opacity,
        transform: `translateY(${translateY}px)`,
        zIndex: 7,
      }}
    >
      <div
        style={{
          position: "relative",
          maxWidth: "96%",
          backgroundColor: "rgba(255, 253, 247, 0.95)",
          border: `3px solid ${AQUA_THEME.border}`,
          boxShadow: "0 8px 0 rgba(27, 53, 84, 0.16)",
          borderRadius: 22,
          padding: "16px 30px 20px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            fontFamily: CJK_FONT_STACK,
            fontSize: 34,
            lineHeight: 1.42,
            fontWeight: 800,
            color: AQUA_THEME.ink,
            textAlign: "center",
            letterSpacing: 0.3,
          }}
        >
          {segments.map((seg, idx) =>
            seg.highlighted ? (
              <span
                key={`${seg.text}-${idx}`}
                style={{
                  display: "inline-block",
                  margin: "0 4px",
                  padding: "1px 10px",
                  borderRadius: 10,
                  backgroundColor: idx % 2 === 0 ? AQUA_THEME.yellow : AQUA_THEME.coralSoft,
                  border: `2px solid ${AQUA_THEME.border}`,
                  color: idx % 2 === 0 ? AQUA_THEME.ink : "#be123c",
                  transform: `scale(${interpolate(enter, [0, 1], [0.95, 1.07]).toFixed(3)}) rotate(-1.2deg)`,
                }}
              >
                {seg.text}
              </span>
            ) : (
              <span key={`${seg.text}-${idx}`}>{seg.text}</span>
            )
          )}
        </div>
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            height: 5,
            backgroundColor: "rgba(27, 53, 84, 0.08)",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${Math.round(progress * 100)}%`,
              background: `linear-gradient(90deg, ${AQUA_THEME.sky}, ${AQUA_THEME.mint}, ${AQUA_THEME.amber})`,
            }}
          />
        </div>
      </div>
    </div>
  );
};
