import { useEffect, useState, type FormEvent } from "react";
import { useUnsavedGuard } from "@/shared";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./operations.module.css";
import type { DiscoverySourceConfig, DiscoveryStatus } from "./types";

const DISCOVERY_STATES: Record<string, string> = {
  off: "待命",
  starting: "启动检查",
  discovering: "正在找视频",
  reading: "正在取材",
  judging: "正在判断与写稿",
  publishing: "正在发送",
  reconciling: "正在查询原回执",
  finishing: "正在结束本轮",
  waiting: "轮间等待",
  blocked: "需要处理",
  draining: "当前批次收尾",
  stopping: "正在停止",
  emergency: "已紧急停止",
  unavailable: "状态不可用",
};

const ROUND_STATES: Record<string, string> = {
  completed: "已完成",
  partial: "部分完成",
  failed: "未完成",
  invalid: "输出无效，诊断已保存",
  cancelled: "已停止",
  paused: "已暂停，进度保留",
  deferred_daily: "今日观看次数已用完",
};

const SOURCE_LABELS: Record<string, string> = {
  recommend: "首页推荐",
  search: "关键词搜索",
  popular: "热门榜单",
  autonomous: "由当前角色自主选题",
  specified: "指定特定视频 BV",
};

function formatTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString("zh-CN");
}

export interface DiscoveryProgressDetailsProps {
  discovery: DiscoveryStatus | null;
}

