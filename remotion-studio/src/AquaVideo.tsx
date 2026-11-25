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
import { CJK_FONT_STACK, type AquaCueProp, type AquaSceneProp, type AquaVideoProps } from "./types";

const SceneRenderer: React.FC<{ scene: AquaSceneProp; activeCue?: AquaCueProp }> = ({
  scene,
  activeCue,
}) => {
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
      body = <TitleCard scene={scene} />;
      break;
    case "CodeWalkthrough":
      body = <CodeWalkthrough scene={scene} activeCue={activeCue} />;
      break;
    case "ChartCompare":
      body = <ChartCompare scene={scene} />;
      break;
    case "MemeCutaway":
      body = <MemeCutaway scene={scene} />;
      break;
    case "TimelineSummary":
      body = <TimelineSummary scene={scene} />;
      break;
    case "QuoteSpotlight":
    default:
      body = <QuoteSpotlight scene={scene} />;
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
  includeAvatar = false,
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
  const glowShift = Math.sin(frame * 0.025) * 30;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#060d1f",
        backgroundImage:
          "radial-gradient(circle at 18% 18%, rgba(56, 189, 248, 0.20), transparent 46%), " +
          "radial-gradient(circle at 82% 76%, rgba(129, 140, 248, 0.18), transparent 44%), " +
          "linear-gradient(160deg, #071127 0%, #0f172a 55%, #091326 100%)",
        color: "#f8fafc",
        fontFamily: CJK_FONT_STACK,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: -120 + glowShift,
          right: -80,
          width: 480,
          height: 480,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(34, 211, 238, 0.16), transparent 70%)",
          pointerEvents: "none",
        }}
      />

      {audioFile ? <Audio src={staticFile(audioFile)} /> : null}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "36px 56px 20px",
          zIndex: 2,
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 24, fontWeight: 700, color: "#e2e8f0", opacity: 0.92 }}>
            {title}
          </div>
          {scenes.length > 1 ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {scenes.map((scene, idx) => (
                <div
                  key={scene.scene_id}
                  style={{
                    height: 6,
                    width: idx === activeSceneIndex ? 44 : 18,
                    borderRadius: 999,
                    backgroundColor:
                      idx === activeSceneIndex
                        ? "#38bdf8"
                        : idx < activeSceneIndex
                          ? "rgba(56, 189, 248, 0.55)"
                          : "rgba(148, 163, 184, 0.28)",
                  }}
                />
              ))}
              <span style={{ fontSize: 15, color: "#94a3b8", marginLeft: 8 }}>
                {activeSceneIndex + 1} / {scenes.length}
              </span>
            </div>
          ) : null}
        </div>

        <AquaBadge cue={activeAvatarCue} speaking={Boolean(activeCue)} includeAvatar={includeAvatar} />
      </div>

      <div style={{ position: "relative", flex: 1, display: "flex", padding: "12px 40px 0" }}>
        {scenes.map((scene) => (
          <Sequence
            key={scene.scene_id}
            from={scene.start_frame}
            durationInFrames={Math.max(1, scene.duration_frames)}
            style={{ display: "flex", width: "100%", height: "100%" }}
          >
            <SceneRenderer scene={scene} activeCue={activeCue} />
          </Sequence>
        ))}
      </div>

      {activeCue ? <SubtitleBar cue={activeCue} /> : null}

      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 4,
          backgroundColor: "rgba(148, 163, 184, 0.15)",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${Math.round(overallProgress * 100)}%`,
            background: "linear-gradient(90deg, #38bdf8, #22d3ee, #818cf8)",
          }}
        />
      </div>
    </AbsoluteFill>
  );
};
