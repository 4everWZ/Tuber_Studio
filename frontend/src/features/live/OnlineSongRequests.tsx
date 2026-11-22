import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import { sanitizeCoverUrl } from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import type { OnlineSongItem, OnlineSongSource, OnlineSongsStatus } from "./types";

export interface OnlineSongRequestsProps {
  visible?: boolean;
  onFeedback?: (message: string, error?: boolean) => void;
}

export function OnlineSongRequests({ visible = true, onFeedback }: OnlineSongRequestsProps) {
  const [statusData, setStatusData] = useState<OnlineSongsStatus | null>(null);
  const [errorText, setErrorText] = useState("");
  const [searchState, setSearchState] = useState(
    "弹幕发送「点歌 歌名」；有歧义时返回候选，可回复序号或由主播确认。"
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<OnlineSongItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [busyControl, setBusyControl] = useState(false);
  const [pendingRowId, setPendingRowId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  // Form fields synced on initial load & explicit save unless dirty
  const [usage, setUsage] = useState<"broadcast" | "local_test">("broadcast");
  const [sources, setSources] = useState("bodian,migu");
  const [quality, setQuality] = useState<"flac_preferred" | "high_bitrate">("flac_preferred");
  const [fallbackBitrate, setFallbackBitrate] = useState("320000");
  const [cacheLimitMib, setCacheLimitMib] = useState("15360");
  const [maxDownloadMib, setMaxDownloadMib] = useState("96");
  const [maxDurationSec, setMaxDurationSec] = useState("600");
  const [allowLower, setAllowLower] = useState(false);
  const [licenseNote, setLicenseNote] = useState("");
  const [liveAllowed, setLiveAllowed] = useState(false);
  const [commercialAllowed, setCommercialAllowed] = useState(false);

  const componentErrorRef = useRef<string | null | undefined>(undefined);
  const initializedRef = useRef(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useUnsavedGuard("live-online-songs", dirty);

  const syncFields = useCallback((next: OnlineSongsStatus, force = false) => {
    const s = next.settings;
    if (!initializedRef.current || force || !dirtyRef.current) {
      if (!initializedRef.current || force) {
        setUsage(s.usage);
        setSources((s.sources || ["bodian", "migu"]).join(","));
        setQuality(s.quality);
        setFallbackBitrate(String(s.fallback_bitrate));
        setCacheLimitMib(String(s.cache_limit_mib));
        setMaxDownloadMib(String(s.max_download_mib));
        setMaxDurationSec(String(s.max_duration_seconds));
        setAllowLower(Boolean(s.allow_lower_quality));
        setLicenseNote(s.license_note || "");
        setLiveAllowed(Boolean(s.live_allowed));
        setCommercialAllowed(Boolean(s.commercial_allowed));
        initializedRef.current = true;
      }
    }
    if (!s.enabled) {
      setSearchResults([]);
    }
    if (next.component.error) {
      setErrorText(next.component.error);
    } else if (componentErrorRef.current) {
      setErrorText((prev) => (prev === componentErrorRef.current ? "" : prev));
    }
    componentErrorRef.current = next.component.error;
  }, []);

  const refresh = useCallback(async () => {
    if (!visible) return;
    if (typeof document !== "undefined" && document.hidden) return;
    try {
      const next = await apiGet<"/api/v1/live/online", OnlineSongsStatus>("/api/v1/live/online");
      setStatusData(next);
      syncFields(next, false);
    } catch (error) {
      if (statusData?.settings.enabled) {
        setErrorText(errorMessage(error));
      }
    }
  }, [visible, syncFields, statusData?.settings.enabled]);

  useEffect(() => {
    if (!visible) return undefined;
    void refresh();
    const timer = setInterval(() => {
      void refresh();
    }, 1000);
    return () => clearInterval(timer);
  }, [visible, refresh]);

  const handleToggleEnabled = async (checked: boolean) => {
    if (!statusData || busyControl) return;
    setBusyControl(true);
    setErrorText("");
    try {
      const next = await apiPut<"/api/v1/live/online", OnlineSongsStatus>("/api/v1/live/online", {
        body: { ...statusData.settings, enabled: checked },
      });
      setStatusData(next);
      syncFields(next, true);
    } catch (error) {
      setErrorText(errorMessage(error));
    } finally {
      setBusyControl(false);
    }
  };

  const handleRestart = async () => {
    if (busyControl) return;
    setBusyControl(true);
    setErrorText("");
    try {
      const next = await apiPost<"/api/v1/live/online/restart", OnlineSongsStatus>(
        "/api/v1/live/online/restart"
      );
      setStatusData(next);
      syncFields(next, true);
    } catch (error) {
      setErrorText(errorMessage(error));
    } finally {
      setBusyControl(false);
    }
  };

  const handleSaveSettings = async (event: FormEvent) => {
    event.preventDefault();
    if (!statusData || busyControl) return;
    setBusyControl(true);
    setErrorText("");
    try {
      const config = {
        ...statusData.settings,
        usage,
        sources: sources
          .split(",")
          .map((s) => s.trim())
          .filter((s): s is OnlineSongSource => s === "bodian" || s === "migu"),
        quality,
        fallback_bitrate: Number(fallbackBitrate),
        allow_lower_quality: allowLower,
        cache_limit_mib: Number(cacheLimitMib),
        max_download_mib: Number(maxDownloadMib),
        max_duration_seconds: Number(maxDurationSec),
        license_note: licenseNote,
        live_allowed: liveAllowed,
        commercial_allowed: commercialAllowed,
      };
      const next = await apiPut<"/api/v1/live/online", OnlineSongsStatus>("/api/v1/live/online", {
        body: config,
      });
      setDirty(false);
      setStatusData(next);
      syncFields(next, true);
      onFeedback?.("在线点歌设置已保存。");
    } catch (error) {
      setErrorText(errorMessage(error));
    } finally {
      setBusyControl(false);
    }
  };

  const handleSearch = async (event: FormEvent) => {
    event.preventDefault();
    const q = searchQuery.trim();
    if (!q || searching) return;
    setSearching(true);
    setErrorText("");
    setSearchState("正在搜索在线曲库…");
    try {
      const result = await apiGet<"/api/v1/live/online/search", { songs: OnlineSongItem[] }>(
        "/api/v1/live/online/search",
        { query: { q } }
      );
      const songs = result.songs || [];
      setSearchResults(songs);
      setSearchState(
        songs.length
          ? `找到 ${songs.length} 首，请确认歌手、专辑与版本。音质将在取到音频后显示。`
          : "没有找到歌曲，请补充歌手或版本。"
      );
    } catch (error) {
      if (statusData?.settings.enabled) {
        setErrorText(errorMessage(error));
      }
      setSearchState(statusData?.settings.enabled ? "搜索失败，请重试。" : "搜索已取消。");
    } finally {
      setSearching(false);
    }
  };

  const handleRequestSong = async (
    rowKey: string,
    song: OnlineSongItem,
    requestFn: () => Promise<{ status?: string; reason?: string | null }>
  ) => {
    if (pendingRowId) return;
    setPendingRowId(rowKey);
    setErrorText("");
    try {
      const result = await requestFn();
      if (result.status !== "queued") {
        throw new Error(result.reason || "未能加入队列");
      }
      onFeedback?.(`已加入队列：${song.title} · ${song.artist}`);
    } catch (error) {
      if (statusData?.settings.enabled) {
        setErrorText(errorMessage(error));
      }
    } finally {
      setPendingRowId(null);
    }
  };

  const settings = statusData?.settings;
  const component = statusData?.component;
  const statusLine = statusData
    ? `${settings?.enabled ? (component?.running ? "点歌组件运行中" : "点歌组件未运行") : "在线点歌已关闭"} · ${settings?.usage === "local_test" ? "仅本机／OBS测试" : "直播用途"} · 缓存 ${(statusData.cache_bytes / 1048576).toFixed(1)} / ${settings?.cache_limit_mib} MiB${component?.running && !component.migu_configured ? " · 咪咕尚未配置凭证" : ""}`
    : "正在读取组件状态";

  const renderSongRow = (
    song: OnlineSongItem,
    rowKey: string,
    buttonLabel: string,
    requestFn: () => Promise<{ status?: string; reason?: string | null }>
  ) => {
    const coverUrl = sanitizeCoverUrl(song.cover);
    const durMins = Math.floor(song.duration / 60);
    const durSecs = String(Math.round(song.duration) % 60).padStart(2, "0");
    return (
      <div key={rowKey} className={styles.songRow}>
        {coverUrl && (
          <img
            className={styles.songCover}
            src={coverUrl}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            width={44}
            height={44}
          />
        )}
        <div className={styles.songInfo}>
          <strong>{song.title}</strong>
          <small>
            {song.artist} · {song.album || "未知专辑"} · {durMins}:{durSecs}
          </small>
        </div>
        <button
          type="button"
          disabled={pendingRowId !== null}
          aria-busy={pendingRowId === rowKey || undefined}
          onClick={() => void handleRequestSong(rowKey, song, requestFn)}
        >
          {buttonLabel}
        </button>
      </div>
    );
  };

  return (
    <section
      className={`panel ${styles.onlineSection}`}
      aria-labelledby="online-heading"
      data-live-page="music"
    >
      <div className="panel-heading">
        <div>
          <p className="eyebrow">ONLINE MUSIC · 可选组件</p>
          <h2 id="online-heading">在线点歌</h2>
        </div>
        <label className="check-field">
          <input
            id="online-enabled"
            type="checkbox"
            checked={Boolean(settings?.enabled)}
            disabled={!statusData || busyControl}
            onChange={(e) => void handleToggleEnabled(e.target.checked)}
          />
          启用点歌
        </label>
        <button
          type="button"
          id="online-restart"
          hidden={!settings?.enabled}
          disabled={busyControl}
          onClick={() => void handleRestart()}
        >
          重启点歌组件
        </button>
      </div>

      <p id="online-status" role="status">
        {statusLine}
      </p>
      <p id="online-error" className="error-text" role="alert">
        {errorText}
      </p>
      <p id="online-install" className="hint" hidden={component?.installed !== false}>
        首次使用需安装 Node.js 22+，在项目的 components/song_requests 目录执行{" "}
        <code>npm ci --ignore-scripts</code>，再开启点歌。
      </p>

      <details>
        <summary>音源、音质与缓存设置</summary>
        <form
          id="online-settings"
          onInput={() => setDirty(true)}
          onSubmit={(e) => void handleSaveSettings(e)}
        >
          <div className={styles.onlineFields}>
            <label>
              播放用途
              <select
                id="online-usage"
                value={usage}
                onChange={(e) => setUsage(e.target.value as "broadcast" | "local_test")}
              >
                <option value="broadcast">直播（需确认用途范围）</option>
                <option value="local_test">仅本机／OBS 技术测试</option>
              </select>
            </label>
            <label>
              音源优先顺序
              <select
                id="online-sources"
                value={sources}
                onChange={(e) => setSources(e.target.value)}
              >
                <option value="bodian,migu">波点 → 咪咕</option>
                <option value="migu,bodian">咪咕 → 波点</option>
                <option value="bodian">仅波点</option>
                <option value="migu">仅咪咕</option>
              </select>
            </label>
            <label>
              请求音质
              <select
                id="online-quality"
                value={quality}
                onChange={(e) => setQuality(e.target.value as "flac_preferred" | "high_bitrate")}
              >
                <option value="flac_preferred">优先 FLAC，回退高码率</option>
                <option value="high_bitrate">高码率</option>
              </select>
            </label>
            <label>
              回退码率
              <select
                id="online-bitrate"
                value={fallbackBitrate}
                onChange={(e) => setFallbackBitrate(e.target.value)}
              >
                <option value="320000">320 kbps</option>
                <option value="256000">256 kbps</option>
                <option value="192000">192 kbps</option>
                <option value="128000">128 kbps</option>
              </select>
            </label>
            <label>
              音乐总缓存上限（MiB，最高15360＝15GiB）
              <input
                id="online-cache"
                type="number"
                min={96}
                max={15360}
                value={cacheLimitMib}
                onChange={(e) => setCacheLimitMib(e.target.value)}
              />
            </label>
            <label>
              单曲下载上限（MiB）
              <input
                id="online-download"
                type="number"
                min={1}
                max={96}
                value={maxDownloadMib}
                onChange={(e) => setMaxDownloadMib(e.target.value)}
              />
            </label>
            <label>
              单曲最长（秒）
              <input
                id="online-duration"
                type="number"
                min={10}
                max={600}
                value={maxDurationSec}
                onChange={(e) => setMaxDurationSec(e.target.value)}
              />
            </label>
          </div>
          <label className="check-field">
            <input
              id="online-lower"
              type="checkbox"
              checked={allowLower}
              onChange={(e) => setAllowLower(e.target.checked)}
            />
            允许低于回退码率
          </label>
          <p className="hint">
            按音源顺序寻找可用音质；播放信息以实际文件为准。咪咕凭证通过辅助进程的 MIGU_COOKIE
            环境变量配置。仅预备当前及下一首，缓存保留原始文件。
          </p>
          <label>
            音源与用途许可说明
            <textarea
              id="online-license"
              rows={2}
              maxLength={500}
              placeholder="记录本机测试范围或目标直播场景的音源与授权范围"
              value={licenseNote}
              onChange={(e) => setLicenseNote(e.target.value)}
            />
          </label>
          <div className="checks">
            <label>
              <input
                id="online-live-allowed"
                type="checkbox"
                checked={liveAllowed}
                onChange={(e) => setLiveAllowed(e.target.checked)}
              />
              已确认可用于目标直播场景
            </label>
            <label>
              <input
                id="online-commercial"
                type="checkbox"
                checked={commercialAllowed}
                onChange={(e) => setCommercialAllowed(e.target.checked)}
              />
              已确认商业用途
            </label>
          </div>
          <button type="submit" disabled={!statusData || busyControl}>
            保存在线点歌设置
          </button>
          <p className="hint">
            关闭或重新配置会停止在线歌曲并取消在线待播与准备；缓存、配置、TTS 互动和本地曲库保留。
          </p>
        </form>
      </details>

      <div id="online-controls" hidden={!settings?.enabled || !component?.running}>
        <form id="online-search-form" className={styles.songTools} onSubmit={(e) => void handleSearch(e)}>
          <label className={styles.srOnly} htmlFor="online-query">
            搜索在线歌曲
          </label>
          <input
            id="online-query"
            type="search"
            maxLength={100}
            required
            placeholder="输入歌名，可补充歌手或版本"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          <button type="submit" id="online-search-button" disabled={searching} aria-busy={searching || undefined}>
            搜索在线曲库
          </button>
        </form>
        <p id="online-search-state" role="status">
          {searchState}
        </p>
        <div id="online-results" className={styles.songList}>
          {searchResults.map((song) =>
            renderSongRow(song, `search-${song.id}`, "点歌", () =>
              apiPost("/api/v1/live/mock-event", {
                body: {
                  type: "song",
                  platform: "mock",
                  room_id: "debug",
                  display_name: "主播",
                  platform_user_id: "operator",
                  text: song.title,
                  metadata: { online_id: song.id, mode: "direct" },
                },
              })
            )
          )}
        </div>
        <div id="online-candidates">
          {(statusData?.candidates ?? []).map((choice) => (
            <section key={choice.token} className={styles.onlineChoice}>
              <h3>{choice.requester} 的点歌 · 待确认版本</h3>
              {choice.songs.map((song, idx) =>
                renderSongRow(song, `${choice.token}-${idx}`, `确认 ${idx + 1}`, () =>
                  apiPost("/api/v1/live/online/confirm", {
                    body: { token: choice.token, number: idx + 1 },
                  })
                )
              )}
            </section>
          ))}
        </div>
      </div>
    </section>
  );
}
