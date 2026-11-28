import { useEffect, useRef, useState } from "react";
import { studioPath } from "@/shared/api/client";
import type {
  ExtendedLiveShowsStatusData,
  ObsLayoutId,
  RundownRuntimeState,
  SceneLayoutState,
} from "./consoleTypes";
import { formatElapsed, liveActionDisabled, type LiveControlAction } from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import type { LiveStudioData } from "./types";

const SESSION_LABELS: Record<string, string> = {
  stopped: "未开播",
  live: "本地会话运行中",
  paused: "队列已暂停",
  degraded: "连接降级",
};

const COLD_TIER_NAMES: Record<number, string> = {
  0: "互动热聊",
  1: "暖场闲聊",
  2: "自娱整活",
  3: "深夜伴播",
};

export function buildOverlayLayoutUrl(
  baseOverlayUrl: string | undefined,
  layout?: ObsLayoutId
): string {
  const fallback =
    typeof window !== "undefined"
      ? `${window.location.origin}${studioPath("/overlay")}`
      : studioPath("/overlay");
  const raw = (baseOverlayUrl || "").trim() || fallback;
  const hashIdx = raw.indexOf("#");
  const basePart = hashIdx >= 0 ? raw.slice(0, hashIdx) : raw;
  const hashPart = hashIdx >= 0 ? raw.slice(hashIdx) : "";
  const queryIdx = basePart.indexOf("?");
  const pathPart = queryIdx >= 0 ? basePart.slice(0, queryIdx) : basePart;
  const search = new URLSearchParams(queryIdx >= 0 ? basePart.slice(queryIdx + 1) : "");
  if (layout) {
    search.set("layout", layout);
  } else {
    search.delete("layout");
  }
  const qs = search.toString();
  return `${pathPart}${qs ? `?${qs}` : ""}${hashPart}`;
}

export function buildDrawGuessOverlayUrl(
  baseOverlayUrl: string | undefined,
  preview = false
): string {
  const autoOverlay = buildOverlayLayoutUrl(baseOverlayUrl);
  const withoutHash = autoOverlay.split("#")[0].split("?")[0];
  const base = withoutHash.endsWith("/overlay")
    ? `${withoutHash}/draw-guess`
    : typeof window !== "undefined"
      ? `${window.location.origin}${studioPath("/overlay/draw-guess")}`
      : studioPath("/overlay/draw-guess");
  return preview ? `${base}?preview=1` : base;
}

export interface BroadcastTopBarProps {
  view: LiveStudioData | null;
  shows: ExtendedLiveShowsStatusData | null;
  rundown: RundownRuntimeState | null;
  sceneLayout: SceneLayoutState;
  layoutMode?: "cockpit" | "theater";
  onLayoutModeChange?: (mode: "cockpit" | "theater") => void;
  canAct: boolean;
  enabled: boolean;
  overlayUrl?: string;
  onControl: (action: LiveControlAction) => void;
  onLayoutChange: (patch: { layout?: ObsLayoutId; locked?: boolean }) => void;
  onRundownNext: () => void;
  onOpenOutput: () => void;
  onOpenDrawGuessPreview: (url: string) => void;
  onCopyUrl: (url: string, label: string) => void;
  renderViewLink: (target: "interaction" | "scene" | "music" | "settings") => {
    href: string;
    onClick?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
  };
}

