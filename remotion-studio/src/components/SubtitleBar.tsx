import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, type AquaCueProp, type AvatarLayout } from "../types";

export interface SubtitleBarProps {
  cue: AquaCueProp;
  includeAvatar?: boolean;
  avatarLayout?: AvatarLayout;
  theme?: string;
}

interface TextSegment {
  text: string;
  highlighted: boolean;
  startChar: number;
}

function splitByEmphasis(text: string, emphasis: string[] = []): TextSegment[] {
  const cleanKeywords = emphasis
    .map((kw) => kw.trim())
    .filter((kw) => kw.length > 0 && text.includes(kw))
    .sort((a, b) => b.length - a.length);

  if (cleanKeywords.length === 0) {
    return [{ text, highlighted: false, startChar: 0 }];
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
      segments.push({ text: text.slice(cursor), highlighted: false, startChar: cursor });
      break;
    }
    if (matchedIdx > cursor) {
      segments.push({
        text: text.slice(cursor, matchedIdx),
        highlighted: false,
        startChar: cursor,
      });
    }
    segments.push({ text: matchedKw, highlighted: true, startChar: matchedIdx });
    cursor = matchedIdx + matchedKw.length;
  }
  return segments;
}

export const SubtitleBar: React.FC<SubtitleBarProps> = ({
  cue,
  includeAvatar = true,
  avatarLayout,
  theme,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(theme);

  const effectiveLayout: AvatarLayout =
    avatarLayout ?? (includeAvatar ? "corner_float" : "hidden");
  const rightClearance = getAvatarRightClearance(effectiveLayout);

  const localFrame = Math.max(0, frame - cue.start_frame);
  const progress = Math.min(1, localFrame / Math.max(1, cue.duration_frames));
  const enter = spring({
    frame: localFrame,
    fps,
    config: { damping: 18, stiffness: 210, mass: 0.6 },
  });

  const translateY = interpolate(enter, [0, 1], [12, 0]);
  const opacity = interpolate(enter, [0, 1], [0, 1]);
  const segments = splitByEmphasis(cue.text, cue.emphasis);
  const totalChars = Math.max(1, cue.text.length);
  const spokenChars = Math.ceil(progress * totalChars);

  return (
    <div
      style={{
        position: "absolute",
        bottom: 24,
        left: 56,
        right: rightClearance,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
        opacity,
        transform: `translateY(${translateY.toFixed(1)}px)`,
        zIndex: 7,
      }}
    >
      <div
        style={{
          position: "relative",
          maxWidth: "94%",
          backgroundColor: palette.subtitleBg,
          border: `1px solid ${palette.subtitleBorder}`,
          backdropFilter: "blur(12px)",
          boxShadow: palette.isDark
            ? "0 12px 32px rgba(0, 0, 0, 0.48)"
            : "0 12px 30px rgba(15, 34, 61, 0.12)",
          borderRadius: 16,
          padding: "13px 26px 15px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            fontFamily: CJK_FONT_STACK,
            fontSize: 31,
            lineHeight: 1.44,
            fontWeight: 700,
            color: palette.subtitleInk,
            textAlign: "center",
            letterSpacing: 0.25,
            textShadow: palette.isDark ? "0 1px 6px rgba(0, 0, 0, 0.65)" : "none",
          }}
        >
          {segments.map((seg, idx) => {
            if (seg.highlighted) {
              const reached = spokenChars >= seg.startChar;
              const pillFrame = Math.max(
                0,
                localFrame - Math.floor((seg.startChar / totalChars) * cue.duration_frames)
              );
              const pillPop = reached
                ? spring({
                    frame: pillFrame,
                    fps,
                    config: { damping: 12, stiffness: 220, mass: 0.5 },
                  })
                : 0;
              const scale = reached ? interpolate(pillPop, [0, 1], [0.94, 1.05]) : 1;
              return (
                <span
                  key={`${seg.text}-${idx}`}
                  style={{
                    display: "inline-block",
                    margin: "0 5px",
                    padding: "1px 10px",
                    borderRadius: 8,
                    backgroundColor: palette.isDark
                      ? idx % 2 === 0
                        ? "rgba(56, 189, 248, 0.24)"
                        : "rgba(251, 191, 36, 0.24)"
                      : idx % 2 === 0
                        ? "rgba(14, 165, 233, 0.16)"
                        : "rgba(253, 224, 71, 0.48)",
                    border: `1px solid ${
                      idx % 2 === 0 ? palette.accent : palette.highlight
                    }`,
                    color: palette.isDark
                      ? idx % 2 === 0
                        ? "#7dd3fc"
                        : "#fde68a"
                      : palette.ink,
                    fontWeight: 800,
                    transform: `scale(${scale.toFixed(3)})`,
                  }}
                >
                  {seg.text}
                </span>
              );
            }

            const chars = Array.from(seg.text);
            const relSpoken = Math.max(0, spokenChars - seg.startChar);
            const spokenPart = chars.slice(0, relSpoken).join("");
            const upcomingPart = chars.slice(relSpoken).join("");

            return (
              <span key={`${seg.text}-${idx}`}>
                {spokenPart ? (
                  <span style={{ color: palette.subtitleInk }}>{spokenPart}</span>
                ) : null}
                {upcomingPart ? (
                  <span style={{ opacity: 0.62 }}>{upcomingPart}</span>
                ) : null}
              </span>
            );
          })}
        </div>

        {/* Hairline progress accent */}
        <div
          style={{
            position: "absolute",
            left: 16,
            right: 16,
            bottom: 0,
            height: 2.5,
            borderRadius: 999,
            backgroundColor: palette.isDark
              ? "rgba(255, 255, 255, 0.08)"
              : "rgba(15, 34, 61, 0.08)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${Math.round(progress * 100)}%`,
              background: `linear-gradient(90deg, ${palette.accent}, ${palette.secondary})`,
            }}
          />
        </div>
      </div>
    </div>
  );
};
