import React from "react";
import { Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import type { AquaCueProp, CameraMotion } from "../types";

export interface CameraRigProps {
  motion?: CameraMotion | string;
  durationFrames: number;
  activeCue?: AquaCueProp;
  globalFrame: number;
  focusTarget?: { x: number; y: number };
  children: React.ReactNode;
}

export const CameraRig: React.FC<CameraRigProps> = ({
  motion = "static",
  durationFrames,
  activeCue,
  globalFrame,
  focusTarget,
  children,
}) => {
  const localFrame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const safeDuration = Math.max(1, durationFrames);
  const t = Math.min(1, Math.max(0, localFrame / safeDuration));
  const eased = Easing.inOut(Easing.cubic)(t);

  let scale = 1;
  let translateX = 0;
  let translateY = 0;

  switch (motion) {
    case "push_in":
      scale = interpolate(eased, [0, 1], [1.0, 1.045]);
      break;
    case "pull_out":
      scale = interpolate(eased, [0, 1], [1.055, 1.0]);
      break;
    case "pan_right":
      scale = 1.035;
      translateX = interpolate(eased, [0, 1], [18, -18]);
      break;
    case "focus_target": {
      const targetX = focusTarget ? (0.5 - focusTarget.x) * 48 : -10;
      const targetY = focusTarget ? (0.5 - focusTarget.y) * 32 : -6;
      scale = interpolate(eased, [0, 1], [1.0, 1.038]);
      translateX = interpolate(eased, [0, 1], [0, targetX]);
      translateY = interpolate(eased, [0, 1], [0, targetY]);
      break;
    }
    case "static":
    default:
      scale = 1.0;
      break;
  }

  let beatPunch = 0;
  if (activeCue) {
    const cueLocal = globalFrame - activeCue.start_frame;
    if (cueLocal >= 0 && cueLocal <= 14) {
      const hit = spring({
        frame: cueLocal,
        fps,
        config: { damping: 14, stiffness: 220, mass: 0.5 },
      });
      const decay = interpolate(cueLocal, [0, 14], [1, 0], { extrapolateRight: "clamp" });
      beatPunch = hit * decay * 0.011;
    }
  }

  const finalScale = scale + beatPunch;

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        transform: `translate3d(${translateX.toFixed(2)}px, ${translateY.toFixed(2)}px, 0) scale(${finalScale.toFixed(4)})`,
        transformOrigin: "center center",
        willChange: "transform",
      }}
    >
      {children}
    </div>
  );
};
