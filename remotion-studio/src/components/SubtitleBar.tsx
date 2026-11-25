import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CJK_FONT_STACK, type AquaCueProp } from "../types";

export interface SubtitleBarProps {
  cue: AquaCueProp;
}

export const SubtitleBar: React.FC<SubtitleBarProps> = ({ cue }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = Math.max(0, frame - cue.start_frame);
  const progress = Math.min(1, localFrame / Math.max(1, cue.duration_frames));
  const enter = spring({
    frame: localFrame,
    fps,
    config: { damping: 18, stiffness: 180, mass: 0.7 },
  });
  const translateY = interpolate(enter, [0, 1], [18, 0]);
  const opacity = interpolate(enter, [0, 1], [0, 1]);

  return (
    <div
      style={{
        position: "absolute",
        bottom: 44,
        left: 72,
        right: 72,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
        opacity,
        transform: `translateY(${translateY}px)`,
      }}
    >
      <div
        style={{
          position: "relative",
          maxWidth: "88%",
          backgroundColor: "rgba(7, 13, 28, 0.88)",
          border: "1px solid rgba(56, 189, 248, 0.35)",
          boxShadow: "0 16px 40px rgba(2, 6, 23, 0.65)",
          borderRadius: 18,
          padding: "18px 34px 22px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            fontFamily: CJK_FONT_STACK,
            fontSize: 34,
            lineHeight: 1.4,
            fontWeight: 600,
            color: "#f8fafc",
            textAlign: "center",
            letterSpacing: 0.3,
            textShadow: "0 2px 10px rgba(0, 0, 0, 0.55)",
          }}
        >
          {cue.text}
        </div>
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            height: 4,
            backgroundColor: "rgba(148, 163, 184, 0.18)",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${Math.round(progress * 100)}%`,
              background: "linear-gradient(90deg, #38bdf8, #22d3ee, #34d399)",
            }}
          />
        </div>
      </div>
    </div>
  );
};
