import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  apiRequest,
  studioPath,
} from "@/shared/api/client";
import type { SystemSettings } from "@/shared/hooks/useAppSession";
import { errorMessage } from "@/shared/lib/errors";
import { BroadcastTopBar } from "./BroadcastTopBar";
import type {
  ExtendedLiveShowsStatusData,
  LivePollState,
  ObsLayoutId,
  RundownStatusResponse,
  SceneLayoutState,
} from "./consoleTypes";
import { ContextualMockRail, type MockEventType } from "./ContextualMockRail";
import { liveActionDisabled, moveSongInQueue, type LiveControlAction } from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import { ShowDock, type DockTabId } from "./ShowDock";
import { StageMonitor } from "./StageMonitor";
import type { GameStatusData, LiveStudioData } from "./types";

function isAcceptedMockResult(
  result: { status?: string; reason?: string | null },
  submittedType?: string
): boolean {
  return (
    result.status === "queued" ||
    result.status === "recorded" ||
    result.reason === "已计票" ||
    result.reason === "辩论已计票" ||
    (submittedType === "disconnect" && result.status === "filtered")
  );
}

function isNonEmptyObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && Object.keys(value).length > 0);
}

export interface LiveDeskProps {
  settings: SystemSettings | null;
  mode?: "workbench" | "standalone";
  visible?: boolean;
  onNavigateStandaloneView?: (view: "interaction" | "scene" | "music" | "settings") => void;
}

