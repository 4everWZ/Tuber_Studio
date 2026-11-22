import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPost, apiPut } from "@/shared/api/client";
import type { SystemSettings } from "@/shared/hooks/useAppSession";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import { parseLrcLyrics } from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import { OnlineSongRequests } from "./OnlineSongRequests";
import type { LiveMusicData, SongItem } from "./types";

export interface LiveMusicPageProps {
  settings: SystemSettings | null;
  visible?: boolean;
  showWorkbenchHeading?: boolean;
  onFeedback?: (message: string, error?: boolean) => void;
}

export function LiveMusicPage({
  settings,
  visible = true,
  showWorkbenchHeading = true,
  onFeedback,
}: LiveMusicPageProps) {
  const [music, setMusic] = useState<LiveMusicData | null>(null);
  const [search, setSearch] = useState("");
  const [requestMode, setRequestMode] = useState<"direct" | "sing">("direct");
  const [musicDirty, setMusicDirty] = useState(false);
  const [defaultMode, setDefaultMode] = useState<"direct" | "sing">("direct");
  const [musicVolume, setMusicVolume] = useState("0.8");
  const [musicLimit, setMusicLimit] = useState("20");
  const [musicCooldown, setMusicCooldown] = useState("30");
  const [busyAction, setBusyAction] = useState(false);

  // Import dialog state
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const songFormRef = useRef<HTMLFormElement | null>(null);
  const audioInputRef = useRef<HTMLInputElement | null>(null);
  const vocalInputRef = useRef<HTMLInputElement | null>(null);
  const accompanimentInputRef = useRef<HTMLInputElement | null>(null);
  const [songTitle, setSongTitle] = useState("");
  const [songArtist, setSongArtist] = useState("");
  const [songLyrics, setSongLyrics] = useState("");
  const [songLicense, setSongLicense] = useState("");
  const [songLiveAllowed, setSongLiveAllowed] = useState(false);
  const [songCommercial, setSongCommercial] = useState(false);
  const [importState, setImportState] = useState("");
  const [importing, setImporting] = useState(false);

  const firstLoadRef = useRef(true);
  const musicDirtyRef = useRef(musicDirty);
  musicDirtyRef.current = musicDirty;

  useUnsavedGuard("live-music", musicDirty);

  const loadMusic = useCallback(async () => {
    try {
      const data = await apiGet<"/api/v1/live/music", LiveMusicData>("/api/v1/live/music");
      setMusic(data);
      const cfg = data.settings;
      if (!musicDirtyRef.current) {
        setDefaultMode(cfg.default_mode);
        setMusicVolume(String(cfg.volume));
        setMusicLimit(String(cfg.queue_limit));
        setMusicCooldown(String(cfg.cooldown_seconds));
      }
      if (firstLoadRef.current) {
        firstLoadRef.current = false;
        setRequestMode(cfg.default_mode);
      }
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    }
  }, [onFeedback]);

  useEffect(() => {
    if (!visible) return;
    void loadMusic();
  }, [visible, loadMusic]);

  const openDialog = () => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  };

  const closeDialog = () => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  };

  const handleRequestSong = async (song: SongItem) => {
    if (busyAction) return;
    setBusyAction(true);
    try {
      const result = await apiPost<
        "/api/v1/live/mock-event",
        { status?: string; reason?: string | null }
      >("/api/v1/live/mock-event", {
        body: {
          type: "song",
          platform: "mock",
          room_id: "debug",
          text: song.title,
          display_name: "主播",
          metadata: { song_id: song.id, mode: requestMode },
        },
      });
      if (result.status !== "queued") {
        throw new Error(result.reason || result.status || "点歌失败");
      }
      onFeedback?.(`已加入队列：${song.title}`);
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusyAction(false);
    }
  };

  const handleRemoveSong = async (song: SongItem) => {
    if (busyAction) return;
    if (
      typeof window !== "undefined" &&
      !window.confirm(`从曲库移除「${song.title}」？已有排队项保留。`)
    ) {
      return;
    }
    setBusyAction(true);
    try {
      await apiDelete("/api/v1/live/songs/{identity}", { pathParams: { identity: song.id } });
      await loadMusic();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusyAction(false);
    }
  };

  const handleSaveMusicSettings = async (event: FormEvent) => {
    event.preventDefault();
    if (!music || busyAction) return;
    setBusyAction(true);
    try {
      await apiPut("/api/v1/live/music", {
        body: {
          ...music.settings,
          default_mode: defaultMode,
          volume: Number(musicVolume),
          queue_limit: Number(musicLimit),
          cooldown_seconds: Number(musicCooldown),
        },
      });
      setMusicDirty(false);
      await loadMusic();
      onFeedback?.("点歌设置已保存，对新确认项生效。");
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusyAction(false);
    }
  };

  const handleImportSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (importing) return;
    setImporting(true);
    try {
      const lyrics = parseLrcLyrics(songLyrics);
      const uploadedIds: Record<string, string> = {};
      const uploadTargets: Array<["audio" | "vocal" | "accompaniment", string, File | undefined]> = [
        ["audio", "歌曲", audioInputRef.current?.files?.[0]],
        ["vocal", "人声", vocalInputRef.current?.files?.[0]],
        ["accompaniment", "伴奏", accompanimentInputRef.current?.files?.[0]],
      ];

      for (const [key, label, file] of uploadTargets) {
        if (!file) continue;
        if (file.size > 96 * 1024 * 1024) {
          throw new Error("单个音频上限96MiB");
        }
        setImportState(`正在上传${label}…`);
        const uploaded = await apiPost<"/api/v1/live/uploads", { id: string }, File>(
          "/api/v1/live/uploads",
          {
            body: file,
            headers: { "Content-Type": file.type || "audio/wav" },
          }
        );
        uploadedIds[key] = uploaded.id;
      }

      const id =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `song_${Date.now()}`;
      await apiPut("/api/v1/live/songs/{identity}", {
        pathParams: { identity: id },
        body: {
          id,
          title: songTitle.trim(),
          artist: songArtist.trim(),
          ...uploadedIds,
          lyrics,
          license_note: songLicense,
          live_allowed: songLiveAllowed,
          commercial_allowed: songCommercial,
        },
      });

      closeDialog();
      songFormRef.current?.reset();
      setSongTitle("");
      setSongArtist("");
      setSongLyrics("");
      setSongLicense("");
      setSongLiveAllowed(false);
      setSongCommercial(false);
      setImportState("");
      await loadMusic();
      onFeedback?.("歌曲已保存，可以选择模式点歌。");
    } catch (error) {
      setImportState(errorMessage(error));
    } finally {
      setImporting(false);
    }
  };

  const singingAvailable = music?.singing.available === true;
  const singOptionLabel = singingAvailable ? "角色音色演唱" : "演唱（模型待接入）";
  const query = search.trim().toLowerCase();
  const filteredSongs = useMemo(
    () =>
      (music?.songs ?? []).filter((song) =>
        `${song.title} ${song.artist ?? ""}`.toLowerCase().includes(query)
      ),
    [music?.songs, query]
  );

  const liveEnabled = settings?.features?.live_enabled === true;

  return (
    <div data-live-page="music">
      {showWorkbenchHeading ? (
        <div className="page-heading win-heading">
          <h1>在线点歌与曲库配置</h1>
          <a href="#live" className="win-link-btn">
            返回直播控制台
          </a>
        </div>
      ) : null}

      <section className={`panel ${styles.musicSection}`}>
        <div className="panel-heading">
          <h1>曲库与点歌</h1>
          <button type="button" id="import-song" onClick={openDialog}>
            ＋ 导入歌曲
          </button>
        </div>

        <div className={styles.songTools}>
          <label className={styles.srOnly} htmlFor="song-search">
            搜索曲库
          </label>
          <input
            id="song-search"
            type="search"
            placeholder="搜索歌曲或演唱者"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <label>
            本次模式
            <select
              id="request-mode"
              value={requestMode}
              onChange={(e) => setRequestMode(e.target.value as "direct" | "sing")}
            >
              <option value="direct">直接播放</option>
              <option value="sing" disabled={!singingAvailable}>
                {singOptionLabel}
              </option>
            </select>
          </label>
        </div>

        <p id="singing-state" className="hint">
          {music?.singing.reason ?? ""}
        </p>

        <div id="songs" className={styles.songList}>
          {filteredSongs.length > 0 ? (
            filteredSongs.map((song) => (
              <div key={song.id} className={styles.songRow}>
                <span className={styles.songIcon}>♫</span>
                <div className={styles.songInfo}>
                  <strong>{song.title}</strong>
                  <small>
                    {song.artist || "未填写演唱者"} ·{" "}
                    {song.live_allowed ? "已确认直播用途" : "待确认用途"}
                    {song.vocal ? " · 已有人声轨" : " · 口型待人声轨"}
                  </small>
                </div>
                <button
                  type="button"
                  disabled={!song.live_allowed || !liveEnabled || busyAction}
                  onClick={() => void handleRequestSong(song)}
                >
                  点歌
                </button>
                <button
                  type="button"
                  className={styles.deleteSongBtn}
                  aria-label={`移除歌曲 ${song.title}`}
                  disabled={busyAction}
                  onClick={() => void handleRemoveSong(song)}
                >
                  ×
                </button>
              </div>
            ))
          ) : (
            <div className={styles.emptyState}>
              <span>♫</span>
              <div>{query ? "没有找到这首歌" : "曲库还是空的，导入一首获准使用的歌吧。"}</div>
            </div>
          )}
        </div>

        <details className={styles.musicSettings}>
          <summary>点歌设置</summary>
          <form
            id="music-form"
            onInput={() => setMusicDirty(true)}
            onSubmit={(e) => void handleSaveMusicSettings(e)}
          >
            <div className={styles.fieldsGrid}>
              <label>
                默认模式
                <select
                  id="default-mode"
                  value={defaultMode}
                  onChange={(e) => setDefaultMode(e.target.value as "direct" | "sing")}
                >
                  <option value="direct">直接播放</option>
                  <option value="sing" disabled={!singingAvailable}>
                    {singOptionLabel}
                  </option>
                </select>
              </label>
              <label>
                音乐音量
                <input
                  id="music-volume"
                  type="number"
                  step="0.05"
                  min={0}
                  max={1}
                  value={musicVolume}
                  onChange={(e) => setMusicVolume(e.target.value)}
                />
              </label>
              <label>
                点歌队列上限
                <input
                  id="music-limit"
                  type="number"
                  min={1}
                  max={100}
                  value={musicLimit}
                  onChange={(e) => setMusicLimit(e.target.value)}
                />
              </label>
              <label>
                同用户冷却（秒）
                <input
                  id="music-cooldown"
                  type="number"
                  min={0}
                  max={3600}
                  value={musicCooldown}
                  onChange={(e) => setMusicCooldown(e.target.value)}
                />
              </label>
            </div>
            <button type="submit" disabled={!music || busyAction}>
              保存点歌设置
            </button>
          </form>
        </details>
      </section>

      <OnlineSongRequests visible={visible} onFeedback={onFeedback} />

      <dialog id="song-dialog" ref={dialogRef} className={styles.songDialog}>
        <form id="song-form" ref={songFormRef} onSubmit={(e) => void handleImportSubmit(e)}>
          <div className="panel-heading">
            <div>
              <p className="eyebrow">添加到曲库</p>
              <h2>导入歌曲</h2>
            </div>
            <button type="button" id="close-import" aria-label="关闭" onClick={closeDialog}>
              ✕
            </button>
          </div>
          <div className={styles.fieldsGrid}>
            <label>
              歌曲名
              <input
                id="song-title"
                required
                maxLength={100}
                value={songTitle}
                onChange={(e) => setSongTitle(e.target.value)}
              />
            </label>
            <label>
              演唱者／来源
              <input
                id="song-artist"
                maxLength={100}
                value={songArtist}
                onChange={(e) => setSongArtist(e.target.value)}
              />
            </label>
          </div>
          <label>
            歌曲音频 <span className="hint">WAV／FLAC／MP3 · 最长10分钟 · 96MiB以内</span>
            <input
              ref={audioInputRef}
              id="song-file"
              type="file"
              accept=".wav,.flac,.mp3,audio/wav,audio/flac,audio/mpeg"
              required
            />
          </label>
          <details>
            <summary>人声、伴奏与歌词</summary>
            <p className="hint">独立人声用于跟唱口型；伴奏作为后续演唱模型接口的素材预留。</p>
            <label>
              独立人声 WAV
              <input ref={vocalInputRef} id="vocal-file" type="file" accept=".wav,audio/wav" />
            </label>
            <label>
              伴奏 WAV
              <input
                ref={accompanimentInputRef}
                id="accompaniment-file"
                type="file"
                accept=".wav,audio/wav"
              />
            </label>
            <label>
              同步歌词（LRC）
              <textarea
                id="song-lyrics"
                rows={4}
                placeholder="[00:12.00]第一句歌词"
                value={songLyrics}
                onChange={(e) => setSongLyrics(e.target.value)}
              />
            </label>
          </details>
          <label>
            来源与用途许可说明
            <textarea
              id="song-license"
              rows={2}
              required
              maxLength={500}
              placeholder="记录来源、授权范围或自有作品说明"
              value={songLicense}
              onChange={(e) => setSongLicense(e.target.value)}
            />
          </label>
          <label className="check-field">
            <input
              id="song-live-allowed"
              type="checkbox"
              checked={songLiveAllowed}
              onChange={(e) => setSongLiveAllowed(e.target.checked)}
            />
            已确认可用于目标直播场景
          </label>
          <label className="check-field">
            <input
              id="song-commercial"
              type="checkbox"
              checked={songCommercial}
              onChange={(e) => setSongCommercial(e.target.checked)}
            />
            已确认允许商业用途
          </label>
          <p id="import-state" role="status">
            {importState}
          </p>
          <div className="actions">
            <button type="submit" className="primary" id="import-submit" disabled={importing}>
              上传并保存歌曲
            </button>
            <button type="button" id="cancel-import" onClick={closeDialog}>
              取消
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