export function BroadcastTopBar({
  view,
  shows,
  rundown,
  sceneLayout,
  layoutMode = "cockpit",
  onLayoutModeChange,
  canAct,
  enabled,
  overlayUrl,
  onControl,
  onLayoutChange,
  onRundownNext,
  onOpenOutput,
  onOpenDrawGuessPreview,
  onCopyUrl,
  renderViewLink,
}: BroadcastTopBarProps) {
  const [obsMenuOpen, setObsMenuOpen] = useState(false);
  const obsMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!obsMenuOpen || typeof document === "undefined") return undefined;
    const onPointerDown = (event: MouseEvent) => {
      if (obsMenuRef.current && !obsMenuRef.current.contains(event.target as Node)) {
        setObsMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [obsMenuOpen]);

  const liveStatusInput = {
    state: view?.state,
    muted: view?.muted,
    current_job: view?.current,
    queue_length: view?.queue.length ?? 0,
  };
  const isDisabled = (action: LiveControlAction) =>
    !canAct || liveActionDisabled(action, liveStatusInput, { enabled });

  const coldTier = shows?.cold_director?.tier ?? 0;
  const overtime = shows?.overtime;
  const otThreshold = overtime?.threshold && overtime.threshold > 0 ? overtime.threshold : 100;
  const otPct = Math.min(100, Math.round((Math.max(0, overtime?.energy ?? 0) / otThreshold) * 100));

  const sessionPillClass =
    view?.state === "live"
      ? `${styles.statusPill} ${styles.statusPillLive}`
      : view?.state === "degraded"
        ? `${styles.statusPill} ${styles.statusPillWarn}`
        : styles.statusPill;

  const currentSegTitle = rundown?.current_segment?.title || "节目单待机";

  return (
    <header className={styles.broadcastTopBar} data-broadcast-topbar>
      <div className={styles.topBarRow}>
        <div className={styles.topBarCluster}>
          <h1 className={styles.topBarTitle}>导播台</h1>
          <span className={sessionPillClass} data-desk-session role="status">
            {view ? SESSION_LABELS[view.state] || view.state : "读取状态中"}
          </span>
          <span className={styles.statusPill} data-desk-player role="status">
            {view ? (view.overlay_connected ? "播放器已连接" : "播放器未连接") : "检测播放器"}
          </span>
          <button
            type="button"
            className={styles.compactBtn}
            data-desk-open-output
            hidden={!view || Boolean(view.overlay_connected)}
            onClick={onOpenOutput}
          >
            打开播放器 ↗
          </button>
          <strong className={styles.deskError} data-desk-mute-state>
            {view?.muted ? "已静音" : ""}
          </strong>
        </div>

        <div className={styles.topBarCluster}>
          <span
            className={styles.statusPill}
            data-desk-cold-tier
            title={`冷场空闲 ${Math.round(shows?.cold_director?.elapsed_seconds ?? 0)}s`}
          >
            🌙 T{coldTier} · {COLD_TIER_NAMES[coldTier] || "伴播"}
          </span>
          <span
            className={`${styles.statusPill} ${overtime?.active ? styles.statusPillAccent : ""}`}
            data-desk-overtime-pill
          >
            {overtime?.active
              ? `🍚 +${overtime.overtime_minutes ?? 0}m · ${otPct}% · ${formatElapsed(overtime.remaining_seconds)}`
              : "🍚 加班条待机"}
          </span>
          <a
            className={styles.deskChip}
            data-desk-link="interaction"
            {...renderViewLink("interaction")}
          >
            配置排班与规则 →
          </a>

          {onLayoutModeChange && (
            <div className={styles.segmentedGroup} role="group" aria-label="导播台排版模式">
              <button
                type="button"
                data-console-layout="cockpit"
                aria-pressed={layoutMode === "cockpit"}
                onClick={() => onLayoutModeChange("cockpit")}
              >
                三栏一屏
              </button>
              <button
                type="button"
                data-console-layout="theater"
                aria-pressed={layoutMode === "theater"}
                onClick={() => onLayoutModeChange("theater")}
              >
                大屏监视
              </button>
            </div>
          )}

          <div className={styles.obsMenuWrap} ref={obsMenuRef}>
            <button
              type="button"
              className={styles.compactBtn}
              data-obs-menu-toggle
              aria-expanded={obsMenuOpen}
              onClick={() => setObsMenuOpen((prev) => !prev)}
            >
              OBS 地址 ▾
            </button>
            {obsMenuOpen && (
              <div className={styles.obsUrlMenu} data-obs-url-menu role="menu">
                <button
                  type="button"
                  role="menuitem"
                  data-copy-obs="auto"
                  onClick={() => {
                    onCopyUrl(buildOverlayLayoutUrl(overlayUrl), "/overlay (自动跟随)");
                    setObsMenuOpen(false);
                  }}
                >
                  复制 /overlay (自动跟随)
                </button>
                <button
                  type="button"
                  role="menuitem"
                  data-copy-obs="game"
                  onClick={() => {
                    onCopyUrl(
                      buildOverlayLayoutUrl(overlayUrl, "game"),
                      "/overlay?layout=game (游戏固定源)"
                    );
                    setObsMenuOpen(false);
                  }}
                >
                  复制 /overlay?layout=game (游戏固定源)
                </button>
                <button
                  type="button"
                  role="menuitem"
                  data-copy-obs="chat"
                  onClick={() => {
                    onCopyUrl(
                      buildOverlayLayoutUrl(overlayUrl, "chat"),
                      "/overlay?layout=chat (闲聊固定源)"
                    );
                    setObsMenuOpen(false);
                  }}
                >
                  复制 /overlay?layout=chat (闲聊固定源)
                </button>
                <button
                  type="button"
                  role="menuitem"
                  data-copy-obs="draw-guess"
                  onClick={() => {
                    onCopyUrl(buildDrawGuessOverlayUrl(overlayUrl, false), "/overlay/draw-guess");
                    setObsMenuOpen(false);
                  }}
                >
                  复制 /overlay/draw-guess
                </button>
                <button
                  type="button"
                  role="menuitem"
                  data-open-draw-guess-preview
                  onClick={() => {
                    onOpenDrawGuessPreview(buildDrawGuessOverlayUrl(overlayUrl, true));
                    setObsMenuOpen(false);
                  }}
                >
                  打开 /overlay/draw-guess?preview=1 ↗
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className={styles.topBarRow}>
        <div className={styles.rundownBarGroup}>
          <span
            className={`${styles.statusPill} ${rundown?.active ? styles.statusPillAccent : ""}`}
            data-desk-rundown-pill
          >
            🎬 {currentSegTitle}
            {rundown?.active && (
              <>
                {" · "}
                {rundown.paused ? "已暂停" : formatElapsed(rundown.remaining_seconds)}
              </>
            )}
          </span>
          {rundown?.hot_chat_prompt && (
            <span className={`${styles.statusPill} ${styles.statusPillAccent}`} data-rundown-hot-chat>
              🔥 热聊提醒
            </span>
          )}
          <button
            type="button"
            className={styles.compactBtn}
            data-rundown-action="next"
            disabled={!canAct || !rundown?.active}
            onClick={onRundownNext}
          >
            下一环节 ⏭
          </button>
        </div>

        <div className={styles.topBarCluster}>
          <div className={styles.segmentedGroup} role="group" aria-label="OBS 布局切换与锁定">
            <button
              type="button"
              data-layout-btn="chat"
              aria-pressed={sceneLayout.active_layout === "chat"}
              disabled={!canAct}
              onClick={() => onLayoutChange({ layout: "chat" })}
            >
              💬 闲聊布局
            </button>
            <button
              type="button"
              data-layout-btn="game"
              aria-pressed={sceneLayout.active_layout === "game"}
              disabled={!canAct}
              onClick={() => onLayoutChange({ layout: "game" })}
            >
              🎮 游戏布局
            </button>
            <button
              type="button"
              data-layout-lock
              aria-pressed={sceneLayout.layout_locked}
              disabled={!canAct}
              onClick={() => onLayoutChange({ locked: !sceneLayout.layout_locked })}
            >
              {sceneLayout.layout_locked ? "🔒 已锁定" : "🔒 锁定"}
            </button>
          </div>

          <div className="actions">
            <button
              type="button"
              className={`primary ${styles.compactBtn}`}
              data-desk-action="start"
              disabled={isDisabled("start")}
              onClick={() => onControl("start")}
            >
              开播
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="resume"
              hidden={!view || !["paused", "degraded"].includes(view.state)}
              disabled={isDisabled("resume")}
              onClick={() => onControl("resume")}
            >
              继续
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="pause"
              hidden={view?.state === "paused"}
              disabled={isDisabled("pause")}
              onClick={() => onControl("pause")}
            >
              暂停
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="stop"
              disabled={isDisabled("stop")}
              onClick={() => onControl("stop")}
            >
              停止
            </button>
            <button
              type="button"
              className={`danger ${styles.compactBtn}`}
              data-desk-action="mute"
              hidden={Boolean(view?.muted)}
              disabled={isDisabled("mute")}
              onClick={() => onControl("mute")}
            >
              静音
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="unmute"
              hidden={!view?.muted}
              disabled={isDisabled("unmute")}
              onClick={() => onControl("unmute")}
            >
              恢复
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="skip"
              disabled={isDisabled("skip")}
              onClick={() => onControl("skip")}
            >
              跳过
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-desk-action="clear"
              disabled={isDisabled("clear")}
              onClick={() => onControl("clear")}
            >
              清空队列
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
