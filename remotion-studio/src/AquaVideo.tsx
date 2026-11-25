import React from "react";
import { AbsoluteFill, Audio, Sequence, interpolate, staticFile, useCurrentFrame } from "remotion";
import { AquaBadge } from "./components/AquaBadge";
import { SubtitleBar } from "./components/SubtitleBar";
import { ChartCompare } from "./templates/ChartCompare";
import { CodeWalkthrough } from "./templates/CodeWalkthrough";
import { MemeCutaway } from "./templates/MemeCutaway";
import { QuoteSpotlight } from "./templates/QuoteSpotlight";
import { TimelineSummary } from "./templates/TimelineSummary";
import { TitleCard } from "./templates/TitleCard";
import {
  AQUA_THEME,
  CJK_FONT_STACK,
  type AquaCueProp,
  type AquaSceneProp,
  type AquaVideoProps,
  type VisualAssetEntry,
} from "./types";

const SceneRenderer: React.FC<{
  scene: AquaSceneProp;
  activeCue?: AquaCueProp;
  includeAvatar: boolean;
  memeAssets?: Record<string, VisualAssetEntry>;
}> = ({ scene, activeCue, includeAvatar, memeAssets }) => {
  const localFrame = useCurrentFrame();
  const fadeFrames = Math.min(8, Math.max(1, Math.floor(scene.duration_frames / 4)));
  const fadeIn = interpolate(localFrame, [0, fadeFrames], [0, 1], { extrapolateRight: "clamp" });
  const fadeOut = interpolate(
    localFrame,
    [Math.max(0, scene.duration_frames - fadeFrames), scene.duration_frames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const opacity = scene.duration_frames > 16 ? Math.min(fadeIn, fadeOut) : fadeIn;

  let body: React.ReactNode;
  switch (scene.template) {
    case "TitleCard":
      body = <TitleCard scene={scene} memeAssets={memeAssets} includeAvatar={includeAvatar} />;
      break;
    case "CodeWalkthrough":
      body = <CodeWalkthrough scene={scene} activeCue={activeCue} memeAssets={memeAssets} includeAvatar={includeAvatar} />;
      break;
    case "ChartCompare":
      body = <ChartCompare scene={scene} includeAvatar={includeAvatar} />;
      break;
    case "MemeCutaway":
      body = <MemeCutaway scene={scene} memeAssets={memeAssets} includeAvatar={includeAvatar} />;
      break;
    case "TimelineSummary":
      body = <TimelineSummary scene={scene} includeAvatar={includeAvatar} />;
      break;
    case "QuoteSpotlight":
    default:
      body = <QuoteSpotlight scene={scene} includeAvatar={includeAvatar} />;
      break;
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", opacity, width: "100%", height: "100%" }}>
      {body}
    </div>
  );
};

export const AquaVideo: React.FC<AquaVideoProps> = ({
  title,
  audioFile,
  includeAvatar = true,
  bgmEnabled = true,
  bgmFile,
  transitionSfxFile,
  avatarAssets,
  memeAssets,
  scenes = [],
  cues = [],
  totalFrames = 300,
}) => {
  const frame = useCurrentFrame();
  const activeCue = cues.find(
    (c) => frame >= c.start_frame && frame < c.start_frame + c.duration_frames
  );
  const activeSceneIndex = Math.max(
    0,
    scenes.findIndex((s) => frame >= s.start_frame && frame < s.start_frame + s.duration_frames)
  );
  const activeScene = scenes[activeSceneIndex];
  const activeAvatarCue = activeCue?.avatar_cue || activeScene?.avatar_cue || "idle";
  const overallProgress = Math.min(1, frame / Math.max(1, totalFrames));
  const driftY = Math.sin(frame * 0.03) * 14;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#eef8ff",
        backgroundImage:
          "radial-gradient(circle at 14% 16%, rgba(14, 165, 233, 0.16), transparent 44%), " +
          "radial-gradient(circle at 86% 24%, rgba(250, 204, 21, 0.18), transparent 38%), " +
          "radial-gradient(circle at 75% 84%, rgba(251, 113, 133, 0.14), transparent 40%), " +
          "radial-gradient(rgba(27, 53, 84, 0.08) 1.5px, transparent 1.5px), " +
          "linear-gradient(155deg, #e8f5ff 0%, #f8fcff 52%, #fffaf0 100%)",
        backgroundSize: "100% 100%, 100% 100%, 100% 100%, 28px 28px, 100% 100%",
        color: AQUA_THEME.ink,
        fontFamily: CJK_FONT_STACK,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: -70 + driftY,
          right: 120,
          width: 300,
          height: 300,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(56, 189, 248, 0.18), transparent 70%)",
          pointerEvents: "none",
        }}
      />

      {audioFile ? <Audio src={staticFile(audioFile)} volume={1.0} /> : null}
      {bgmEnabled && bgmFile ? <Audio src={staticFile(bgmFile)} volume={0.12} loop /> : null}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "28px 46px 14px",
          zIndex: 4,
        }}
      >
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 14,
            padding: "10px 20px",
            borderRadius: 999,
            backgroundColor: "rgba(255, 253, 247, 0.9)",
            border: `2px solid ${AQUA_THEME.border}`,
            boxShadow: "4px 4px 0 rgba(27, 53, 84, 0.1)",
          }}
        >
          <span style={{ fontSize: 20 }}>🐋</span>
          <span style={{ fontSize: 21, fontWeight: 800, color: AQUA_THEME.ink }}>{title}</span>
          {scenes.length > 1 ? (
            <div style={{ display: "flex", gap: 6, alignItems: "center", marginLeft: 8 }}>
              {scenes.map((scene, idx) => (
                <div
                  key={scene.scene_id}
                  style={{
                    height: 8,
                    width: idx === activeSceneIndex ? 32 : 12,
                    borderRadius: 999,
                    backgroundColor:
                      idx === activeSceneIndex
                        ? AQUA_THEME.sky
                        : idx < activeSceneIndex
                          ? AQUA_THEME.mint
                          : "rgba(27, 53, 84, 0.18)",
                  }}
                />
              ))}
              <span style={{ fontSize: 15, fontWeight: 700, color: AQUA_THEME.inkSoft, marginLeft: 4 }}>
                {activeSceneIndex + 1}/{scenes.length}
              </span>
            </div>
          ) : null}
        </div>

        <div
          style={{
            padding: "8px 16px",
            borderRadius: 999,
            backgroundColor: AQUA_THEME.yellow,
            border: `2px solid ${AQUA_THEME.border}`,
            fontSize: 16,
            fontWeight: 800,
            color: AQUA_THEME.ink,
            boxShadow: "4px 4px 0 rgba(27, 53, 84, 0.1)",
          }}
        >
          浅海手账 · 杂谈桌
        </div>
      </div>

      <div style={{ position: "relative", flex: 1, display: "flex", padding: "6px 40px 0" }}>
        {scenes.map((scene, idx) => (
          <Sequence
            key={scene.scene_id}
            from={scene.start_frame}
            durationInFrames={Math.max(1, scene.duration_frames)}
            style={{ display: "flex", width: "100%", height: "100%" }}
          >
            {bgmEnabled && transitionSfxFile && idx > 0 ? (
              <Audio src={staticFile(transitionSfxFile)} volume={0.2} />
            ) : null}
            <SceneRenderer
              scene={scene}
              activeCue={activeCue}
              includeAvatar={includeAvatar}
              memeAssets={memeAssets}
            />
          </Sequence>
        ))}
      </div>

      {activeCue ? <SubtitleBar cue={activeCue} includeAvatar={includeAvatar} /> : null}
      <AquaBadge
        cue={activeAvatarCue}
        speaking={Boolean(activeCue)}
        includeAvatar={includeAvatar}
        avatarAssets={avatarAssets}
      />

      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 6,
          backgroundColor: "rgba(27, 53, 84, 0.08)",
          zIndex: 9,
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${Math.round(overallProgress * 100)}%`,
            background: `linear-gradient(90deg, ${AQUA_THEME.sky}, ${AQUA_THEME.mint}, ${AQUA_THEME.yellow})`,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};
