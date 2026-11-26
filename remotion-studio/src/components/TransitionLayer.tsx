import React from "react";
import { Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import type { SceneTransition } from "../types";

export interface TransitionLayerProps {
  transition?: SceneTransition | string;
  durationFrames: number;
  children: React.ReactNode;
}

export const TransitionLayer: React.FC<TransitionLayerProps> = ({
  transition = "fade",
  durationFrames,
  children,
}) => {
  const localFrame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const safeDuration = Math.max(1, durationFrames);
  const transFrames = Math.min(12, Math.max(1, Math.floor(safeDuration / 4)));
  const progress = interpolate(localFrame, [0, transFrames], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });

  let opacity = 1;
  let transform = "none";
  let clipPath: string | undefined;
  let flashOpacity = 0;

  switch (transition) {
    case "cut":
      opacity = 1;
      break;
    case "slide_left": {
      const spr = spring({
        frame: localFrame,
        fps,
        config: { damping: 18, stiffness: 170, mass: 0.7 },
      });
      const x = interpolate(spr, [0, 1], [72, 0]);
      opacity = interpolate(progress, [0, 1], [0.2, 1]);
      transform = `translateX(${x.toFixed(2)}px)`;
      break;
    }
    case "wipe_right": {
      const revealPct = interpolate(progress, [0, 1], [0, 100]);
      clipPath = `inset(0 ${(100 - revealPct).toFixed(2)}% 0 0)`;
      break;
    }
    case "zoom_punch": {
      const spr = spring({
        frame: localFrame,
        fps,
        config: { damping: 14, stiffness: 200, mass: 0.6 },
      });
      const scale = interpolate(spr, [0, 1], [1.1, 1.0]);
      opacity = interpolate(progress, [0, 1], [0.25, 1]);
      transform = `scale(${scale.toFixed(4)})`;
      break;
    }
    case "flash": {
      flashOpacity = interpolate(localFrame, [0, 8], [0.7, 0], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });
      opacity = 1;
      break;
    }
    case "fade":
    default: {
      opacity = progress;
      break;
    }
  }

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        opacity,
        transform,
        clipPath,
      }}
    >
      {children}
      {flashOpacity > 0.01 ? (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "radial-gradient(circle, rgba(255,255,255,0.95), rgba(186,230,253,0.85))",
            opacity: flashOpacity,
            pointerEvents: "none",
            zIndex: 20,
          }}
        />
      ) : null}
    </div>
  );
};
