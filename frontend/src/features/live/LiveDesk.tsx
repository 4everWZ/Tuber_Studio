import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError, apiDelete, apiGet, apiPost, apiPut, studioPath } from "@/shared/api/client";
import type { SystemSettings } from "@/shared/hooks/useAppSession";
import { errorMessage } from "@/shared/lib/errors";
import { LiveSceneCanvas } from "./LiveSceneCanvas";
import {
  formatElapsed,
  formatSongQuality,
  liveActionDisabled,
  moveSongInQueue,
  type LiveControlAction,
} from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import type { LiveStudioData } from "./types";

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

const SESSION_LABELS: Record<string, string> = {
  stopped: "未开播",
  live: "本地会话运行中",
  paused: "队列已暂停",
  degraded: "连接降级",
};

const KIND_LABELS: Record<string, string> = {
  chat: "弹幕",
  gift: "礼物",
  song: "歌曲",
  sfx: "音效",
};

const PREP_LABELS: Record<string, string> = {
  waiting: "等待准备",
  pending: "等待准备",
  ready: "准备就绪",
  preparing: "准备中",
  failed: "准备失败",
};

function formatTime(timestamp: number | undefined): string {
  if (typeof timestamp !== "number" || !Number.isFinite(timestamp)) return "";
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export interface LiveDeskProps {
  settings: SystemSettings | null;
  mode?: "workbench" | "standalone";
  visible?: boolean;
  onNavigateStandaloneView?: (view: "scene" | "music" | "settings") => void;
}

export function LiveDesk({
  settings,
  mode = "workbench",
  visible = true,
  onNavigateStandaloneView,
}: LiveDeskProps) {
  const [view, setView] = useState<LiveStudioData | null>(null);
  const [fresh, setFresh] = useState(false);
  const [pending, setPending] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionState, setActionState] = useState("");
  const [feedTab, setFeedTab] = useState<"chat" | "dialogue">("chat");
  const [follow, setFollow] = useState(true);
  const [unread, setUnread] = useState(0);
type MockEventType = "chat" | "gift" | "song" | "sfx" | "disconnect";

  const [mockType, setMockType] = useState<MockEventType>("chat");
  const [mockName, setMockName] = useState("测试观众");
  const [mockText, setMockText] = useState("");
  const [mockAsset, setMockAsset] = useState("");

  const pollTaskRef = useRef<Promise<void> | null>(null);
  const sessionIdRef = useRef<string | null | undefined>(undefined);
  const chatIdsRef = useRef<Set<string | number>>(new Set());
  const followRef = useRef(follow);
  followRef.current = follow;

  const queueListRef = useRef<HTMLOListElement | null>(null);
  const queueHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const chatPaneRef = useRef<HTMLDivElement | null>(null);
  const dialoguePaneRef = useRef<HTMLDivElement | null>(null);
  const chatTabRef = useRef<HTMLButtonElement | null>(null);
  const dialogueTabRef = useRef<HTMLButtonElement | null>(null);

  const isVisible = useCallback(() => {
    if (!visible) return false;
    if (typeof document !== "undefined" && document.hidden) return false;
    return true;
  }, [visible]);

  const load = useCallback(async () => {
    try {
      const next = await apiGet<"/api/v1/live/studio", LiveStudioData>("/api/v1/live/studio");
      const changedSession =
        sessionIdRef.current === undefined || sessionIdRef.current !== next.session_id;
      sessionIdRef.current = next.session_id;
      setView(next);
      setFresh(true);
      setLoadError("");

      const nextChat = next.chat ?? [];
      if (changedSession) {
        chatIdsRef.current = new Set(nextChat.map((item, idx) => item.id ?? idx));
        setFollow(true);
        setUnread(0);
        if (dialoguePaneRef.current) {
          dialoguePaneRef.current.scrollTop = 0;
        }
      } else {
        if (!followRef.current) {
          const added = nextChat.filter(
            (item, idx) => !chatIdsRef.current.has(item.id ?? idx)
          ).length;
          if (added > 0) {
            setUnread((count) => count + added);
          }
        }
        chatIdsRef.current = new Set(nextChat.map((item, idx) => item.id ?? idx));
      }
    } catch {
      setFresh(false);
      setLoadError("暂时读不到直播状态，可刷新重试。");
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

  useEffect(() => {
    if (follow && feedTab === "chat" && chatPaneRef.current) {
      chatPaneRef.current.scrollTop = chatPaneRef.current.scrollHeight;
      setUnread(0);
    }
  }, [follow, feedTab, view?.chat]);

  const runAction = useCallback(
    async (work: () => Promise<void>, { queueEdit = false }: { queueEdit?: boolean } = {}) => {
      if (pending || !fresh) return;
      setPending(true);
      setActionError("");
      setActionState("正在处理…");
      try {
        await work();
        setActionState("操作已提交。");
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
    const activeEl = typeof document !== "undefined" ? (document.activeElement as HTMLElement | null) : null;
    const restoreFocus = Boolean(activeEl && queueListRef.current?.contains(activeEl));

    if (action === "remove") {
      void runAction(
        async () => {
          await apiDelete("/api/v1/live/queue/{identity}", { pathParams: { identity: id } });
        },
        { queueEdit: true }
      ).then(() => {
        if (restoreFocus) {
          const escapedId = typeof CSS !== "undefined" && typeof CSS.escape === "function" ? CSS.escape(id) : id;
          const target = queueListRef.current?.querySelector<HTMLButtonElement>(
            `button[data-queue-id="${escapedId}"][data-queue-action="${action}"]`
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
        const target = queueListRef.current?.querySelector<HTMLButtonElement>(
          `button[data-queue-id="${CSS?.escape ? CSS.escape(id) : id}"][data-queue-action="${action}"]`
        );
        (target || queueHeadingRef.current)?.focus({ preventScroll: true });
      }
    });
  };

  const handleInjectMock = (event: FormEvent) => {
    event.preventDefault();
    const submittedType = mockType;
    const submittedName = mockName;
    const submittedText = mockText;
    const submittedAsset = mockAsset;

    void runAction(async () => {
      const result = await apiPost<
        "/api/v1/live/mock-event",
        { status?: string; reason?: string | null }
      >("/api/v1/live/mock-event", {
        body: {
          type: submittedType,
          platform: "mock",
          room_id: "debug",
          display_name: submittedName,
          platform_user_id: "workbench-debug",
          text: submittedText,
          metadata: submittedType === "sfx" ? { asset_id: submittedAsset.trim() } : {},
        },
      });
      if (
        result.status !== "queued" &&
        result.reason !== "已计票" &&
        !(submittedType === "disconnect" && result.status === "filtered")
      ) {
        throw new Error(result.reason || result.status || "注入失败");
      }
      setMockText("");
    });
  };

  const handleOpenOutput = () => {
    const url = settings?.overlay_url;
    if (!url || typeof window === "undefined") return;
    const output = window.open("about:blank", "_blank");
    if (!output) {
      setActionError("请允许打开 OBS 播放器窗口。");
      return;
    }
    output.opener = null;
    output.location = url;
  };

  const handleCopyOutput = async () => {
    const url = settings?.overlay_url;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setActionState("已复制 OBS 浏览器源地址。");
    } catch {
      setActionError("无法复制地址，请检查浏览器剪贴板权限。");
    }
  };

  const enabled = view?.live_enabled === true;
  const canAct = !pending && fresh;
  const playback = view?.playback;
  const current = view?.current;
  const liveStatusInput = {
    state: view?.state,
    muted: view?.muted,
    current_job: current,
    queue_length: view?.queue.length ?? 0,
  };

  const isDisabled = (action: LiveControlAction | "media") => {
    if (!canAct) return true;
    if (action === "media") {
      return !enabled || !playback?.song || !playback?.started;
    }
    return liveActionDisabled(action, liveStatusInput, { enabled });
  };

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
  const runtimeNotices = useMemo(
    () => [...new Set([view?.error, current?.error, playback?.visual_warning].filter(Boolean))].join(" · "),
    [view?.error, current?.error, playback?.visual_warning]
  );

  const songs = useMemo(
    () => (view?.queue ?? []).filter((item) => item.type === "song"),
    [view?.queue]
  );
  const otherQueueCount = (view?.queue.length ?? 0) - songs.length;

  const viewLinkProps = (target: "scene" | "music" | "settings") => {
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
  };

  const switchFeedTab = (tab: "chat" | "dialogue") => {
    setFeedTab(tab);
    if (tab === "chat" && follow && chatPaneRef.current) {
      chatPaneRef.current.scrollTop = chatPaneRef.current.scrollHeight;
      setUnread(0);
    }
  };

  const handleFeedTabKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next: "chat" | "dialogue" =
      event.key === "Home"
        ? "chat"
        : event.key === "End"
          ? "dialogue"
          : feedTab === "chat"
            ? "dialogue"
            : "chat";
    switchFeedTab(next);
    if (next === "chat") chatTabRef.current?.focus();
    else dialogueTabRef.current?.focus();
  };

  return (
    <main id="live-desk" data-live-page="desk" className={styles.deskRoot}>
      <header className={styles.deskHeader}>
        <div>
          <h1>直播播控台</h1>
          <div className={styles.deskStatus}>
            <span>本地演播 · Mock</span>
            <span data-desk-session role="status">
              {view ? SESSION_LABELS[view.state] || view.state : "读取状态中"}
            </span>
            <span data-desk-player role="status">
              {view ? (view.overlay_connected ? "播放器已连接" : "播放器未连接") : ""}
            </span>
            <button
              type="button"
              data-desk-open-output
              hidden={!view || Boolean(view.overlay_connected)}
              onClick={handleOpenOutput}
            >
              打开播放器 ↗
            </button>
            <strong data-desk-mute-state>{view?.muted ? "已静音" : ""}</strong>
          </div>
        </div>
        <div className="actions">
          <button
            type="button"
            className="primary"
            data-desk-action="start"
            disabled={isDisabled("start")}
            onClick={() => handleControl("start")}
          >
            启动会话
          </button>
          <button
            type="button"
            data-desk-action="pause"
            hidden={view?.state === "paused"}
            disabled={isDisabled("pause")}
            onClick={() => handleControl("pause")}
          >
            暂停队列
          </button>
          <button
            type="button"
            data-desk-action="resume"
            hidden={!view || !["paused", "degraded"].includes(view.state)}
            disabled={isDisabled("resume")}
            onClick={() => handleControl("resume")}
          >
            {view?.state === "degraded" ? "恢复连接" : "恢复队列"}
          </button>
          <button
            type="button"
            className="danger"
            data-desk-action="mute"
            hidden={Boolean(view?.muted)}
            disabled={isDisabled("mute")}
            onClick={() => handleControl("mute")}
          >
            紧急静音
          </button>
          <button
            type="button"
            data-desk-action="unmute"
            hidden={!view?.muted}
            disabled={isDisabled("unmute")}
            onClick={() => handleControl("unmute")}
          >
            解除静音
          </button>
          <button
            type="button"
            data-desk-action="stop"
            disabled={isDisabled("stop")}
            onClick={() => handleControl("stop")}
          >
            停止会话
          </button>
        </div>
      </header>

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

      <div className={styles.deskGrid}>
        <section className={`panel ${styles.deskPanel}`} aria-labelledby="desk-program-title">
          <div className={styles.deskPanelHeading}>
            <h2 id="desk-program-title">节目预览</h2>
            <span className={styles.deskHint}>
              预览静音 ·{" "}
              <a data-desk-link="scene" {...viewLinkProps("scene")}>
                编辑场景
              </a>
            </span>
          </div>
          <div className={styles.deskPreview} data-desk-preview>
            <LiveSceneCanvas scene={view?.scene} view={view} editable={false} />
          </div>
          <span className={styles.deskHint} data-desk-portrait>
            {view
              ? `${view.character?.name || "角色"} · ${view.performance?.pets_enabled ? "显示形象" : "已隐藏形象"}`
              : ""}
          </span>
          <div className={styles.deskNow}>
            <div className={styles.deskPanelHeading}>
              <strong data-desk-current-title>
                {song?.title || (current ? (current.type ? KIND_LABELS[current.type] : "") || "互动播报" : "暂无当前节目")}
              </strong>
              <span className={styles.deskChip} data-desk-current-state>
                {phase}
              </span>
            </div>
            <p className={styles.deskHint} data-desk-current-detail>
              {trackDetails}
            </p>
            <div className={styles.deskClock} data-desk-clock hidden={!hasProgress}>
              <progress
                data-desk-progress
                aria-label="播出进度"
                max={playback?.duration || 1}
                value={Math.min(
                  playback?.duration || 1,
                  Math.max(0, Number(playback?.position) || 0)
                )}
              />
              <span data-desk-position>
                {hasProgress ? `${formatElapsed(playback?.position)} / ${formatElapsed(playback?.duration)}` : ""}
              </span>
            </div>
            <p data-desk-caption className={styles.deskCaption} hidden={!caption}>
              {caption}
            </p>
            <div data-desk-last className={styles.deskLast} hidden={!lastSpokenText || Boolean(caption)}>
              <span className={styles.deskHint}>上一条已确认播报</span>
              <p data-desk-last-spoken>{lastSpokenText}</p>
            </div>
            <div className="actions">
              <button
                type="button"
                data-desk-action="media"
                disabled={isDisabled("media")}
                onClick={() => handleControl("media")}
              >
                {playback?.paused ? "继续歌曲" : "暂停歌曲"}
              </button>
              <button
                type="button"
                data-desk-action="skip"
                disabled={isDisabled("skip")}
                onClick={() => handleControl("skip")}
              >
                跳过当前
              </button>
            </div>
          </div>

          <details className={styles.deskTools}>
            <summary>OBS 输出与运行工具</summary>
            <div className={`actions ${styles.actionsRow}`}>
              <button type="button" data-desk-copy-output onClick={() => void handleCopyOutput()}>
                复制 OBS 浏览器源地址
              </button>
              <button type="button" data-desk-refresh onClick={() => void refresh()}>
                刷新状态
              </button>
              <a data-desk-link="settings" {...viewLinkProps("settings")}>
                直播设置 →
              </a>
            </div>
            <p className={styles.deskHint}>只有获得播放权的播放器输出音频。</p>
          </details>
        </section>

        <section className={`panel ${styles.deskPanel}`} aria-labelledby="desk-queue-title">
          <div className={styles.deskPanelHeading}>
            <h2 id="desk-queue-title" ref={queueHeadingRef} data-desk-queue-heading tabIndex={-1}>
              歌曲队列
            </h2>
            <span className={styles.deskChip} data-desk-song-count>
              {songs.length} 首待播
            </span>
          </div>
          <p className={styles.deskHint} data-desk-other-count>
            {otherQueueCount > 0 ? `另有 ${otherQueueCount} 条待播互动` : ""}
          </p>
          <ol ref={queueListRef} data-desk-queue className={`${styles.deskList} ${styles.deskQueue}`} aria-label="待播歌曲">
            {songs.map((item, index) => {
              const queuedSong = item.media?.song;
              const title = queuedSong?.title || item.text || "未命名歌曲";
              const subtitle = [queuedSong?.artist, item.requester && `点歌 · ${item.requester}`]
                .filter(Boolean)
                .join(" · ");
              const prep = item.preparation;
              const prepLabel = prep ? PREP_LABELS[prep[0]] || "等待准备" : "";
              const prepError = prep?.[1] ?? "";
              return (
                <li key={item.id} className={styles.deskQueueRow}>
                  <span className={styles.deskIndex}>{index + 1}</span>
                  <div className={styles.deskRowContent}>
                    <strong>{title}</strong>
                    <small>{subtitle}</small>
                    {prep && (
                      <small className={prepError ? styles.deskError : undefined}>
                        {prepError || prepLabel}
                      </small>
                    )}
                  </div>
                  <div className={styles.deskRowActions}>
                    {(
                      [
                        ["up", "上移", index === 0],
                        ["down", "下移", index === songs.length - 1],
                        ["remove", "移除", false],
                      ] as const
                    ).map(([action, label, isEdge]) => (
                      <button
                        key={action}
                        type="button"
                        data-queue-action={action}
                        data-queue-id={item.id}
                        data-edge={String(isEdge)}
                        aria-label={`${label} ${title}`}
                        disabled={!canAct || !enabled || isEdge}
                        onClick={() => handleQueueChange(item.id, action)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </li>
              );
            })}
          </ol>
          <p data-desk-queue-empty className={styles.deskEmpty} hidden={songs.length > 0}>
            暂无待播歌曲。
          </p>
          <div className={styles.deskQueueFooter}>
            <a data-desk-link="music" {...viewLinkProps("music")}>
              曲库与点歌 →
            </a>
            <button
              type="button"
              data-desk-action="clear"
              disabled={isDisabled("clear")}
              onClick={() => handleControl("clear")}
            >
              清空全部待播
            </button>
          </div>
        </section>

        <section className={`panel ${styles.deskPanel}`} aria-labelledby="desk-room-title">
          <div className={styles.deskPanelHeading}>
            <h2 id="desk-room-title">直播间</h2>
            <button
              type="button"
              className={styles.deskFollowBtn}
              data-desk-follow
              aria-pressed={follow}
              hidden={feedTab !== "chat"}
              onClick={() => {
                const next = !follow;
                setFollow(next);
                if (next && chatPaneRef.current) {
                  chatPaneRef.current.scrollTop = chatPaneRef.current.scrollHeight;
                  setUnread(0);
                }
              }}
            >
              {follow ? "跟随最新：开" : unread ? `跳到最新 · ${unread} 条` : "跟随最新：关"}
            </button>
          </div>
          <div className={styles.deskFeedTabs} role="tablist" aria-label="直播间记录">
            <button
              ref={chatTabRef}
              type="button"
              role="tab"
              id="desk-chat-tab"
              aria-controls="desk-chat"
              aria-selected={feedTab === "chat"}
              tabIndex={feedTab === "chat" ? 0 : -1}
              data-desk-feed="chat"
              onClick={() => switchFeedTab("chat")}
              onKeyDown={handleFeedTabKeyDown}
            >
              弹幕与礼物
            </button>
            <button
              ref={dialogueTabRef}
              type="button"
              role="tab"
              id="desk-dialogue-tab"
              aria-controls="desk-dialogue"
              aria-selected={feedTab === "dialogue"}
              tabIndex={feedTab === "dialogue" ? 0 : -1}
              data-desk-feed="dialogue"
              onClick={() => switchFeedTab("dialogue")}
              onKeyDown={handleFeedTabKeyDown}
            >
              播出记录
            </button>
          </div>
          <div
            ref={chatPaneRef}
            data-desk-chat
            id="desk-chat"
            role="tabpanel"
            aria-labelledby="desk-chat-tab"
            tabIndex={0}
            className={styles.deskFeed}
            hidden={feedTab !== "chat"}
            onScroll={() => {
              const el = chatPaneRef.current;
              if (!el || feedTab !== "chat") return;
              const atBottom = el.scrollHeight - el.clientHeight - el.scrollTop < 24;
              setFollow(atBottom);
              if (atBottom) setUnread(0);
            }}
          >
            <ol className={styles.deskList}>
              {(view?.chat ?? []).map((item, idx) => (
                <li key={item.id ?? idx} className={styles.deskFeedRow}>
                  <div className={styles.deskFeedMeta}>
                    <strong>{item.requester || "直播间"}</strong>
                    <span>{KIND_LABELS[item.type] || "互动"}</span>
                    <span>{(item.status ? JOB_LABELS[item.status] : "") || item.status || ""}</span>
                    <time>{formatTime(item.received_at)}</time>
                  </div>
                  <p>{item.text || "（无文字）"}</p>
                  {item.reason && <small>{item.reason}</small>}
                </li>
              ))}
            </ol>
            <p data-feed-empty className={styles.deskEmpty} hidden={(view?.chat?.length ?? 0) > 0}>
              本会话暂无弹幕或礼物。
            </p>
          </div>
          <div
            ref={dialoguePaneRef}
            data-desk-dialogue
            id="desk-dialogue"
            role="tabpanel"
            aria-labelledby="desk-dialogue-tab"
            tabIndex={0}
            className={styles.deskFeed}
            hidden={feedTab !== "dialogue"}
          >
            <ol className={styles.deskList}>
              {(view?.dialogue ?? []).map((item) => {
                const stateText =
                  item.status === "completed"
                    ? "已确认播放"
                    : JOB_LABELS[item.status] || item.status;
                return (
                  <li key={item.id} className={styles.deskFeedRow}>
                    <div className={styles.deskFeedMeta}>
                      <strong>{item.requester || "直播间"}</strong>
                      <span>{KIND_LABELS[item.type] || "互动"}</span>
                      <span>{stateText}</span>
                      <time>{formatTime(item.created_at)}</time>
                    </div>
                    {item.status === "completed" && item.text && <p>{item.text}</p>}
                    {item.type === "song" && item.song_title && <p>{item.song_title}</p>}
                    {item.error && item.status !== "completed" && (
                      <small className={styles.deskError}>{item.error}</small>
                    )}
                  </li>
                );
              })}
            </ol>
            <p
              data-feed-empty
              className={styles.deskEmpty}
              hidden={(view?.dialogue?.length ?? 0) > 0}
            >
              本会话暂无播出记录。
            </p>
          </div>

          <details className={styles.deskTools}>
            <summary>离线事件调试</summary>
            <form data-desk-mock onSubmit={handleInjectMock}>
              <fieldset disabled={!canAct || !enabled}>
                <div className={styles.fieldsGrid}>
                  <label>
                    类型
                    <select
                      name="type"
                      value={mockType}
                      onChange={(e) => setMockType(e.target.value as MockEventType)}
                    >
                      <option value="chat">弹幕</option>
                      <option value="gift">礼物</option>
                      <option value="song">点歌</option>
                      <option value="sfx">预录音效</option>
                      <option value="disconnect">模拟断线</option>
                    </select>
                  </label>
                  <label>
                    昵称
                    <input
                      name="name"
                      maxLength={80}
                      required
                      value={mockName}
                      onChange={(e) => setMockName(e.target.value)}
                    />
                  </label>
                </div>
                <label>
                  消息
                  <input
                    name="text"
                    maxLength={500}
                    required={!["sfx", "disconnect"].includes(mockType)}
                    value={mockText}
                    onChange={(e) => setMockText(e.target.value)}
                  />
                </label>
                <label data-desk-asset-label hidden={mockType !== "sfx"}>
                  音效素材 ID
                  <input
                    name="asset"
                    maxLength={100}
                    required={mockType === "sfx"}
                    value={mockAsset}
                    onChange={(e) => setMockAsset(e.target.value)}
                  />
                </label>
                <button type="submit">注入 Mock 事件</button>
              </fieldset>
            </form>
          </details>
        </section>
      </div>
    </main>
  );
}
