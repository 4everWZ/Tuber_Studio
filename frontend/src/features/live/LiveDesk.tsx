import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
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
import { LiveSceneCanvas } from "./LiveSceneCanvas";
import {
  formatElapsed,
  formatSongQuality,
  liveActionDisabled,
  moveSongInQueue,
  type LiveControlAction,
} from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import type {
  ColdDirectorStatusData,
  LiveScheduleItem,
  LiveStudioData,
  OvertimeStatusData,
} from "./types";

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

function formatLocalDateTime(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function defaultOnceWindow(): { start: string; end: string } {
  const now = Date.now();
  return {
    start: formatLocalDateTime(now + 5 * 60 * 1000),
    end: formatLocalDateTime(now + 65 * 60 * 1000),
  };
}

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

  const [schedules, setSchedules] = useState<LiveScheduleItem[]>([]);
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [scheduleLabel, setScheduleLabel] = useState("晚间直播");
  const [scheduleKind, setScheduleKind] = useState<"duration" | "daily_window" | "once_window">("duration");
  const [scheduleDuration, setScheduleDuration] = useState("60");
  const [scheduleStartLocal, setScheduleStartLocal] = useState("20:00");
  const [scheduleEndLocal, setScheduleEndLocal] = useState("22:00");
  const [scheduleOnceStart, setScheduleOnceStart] = useState(() => defaultOnceWindow().start);
  const [scheduleOnceEnd, setScheduleOnceEnd] = useState(() => defaultOnceWindow().end);
  const [scheduleWeekdays, setScheduleWeekdays] = useState<number[]>([1, 2, 3, 4, 5]);
  const [scheduleTimezone, setScheduleTimezone] = useState("Asia/Shanghai");
  const [scheduleBaseMinutes, setScheduleBaseMinutes] = useState("60");
  const [scheduleMaxOvertime, setScheduleMaxOvertime] = useState("120");
  const [scheduleExtStep, setScheduleExtStep] = useState("15");
  const [scheduleAutoPause, setScheduleAutoPause] = useState(true);

  const [overtime, setOvertime] = useState<OvertimeStatusData | null>(null);
  const [overtimeBusy, setOvertimeBusy] = useState(false);
  const [overtimeBaseMinutes, setOvertimeBaseMinutes] = useState("60");
  const [overtimeMaxMinutes, setOvertimeMaxMinutes] = useState("120");
  const [overtimeStepMinutes, setOvertimeStepMinutes] = useState("15");

  const [coldDirector, setColdDirector] = useState<ColdDirectorStatusData | null>(null);
  const [coldBusy, setColdBusy] = useState(false);

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

  const loadAuxiliary = useCallback(async () => {
    const [schedRes, overtimeRes, coldRes] = await Promise.allSettled([
      apiRequest<LiveScheduleItem[] | { schedules?: LiveScheduleItem[] }>("/api/v1/live/schedules", {
        method: "GET",
      }),
      apiRequest<OvertimeStatusData>("/api/v1/live/overtime", { method: "GET" }),
      apiRequest<ColdDirectorStatusData>("/api/v1/live/cold-director/status", { method: "GET" }),
    ]);

    if (schedRes.status === "fulfilled" && schedRes.value) {
      const raw = schedRes.value;
      const list = Array.isArray(raw)
        ? raw
        : Array.isArray(raw.schedules)
          ? raw.schedules
          : [];
      setSchedules(list);
    }
    if (
      overtimeRes.status === "fulfilled" &&
      overtimeRes.value &&
      typeof overtimeRes.value === "object" &&
      ("active" in overtimeRes.value || "energy" in overtimeRes.value || "stage" in overtimeRes.value)
    ) {
      setOvertime(overtimeRes.value);
    }
    if (
      coldRes.status === "fulfilled" &&
      coldRes.value &&
      typeof coldRes.value === "object" &&
      "tier" in coldRes.value
    ) {
      setColdDirector(coldRes.value);
    }
  }, []);

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
      void loadAuxiliary();
    } catch {
      setFresh(false);
      setLoadError("暂时读不到直播状态，可刷新重试。");
    }
  }, [loadAuxiliary]);

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
      if (!isAcceptedMockResult(result, submittedType)) {
        throw new Error(result.reason || result.status || "注入失败");
      }
      setMockText("");
    });
  };

  const handleCreateSchedule = async (event: FormEvent) => {
    event.preventDefault();
    if (scheduleBusy) return;
    setScheduleBusy(true);
    setActionError("");
    try {
      const parsedDuration = Number.parseInt(scheduleDuration || "60", 10);
      const parsedBase = Number.parseInt(scheduleBaseMinutes || "60", 10);
      const parsedMaxOt = Number.parseInt(scheduleMaxOvertime || "120", 10);
      const parsedStep = Number.parseInt(scheduleExtStep || "15", 10);

      const body: Record<string, unknown> = {
        kind: scheduleKind,
        label: scheduleLabel.trim() || "直播排班",
        timezone: scheduleTimezone.trim() || "Asia/Shanghai",
        base_duration_minutes: Number.isNaN(parsedBase) ? 60 : parsedBase,
        max_overtime_minutes: Number.isNaN(parsedMaxOt) ? 120 : parsedMaxOt,
        extension_step_minutes: Number.isNaN(parsedStep) ? 15 : parsedStep,
        auto_pause_operations: scheduleAutoPause,
      };
      if (scheduleKind === "duration") {
        body.duration_minutes = Number.isNaN(parsedDuration) ? 60 : parsedDuration;
      } else if (scheduleKind === "once_window") {
        body.start_local = scheduleOnceStart.trim();
        body.end_local = scheduleOnceEnd.trim();
      } else {
        body.start_time = scheduleStartLocal.trim();
        body.end_time = scheduleEndLocal.trim();
        body.weekdays = scheduleWeekdays;
      }

      await apiRequest("/api/v1/live/schedules", { method: "POST", body });
      setActionState("已新增直播排班。");
      await loadAuxiliary();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setScheduleBusy(false);
    }
  };

  const handleToggleSchedule = async (item: LiveScheduleItem) => {
    if (scheduleBusy) return;
    setScheduleBusy(true);
    setActionError("");
    try {
      await apiRequest(`/api/v1/live/schedules/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: { enabled: !(item.enabled ?? true) },
      });
      await loadAuxiliary();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setScheduleBusy(false);
    }
  };

  const handleDeleteSchedule = async (id: string) => {
    if (scheduleBusy) return;
    setScheduleBusy(true);
    setActionError("");
    try {
      await apiRequest(`/api/v1/live/schedules/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      setActionState("已删除直播排班。");
      await loadAuxiliary();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setScheduleBusy(false);
    }
  };

  const handleStartOvertime = async () => {
    if (overtimeBusy) return;
    setOvertimeBusy(true);
    setActionError("");
    try {
      const base = Number.parseInt(overtimeBaseMinutes || "60", 10);
      const maxOt = Number.parseInt(overtimeMaxMinutes || "120", 10);
      const step = Number.parseInt(overtimeStepMinutes || "15", 10);
      const next = await apiRequest<OvertimeStatusData>("/api/v1/live/overtime/start", {
        method: "POST",
        body: {
          base_duration_minutes: Number.isNaN(base) ? 60 : base,
          max_overtime_minutes: Number.isNaN(maxOt) ? 120 : maxOt,
          extension_step_minutes: Number.isNaN(step) ? 15 : step,
        },
      });
      setOvertime(next);
      setActionState("白饭加班能量条已开启。");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setOvertimeBusy(false);
    }
  };

  const handleStopOvertime = async () => {
    if (overtimeBusy) return;
    setOvertimeBusy(true);
    setActionError("");
    try {
      const next = await apiRequest<OvertimeStatusData>("/api/v1/live/overtime/stop", {
        method: "POST",
      });
      setOvertime(next);
      setActionState("已打卡下班，停止加班剧场。");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setOvertimeBusy(false);
    }
  };

  const handleTriggerColdDirector = async () => {
    if (coldBusy) return;
    setColdBusy(true);
    setActionError("");
    try {
      const res = await apiRequest<{ spoken_text?: string }>("/api/v1/live/cold-director/trigger", {
        method: "POST",
      });
      setActionState(res.spoken_text || "已触发冷场导演暖场。");
      await loadAuxiliary();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setColdBusy(false);
    }
  };

  const handleQuickMock = (
    type: "chat" | "gift",
    text: string,
    metadata: Record<string, unknown> = {}
  ) => {
    void runAction(async () => {
      const result = await apiPost<
        "/api/v1/live/mock-event",
        { status?: string; reason?: string | null }
      >("/api/v1/live/mock-event", {
        body: {
          type,
          platform: "mock",
          room_id: "debug",
          display_name: mockName.trim() || "测试观众",
          platform_user_id: "workbench-debug",
          text,
          metadata,
        },
      });
      if (!isAcceptedMockResult(result)) {
        throw new Error(result.reason || result.status || "注入失败");
      }
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

      <div className={styles.phase2Grid}>
        <section
          id="desk-schedules-panel"
          data-desk-schedules
          className={`panel ${styles.deskPanel}`}
          aria-labelledby="desk-schedules-title"
        >
          <div className={styles.deskPanelHeading}>
            <h2 id="desk-schedules-title">定时开播排班</h2>
            <span className={styles.deskChip} data-desk-schedule-count>
              {schedules.length} 条排班
            </span>
          </div>
          <form data-desk-schedule-form onSubmit={(e) => void handleCreateSchedule(e)}>
            <fieldset disabled={scheduleBusy}>
              <div className={styles.compactFormGrid}>
                <label>
                  排班名称
                  <input
                    name="schedule_label"
                    value={scheduleLabel}
                    onChange={(e) => setScheduleLabel(e.target.value)}
                    required
                  />
                </label>
                <label>
                  排班类型
                  <select
                    name="schedule_kind"
                    value={scheduleKind}
                    onChange={(e) =>
                      setScheduleKind(e.target.value as "duration" | "daily_window" | "once_window")
                    }
                  >
                    <option value="duration">单次定时长</option>
                    <option value="daily_window">每日时段</option>
                    <option value="once_window">单次时段</option>
                  </select>
                </label>
                {scheduleKind === "duration" ? (
                  <label>
                    直播时长（分钟）
                    <input
                      name="duration_minutes"
                      type="number"
                      min={1}
                      max={720}
                      value={scheduleDuration}
                      onChange={(e) => setScheduleDuration(e.target.value)}
                    />
                  </label>
                ) : scheduleKind === "once_window" ? (
                  <>
                    <label>
                      开始时间 (YYYY-MM-DD HH:MM)
                      <input
                        name="start_local"
                        type="text"
                        placeholder="YYYY-MM-DD HH:MM"
                        value={scheduleOnceStart}
                        onChange={(e) => setScheduleOnceStart(e.target.value)}
                      />
                    </label>
                    <label>
                      结束时间 (YYYY-MM-DD HH:MM)
                      <input
                        name="end_local"
                        type="text"
                        placeholder="YYYY-MM-DD HH:MM"
                        value={scheduleOnceEnd}
                        onChange={(e) => setScheduleOnceEnd(e.target.value)}
                      />
                    </label>
                  </>
                ) : (
                  <>
                    <label>
                      开始时间 (HH:MM)
                      <input
                        name="start_local"
                        type="text"
                        placeholder="20:00"
                        value={scheduleStartLocal}
                        onChange={(e) => setScheduleStartLocal(e.target.value)}
                      />
                    </label>
                    <label>
                      结束时间 (HH:MM)
                      <input
                        name="end_local"
                        type="text"
                        placeholder="22:00"
                        value={scheduleEndLocal}
                        onChange={(e) => setScheduleEndLocal(e.target.value)}
                      />
                    </label>
                  </>
                )}
                <label>
                  时区
                  <input
                    name="timezone"
                    value={scheduleTimezone}
                    onChange={(e) => setScheduleTimezone(e.target.value)}
                  />
                </label>
                <label>
                  基础时长（分）
                  <input
                    name="base_duration_minutes"
                    type="number"
                    min={5}
                    value={scheduleBaseMinutes}
                    onChange={(e) => setScheduleBaseMinutes(e.target.value)}
                  />
                </label>
                <label>
                  加班上限（分）
                  <input
                    name="max_overtime_minutes"
                    type="number"
                    min={0}
                    value={scheduleMaxOvertime}
                    onChange={(e) => setScheduleMaxOvertime(e.target.value)}
                  />
                </label>
                <label>
                  单次延长（分）
                  <input
                    name="extension_step_minutes"
                    type="number"
                    min={1}
                    value={scheduleExtStep}
                    onChange={(e) => setScheduleExtStep(e.target.value)}
                  />
                </label>
              </div>
              {scheduleKind === "daily_window" && (
                <div className={styles.quickActions} aria-label="重复星期">
                  {[
                    [1, "一"],
                    [2, "二"],
                    [3, "三"],
                    [4, "四"],
                    [5, "五"],
                    [6, "六"],
                    [0, "日"],
                  ].map(([day, label]) => {
                    const checked = scheduleWeekdays.includes(Number(day));
                    return (
                      <label key={day} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            const num = Number(day);
                            setScheduleWeekdays((prev) =>
                              e.target.checked
                                ? [...new Set([...prev, num])]
                                : prev.filter((d) => d !== num)
                            );
                          }}
                        />
                        周{label}
                      </label>
                    );
                  })}
                </div>
              )}
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginTop: 8 }}>
                <input
                  type="checkbox"
                  name="auto_pause_operations"
                  checked={scheduleAutoPause}
                  onChange={(e) => setScheduleAutoPause(e.target.checked)}
                />
                开播时自动暂停后台运维巡检，下播后恢复
              </label>
              <div className="actions" style={{ marginTop: 10 }}>
                <button type="submit" className="primary" data-desk-schedule-create>
                  新增定时开播
                </button>
              </div>
            </fieldset>
          </form>

          <ul className={styles.scheduleList} data-desk-schedule-list>
            {schedules.map((item) => {
              const isEnabled = item.enabled !== false;
              const isActive = item.status === "active" || Boolean(item.active);
              const startText =
                item.start_local_display ||
                item.next_start_local_display ||
                item.start_local ||
                item.start_time ||
                "--:--";
              const endText =
                item.end_local_display ||
                item.next_end_local_display ||
                item.end_local ||
                item.end_time ||
                "--:--";
              const windowText =
                item.kind === "duration"
                  ? `${item.duration_minutes ?? item.base_duration_minutes ?? 60} 分钟`
                  : `${startText} ~ ${endText}`;
              return (
                <li key={item.id} className={styles.scheduleRow} data-schedule-id={item.id}>
                  <div className={styles.scheduleMeta}>
                    <strong>
                      {item.label || item.kind} · {isEnabled ? "已启用" : "已暂停"}
                      {isActive ? " (开播中)" : ""}
                    </strong>
                    <small>
                      {item.kind} · {windowText} · 加班上限 {item.max_overtime_minutes ?? 120}m
                      {item.auto_pause_operations !== false ? " · 自动暂停运维" : ""}
                    </small>
                  </div>
                  <div className={styles.deskRowActions}>
                    <button
                      type="button"
                      data-schedule-toggle={item.id}
                      disabled={scheduleBusy}
                      onClick={() => void handleToggleSchedule(item)}
                    >
                      {isEnabled ? "暂停" : "启用"}
                    </button>
                    <button
                      type="button"
                      className="danger"
                      data-schedule-delete={item.id}
                      disabled={scheduleBusy}
                      onClick={() => void handleDeleteSchedule(item.id)}
                    >
                      删除
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
          <p className={styles.deskEmpty} hidden={schedules.length > 0}>
            暂无定时开播排班。
          </p>
        </section>

        <section
          id="desk-overtime-panel"
          data-desk-overtime
          className={`panel ${styles.deskPanel}`}
          aria-labelledby="desk-overtime-title"
        >
          <div className={styles.deskPanelHeading}>
            <h2 id="desk-overtime-title">🍚 白饭加班剧场 & 本地快速测试</h2>
            <span className={styles.deskChip} data-desk-overtime-state>
              {overtime?.active ? `加班中 +${overtime.overtime_minutes ?? 0}m` : "未开启加班条"}
            </span>
          </div>
          {(() => {
            const th = overtime?.threshold && overtime.threshold > 0 ? overtime.threshold : 100;
            const en = Math.max(0, overtime?.energy ?? 0);
            const pct = Math.min(100, Math.round((en / th) * 100));
            return (
              <div className={styles.overtimeMeter}>
                <div className={styles.overtimeMeterRow}>
                  <strong data-desk-overtime-energy>
                    白饭能量 {en} / {th} ({pct}%)
                  </strong>
                  <span data-desk-overtime-remaining>
                    剩余 {Math.max(0, Math.ceil((overtime?.remaining_seconds ?? 0) / 60))} 分钟 ·{" "}
                    延长 {overtime?.extensions_count ?? 0} 次
                  </span>
                </div>
                <div className={styles.overtimeBarTrack}>
                  <div className={styles.overtimeBarFill} style={{ width: `${pct}%` }} />
                </div>
                <p className={styles.deskHint} data-desk-overtime-line style={{ margin: 0 }}>
                  {overtime?.last_event_line || "开启加班剧场后，送礼或高活跃弹幕会为白饭能量条蓄力续时。"}
                </p>
              </div>
            );
          })()}
          <div className={styles.compactFormGrid}>
            <label>
              基础时长（分）
              <input
                name="ot_base"
                type="number"
                min={5}
                value={overtimeBaseMinutes}
                onChange={(e) => setOvertimeBaseMinutes(e.target.value)}
              />
            </label>
            <label>
              加班上限（分）
              <input
                name="ot_max"
                type="number"
                min={0}
                value={overtimeMaxMinutes}
                onChange={(e) => setOvertimeMaxMinutes(e.target.value)}
              />
            </label>
            <label>
              单次延长时间（分）
              <input
                name="ot_step"
                type="number"
                min={1}
                value={overtimeStepMinutes}
                onChange={(e) => setOvertimeStepMinutes(e.target.value)}
              />
            </label>
          </div>
          <div className="actions" style={{ marginTop: 10 }}>
            <button
              type="button"
              id="overtime-start-btn"
              className="primary"
              disabled={overtimeBusy}
              onClick={() => void handleStartOvertime()}
            >
              开启加班能量条
            </button>
            <button
              type="button"
              id="overtime-stop-btn"
              className="danger"
              disabled={overtimeBusy || !overtime?.active}
              onClick={() => void handleStopOvertime()}
            >
              打卡下班
            </button>
            <button
              type="button"
              id="desk-cold-trigger-btn"
              disabled={coldBusy}
              onClick={() => void handleTriggerColdDirector()}
            >
              冷场导演 Tier {coldDirector?.tier ?? 0} · 触发暖场
            </button>
          </div>

          <div className={styles.deskTools} data-desk-quick-mock>
            <span className={styles.deskHint}>
              本地联调快捷测试（通过 /api/v1/live/mock-event 注入）
            </span>
            <div className={styles.quickActions}>
              <button
                type="button"
                data-quick-mock="chat-hello"
                disabled={!canAct || !enabled}
                onClick={() => handleQuickMock("chat", "Aqua晚上好，今天吃白饭了吗？")}
              >
                测试弹幕：打招呼
              </button>
              <button
                type="button"
                data-quick-mock="chat-guess"
                disabled={!canAct || !enabled}
                onClick={() => handleQuickMock("chat", "西瓜")}
              >
                测试弹幕：猜西瓜
              </button>
              <button
                type="button"
                data-quick-mock="chat-soup"
                disabled={!canAct || !enabled}
                onClick={() => handleQuickMock("chat", "这件事发生在太空里吗？")}
              >
                测试海龟汤提问
              </button>
              <button
                type="button"
                data-quick-mock="chat-vote"
                disabled={!canAct || !enabled}
                onClick={() => handleQuickMock("chat", "1")}
              >
                测试辩论投票 1
              </button>
              <button
                type="button"
                data-quick-mock="gift-brush"
                disabled={!canAct || !enabled}
                onClick={() =>
                  handleQuickMock("gift", "画笔", { gift_name: "画笔", count: 1, price: 10 })
                }
              >
                测试礼物：画笔 (10电池)
              </button>
              <button
                type="button"
                data-quick-mock="gift-rice"
                disabled={!canAct || !enabled}
                onClick={() =>
                  handleQuickMock("gift", "白饭应援", {
                    gift_name: "白饭应援",
                    count: 1,
                    price: 100,
                  })
                }
              >
                测试礼物：白饭应援 (100电池)
              </button>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
