import React from "react";
import { Composition } from "remotion";
import { AquaVideo } from "./AquaVideo";
import type { AquaVideoProps } from "./types";

const DEFAULT_PROPS: AquaVideoProps = {
  title: "Aqua Code-as-Video Studio",
  fps: 30,
  width: 1920,
  height: 1080,
  totalFrames: 180,
  includeAvatar: false,
  scenes: [
    {
      scene_id: "demo_intro",
      template: "TitleCard",
      heading: "Aqua 视频工坊演示",
      start_frame: 0,
      duration_frames: 180,
      avatar_cue: "cheerful",
      visual_props: {
        subtitle: "Remotion Code-as-Video 渲染引擎就绪",
        bullets: ["音频驱动精准帧对齐", "六套结构化动效模板", "公开来源可追溯"],
        citation_label: "Aqua Studio",
      },
    },
  ],
  cues: [
    {
      line_id: "demo_l1",
      scene_id: "demo_intro",
      text: "哈喽，我是 Aqua！Remotion 视频渲染引擎已经就绪啦。",
      start_frame: 0,
      duration_frames: 180,
      avatar_cue: "cheerful",
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
