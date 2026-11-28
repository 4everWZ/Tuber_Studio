import { useMemo, useState } from "react";
import type {
  ExtendedLiveShowsStatusData,
  LivePollState,
  ObsLayoutId,
  RundownRuntimeState,
} from "./consoleTypes";
import { formatElapsed, formatSongQuality } from "./liveControls";
import { LiveSceneCanvas } from "./LiveSceneCanvas";
import styles from "./LiveWorkbench.module.css";
import { OvertimeHud } from "./OvertimeHud";
import { ShowStageLayer } from "./ShowStageLayer";
import type { LiveStudioData } from "./types";

export type StageLayerMode = "all" | "scene_only" | "show_only";

const JOB_LABELS: Record<string, string> = {
  received: "已收到",
  queued: "排队中",
  generating: "生成中",
  synthesizing: "准备音频",
  ready_to_play: "等待播放器",
  playing: "播出中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
  filtered: "已过滤",
  expired: "已过期",
  dropped: "未加入队列",
};

const KIND_LABELS: Record<string, string> = {
  chat: "弹幕",
  gift: "礼物",
  song: "歌曲",
  sfx: "音效",
};

export interface StageMonitorProps {
  view: LiveStudioData | null;
  shows: ExtendedLiveShowsStatusData | null;
  poll: LivePollState | null;
  rundown: RundownRuntimeState | null;
  activeLayout: ObsLayoutId;
  mediaDisabled: boolean;
  skipDisabled: boolean;
  onMediaToggle: () => void;
  onSkip: () => void;
  sceneLinkProps: {
    href: string;
    onClick?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
  };
}

export function StageMonitor({
  view,
  shows,
  poll,
  rundown,
  activeLayout,
  mediaDisabled,
  skipDisabled,
  onMediaToggle,
  onSkip,
  sceneLinkProps,
}: StageMonitorProps) {
  const [layerMode, setLayerMode] = useState<StageLayerMode>("all");

  const playback = view?.playback;
  const current = view?.current;
  const song = playback?.song?.song || current?.song;
  const audible = Boolean(playback?.started && !playback.paused && !view?.muted);

  const phase = view?.muted
    ? "已静音"
    : playback?.paused
      ? "歌曲已暂停"
      : audible
        ? "正在播出"
        : playback
          ? "等待播放器确认"
          : (current?.status ? JOB_LABELS[current.status] : undefined) || "空闲";

  const qualityText = formatSongQuality(playback?.song?.song?.quality);
  const trackDetails = song
    ? [song.artist, current?.requester && `点歌 · ${current.requester}`, qualityText]
        .filter(Boolean)
        .join(" · ")
    : current?.requester || "";

  const caption = audible && !song ? playback?.text || "" : "";
  const lastSpokenText = view?.last_spoken?.text || "";
  const hasProgress = Boolean(playback?.started && (playback.duration ?? 0) > 0);
  const isTransparentStage =
    layerMode === "show_only" || view?.scene?.background === "transparent" || activeLayout === "game";

  const layerButtons = useMemo(
    () =>
      [
        ["all", "全部叠加"],
        ["scene_only", "仅主画面"],
        ["show_only", "仅节目层"],
      ] as const,
    []
  );

  return (
    <section className={`panel ${styles.deskPanel}`} aria-labelledby="desk-program-title">
      <div className={styles.deskPanelHeading}>
        <div className={styles.topBarCluster}>
          <h2 id="desk-program-title">主舞台监视器</h2>
          <span className={styles.deskChip} data-stage-layout-badge>
            {activeLayout === "game" ? "🎮 游戏布局" : "💬 闲聊布局"}
          </span>
        </div>

        <div className={styles.topBarCluster}>
          <div className={styles.segmentedGroup} role="group" aria-label="监视器图层">
            {layerButtons.map(([mode, label]) => (
              <button
                key={mode}
                type="button"
                data-stage-layer={mode}
                aria-pressed={layerMode === mode}
                onClick={() => setLayerMode(mode)}
              >
                {label}
              </button>
            ))}
          </div>
          <a className={styles.deskHint} data-desk-link="scene" {...sceneLinkProps}>
            编辑场景
          </a>
        </div>
      </div>

      <div
        className={`${styles.stageViewport} ${isTransparentStage ? styles.stageCheckerboard : ""}`}
        data-desk-preview
        data-layer-mode={layerMode}
      >
        {layerMode !== "show_only" && (
          <LiveSceneCanvas scene={view?.scene} view={view} editable={false} />
        )}
        {layerMode !== "scene_only" && (
          <>
            <ShowStageLayer
              shows={shows}
              poll={poll}
              rundown={rundown}
              previewStandby={layerMode === "show_only"}
              compact
            />
            <OvertimeHud
              shows={shows}
              overtime={shows?.overtime}
              coldDirector={shows?.cold_director}
              rundown={rundown}
              compact
            />
          </>
        )}
      </div>

      <div className={styles.stageFooter}>
        <div className={styles.stageFooterMeta}>
          <div className={styles.topBarCluster}>
            <strong data-desk-current-title>
              {song?.title ||
                (current ? (current.type ? KIND_LABELS[current.type] : "") || "互动播报" : "暂无当前节目")}
            </strong>
            <span className={styles.deskChip} data-desk-current-state>
              {phase}
            </span>
          </div>
          <div className="actions">
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="media"
              disabled={mediaDisabled}
              onClick={onMediaToggle}
            >
              {playback?.paused ? "继续歌曲" : "暂停歌曲"}
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-footer-skip
              disabled={skipDisabled}
              onClick={onSkip}
            >
              跳过当前
            </button>
          </div>
        </div>

        <p className={styles.deskHint} data-desk-current-detail style={{ margin: 0 }}>
          {trackDetails}
        </p>

        <div className={styles.deskClock} data-desk-clock hidden={!hasProgress}>
          <progress
            data-desk-progress
            aria-label="播出进度"
            max={playback?.duration || 1}
            value={Math.min(playback?.duration || 1, Math.max(0, Number(playback?.position) || 0))}
          />
          <span data-desk-position>
            {hasProgress ? `${formatElapsed(playback?.position)} / ${formatElapsed(playback?.duration)}` : ""}
          </span>
        </div>

        <p data-desk-caption className={styles.deskCaption} hidden={!caption}>
          {caption}
        </p>
        <div data-desk-last className={styles.deskLast} hidden={!lastSpokenText || Boolean(caption)}>
          <span className={styles.deskHint}>上一条已确认播报：</span>
          <span data-desk-last-spoken>{lastSpokenText}</span>
        </div>

        <div className={styles.stageFooterMeta}>
          <span className={styles.deskHint} data-desk-portrait>
            {view
              ? `${view.character?.name || "Aqua"} · ${view.performance?.pets_enabled ? "显示形象" : "已隐藏形象"}`
              : ""}
          </span>
          <span className={styles.deskHint}>本地预览静音，声音仅由 OBS 输出</span>
        </div>
      </div>
    </section>
  );
}