export function LiveDesk({
  settings,
  mode = "workbench",
  visible = true,
  onNavigateStandaloneView,
}: LiveDeskProps) {
  const [view, setView] = useState<LiveStudioData | null>(null);
  const [shows, setShows] = useState<ExtendedLiveShowsStatusData | null>(null);
  const [gameStatus, setGameStatus] = useState<GameStatusData | null>(null);
  const [rundownStatus, setRundownStatus] = useState<RundownStatusResponse | null>(null);

  const [fresh, setFresh] = useState(false);
  const [pending, setPending] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionState, setActionState] = useState("");
  const [activeDockTab, setActiveDockTab] = useState<DockTabId>("rundown");
  const [layoutMode, setLayoutMode] = useState<"cockpit" | "theater">(() => {
    if (typeof window === "undefined") return "cockpit";
    try {
      return window.localStorage.getItem("live_console_layout_mode") === "theater"
        ? "theater"
        : "cockpit";
    } catch {
      return "cockpit";
    }
  });

  const handleLayoutModeChange = useCallback((nextMode: "cockpit" | "theater") => {
    setLayoutMode(nextMode);
    if (typeof window !== "undefined") {
      try {
        window.localStorage.setItem("live_console_layout_mode", nextMode);
      } catch {
        // Ignore storage errors
      }
    }
  }, []);

  const pollTaskRef = useRef<Promise<void> | null>(null);
  const queueListRef = useRef<HTMLOListElement | null>(null);
  const queueHeadingRef = useRef<HTMLSpanElement | null>(null);

  const isVisible = useCallback(() => {
    if (!visible) return false;
    if (typeof document !== "undefined" && document.hidden) return false;
    return true;
  }, [visible]);

  const load = useCallback(async () => {
    const [studioRes, showsRes, gamesRes, rundownRes] = await Promise.allSettled([
      apiGet<"/api/v1/live/studio", LiveStudioData>("/api/v1/live/studio"),
      apiRequest<ExtendedLiveShowsStatusData>("/api/v1/live/shows/status", { method: "GET" }),
      apiGet<"/api/v1/live/games/status", GameStatusData>("/api/v1/live/games/status"),
      apiRequest<RundownStatusResponse>("/api/v1/live/rundown", { method: "GET" }),
    ]);

    if (studioRes.status === "fulfilled" && studioRes.value && typeof studioRes.value === "object") {
      setView(studioRes.value);
      setFresh(true);
      setLoadError("");
    } else {
      setFresh(false);
      setLoadError("暂时读不到直播状态，可刷新重试。");
    }

    if (
      showsRes.status === "fulfilled" &&
      isNonEmptyObject(showsRes.value) &&
      ("draw_and_guess" in showsRes.value ||
        "turtle_soup" in showsRes.value ||
        "roast_radio" in showsRes.value ||
        "overtime" in showsRes.value ||
        "cold_director" in showsRes.value ||
        "rundown" in showsRes.value ||
        "poll" in showsRes.value ||
        "scene_layout" in showsRes.value)
    ) {
      setShows(showsRes.value);
    }

    if (
      gamesRes.status === "fulfilled" &&
      isNonEmptyObject(gamesRes.value) &&
      ("active_game_id" in gamesRes.value || "active_status" in gamesRes.value)
    ) {
      setGameStatus(gamesRes.value);
    }

    if (
      rundownRes.status === "fulfilled" &&
      isNonEmptyObject(rundownRes.value) &&
      "plan" in rundownRes.value &&
      "state" in rundownRes.value
    ) {
      setRundownStatus(rundownRes.value);
    }
  }, []);

  const refresh = useCallback(() => {
    if (!isVisible()) return Promise.resolve();
    if (!pollTaskRef.current) {
      pollTaskRef.current = load().finally(() => {
        pollTaskRef.current = null;
      });
    }
    return pollTaskRef.current;
  }, [isVisible, load]);

  useEffect(() => {
    if (!visible) return undefined;
    void refresh();
    const timer = setInterval(() => {
      void refresh();
    }, 1000);
    return () => clearInterval(timer);
  }, [visible, refresh]);

  const runAction = useCallback(
    async (
      work: () => Promise<string | void>,
      { queueEdit = false, successFallback = "操作已提交。" }: { queueEdit?: boolean; successFallback?: string } = {}
    ): Promise<boolean> => {
      if (pending || !fresh) return false;
      setPending(true);
      setActionError("");
      setActionState("正在处理…");
      let ok = false;
      try {
        const message = await work();
        setActionState(message || successFallback);
        ok = true;
      } catch (error) {
        setActionState("");
        const raced =
          queueEdit && error instanceof ApiError && [400, 404, 409, 422].includes(error.status);
        setActionError(raced ? "队列已更新，请重新选择。" : errorMessage(error));
      } finally {
        await pollTaskRef.current;
        await refresh();
        setPending(false);
      }
      return ok;
    },
    [pending, fresh, refresh]
  );

  const handleControl = (action: LiveControlAction | "media") => {
    if (action === "clear" && typeof window !== "undefined") {
      if (!window.confirm("清空全部待播歌曲和互动？当前播放保持。")) return;
    }
    const endpoint =
      action === "media" ? (view?.playback?.paused ? "media-resume" : "media-pause") : action;
    void runAction(async () => {
      await apiPost("/api/v1/live/{action}", { pathParams: { action: endpoint } });
    });
  };

  const handleQueueChange = (id: string, action: "up" | "down" | "remove") => {
    const activeEl =
      typeof document !== "undefined" ? (document.activeElement as HTMLElement | null) : null;
    const restoreFocus = Boolean(activeEl && queueListRef.current?.contains(activeEl));

    if (action === "remove") {
      void runAction(
        async () => {
          await apiDelete("/api/v1/live/queue/{identity}", { pathParams: { identity: id } });
        },
        { queueEdit: true }
      ).then(() => {
        if (restoreFocus) {
          const escaped = typeof CSS !== "undefined" && typeof CSS.escape === "function" ? CSS.escape(id) : id;
          const target = queueListRef.current?.querySelector<HTMLButtonElement>(
            `button[data-queue-id="${escaped}"][data-queue-action="${action}"]`
          );
          (target || queueHeadingRef.current)?.focus({ preventScroll: true });
        }
      });
      return;
    }

    if (!view) return;
    const order = moveSongInQueue(view.queue, id, action === "up" ? -1 : 1);
    if (!order) return;
    void runAction(
      async () => {
        await apiPut("/api/v1/live/queue", { body: order });
      },
      { queueEdit: true }
    ).then(() => {
      if (restoreFocus) {
        const escaped = typeof CSS !== "undefined" && typeof CSS.escape === "function" ? CSS.escape(id) : id;
        const target = queueListRef.current?.querySelector<HTMLButtonElement>(
          `button[data-queue-id="${escaped}"][data-queue-action="${action}"]`
        );
        (target || queueHeadingRef.current)?.focus({ preventScroll: true });
      }
    });
  };

  const handleLayoutChange = (patch: { layout?: ObsLayoutId; locked?: boolean }) => {
    void runAction(
      async () => {
        const res = await apiRequest<SceneLayoutState>("/api/v1/live/scene/layout", {
          method: "POST",
          body: patch,
        });
        if (res && typeof res === "object" && "active_layout" in res) {
          setShows((prev) => ({ ...(prev || {}), scene_layout: res }));
        }
        return patch.layout
          ? `已切换至${patch.layout === "game" ? "游戏" : "闲聊"} OBS 布局。`
          : res?.layout_locked
            ? "已锁定当前 OBS 布局。"
            : "已解锁 OBS 布局自动切换。";
      },
      { successFallback: "OBS 布局已更新。" }
    );
  };

  const handlePostAction = async (
    path: string,
    body: Record<string, unknown> = {},
    successMsg?: string
  ) => {
    await runAction(
      async () => {
        const res = await apiRequest<{ spoken_text?: string; word?: string; answer?: string }>(path, {
          method: "POST",
          body,
        });
        return res?.spoken_text || successMsg;
      },
      { successFallback: successMsg || "操作已提交。" }
    );
  };

  const handleInjectMock = async (payload: {
    type: MockEventType;
    display_name: string;
    text: string;
    metadata?: Record<string, unknown>;
  }): Promise<boolean> => {
    return runAction(async () => {
      const result = await apiPost<
        "/api/v1/live/mock-event",
        { status?: string; reason?: string | null }
      >("/api/v1/live/mock-event", {
        body: {
          type: payload.type,
          platform: "mock",
          room_id: "debug",
          display_name: payload.display_name,
          platform_user_id: "workbench-debug",
          text: payload.text,
          metadata: payload.metadata ?? {},
        },
      });
      if (!isAcceptedMockResult(result, payload.type)) {
        throw new Error(result.reason || result.status || "注入失败");
      }
    });
  };

  const openBlankWindow = (url: string, blockedMsg: string) => {
    if (!url || typeof window === "undefined") return;
    const output = window.open("about:blank", "_blank");
    if (!output) {
      setActionError(blockedMsg);
      return;
    }
    output.opener = null;
    output.location = url;
  };

  const handleCopyUrl = async (url: string, label: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setActionError("");
      setActionState(`已复制 OBS 地址：${label}`);
    } catch {
      setActionError("无法复制地址，请检查浏览器剪贴板权限。");
    }
  };

  const renderViewLink = useCallback(
    (target: "interaction" | "scene" | "music" | "settings") => {
      if (mode === "standalone") {
        return {
          href: studioPath(`/live?view=${target}`),
          onClick: (event: React.MouseEvent<HTMLAnchorElement>) => {
            if (onNavigateStandaloneView) {
              event.preventDefault();
              onNavigateStandaloneView(target);
            }
          },
        };
      }
      return {
        href: `#live-${target}`,
      };
    },
    [mode, onNavigateStandaloneView]
  );

  const enabled = view?.live_enabled === true;
  const canAct = !pending && fresh;
  const viewError = view?.error;
  const currentError = view?.current?.error;
  const visualWarning = view?.playback?.visual_warning;
  const runtimeNotices = useMemo(
    () => [...new Set([viewError, currentError, visualWarning].filter(Boolean))].join(" · "),
    [viewError, currentError, visualWarning]
  );

  const poll: LivePollState | null = useMemo(() => {
    if (shows?.poll) return shows.poll;
    if (!view?.poll) return null;
    const raw = view.poll as LivePollState & { complete?: boolean };
    return {
      ...raw,
      id: raw.id ?? "live-poll",
      phase: raw.phase ?? (raw.complete ? "finished" : "started"),
    };
  }, [shows?.poll, view?.poll]);
  const rundownState = rundownStatus?.state ?? shows?.rundown ?? null;
  const sceneLayout: SceneLayoutState = shows?.scene_layout ?? {
    active_layout: view?.scene?.background === "transparent" ? "game" : "chat",
    layout_locked: false,
  };

  const liveStatusInput = {
    state: view?.state,
    muted: view?.muted,
    current_job: view?.current,
    queue_length: view?.queue.length ?? 0,
  };

  const mediaDisabled = !canAct || !enabled || !view?.playback?.song || !view?.playback?.started;
  const skipDisabled = !canAct || liveActionDisabled("skip", liveStatusInput, { enabled });
  const clearDisabled = !canAct || liveActionDisabled("clear", liveStatusInput, { enabled });

  return (
    <main id="live-desk" data-live-page="desk" className={styles.consoleRoot}>
      <BroadcastTopBar
        view={view}
        shows={shows}
        rundown={rundownState}
        sceneLayout={sceneLayout}
        layoutMode={layoutMode}
        onLayoutModeChange={handleLayoutModeChange}
        canAct={canAct}
        enabled={enabled}
        overlayUrl={settings?.overlay_url ?? undefined}
        onControl={(action) => handleControl(action)}
        onLayoutChange={handleLayoutChange}
        onRundownNext={() =>
          void handlePostAction("/api/v1/live/rundown/next", {}, "已切换到下一环节。")
        }
        onOpenOutput={() =>
          openBlankWindow(settings?.overlay_url || "", "请允许打开 OBS 播放器窗口。")
        }
        onOpenDrawGuessPreview={(url) => openBlankWindow(url, "请允许打开节目舞台预览窗口。")}
        onCopyUrl={(url, label) => void handleCopyUrl(url, label)}
        renderViewLink={renderViewLink}
      />

      <div className={styles.deskNotices}>
        <p data-desk-feature className={styles.deskHint} role="status">
          {!view ? "正在读取直播配置" : enabled ? "" : "直播功能未启用。"}
        </p>
        <p data-desk-load-error className={styles.deskError} role="alert">
          {loadError}
        </p>
        <p data-desk-runtime-error className={styles.deskError} role="alert">
          {runtimeNotices}
        </p>
        <p data-desk-action-error className={styles.deskError} role="alert">
          {actionError}
        </p>
        <p data-desk-action-state className={styles.deskHint} role="status">
          {actionState}
        </p>
      </div>

      {layoutMode === "cockpit" ? (
        <div className={styles.cockpitGrid} data-console-grid-mode="cockpit">
          <StageMonitor
            view={view}
            shows={shows}
            poll={poll}
            rundown={rundownState}
            activeLayout={sceneLayout.active_layout}
            compactViewport
            mediaDisabled={mediaDisabled}
            skipDisabled={skipDisabled}
            onMediaToggle={() => handleControl("media")}
            onSkip={() => handleControl("skip")}
            sceneLinkProps={renderViewLink("scene")}
          />

          <ShowDock
            activeTab={activeDockTab}
            onTabChange={setActiveDockTab}
            shows={shows}
            poll={poll}
            rundownStatus={rundownStatus}
            gameStatus={gameStatus}
            busy={pending}
            canAct={canAct}
            onPostAction={handlePostAction}
          />

          <ContextualMockRail
            view={view}
            poll={poll}
            activeDockTab={activeDockTab}
            canAct={canAct}
            enabled={enabled}
            clearDisabled={clearDisabled}
            onQueueChange={handleQueueChange}
            onClearQueue={() => handleControl("clear")}
            onInjectMock={handleInjectMock}
            onSettleVote={() =>
              void handlePostAction("/api/v1/live/rundown/settle-vote", {}, "已结算投票并执行兜底推进。")
            }
            musicLinkProps={renderViewLink("music")}
            queueListRef={queueListRef}
            queueHeadingRef={queueHeadingRef}
          />
        </div>
      ) : (
        <div className={styles.consoleGrid} data-console-grid-mode="theater">
          <div className={styles.consoleMainCol}>
            <StageMonitor
              view={view}
              shows={shows}
              poll={poll}
              rundown={rundownState}
              activeLayout={sceneLayout.active_layout}
              mediaDisabled={mediaDisabled}
              skipDisabled={skipDisabled}
              onMediaToggle={() => handleControl("media")}
              onSkip={() => handleControl("skip")}
              sceneLinkProps={renderViewLink("scene")}
            />

            <ShowDock
              activeTab={activeDockTab}
              onTabChange={setActiveDockTab}
              shows={shows}
              poll={poll}
              rundownStatus={rundownStatus}
              gameStatus={gameStatus}
              busy={pending}
              canAct={canAct}
              onPostAction={handlePostAction}
            />
          </div>

          <div className={styles.consoleRailCol}>
            <ContextualMockRail
              view={view}
              poll={poll}
              activeDockTab={activeDockTab}
              canAct={canAct}
              enabled={enabled}
              clearDisabled={clearDisabled}
              onQueueChange={handleQueueChange}
              onClearQueue={() => handleControl("clear")}
              onInjectMock={handleInjectMock}
              onSettleVote={() =>
                void handlePostAction("/api/v1/live/rundown/settle-vote", {}, "已结算投票并执行兜底推进。")
              }
              musicLinkProps={renderViewLink("music")}
              queueListRef={queueListRef}
              queueHeadingRef={queueHeadingRef}
            />
          </div>
        </div>
      )}
    </main>
  );
}
