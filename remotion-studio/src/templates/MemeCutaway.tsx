import React from "react";
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { AnimatedSticker } from "../components/AnimatedSticker";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

const REACTION_ICON: Record<string, string> = {
  cheerful: "😆",
  curious: "🧐",
  proud: "😎",
  thinking: "🤔",
  surprised: "🤯",
  calm: "😌",
  idle: "🐋",
};

export const MemeCutaway: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  memeAssets,
  avatarLayout = "corner_float",
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};
  const bullets = vp.bullets ?? [];

  const pop = spring({ frame, fps, config: { damping: 11, stiffness: 170 } });
  const wobble = Math.sin(frame * 0.16) * 2.4;
  const icon = REACTION_ICON[scene.avatar_cue || "idle"] ?? "🐋";
  const memeEntry =
    (vp.meme_asset_id && memeAssets?.[vp.meme_asset_id]) ||
    (memeAssets ? Object.values(memeAssets)[0] : undefined);

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    Math.max(1, bullets.length),
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  const effectiveLayout = includeAvatar ? avatarLayout : "hidden";
  const rightClearance = getAvatarRightClearance(effectiveLayout);

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        padding: `18px ${rightClearance}px 128px 48px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div
        style={{
          width: "100%",
          borderRadius: 28,
          padding: "38px 44px",
          backgroundColor: palette.surface,
          border: `1.5px solid ${palette.border}`,
          boxShadow: palette.cardShadow,
          backdropFilter: "blur(12px)",
          display: "flex",
          alignItems: "center",
          gap: 36,
          transform: `scale(${interpolate(pop, [0, 1], [0.9, 1]).toFixed(3)})`,
          opacity: pop,
        }}
      >
        <div
          style={{
            width: 224,
            height: 224,
            flexShrink: 0,
            borderRadius: 26,
            padding: 16,
            background: `linear-gradient(145deg, ${palette.accentSoft}, ${palette.secondarySoft})`,
            border: `1.5px solid ${palette.borderStrong}`,
            boxShadow: palette.cardShadow,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            transform: `rotate(${wobble.toFixed(2)}deg)`,
          }}
        >
          {memeEntry ? (
            <div style={{ width: 174, height: 174 }}>
              <AnimatedSticker asset={memeEntry} alt={memeEntry.title || vp.meme_asset_id || "鲸鱼表情包"} />
            </div>
          ) : (
            <span style={{ fontSize: 92 }}>{icon}</span>
          )}
          {memeEntry?.title ? (
            <span
              style={{
                marginTop: 4,
                padding: "2px 10px",
                borderRadius: 999,
                backgroundColor: palette.highlightSoft,
                border: `1px solid ${palette.border}`,
                fontSize: 13,
                fontWeight: 800,
                color: palette.ink,
              }}
            >
              {memeEntry.title}
            </span>
          ) : null}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 14, flex: 1 }}>
          <span
            style={{
              alignSelf: "flex-start",
              padding: "5px 15px",
              borderRadius: 999,
              backgroundColor: palette.dangerSoft,
              border: `1px solid ${palette.danger}`,
              color: palette.danger,
              fontSize: 16,
              fontWeight: 800,
            }}
          >
            REACTION CUTAWAY · 槽点现场
          </span>
          <div style={{ fontSize: 48, fontWeight: 900, color: palette.ink, lineHeight: 1.18 }}>
            {scene.heading}
          </div>
          {vp.subtitle ? (
            <div style={{ fontSize: 26, fontWeight: 600, color: palette.inkSoft, lineHeight: 1.42 }}>
              {vp.subtitle}
            </div>
          ) : null}

          {bullets.length > 0 ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 4 }}>
              {bullets.map((b, i) => {
                const unlocked = i <= revealStep;
                const isCurrent = i === revealStep;
                const tagSpring = spring({
                  frame: unlocked ? Math.max(0, frame - i * 4) : 0,
                  fps,
                  config: { damping: 14, stiffness: 170 },
                });
                return (
                  <span
                    key={`${b}-${i}`}
                    style={{
                      padding: "8px 16px",
                      borderRadius: 12,
                      backgroundColor: isCurrent ? palette.accentSoft : palette.surfaceMuted,
                      border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                      color: isCurrent ? palette.ink : palette.inkSoft,
                      fontSize: 20,
                      fontWeight: isCurrent ? 800 : 700,
                      opacity: unlocked ? interpolate(tagSpring, [0, 1], [0, 1]) : 0.25,
                      transform: `scale(${isCurrent ? 1.04 : 1})`,
                    }}
                  >
                    #{b}
                  </span>
                );
              })}
            </div>
          ) : null}

          {vp.uploaded_image_file ? (
            <div
              style={{
                marginTop: 6,
                borderRadius: 16,
                padding: 8,
                backgroundColor: palette.surfaceElevated,
                border: `1px solid ${palette.border}`,
                display: "flex",
                justifyContent: "center",
                alignItems: "center",
                maxHeight: 220,
                overflow: "hidden",
              }}
            >
              <Img
                src={staticFile(vp.uploaded_image_file)}
                alt={scene.heading}
                style={{ width: "100%", maxHeight: 198, objectFit: "contain", borderRadius: 10 }}
              />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};
