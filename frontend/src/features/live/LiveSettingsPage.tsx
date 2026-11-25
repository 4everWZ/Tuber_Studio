import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { characterDisplayStore, useCharacterDisplay } from "@/shared/hooks/useCharacterDisplay";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./LiveWorkbench.module.css";
import type { LiveOptionsData, LiveViewData } from "./types";

export interface LiveSettingsPageProps {
  visible?: boolean;
  onFeedback?: (message: string, error?: boolean) => void;
}

export function LiveSettingsPage({ visible = true, onFeedback }: LiveSettingsPageProps) {
  const display = useCharacterDisplay();
  const [liveState, setLiveState] = useState<LiveViewData | null>(null);

  // Live options form state
  const [timezone, setTimezone] = useState("");
  const [idle, setIdle] = useState(false);
  const [idleText, setIdleText] = useState("");
  const [webFetch, setWebFetch] = useState(false);
  const [weatherEnabled, setWeatherEnabled] = useState(false);
  const [weatherName, setWeatherName] = useState("");
  const [weatherLat, setWeatherLat] = useState("");
  const [weatherLon, setWeatherLon] = useState("");
  const [optionsDirty, setOptionsDirty] = useState(false);
  const [savingOptions, setSavingOptions] = useState(false);

  // Poll form state
  const [pollTopic, setPollTopic] = useState("");
  const [pollOptions, setPollOptions] = useState("");
  const [pollDuration, setPollDuration] = useState("60");
  const [busyPoll, setBusyPoll] = useState(false);

  const pollingViewRef = useRef(false);

  useUnsavedGuard("live-options", optionsDirty);

  const applyOptions = useCallback((options: LiveOptionsData) => {
    setTimezone(options.timezone || "");
    setIdle(Boolean(options.idle));
    setIdleText(options.idle_text || "");
    setWebFetch(Boolean(options.web_fetch));
    setWeatherEnabled(Boolean(options.weather));
    setWeatherName(options.weather?.name || "");
    setWeatherLat(options.weather?.latitude != null ? String(options.weather.latitude) : "");
    setWeatherLon(options.weather?.longitude != null ? String(options.weather.longitude) : "");
    setOptionsDirty(false);
  }, []);

  useEffect(() => {
    if (!visible) return undefined;
    let active = true;
    void apiGet<"/api/v1/live/options", LiveOptionsData>("/api/v1/live/options")
      .then((opts) => {
        if (active) applyOptions(opts);
      })
      .catch((error) => {
        if (active) onFeedback?.(errorMessage(error), true);
      });

    if (!characterDisplayStore.getSnapshot().value) {
      void characterDisplayStore.refresh().catch(() => {});
    }
    return () => {
      active = false;
    };
  }, [visible, applyOptions, onFeedback]);

  const pollView = useCallback(async () => {
    if (!visible || pollingViewRef.current) return;
    if (typeof document !== "undefined" && document.hidden) return;
    pollingViewRef.current = true;
    try {
      const next = await apiGet<"/api/v1/live/view", LiveViewData>("/api/v1/live/view");
      setLiveState(next);
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      pollingViewRef.current = false;
    }
  }, [visible, onFeedback]);

  useEffect(() => {
    if (!visible) return undefined;
    void pollView();
    const timer = setInterval(() => {
      void pollView();
    }, 1000);
    return () => clearInterval(timer);
  }, [visible, pollView]);

  const handleSaveOptions = async (event: FormEvent) => {
    event.preventDefault();
    if (savingOptions) return;
    setSavingOptions(true);
    try {
      const weather = weatherEnabled
        ? {
            name: weatherName.trim(),
            latitude: Number(weatherLat),
            longitude: Number(weatherLon),
          }
        : null;
      const saved = await apiPut<"/api/v1/live/options", LiveOptionsData>("/api/v1/live/options", {
        body: {
          timezone: timezone.trim() || null,
          idle,
          idle_text: idleText.trim(),
          web_fetch: webFetch,
          weather,
        },
      });
      applyOptions(saved);
      onFeedback?.("直播设置已保存。");
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setSavingOptions(false);
    }
  };

  const handleStartPoll = async (event: FormEvent) => {
    event.preventDefault();
    if (busyPoll) return;
    setBusyPoll(true);
    try {
      await apiPost("/api/v1/live/poll/start", {
        body: {
          topic: pollTopic.trim(),
          options: pollOptions
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean),
          duration_seconds: Number(pollDuration),
        },
      });
      setPollTopic("");
      setPollOptions("");
      await pollView();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusyPoll(false);
    }
  };

  const handleFinishPoll = async () => {
    if (busyPoll) return;
    setBusyPoll(true);
    try {
      await apiPost("/api/v1/live/poll/finish");
      await pollView();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusyPoll(false);
    }
  };

  const activePoll = liveState?.poll;
  const latestActivity = activePoll || liveState?.activities?.at(-1);
  const pollStateText = !latestActivity
    ? "暂无投票"
    : activePoll
      ? `投票中 · ${latestActivity.topic} · ${latestActivity.options.join("／")}`
      : latestActivity.counts
        ? `${latestActivity.topic} · ${latestActivity.total_votes} 票 · ${Object.entries(
            latestActivity.counts
          )
            .map(([name, count]) => `${name} ${count}`)
            .join("／")}${latestActivity.complete ? "" : " · 统计未完整"}`
        : `${latestActivity.topic} · ${latestActivity.result || "已更新"}`;

  const canStartPoll = Boolean(
    liveState && ["live", "paused"].includes(liveState.state) && !activePoll && !busyPoll
  );
  const live2dAvailable = liveState?.live2d?.status === "available";

  return (
    <div data-live-page="settings">
      <section className={`panel ${styles.onlineSection}`} aria-labelledby="room-heading">
        <h1 id="room-heading">直播设置</h1>

        <details open>
          <summary>冷场与按需联网</summary>
          <form
            id="live-options-form"
            onInput={() => setOptionsDirty(true)}
            onSubmit={(e) => void handleSaveOptions(e)}
          >
            <label>
              时区
              <input
                id="live-timezone"
                placeholder="沿用运营时区"
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
              />
            </label>
            <label className="check-field">
              <input
                id="live-idle"
                type="checkbox"
                checked={idle}
                onChange={(e) => setIdle(e.target.checked)}
              />
              无人互动时说一句
            </label>
            <label>
              冷场台词（可选）
              <input
                id="live-idle-text"
                maxLength={120}
                value={idleText}
                onChange={(e) => setIdleText(e.target.value)}
              />
            </label>
            <label className="check-field">
              <input
                id="live-web-fetch"
                type="checkbox"
                checked={webFetch}
                onChange={(e) => setWebFetch(e.target.checked)}
              />
              允许按需读取公开网页
            </label>
            <label className="check-field">
              <input
                id="live-weather"
                type="checkbox"
                checked={weatherEnabled}
                onChange={(e) => setWeatherEnabled(e.target.checked)}
              />
              允许查询指定地点的天气
            </label>
            <fieldset id="live-weather-location" hidden={!weatherEnabled} disabled={!weatherEnabled}>
              <label>
                地点名称
                <input
                  id="live-weather-name"
                  maxLength={60}
                  required={weatherEnabled}
                  value={weatherName}
                  onChange={(e) => setWeatherName(e.target.value)}
                />
              </label>
              <div className={styles.fieldsGrid}>
                <label>
                  纬度
                  <input
                    id="live-weather-lat"
                    type="number"
                    min={-90}
                    max={90}
                    step="any"
                    required={weatherEnabled}
                    value={weatherLat}
                    onChange={(e) => setWeatherLat(e.target.value)}
                  />
                </label>
                <label>
                  经度
                  <input
                    id="live-weather-lon"
                    type="number"
                    min={-180}
                    max={180}
                    step="any"
                    required={weatherEnabled}
                    value={weatherLon}
                    onChange={(e) => setWeatherLon(e.target.value)}
                  />
                </label>
              </div>
              <p className="hint">
                数据来自{" "}
                <a href="https://open-meteo.com/" rel="noopener noreferrer" target="_blank">
                  Open-Meteo
                </a>
                ，用于非商业直播。
              </p>
            </fieldset>
            <button type="submit" disabled={savingOptions}>
              保存直播设置
            </button>
          </form>
        </details>

        <details>
          <summary>房间投票</summary>
          <p id="live-poll-state" role="status">
            {pollStateText}
          </p>
          <form id="live-poll-form" onSubmit={(e) => void handleStartPoll(e)}>
            <label>
              主题
              <input
                id="live-poll-topic"
                maxLength={80}
                required
                value={pollTopic}
                onChange={(e) => setPollTopic(e.target.value)}
              />
            </label>
            <label>
              选项
              <textarea
                id="live-poll-options"
                rows={3}
                required
                placeholder="每行一个选项"
                value={pollOptions}
                onChange={(e) => setPollOptions(e.target.value)}
              />
            </label>
            <label>
              时长（秒）
              <input
                id="live-poll-duration"
                type="number"
                min={1}
                max={600}
                required
                value={pollDuration}
                onChange={(e) => setPollDuration(e.target.value)}
              />
            </label>
            <div className="actions">
              <button id="live-poll-start" type="submit" disabled={!canStartPoll}>
                开始投票
              </button>
              <button
                id="live-poll-finish"
                type="button"
                disabled={!activePoll || busyPoll}
                onClick={() => void handleFinishPoll()}
              >
                结束投票
              </button>
            </div>
          </form>
        </details>

        <details>
          <summary>角色显示与形象</summary>
          <fieldset className="win-group-box">
            <legend>角色显示</legend>
            <label className="check-field">
              <input
                type="checkbox"
                data-display-setting="pets_enabled"
                checked={display.value?.pets_enabled === true}
                disabled={!display.value || display.saving}
                onChange={(e) => {
                  void display.save("pets_enabled", e.target.checked).catch((err) => {
                    onFeedback?.(errorMessage(err), true);
                  });
                }}
              />{" "}
              显示角色宠物
            </label>
            <label className="check-field">
              <input
                type="checkbox"
                data-display-setting="avatar_enabled"
                checked={live2dAvailable ? display.value?.avatar_enabled === true : false}
                disabled={!live2dAvailable || !display.value || display.saving}
                onChange={(e) => {
                  if (!live2dAvailable) return;
                  void display.save("avatar_enabled", e.target.checked).catch((err) => {
                    onFeedback?.(errorMessage(err), true);
                  });
                }}
              />{" "}
              直播 Avatar（TBD／暂不可用）
            </label>
            <p className="hint">
              宠物开关控制对话、电话和直播中的一只宠物。关闭后仍可通话和播放直播音频。
            </p>
          </fieldset>
        </details>
      </section>

      <section className={`panel ${styles.onlineSection}`} data-live-page="settings">
        <div className="panel-heading">
          <h2>媒体与资产缓存</h2>
          <a href="#cache-panel" className="win-link-btn">
            前往统一缓存设置 →
          </a>
        </div>
        <p className="hint">
          视频创作、音乐点歌、语音合成、浏览片段与其他预览资产的缓存上限和清理已统一收口到「设置 → 存储与维护」。
        </p>
      </section>
    </div>
  );
}
