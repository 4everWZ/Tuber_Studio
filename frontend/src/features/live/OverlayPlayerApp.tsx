import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost, apiRequest, studioPath, studioWsUrl } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import { LiveSceneCanvas } from "./LiveSceneCanvas";
import { buildSilentWavBlob } from "./liveControls";
import styles from "./OverlayPlayer.module.css";
import type {
  ColdDirectorStatusData,
  LiveShowsStatusData,
  LiveViewData,
  OverlayActiveJob,
  OverlayPlayMessage,
  OverlayWsMessage,
  OvertimeStatusData,
  PlaybackSnapshot,
} from "./types";

const AUTH_CLOSE_CODES = new Set([1008, 4401, 4403]);

const COLD_TIER_LABELS: Record<number, string> = {
  0: "活跃互动",
  1: "暖场闲聊",
  2: "自娱整活",
  3: "深夜伴播",
};

function formatCountdown(seconds: number | undefined): string {
  const safe = Math.max(0, Math.floor(seconds ?? 0));
  const hrs = Math.floor(safe / 3600);
  const mins = Math.floor((safe % 3600) / 60);
  const secs = safe % 60;
  if (hrs > 0) {
    return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function computeMouth(job: OverlayActiveJob | null, audio: HTMLAudioElement): number {
  if (!job?.started || audio.paused || audio.ended || audio.muted || audio.volume === 0) {
    return 0;
  }
  const activity = job.activity;
  if (!activity?.envelope?.length || !activity.step) return 0;
  return activity.envelope[Math.floor(audio.currentTime / activity.step)] || 0;
}

function buildProjectionPlayback(
  job: OverlayActiveJob | null,
  audio: HTMLAudioElement
): PlaybackSnapshot | null {
  if (!job) return null;
  return {
    song: job.media ?? null,
    text: job.text ?? null,
    animation_id: job.animation_id ?? null,
    emotion: job.emotion ?? null,
    position: audio.currentTime,
    duration: job.duration,
    started: job.started,
    paused: audio.paused,
    mouth: computeMouth(job, audio),
  };
}

export function OverlayPlayerApp() {
  const [projectionView, setProjectionView] = useState<LiveViewData | null>(null);
  const [overtime, setOvertime] = useState<OvertimeStatusData | null>(null);
  const [coldDirector, setColdDirector] = useState<ColdDirectorStatusData | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<{
    text: string;
    error: boolean;
  }>({ text: "正在连接播放器…", error: false });
  const [audioUnlockNeeded, setAudioUnlockNeeded] = useState(false);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const activeRef = useRef<OverlayActiveJob | null>(null);
  const knownOwnerRef = useRef(false);
  const generationRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const viewRef = useRef<LiveViewData | null>(null);
  const loadingRef = useRef(false);
  const unmountedRef = useRef(false);

  const getAudio = useCallback((): HTMLAudioElement => {
    if (!audioRef.current) {
      const audio = new Audio();
      audio.preload = "auto";
      audioRef.current = audio;
    }
    return audioRef.current;
  }, []);

  const readCurrentMouth = useCallback(() => {
    return computeMouth(activeRef.current, getAudio());
  }, [getAudio]);

  const draw = useCallback(() => {
    const currentView = viewRef.current;
    if (!currentView) return;
    const audio = getAudio();
    const playback = buildProjectionPlayback(activeRef.current, audio);
    setProjectionView({ ...currentView, playback });
  }, [getAudio]);

  const sendWs = useCallback((payload: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }
  }, []);

  const isJobValid = useCallback((job: OverlayActiveJob | null): job is OverlayActiveJob => {
    return Boolean(job && activeRef.current === job && job.generation === generationRef.current);
  }, []);

  const acknowledge = useCallback(
    (type: string, job: OverlayActiveJob, error?: unknown) => {
      if (!isJobValid(job)) return;
      sendWs({
        type,
        job_id: job.job_id,
        playback_token: job.playback_token,
        ...(error ? { error: String(error).slice(0, 300) } : {}),
      });
    },
    [isJobValid, sendWs]
  );

  const report = useCallback(() => {
    const job = activeRef.current;
    if (!job?.started || !knownOwnerRef.current) return;
    const audio = getAudio();
    sendWs({
      type: "progress",
      job_id: job.job_id,
      playback_token: job.playback_token,
      position: audio.currentTime,
      paused: audio.paused,
      mouth: computeMouth(job, audio),
    });
  }, [getAudio, sendWs]);

  const stopPlayback = useCallback(() => {
    generationRef.current += 1;
    activeRef.current = null;
    const audio = getAudio();
    audio.onplaying = null;
    audio.onended = null;
    audio.onerror = null;
    try {
      audio.pause?.();
    } catch {
      // Ignore synthetic media cleanup failures
    }
    audio.removeAttribute?.("src");
    try {
      audio.load?.();
    } catch {
      // Ignore unimplemented jsdom HTMLMediaElement.load
    }
    draw();
  }, [getAudio, draw]);

  const failPlayback = useCallback(
    (job: OverlayActiveJob, error: unknown, autoplay = false) => {
      if (!isJobValid(job)) return;
      acknowledge("failed", job, error);
      stopPlayback();
      setConnectionStatus({
        text: `音频未播放：${String(error).slice(0, 140)}`,
        error: true,
      });
      if (autoplay) {
        setAudioUnlockNeeded(true);
      }
    },
    [isJobValid, acknowledge, stopPlayback]
  );

  const playJob = useCallback(
    async (message: OverlayPlayMessage) => {
      stopPlayback();
      const job: OverlayActiveJob = {
        ...message,
        generation: generationRef.current,
        started: false,
      };
      activeRef.current = job;

      if (!message.audio_ref) {
        failPlayback(job, "播放指令缺少音频文件");
        return;
      }

      const audio = getAudio();
      audio.volume =
        typeof message.volume === "number" ? Math.max(0, Math.min(1, message.volume)) : 1;

      audio.onplaying = () => {
        if (!isJobValid(job)) return;
        if (!job.started) {
          job.started = true;
          acknowledge("started", job);
        }
        setAudioUnlockNeeded(false);
        setConnectionStatus({ text: "", error: false });
        report();
        draw();
      };

      audio.onended = () => {
        if (!isJobValid(job)) return;
        if (!job.started) {
          failPlayback(job, "音频未开始便结束");
          return;
        }
        acknowledge("ended", job);
        stopPlayback();
        setConnectionStatus({ text: "", error: false });
      };

      audio.onerror = () => {
        failPlayback(job, audio.error?.message || "音频读取失败");
      };

      audio.src = studioPath(message.audio_ref);
      try {
        await audio.play();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const isAutoplay = err instanceof Error && err.name === "NotAllowedError";
        failPlayback(job, msg, isAutoplay);
      }
    },
    [stopPlayback, failPlayback, getAudio, isJobValid, acknowledge, report, draw]
  );

  const sync = useCallback(async () => {
    if (loadingRef.current || unmountedRef.current) return;
    loadingRef.current = true;
    try {
      const [viewRes, showsRes] = await Promise.allSettled([
        apiGet<"/api/v1/live/view", LiveViewData>("/api/v1/live/view"),
        apiRequest<LiveShowsStatusData>("/api/v1/live/shows/status", {
          method: "GET",
          cache: "no-store",
        }),
      ]);
      if (unmountedRef.current) return;
      if (viewRes.status === "fulfilled") {
        viewRef.current = viewRes.value;
        draw();
      }
      if (showsRes.status === "fulfilled" && showsRes.value && typeof showsRes.value === "object") {
        const shows = showsRes.value;
        if ("overtime" in shows) setOvertime(shows.overtime ?? null);
        if ("cold_director" in shows) setColdDirector(shows.cold_director ?? null);
      }
    } catch {
      // The playback websocket owns connection failure and cancellation.
    } finally {
      loadingRef.current = false;
    }
  }, [draw]);

  const connect = useCallback(() => {
    if (unmountedRef.current || typeof WebSocket === "undefined") return;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    const socket = new WebSocket(studioWsUrl("/ws/overlay"));
    socketRef.current = socket;

    socket.onopen = () => {
      if (socketRef.current !== socket || unmountedRef.current) return;
      setConnectionStatus({ text: "已连接，等待播放权确认", error: false });
    };

    socket.onmessage = (event) => {
      if (socketRef.current !== socket || unmountedRef.current) return;
      let message: OverlayWsMessage;
      try {
        message = JSON.parse(String(event.data)) as OverlayWsMessage;
      } catch {
        return;
      }

      if (message.type === "hello") {
        knownOwnerRef.current = true;
        setConnectionStatus({ text: "", error: false });
        void sync();
      } else if (message.type === "standby") {
        knownOwnerRef.current = false;
        stopPlayback();
        setConnectionStatus({
          text: message.reason || "已有播放页面，当前待机",
          error: false,
        });
        void sync();
      } else if (message.type === "play" && knownOwnerRef.current) {
        const current = activeRef.current;
        if (
          current?.job_id !== message.job_id ||
          current?.playback_token !== message.playback_token
        ) {
          void playJob(message);
        }
      } else if (message.type === "stop") {
        const current = activeRef.current;
        if (!message.job_id || !current || current.job_id === message.job_id) {
          stopPlayback();
          setConnectionStatus({ text: "", error: false });
        }
      } else if (message.type === "character") {
        stopPlayback();
        void sync();
      } else if (
        message.type === "media-control" &&
        knownOwnerRef.current &&
        activeRef.current?.job_id === message.job_id
      ) {
        const audio = getAudio();
        if (message.paused) {
          audio.pause?.();
          report();
          draw();
        } else {
          const job = activeRef.current;
          void audio.play().catch((err) => {
            if (job) {
              failPlayback(job, err instanceof Error ? err.message : String(err));
            }
          });
        }
      }
    };

    socket.onerror = () => {
      if (socketRef.current !== socket || unmountedRef.current) return;
      setConnectionStatus({ text: "播放器连接失败，检查服务与凭证。", error: true });
    };

    socket.onclose = (event) => {
      if (socketRef.current !== socket) return;
      socketRef.current = null;
      knownOwnerRef.current = false;
      stopPlayback();
      if (unmountedRef.current) return;
      if (AUTH_CLOSE_CODES.has(event.code)) {
        setConnectionStatus({
          text: "播放器凭证无效，请从工作台打开新的播放器。",
          error: true,
        });
        return;
      }
      setConnectionStatus({
        text: "播放器已断线，正在重连；未完成任务不会重播。",
        error: true,
      });
      reconnectTimerRef.current = setTimeout(() => {
        connect();
      }, 3000);
    };
  }, [sync, stopPlayback, playJob, getAudio, report, draw, failPlayback]);

  useEffect(() => {
    unmountedRef.current = false;

    const cleanup = () => {
      unmountedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      stopPlayback();
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
    };

    const tickTimer = setInterval(draw, 100);
    const heartbeatTimer = setInterval(report, 500);
    const refreshTimer = setInterval(() => {
      void sync();
    }, 1000);

    const onPageHide = () => {
      clearInterval(tickTimer);
      clearInterval(heartbeatTimer);
      clearInterval(refreshTimer);
      cleanup();
    };
    if (typeof window !== "undefined") {
      window.addEventListener("pagehide", onPageHide);
    }

    void (async () => {
      if (typeof window !== "undefined") {
        const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
        if (token) {
          try {
            await apiPost("/api/v1/system/auth/overlay", { body: { token } });
            window.history.replaceState(
              null,
              "",
              window.location.pathname + window.location.search
            );
          } catch (error) {
            if (!unmountedRef.current) {
              setConnectionStatus({ text: errorMessage(error, "播放器凭证无效"), error: true });
            }
            return;
          }
        }
      }
      await sync();
      connect();
    })();

    return () => {
      clearInterval(tickTimer);
      clearInterval(heartbeatTimer);
      clearInterval(refreshTimer);
      if (typeof window !== "undefined") {
        window.removeEventListener("pagehide", onPageHide);
      }
      cleanup();
    };
  }, [draw, report, sync, connect, stopPlayback]);

  const handleUnlockAudio = async () => {
    const blob = buildSilentWavBlob();
    const url = URL.createObjectURL(blob);
    const unlock = new Audio(url);
    try {
      await unlock.play();
      unlock.pause?.();
      setAudioUnlockNeeded(false);
      setConnectionStatus({ text: "音频已启用，等待下一次播报", error: false });
    } catch {
      setConnectionStatus({
        text: "请允许站点自动播放音频，再注入下一条事件。",
        error: true,
      });
    } finally {
      URL.revokeObjectURL(url);
    }
  };

  const threshold = overtime?.threshold && overtime.threshold > 0 ? overtime.threshold : 100;
  const energy = Math.max(0, overtime?.energy ?? 0);
  const energyPct = Math.min(100, Math.round((energy / threshold) * 100));
  const hasColdTier = typeof coldDirector?.tier === "number";

  return (
    <div className={styles.overlayRoot}>
      <LiveSceneCanvas
        id="scene"
        className={styles.sceneBox}
        scene={projectionView?.scene}
        view={projectionView}
        editable={false}
        readMouth={readCurrentMouth}
      />
      {(overtime?.active || hasColdTier) && (
        <aside className={styles.hudStack} aria-label="直播剧场状态浮窗">
          {overtime?.active && (
            <section id="overtime-hud" className={styles.overtimeHud}>
              <div className={styles.overtimeHeader}>
                <span>🍚 白饭加班能量条</span>
                <span id="overtime-countdown">
                  下班倒计时 {formatCountdown(overtime.remaining_seconds)}
                </span>
              </div>
              <div className={styles.overtimeMeta}>
                <span id="overtime-energy-pct">能量 {energyPct}%</span>
                <span id="overtime-minutes">
                  已加班 +{overtime.overtime_minutes ?? 0} 分钟
                  {overtime.max_overtime_minutes ? ` / ${overtime.max_overtime_minutes}m` : ""}
                </span>
              </div>
              <div className={styles.overtimeTrack} role="progressbar" aria-valuenow={energyPct} aria-valuemin={0} aria-valuemax={100}>
                <div className={styles.overtimeFill} style={{ width: `${energyPct}%` }} />
              </div>
              <p id="overtime-status-line" className={styles.overtimeLine}>
                {overtime.last_event_line || "准点打卡干白饭中，别随便投喂续命啊！"}
              </p>
            </section>
          )}
          {hasColdTier && coldDirector && (
            <div
              id="cold-director-badge"
              data-cold-tier={coldDirector.tier}
              className={styles.coldTierBadge}
            >
              <span>🌙 冷场导演 Tier {coldDirector.tier}</span>
              <span>
                {COLD_TIER_LABELS[coldDirector.tier ?? 0] || "待命中"}
              </span>
            </div>
          )}
        </aside>
      )}
      <span
        id="connection"
        className={
          connectionStatus.error
            ? `connection error ${styles.connection} ${styles.connectionError}`
            : `connection ${styles.connection}`
        }
      >
        {connectionStatus.text}
      </span>
      <button
        type="button"
        id="enable-audio"
        className={`enable-audio ${styles.enableAudio}`}
        hidden={!audioUnlockNeeded}
        onClick={() => void handleUnlockAudio()}
      >
        启用浏览器音频
        <small>启用后接收下一次播报；失败任务不会重播</small>
      </button>
    </div>
  );
}
