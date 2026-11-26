import React from "react";
import { Audio, Sequence, staticFile, useCurrentFrame } from "remotion";
import { AquaOverlay } from "./components/AquaOverlay";
import { CameraRig } from "./components/CameraRig";
import { SceneCanvas } from "./components/SceneCanvas";
import { SubtitleBar } from "./components/SubtitleBar";
import { TransitionLayer } from "./components/TransitionLayer";
import { ArchitectureFlow } from "./templates/ArchitectureFlow";
import { ChapterBumper } from "./templates/ChapterBumper";
import { ChartCompare } from "./templates/ChartCompare";
import { CodeWalkthrough } from "./templates/CodeWalkthrough";
import { KineticStatement } from "./templates/KineticStatement";
import { MemeCutaway } from "./templates/MemeCutaway";
import { QuoteSpotlight } from "./templates/QuoteSpotlight";
import { TimelineSummary } from "./templates/TimelineSummary";
import { TitleCard } from "./templates/TitleCard";
import { UIShowcase } from "./templates/UIShowcase";
import { getThemePalette, resolveAvatarLayout } from "./theme";
import {
  CJK_FONT_STACK,
  type AquaCueProp,
  type AquaSceneProp,
  type AquaVideoProps,
  type AvatarLayout,
  type SceneTemplateProps,
  type VisualAssetEntry,
} from "./types";
import {
  getActiveLineIndex,
  getSceneCues,
  getSmoothedEnvelope,
} from "./utils/beatSync";

const SceneRenderer: React.FC<{
  scene: AquaSceneProp;
  activeCue?: AquaCueProp;
  sceneCues: AquaCueProp[];
  activeLineIndex: number;
  globalFrame: number;
  includeAvatar: boolean;
  avatarLayout: AvatarLayout;
  memeAssets?: Record<string, VisualAssetEntry>;
}> = ({
  scene,
  activeCue,
  sceneCues,
  activeLineIndex,
  globalFrame,
  includeAvatar,
  avatarLayout,
  memeAssets,
}) => {
  const templateProps: SceneTemplateProps = {
    scene,
    activeCue,
    sceneCues,
    activeLineIndex,
    memeAssets,
    avatarLayout,
    includeAvatar,
  };

  let body: React.ReactNode;
  switch (scene.template) {
    case "TitleCard":
      body = <TitleCard {...templateProps} />;
      break;
    case "CodeWalkthrough":
      body = <CodeWalkthrough {...templateProps} />;
      break;
    case "ChartCompare":
      body = <ChartCompare {...templateProps} />;
      break;
    case "MemeCutaway":
      body = <MemeCutaway {...templateProps} />;
      break;
    case "TimelineSummary":
      body = <TimelineSummary {...templateProps} />;
      break;
    case "ChapterBumper":
      body = <ChapterBumper {...templateProps} />;
      break;
    case "ArchitectureFlow":
      body = <ArchitectureFlow {...templateProps} />;
      break;
    case "UIShowcase":
      body = <UIShowcase {...templateProps} />;
      break;
    case "KineticStatement":
      body = <KineticStatement {...templateProps} />;
      break;
    case "QuoteSpotlight":
    default:
      body = <QuoteSpotlight {...templateProps} />;
      break;
  }

  const hasChapterTag =
    Boolean(scene.chapter_title) &&
    scene.template !== "ChapterBumper" &&
    scene.template !== "TitleCard" &&
    scene.template !== "KineticStatement";

  return (
    <TransitionLayer transition={scene.transition} durationFrames={scene.duration_frames}>
      <CameraRig
        motion={scene.camera_motion}
        durationFrames={scene.duration_frames}
        activeCue={activeCue}
        globalFrame={globalFrame}
      >
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            paddingTop: hasChapterTag ? 42 : 0,
          }}
        >
          {body}
        </div>
      </CameraRig>
    </TransitionLayer>
  );
};

function resolveTransitionSfx(
  scene: AquaSceneProp,
  sfxFiles: Record<string, string> | undefined,
  transitionSfxFile: string | null | undefined,
  bgmEnabled: boolean
): string | null {
  if (scene.transition === "cut") {
    return null;
  }
  if (sfxFiles) {
    if (scene.template === "ChapterBumper" && sfxFiles.chapter_hit) {
      return sfxFiles.chapter_hit;
    }
    if (scene.transition && sfxFiles[scene.transition]) {
      return sfxFiles[scene.transition];
    }
    if (sfxFiles.transition) {
      return sfxFiles.transition;
    }
    if (sfxFiles.whoosh) {
      return sfxFiles.whoosh;
    }
  }
  if (bgmEnabled && transitionSfxFile) {
    return transitionSfxFile;
  }
  return null;
}

