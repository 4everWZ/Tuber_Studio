import React from "react";
import { Img, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import type { VisualAssetEntry } from "../types";

export interface AnimatedStickerProps {
  asset?: VisualAssetEntry | null;
  alt: string;
  style?: React.CSSProperties;
}

export const AnimatedSticker: React.FC<AnimatedStickerProps> = ({ asset, alt, style }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (!asset?.file) {
    return null;
  }
  const frames = asset.frames && asset.frames.length > 0 ? asset.frames : null;
  let relPath = asset.file;
  if (frames) {
    const stepMs = Math.max(40, asset.frameDurationMs || 110);
    const elapsedMs = (frame / Math.max(1, fps)) * 1000;
    const idx = Math.floor(elapsedMs / stepMs) % frames.length;
    relPath = frames[idx] || asset.file;
  }
  return (
    <Img
      src={staticFile(relPath)}
      alt={alt}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "contain",
        ...style,
      }}
    />
  );
};
