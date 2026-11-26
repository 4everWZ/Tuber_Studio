import React from "react";
import { Composition } from "remotion";
import { AquaVideo } from "./AquaVideo";
import type { AquaVideoProps } from "./types";

const DEFAULT_PROPS: AquaVideoProps = {
  title: "Aqua 创作工坊",
  fps: 30,
  width: 1920,
  height: 1080,
  totalFrames: 180,
  includeAvatar: true,
  bgmEnabled: true,
  duration_preset: "standard",
  scenes: [
    {
      scene_id: "demo_intro",
      template: "TitleCard",
      heading: "本鲸的视频小桌准备就绪",
      start_frame: 0,
      duration_frames: 180,
      avatar_cue: "cheerful",
      theme: "dark_tech",
      transition: "fade",
      camera_motion: "push_in",
      avatar_layout: "corner_float",
      visual_props: {
        subtitle: "多镜头动态引擎 + 节拍同步推进",
        bullets: ["实时立绘光环", "Tokyo Night 高亮", "音轨自动避让"],
        citation_label: "Aqua Studio Engine",
      },
    },
  ],
  cues: [
    {
      line_id: "demo_l1",
      scene_id: "demo_intro",
      text: "本鲸的新视频引擎支棱起来了，快丢个选题过来！",
      start_frame: 0,
      duration_frames: 180,
      avatar_cue: "cheerful",
      emphasis: ["新视频引擎", "选题"],
      reveal_step: 0,
      line_index_in_scene: 0,
      sfx: "none",
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