export const AquaVideo: React.FC<AquaVideoProps> = ({
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
  audioEnvelope,
  sfxFiles,
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
  const activeTheme = activeScene?.theme;
  const palette = getThemePalette(activeTheme);
  const activeAvatarLayout = resolveAvatarLayout(activeScene, includeAvatar);
  const activeAvatarCue = activeCue?.avatar_cue || activeScene?.avatar_cue || "idle";
  const overallProgress = Math.min(1, frame / Math.max(1, totalFrames));

  const showChapterTag =
    Boolean(activeScene?.chapter_title) &&
    activeScene?.template !== "ChapterBumper" &&
    activeScene?.template !== "TitleCard" &&
    activeScene?.template !== "KineticStatement";

  const computeBgmVolume = (f: number): number => {
    const sceneAtF = scenes.find(
      (s) => f >= s.start_frame && f < s.start_frame + s.duration_frames
    );
    const cueAtF = cues.find(
      (c) => f >= c.start_frame && f < c.start_frame + c.duration_frames
    );
    const smoothed = getSmoothedEnvelope(audioEnvelope, f, 4);
    if (smoothed !== null) {
      const speechRatio = Math.min(1, Math.max(0, (smoothed - 0.05) / 0.25));
      const base = 0.22 - speechRatio * 0.15;
      return Number(base.toFixed(3));
    }
    if (sceneAtF?.template === "ChapterBumper" && !cueAtF) {
      return 0.22;
    }
    return cueAtF ? 0.07 : 0.22;
  };

  return (
    <SceneCanvas theme={activeTheme}>
      {/* Main narration audio */}
      {audioFile ? <Audio src={staticFile(audioFile)} volume={1.0} /> : null}

      {/* Envelope-ducked BGM */}
      {bgmEnabled && bgmFile ? (
        <Audio src={staticFile(bgmFile)} volume={computeBgmVolume} loop />
      ) : null}

      {/* Per-line SFX cues */}
      {cues.map((cue) => {
        const sfxName = cue.sfx && cue.sfx !== "none" ? cue.sfx : null;
        const sfxPath = sfxName ? sfxFiles?.[sfxName] : undefined;
        if (!sfxPath) return null;
        return (
          <Sequence
            key={`sfx-${cue.line_id}`}
            from={Math.max(0, cue.start_frame)}
            durationInFrames={Math.max(1, Math.min(90, cue.duration_frames))}
          >
            <Audio src={staticFile(sfxPath)} volume={0.32} />
          </Sequence>
        );
      })}

      {/* Subtle floating chapter tag on content scenes */}
      {showChapterTag && activeScene ? (
        <div
          style={{
            position: "absolute",
            top: 20,
            left: 48,
            zIndex: 6,
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 14px",
            borderRadius: 999,
            backgroundColor: palette.subtitleBg,
            border: `1px solid ${palette.subtitleBorder}`,
            backdropFilter: "blur(8px)",
            fontFamily: CJK_FONT_STACK,
            fontSize: 14,
            fontWeight: 700,
            color: palette.inkSoft,
            pointerEvents: "none",
          }}
        >
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              backgroundColor: palette.accent,
            }}
          />
          <span>{activeScene.chapter_title}</span>
        </div>
      ) : null}

      {/* Scene timeline sequences */}
      <div style={{ position: "relative", flex: 1, display: "flex" }}>
        {scenes.map((scene, idx) => {
          const sceneCues = getSceneCues(cues, scene);
          const lineIdx = getActiveLineIndex(scene, activeCue, sceneCues, frame);
          const avatarLayout = resolveAvatarLayout(scene, includeAvatar);
          const transSfx =
            idx > 0
              ? resolveTransitionSfx(scene, sfxFiles, transitionSfxFile, bgmEnabled)
              : null;

          return (
            <Sequence
              key={scene.scene_id}
              from={Math.max(0, scene.start_frame)}
              durationInFrames={Math.max(1, scene.duration_frames)}
              style={{ display: "flex", width: "100%", height: "100%" }}
            >
              {transSfx ? <Audio src={staticFile(transSfx)} volume={0.26} /> : null}
              <SceneRenderer
                scene={scene}
                activeCue={activeCue}
                sceneCues={sceneCues}
                activeLineIndex={lineIdx}
                globalFrame={frame}
                includeAvatar={includeAvatar}
                avatarLayout={avatarLayout}
                memeAssets={memeAssets}
              />
            </Sequence>
          );
        })}
      </div>

      {/* Floating cinema captions */}
      {activeCue ? (
        <SubtitleBar
          cue={activeCue}
          includeAvatar={includeAvatar}
          avatarLayout={activeAvatarLayout}
          theme={activeTheme}
        />
      ) : null}

      {/* Borderless floating Aqua cutout with audioEnvelope ring */}
      <AquaOverlay
        cue={activeAvatarCue}
        speaking={Boolean(activeCue)}
        includeAvatar={includeAvatar}
        avatarLayout={activeAvatarLayout}
        avatarAssets={avatarAssets}
        audioEnvelope={audioEnvelope}
        theme={activeTheme}
      />

      {/* Ultra-thin top progress hairline */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 3.5,
          backgroundColor: palette.isDark
            ? "rgba(255, 255, 255, 0.08)"
            : "rgba(15, 34, 61, 0.08)",
          zIndex: 9,
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${(overallProgress * 100).toFixed(2)}%`,
            background: `linear-gradient(90deg, ${palette.accent}, ${palette.secondary}, ${palette.highlight})`,
          }}
        />
      </div>
    </SceneCanvas>
  );
};

export * from "./types";
export { AquaBadge, AquaOverlay } from "./components/AquaBadge";
export { CameraRig } from "./components/CameraRig";
export { CodeHighlighter } from "./components/CodeHighlighter";
export { SceneCanvas } from "./components/SceneCanvas";
export { SubtitleBar } from "./components/SubtitleBar";
export { TransitionLayer } from "./components/TransitionLayer";
export { ArchitectureFlow } from "./templates/ArchitectureFlow";
export { ChapterBumper } from "./templates/ChapterBumper";
export { ChartCompare } from "./templates/ChartCompare";
export { CodeWalkthrough } from "./templates/CodeWalkthrough";
export { KineticStatement } from "./templates/KineticStatement";
export { MemeCutaway } from "./templates/MemeCutaway";
export { QuoteSpotlight } from "./templates/QuoteSpotlight";
export { TimelineSummary } from "./templates/TimelineSummary";
export { TitleCard } from "./templates/TitleCard";
export { UIShowcase } from "./templates/UIShowcase";
