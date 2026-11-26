import type { AquaCueProp, AquaSceneProp } from "../types";

export function getSceneCues(cues: AquaCueProp[], scene: AquaSceneProp): AquaCueProp[] {
  const byId = cues.filter((c) => c.scene_id === scene.scene_id);
  if (byId.length > 0) {
    return byId;
  }
  const sceneEnd = scene.start_frame + scene.duration_frames;
  return cues.filter((c) => {
    const cueEnd = c.start_frame + c.duration_frames;
    return c.start_frame < sceneEnd && cueEnd > scene.start_frame;
  });
}

export function getActiveLineIndex(
  scene: AquaSceneProp,
  activeCue: AquaCueProp | undefined,
  sceneCues: AquaCueProp[],
  globalFrame: number
): number {
  if (
    activeCue &&
    typeof activeCue.line_index_in_scene === "number" &&
    Number.isFinite(activeCue.line_index_in_scene)
  ) {
    return Math.max(0, Math.floor(activeCue.line_index_in_scene));
  }
  if (activeCue && sceneCues.length > 0) {
    const idx = sceneCues.findIndex((c) => c.line_id === activeCue.line_id);
    if (idx >= 0) {
      return idx;
    }
  }
  const localFrame = Math.max(0, globalFrame - scene.start_frame);
  if (sceneCues.length > 0) {
    const ratio = Math.min(0.9999, localFrame / Math.max(1, scene.duration_frames));
    return Math.floor(ratio * sceneCues.length);
  }
  return 0;
}

export function getRevealStep(
  activeCue: AquaCueProp | undefined,
  activeLineIndex: number,
  totalSteps: number,
  localFrame: number,
  durationFrames: number,
  sceneCuesCount = 0
): number {
  if (totalSteps <= 1) {
    return 0;
  }
  const maxStep = totalSteps - 1;
  if (
    activeCue &&
    typeof activeCue.reveal_step === "number" &&
    Number.isFinite(activeCue.reveal_step)
  ) {
    return Math.min(maxStep, Math.max(0, Math.floor(activeCue.reveal_step)));
  }
  if (sceneCuesCount > 1) {
    const mapped = Math.floor((activeLineIndex / sceneCuesCount) * totalSteps);
    return Math.min(maxStep, Math.max(0, mapped));
  }
  const progress = Math.min(0.9999, Math.max(0, localFrame) / Math.max(1, durationFrames));
  return Math.min(maxStep, Math.floor(progress * totalSteps));
}

export function getEnvelopeLevel(
  audioEnvelope: number[] | undefined,
  frame: number,
  speaking: boolean
): number {
  if (audioEnvelope && audioEnvelope.length > 0) {
    const idx = Math.min(audioEnvelope.length - 1, Math.max(0, Math.floor(frame)));
    const val = Number(audioEnvelope[idx]);
    if (Number.isFinite(val)) {
      return Math.min(1, Math.max(0, val));
    }
  }
  if (!speaking) {
    return 0.05;
  }
  return 0.35 + 0.55 * Math.abs(Math.sin(frame * 0.28) * Math.cos(frame * 0.13));
}

export function getSmoothedEnvelope(
  audioEnvelope: number[] | undefined,
  frame: number,
  windowRadius = 4
): number | null {
  if (!audioEnvelope || audioEnvelope.length === 0) {
    return null;
  }
  const center = Math.floor(frame);
  const start = Math.max(0, center - windowRadius);
  const end = Math.min(audioEnvelope.length - 1, center + windowRadius);
  let sum = 0;
  let count = 0;
  for (let i = start; i <= end; i += 1) {
    const v = Number(audioEnvelope[i]);
    if (Number.isFinite(v)) {
      sum += Math.min(1, Math.max(0, v));
      count += 1;
    }
  }
  return count > 0 ? sum / count : 0;
}
