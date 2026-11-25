import React from "react";
import { Composition } from "remotion";
import { AquaVideo } from "./AquaVideo";
import type { AquaVideoProps } from "./types";

const DEFAULT_PROPS: AquaVideoProps = {
  title: "Aqua 浅海手账工坊",
  fps: 30,
  width: 1920,
  height: 1080,
  totalFrames: 180,
  includeAvatar: true,
  bgmEnabled: true,
  scenes: [
    {
      scene_id: "demo_intro",
      template: "TitleCard",
      heading: "本鲸的视频小桌准备就绪",
      start_frame: 0,
      duration_frames: 180,
      avatar_cue: "cheerful",
      visual_props: {
        subtitle: "浅海手账 + 右下角小窗实时联动",
        bullets: ["真实立绘动图", "B 站综艺花字", "轻量浅海 BGM"],
        citation_label: "Aqua 浅海工坊",
      },
    },
  ],
  cues: [
    {
      line_id: "demo_l1",
      scene_id: "demo_intro",
      text: "本鲸的新杂谈桌支棱起来了，快丢个选题过来！",
      start_frame: 0,
      duration_frames: 180,
      avatar_cue: "cheerful",
      emphasis: ["新杂谈桌", "选题"],
    },
  ],
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="AquaVideo"
      component={AquaVideo}
      durationInFrames={180}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={DEFAULT_PROPS}
      calculateMetadata={({ props }: { props: AquaVideoProps }) => {
        const fps = props.fps === 60 ? 60 : 30;
        const width = props.width === 1280 ? 1280 : 1920;
        const height = props.height === 720 ? 720 : 1080;
        const durationInFrames = Math.max(1, Math.round(Number(props.totalFrames) || 180));
        return { durationInFrames, fps, width, height };
      }}
    />
  );
};
