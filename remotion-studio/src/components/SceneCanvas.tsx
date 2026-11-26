import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { getThemePalette } from "../theme";
import { CJK_FONT_STACK } from "../types";

export interface SceneCanvasProps {
  theme?: string;
  children: React.ReactNode;
}

const ORB_SPECS = [
  { x: 14, y: 18, size: 420, speed: 0.021, phase: 0.4, ampX: 28, ampY: 18 },
  { x: 84, y: 22, size: 360, speed: 0.017, phase: 1.8, ampX: -24, ampY: 22 },
  { x: 72, y: 82, size: 440, speed: 0.015, phase: 3.1, ampX: 20, ampY: -20 },
  { x: 24, y: 78, size: 300, speed: 0.025, phase: 4.5, ampX: -18, ampY: -16 },
];

const PARTICLE_SPECS = [
  { left: "11%", top: "28%", size: 5, speed: 0.04, phase: 0.2 },
  { left: "26%", top: "64%", size: 4, speed: 0.033, phase: 1.3 },
  { left: "43%", top: "19%", size: 6, speed: 0.028, phase: 2.1 },
  { left: "58%", top: "73%", size: 4, speed: 0.036, phase: 3.0 },
  { left: "74%", top: "32%", size: 5, speed: 0.031, phase: 4.2 },
  { left: "88%", top: "59%", size: 6, speed: 0.024, phase: 5.1 },
  { left: "34%", top: "86%", size: 4, speed: 0.038, phase: 2.7 },
  { left: "66%", top: "14%", size: 5, speed: 0.029, phase: 0.9 },
];

export const SceneCanvas: React.FC<SceneCanvasProps> = ({ theme, children }) => {
  const frame = useCurrentFrame();
  const palette = getThemePalette(theme);

  const gridShiftX = (frame * 0.22) % 56;
  const gridShiftY = (frame * 0.14) % 56;

  let backgroundImage: string;
  let backgroundSize = "100% 100%";
  let backgroundPosition = "0 0";

  if (palette.id === "dark_tech") {
    backgroundImage =
      "radial-gradient(circle at 18% 20%, rgba(56, 189, 248, 0.16), transparent 46%), " +
      "radial-gradient(circle at 82% 76%, rgba(45, 212, 191, 0.12), transparent 44%), " +
      "linear-gradient(rgba(56, 189, 248, 0.065) 1px, transparent 1px), " +
      "linear-gradient(90deg, rgba(56, 189, 248, 0.065) 1px, transparent 1px), " +
      "linear-gradient(155deg, #050a14 0%, #0a1529 52%, #0e1c36 100%)";
    backgroundSize = "100% 100%, 100% 100%, 56px 56px, 56px 56px, 100% 100%";
    backgroundPosition = `0 0, 0 0, ${gridShiftX}px ${gridShiftY}px, ${gridShiftX}px ${gridShiftY}px, 0 0`;
  } else if (palette.id === "cinema") {
    const spotlightX = 50 + Math.sin(frame * 0.012) * 8;
    const spotlightY = 38 + Math.cos(frame * 0.01) * 5;
    backgroundImage =
      `radial-gradient(ellipse 68% 58% at ${spotlightX.toFixed(1)}% ${spotlightY.toFixed(1)}%, rgba(245, 158, 11, 0.16), rgba(56, 189, 248, 0.06) 46%, transparent 74%), ` +
      "radial-gradient(circle at 50% 50%, transparent 45%, rgba(0, 0, 0, 0.72) 100%), " +
      "linear-gradient(160deg, #07080d 0%, #10121b 54%, #090a10 100%)";
  } else {
    backgroundImage =
      "radial-gradient(circle at 15% 18%, rgba(14, 165, 233, 0.12), transparent 44%), " +
      "radial-gradient(circle at 85% 22%, rgba(20, 184, 166, 0.10), transparent 42%), " +
      "radial-gradient(circle at 74% 84%, rgba(245, 158, 11, 0.10), transparent 44%), " +
      "radial-gradient(rgba(15, 34, 61, 0.055) 1.2px, transparent 1.2px), " +
      "linear-gradient(155deg, #ecf7ff 0%, #f8fcff 54%, #fffaf2 100%)";
    backgroundSize = "100% 100%, 100% 100%, 100% 100%, 30px 30px, 100% 100%";
    backgroundPosition = `0 0, 0 0, 0 0, ${(gridShiftX * 0.35).toFixed(1)}px ${(gridShiftY * 0.35).toFixed(1)}px, 0 0`;
  }

  const orbColors =
    palette.id === "dark_tech"
      ? [
          "rgba(56, 189, 248, 0.14)",
          "rgba(45, 212, 191, 0.11)",
          "rgba(99, 102, 241, 0.12)",
          "rgba(14, 165, 233, 0.10)",
        ]
      : palette.id === "cinema"
        ? [
            "rgba(245, 158, 11, 0.10)",
            "rgba(251, 113, 133, 0.08)",
            "rgba(56, 189, 248, 0.07)",
            "rgba(253, 224, 71, 0.06)",
          ]
        : [
            "rgba(56, 189, 248, 0.14)",
            "rgba(20, 184, 166, 0.11)",
            "rgba(251, 191, 36, 0.12)",
            "rgba(251, 113, 133, 0.09)",
          ];

  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.bgBase,
        backgroundImage,
        backgroundSize,
        backgroundPosition,
        color: palette.ink,
        fontFamily: CJK_FONT_STACK,
        overflow: "hidden",
      }}
    >
      {ORB_SPECS.map((orb, idx) => {
        const dx = Math.sin(frame * orb.speed + orb.phase) * orb.ampX;
        const dy = Math.cos(frame * orb.speed * 0.85 + orb.phase) * orb.ampY;
        return (
          <div
            key={idx}
            style={{
              position: "absolute",
              left: `calc(${orb.x}% - ${orb.size / 2}px + ${dx.toFixed(1)}px)`,
              top: `calc(${orb.y}% - ${orb.size / 2}px + ${dy.toFixed(1)}px)`,
              width: orb.size,
              height: orb.size,
              borderRadius: "50%",
              background: `radial-gradient(circle, ${orbColors[idx]}, transparent 70%)`,
              pointerEvents: "none",
            }}
          />
        );
      })}

      {PARTICLE_SPECS.map((pt, idx) => {
        const floatY = Math.sin(frame * pt.speed + pt.phase) * 12;
        const pulse = 0.45 + 0.45 * Math.sin(frame * pt.speed * 1.4 + pt.phase);
        const color =
          palette.id === "cinema"
            ? "rgba(251, 191, 36, 0.55)"
            : palette.id === "dark_tech"
              ? "rgba(56, 189, 248, 0.65)"
              : "rgba(14, 165, 233, 0.28)";
        return (
          <div
            key={idx}
            style={{
              position: "absolute",
              left: pt.left,
              top: pt.top,
              width: pt.size,
              height: pt.size,
              borderRadius: "50%",
              backgroundColor: color,
              opacity: pulse,
              transform: `translateY(${floatY.toFixed(1)}px)`,
              boxShadow: palette.isDark ? `0 0 12px ${color}` : "none",
              pointerEvents: "none",
            }}
          />
        );
      })}

      {palette.id === "cinema" ? (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background:
              "radial-gradient(circle at 50% 48%, transparent 55%, rgba(0, 0, 0, 0.48) 100%)",
            pointerEvents: "none",
          }}
        />
      ) : null}

      {children}
    </AbsoluteFill>
  );
};
