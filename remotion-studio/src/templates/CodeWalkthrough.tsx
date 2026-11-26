import React from "react";
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { AnimatedSticker } from "../components/AnimatedSticker";
import { CodeHighlighter } from "../components/CodeHighlighter";
import { getAvatarRightClearance, getThemePalette } from "../theme";
import { CJK_FONT_STACK, MONO_FONT_STACK, type SceneTemplateProps } from "../types";
import { getRevealStep } from "../utils/beatSync";

export const CodeWalkthrough: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  memeAssets,
  avatarLayout = "compact",
  includeAvatar = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};

  const codeSteps = vp.code_steps && vp.code_steps.length > 0 ? vp.code_steps : null;
  const activeStepIdx = codeSteps
    ? getRevealStep(
        activeCue,
        activeLineIndex,
        codeSteps.length,
        frame,
        scene.duration_frames,
        sceneCues.length
      )
    : 0;
  const currentStep = codeSteps ? codeSteps[activeStepIdx] : undefined;

  const rawCode =
    currentStep?.code_snippet?.trim() ||
    vp.code_snippet?.trim() ||
    "// Code walkthrough snippet";
  const codeLines = rawCode.split("\n").slice(0, 18);
  const bullets = vp.bullets ?? [];
  const language = vp.language || "python";
  const highlightTarget = (activeCue?.highlight_code || "").trim();
  const memeEntry = vp.meme_asset_id ? memeAssets?.[vp.meme_asset_id] : undefined;

  const windowEnter = spring({ frame, fps, config: { damping: 16, stiffness: 130 } });
  const cueLocal = activeCue ? Math.max(0, frame - activeCue.start_frame) : frame;
  const stepMorph = spring({
    frame: cueLocal,
    fps,
    config: { damping: 18, stiffness: 170 },
  });

  const autoFocusLine = Math.floor(
    interpolate(frame, [0, Math.max(1, scene.duration_frames)], [0, Math.max(1, codeLines.length)], {
      extrapolateRight: "clamp",
    })
  );

  const bulletReveal = getRevealStep(
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
        flexDirection: "column",
        gap: 14,
        padding: `18px ${rightClearance}px 126px 42px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16 }}>
        <div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "4px 14px",
              borderRadius: 999,
              backgroundColor: palette.secondarySoft,
              border: `1px solid ${palette.borderStrong}`,
              fontSize: 14,
              fontWeight: 800,
              color: palette.pillText,
              letterSpacing: 0.8,
              marginBottom: 8,
            }}
          >
            CODE WALKTHROUGH · 代码拆解
          </div>
          <div style={{ fontSize: 40, fontWeight: 900, color: palette.ink, lineHeight: 1.15 }}>
            {scene.heading}
          </div>
          {vp.subtitle ? (
            <div style={{ fontSize: 20, fontWeight: 600, color: palette.inkSoft, marginTop: 4 }}>
              {vp.subtitle}
            </div>
          ) : null}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {codeSteps && codeSteps.length > 1 ? (
            <div style={{ display: "flex", gap: 6 }}>
              {codeSteps.map((st, idx) => {
                const isCur = idx === activeStepIdx;
                return (
                  <span
                    key={`${st.title || idx}-${idx}`}
                    style={{
                      padding: "5px 12px",
                      borderRadius: 999,
                      backgroundColor: isCur ? palette.accentSoft : palette.surface,
                      border: `1px solid ${isCur ? palette.accent : palette.border}`,
                      color: isCur ? palette.pillText : palette.muted,
                      fontSize: 14,
                      fontWeight: isCur ? 800 : 600,
                    }}
                  >
                    {st.title || `Step ${idx + 1}`}
                  </span>
                );
              })}
            </div>
          ) : null}
          <span
            style={{
              padding: "6px 14px",
              borderRadius: 10,
              backgroundColor: palette.accentSoft,
              border: `1px solid ${palette.borderStrong}`,
              color: palette.pillText,
              fontFamily: MONO_FONT_STACK,
              fontSize: 15,
              fontWeight: 800,
              textTransform: "uppercase",
            }}
          >
            {language}
          </span>
        </div>
      </div>

      <div style={{ display: "flex", gap: 20, flex: 1, minHeight: 0 }}>
        {/* Tokyo Night Code Panel */}
        <div
          style={{
            flex: bullets.length > 0 || Boolean(vp.uploaded_image_file) ? 1.55 : 1,
            borderRadius: 20,
            backgroundColor: "#1a1b26",
            border: "1px solid rgba(122, 162, 247, 0.28)",
            boxShadow: "0 20px 50px rgba(0, 0, 0, 0.45)",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            opacity: windowEnter,
            transform: `translateY(${interpolate(windowEnter, [0, 1], [18, 0]).toFixed(1)}px)`,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "11px 18px",
              backgroundColor: "#16161e",
              borderBottom: "1px solid rgba(122, 162, 247, 0.16)",
            }}
          >
            <div style={{ display: "flex", gap: 8 }}>
              <span style={{ width: 11, height: 11, borderRadius: "50%", backgroundColor: "#f7768e" }} />
              <span style={{ width: 11, height: 11, borderRadius: "50%", backgroundColor: "#e0af68" }} />
              <span style={{ width: 11, height: 11, borderRadius: "50%", backgroundColor: "#9ece6a" }} />
            </div>
            <span style={{ fontFamily: MONO_FONT_STACK, fontSize: 15, fontWeight: 700, color: "#a9b1d6" }}>
              {currentStep?.title || vp.citation_label || `aqua_note.${language}`}
            </span>
          </div>

          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              opacity: codeSteps && codeSteps.length > 1 ? interpolate(stepMorph, [0, 1], [0.55, 1]) : 1,
            }}
          >
            <CodeHighlighter
              code={rawCode}
              language={language}
              highlightLines={currentStep?.highlight_lines}
              highlightTarget={highlightTarget}
              autoFocusLine={autoFocusLine}
              dimInactive={true}
              fontSize={20}
              maxLines={18}
              animateEntrance={!codeSteps || codeSteps.length <= 1}
            />
          </div>
        </div>

        {/* Right annotations & optional media */}
        {bullets.length > 0 || vp.uploaded_image_file ? (
          <div style={{ flex: 0.78, display: "flex", flexDirection: "column", gap: 12 }}>
            {vp.uploaded_image_file ? (
              <div
                style={{
                  borderRadius: 16,
                  padding: 8,
                  backgroundColor: palette.surface,
                  border: `1px solid ${palette.border}`,
                  boxShadow: palette.cardShadow,
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

            {bullets.map((item, idx) => {
              const unlocked = idx <= bulletReveal;
              const isCurrent = idx === bulletReveal;
              const itemEnter = spring({
                frame: Math.max(0, frame - idx * 4),
                fps,
                config: { damping: 16, stiffness: 150 },
              });
              const targetOpacity = isCurrent
                ? 1
                : unlocked
                  ? palette.isDark
                    ? 0.8
                    : 0.88
                  : palette.isDark
                    ? 0.58
                    : 0.72;
              return (
                <div
                  key={`${item}-${idx}`}
                  style={{
                    padding: "14px 18px",
                    borderRadius: 16,
                    backgroundColor: isCurrent ? palette.surfaceElevated : palette.surface,
                    border: `1.5px solid ${isCurrent ? palette.accent : palette.border}`,
                    boxShadow: isCurrent ? `0 8px 24px ${palette.accent}22` : palette.cardShadow,
                    color: isCurrent ? palette.ink : palette.inkSoft,
                    fontSize: 20,
                    fontWeight: isCurrent ? 800 : 600,
                    lineHeight: 1.42,
                    opacity: interpolate(itemEnter, [0, 1], [0, targetOpacity]),
                    transform: `translateX(${interpolate(itemEnter, [0, 1], [16, 0]).toFixed(1)}px) scale(${isCurrent ? 1.02 : 1})`,
                  }}
                >
                  <span style={{ color: palette.accent, fontWeight: 900, marginRight: 8 }}>
                    0{idx + 1}
                  </span>
                  {item}
                </div>
              );
            })}

            {memeEntry ? (
              <div
                style={{
                  marginTop: "auto",
                  alignSelf: "flex-end",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "8px 14px",
                  borderRadius: 16,
                  backgroundColor: palette.surface,
                  border: `1px solid ${palette.border}`,
                  transform: `rotate(${(Math.sin(frame * 0.1) * 2).toFixed(2)}deg)`,
                }}
              >
                <div style={{ width: 78, height: 78 }}>
                  <AnimatedSticker asset={memeEntry} alt={memeEntry.title || "Aqua 贴纸"} />
                </div>
                {memeEntry.title ? (
                  <span style={{ fontSize: 15, fontWeight: 700, color: palette.inkSoft }}>
                    {memeEntry.title}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
};