export function DiscoveryProgressDetails({ discovery }: DiscoveryProgressDetailsProps) {
  const stateLabel = discovery?.state ? DISCOVERY_STATES[discovery.state] || "状态未知" : "状态未知";
  const active = discovery?.active_round;
  const round = active || discovery?.last_round;
  const attention = discovery?.attention || [];
  const timing = discovery?.next_at ? `下次运行 ${formatTime(discovery.next_at)}` : "";

  const resultParts = [
    discovery?.daily_watch && discovery.daily_watch.limit > 0 && discovery.daily_watch.remaining === 0
      ? "今日观看次数已用完，继续处理评论"
      : "",
    active
      ? [discovery.current_video?.title, stateLabel].filter(Boolean).join(" · ")
      : round
        ? `${ROUND_STATES[round.status] || "已结束"} · ${round.updated_at ? formatTime(round.updated_at) : ""}`
        : "还没有运行记录。",
    timing,
  ].filter(Boolean);

  const countsParts = round
    ? [
        discovery?.daily_watch && discovery.daily_watch.limit > 0
          ? `每日观看上限用量 ${discovery.daily_watch.started} / ${discovery.daily_watch.limit}`
          : "",
        discovery?.round_source ? SOURCE_LABELS[discovery.round_source] || "" : "",
        `已处理 ${discovery?.summary?.processed ?? 0} 个视频`,
        `已发送 ${discovery?.sent ?? 0} 条`,
        `已跳过 ${discovery?.summary?.skipped ?? 0} 个视频`,
      ].filter(Boolean)
    : [];

  return (
    <details id="operation-video-progress" className={styles.discoveryStatusCompact}>
      <summary>视频处理进度</summary>
      <div className="panel-heading">
        <span id="discovery-state" className="badge" role="status">
          {stateLabel}
        </span>
        {round && (
          <a id="discovery-round-link" href={`#automation/round/${encodeURIComponent(round.id)}`}>
            来源记录 →
          </a>
        )}
      </div>
      <span id="discovery-round-heading" className="hint">
        {!active && round ? "最近一轮" : "当前一轮"}
      </span>
      <p id="discovery-result" role="status">
        {resultParts.join(" · ")}
      </p>
      <p id="discovery-counts" className="hint">
        {countsParts.join(" · ")}
      </p>
      {attention.length > 0 && (
        <div id="discovery-attention">
          <ul id="discovery-actions">
            {attention.map((item, idx) => (
              <li key={`${item.href}-${idx}`}>
                {item.message} <a href={item.href}>{item.label}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </details>
  );
}

export interface DiscoverySourceFormProps {
  discovery: DiscoveryStatus | null;
  onSaveSource: (selection: DiscoverySourceConfig) => Promise<DiscoveryStatus>;
}

export function DiscoverySourceForm({ discovery, onSaveSource }: DiscoverySourceFormProps) {
  const [mode, setMode] = useState<DiscoverySourceConfig["mode"]>("recommend");
  const [keywords, setKeywords] = useState("");
  const [watchVideos, setWatchVideos] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");
  const [saveFailed, setSaveFailed] = useState(false);

  useUnsavedGuard("discovery-source-form", dirty);

  useEffect(() => {
    if (dirty || !discovery?.source) return;
    setMode(discovery.source.mode || "recommend");
    setKeywords((discovery.source.keywords || []).join("，"));
    setWatchVideos((discovery.source.watch_videos || []).join("，"));
  }, [discovery, dirty]);

  const unavailable = !discovery || discovery.state === "unavailable";
  const isSearch = mode === "search";
  const isSpecified = mode === "specified";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (unavailable || saving || !dirty) return;
    setSaving(true);
    setSaveFailed(false);
    const selection: DiscoverySourceConfig = {
      mode,
      keywords: isSearch
        ? keywords
            .split(/[,，\n]/)
            .map((word) => word.trim())
            .filter(Boolean)
        : [],
      watch_videos: isSpecified ? watchVideos.split(/[,，\s]+/).filter(Boolean) : [],
    };
    try {
      const updated = await onSaveSource({
        ...selection,
      });
      setDirty(false);
      setNote(updated.enabled ? "来源已保存，从下一轮起使用。" : "来源已保存。");
    } catch (err) {
      setSaveFailed(true);
      setNote(`来源保存失败：${errorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      id="discovery-source-form"
      className={styles.discoverySourceForm}
      data-workspace-anchor=""
      onSubmit={handleSubmit}
    >
      <div className={styles.formRowUnified}>
        <label>
          发现来源
          <select
            id="discovery-source"
            value={mode}
            disabled={unavailable || saving}
            onChange={(event) => {
              setMode(event.target.value as DiscoverySourceConfig["mode"]);
              setDirty(true);
              setSaveFailed(false);
              setNote(discovery?.enabled ? "来源有未保存的修改。" : "保存来源后即可开始。");
            }}
          >
            <option value="recommend">首页推荐</option>
            <option value="search">关键词搜索</option>
            <option value="popular">热门榜单</option>
            <option value="autonomous">由当前角色自主选题</option>
            <option value="specified">指定特定视频 BV</option>
          </select>
        </label>

        {isSearch && (
          <div id="discovery-keywords-field" style={{ flex: 1 }}>
            <label>
              关键词
              <input
                id="discovery-keywords"
                value={keywords}
                required={isSearch}
                disabled={unavailable || saving}
                placeholder="多个关键词用逗号隔开"
                onChange={(event) => {
                  setKeywords(event.target.value);
                  setDirty(true);
                  setSaveFailed(false);
                  setNote(discovery?.enabled ? "来源有未保存的修改。" : "保存来源后即可开始。");
                }}
              />
            </label>
          </div>
        )}

        {isSpecified && (
          <div id="discovery-videos-field" style={{ flex: 1 }}>
            <label>
              视频 BV 号
              <input
                id="discovery-videos"
                value={watchVideos}
                required={isSpecified}
                disabled={unavailable || saving}
                placeholder="多个 BV 号用逗号隔开"
                onChange={(event) => {
                  setWatchVideos(event.target.value);
                  setDirty(true);
                  setSaveFailed(false);
                  setNote(discovery?.enabled ? "来源有未保存的修改。" : "保存来源后即可开始。");
                }}
              />
            </label>
          </div>
        )}

        <button
          id="discovery-save-source"
          type="submit"
          className="win-btn"
          disabled={unavailable || saving || !dirty}
        >
          保存来源配置
        </button>
      </div>
      <p id="discovery-source-note" className={saveFailed ? "error-text" : "hint"} role="status">
        {note}
        {saveFailed && (
          <>
            {" "}
            <a href="#issues">查看原因</a>
          </>
        )}
      </p>
    </form>
  );
}
