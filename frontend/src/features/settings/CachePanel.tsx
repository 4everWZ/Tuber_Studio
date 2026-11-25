import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./OverviewPage.module.css";
import {
  formatCacheBytes,
  type CacheCategoryKey,
  type CachePolicy,
  type CacheStatusResponse,
} from "./types";

const CACHE_CATEGORIES: ReadonlyArray<{
  key: CacheCategoryKey;
  field: keyof CachePolicy;
  label: string;
  desc: string;
  min: number;
  max: number;
}> = [
  {
    key: "video",
    field: "video_limit_mib",
    label: "视频创作产物",
    desc: "Remotion 成片 MP4、封面、逐句配音与帧缓存；渲染/发布中受保护，清理保留任务记录与回执",
    min: 64,
    max: 51200,
  },
  {
    key: "music",
    field: "music_limit_mib",
    label: "音乐",
    desc: "点歌、本地曲库与翻唱音频缓存；曲库与待播受保护",
    min: 96,
    max: 15360,
  },
  {
    key: "speech",
    field: "speech_limit_mib",
    label: "语音",
    desc: "TTS 合成语音缓存；当前与待播语音受保护",
    min: 16,
    max: 15360,
  },
  {
    key: "browsing",
    field: "browsing_limit_mib",
    label: "浏览材料",
    desc: "B 站观察记录与裁切视频片段；进行中与待审核证据受保护",
    min: 16,
    max: 8192,
  },
  {
    key: "temporary",
    field: "temporary_limit_mib",
    label: "临时产物",
    desc: "翻唱与临时处理输出目录；写入中的任务受保护",
    min: 16,
    max: 8192,
  },
  {
    key: "assets",
    field: "assets_limit_mib",
    label: "预览与其他资产",
    desc: "私聊预览录像等可再生媒体缓存；原始立绘、上传表情包库与模型永不清理",
    min: 16,
    max: 8192,
  },
  {
    key: "meme_inbox",
    field: "meme_inbox_limit_mib",
    label: "表情包收件箱缓存",
    desc: "自动采集的待审表情包候选图；按容量与条数上限自动回收，0 表示不限",
    min: 0,
    max: 8192,
  },
];

const DEFAULT_POLICY: CachePolicy = {
  video_limit_mib: 4096,
  music_limit_mib: 15360,
  speech_limit_mib: 512,
  browsing_limit_mib: 512,
  temporary_limit_mib: 1024,
  assets_limit_mib: 512,
  meme_inbox_limit_mib: 512,
  meme_inbox_max_items: 200,
};

function formatCacheLimit(limitBytes: number, unlimited?: boolean): string {
  if (unlimited || limitBytes === 0) return "不限";
  return formatCacheBytes(limitBytes);
}

export function CachePanel({ className }: { className?: string }) {
  const [data, setData] = useState<CacheStatusResponse | null>(null);
  const [limits, setLimits] = useState<CachePolicy>(DEFAULT_POLICY);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [clearAllConfirm, setClearAllConfirm] = useState(false);
  const [statusMsg, setStatusMsg] = useState<{ text: string; error: boolean }>({ text: "", error: false });

  const busyRef = useRef(false);
  const dirtyRef = useRef(false);
  const hasLoadedRef = useRef(false);
  busyRef.current = busy;
  dirtyRef.current = dirty;

  useUnsavedGuard("cache-panel", dirty);

  const applyCacheResponse = useCallback((next: CacheStatusResponse, overwriteFields = false) => {
    const first = !hasLoadedRef.current;
    hasLoadedRef.current = true;
    setData(next);
    if (first || overwriteFields || !dirtyRef.current) {
      setLimits((prev) => ({ ...prev, ...next.settings }));
      if (overwriteFields) {
        dirtyRef.current = false;
        setDirty(false);
      }
    }
  }, []);

  const refresh = useCallback(async () => {
    if (busyRef.current) return;
    try {
      const next = await apiGet<"/api/v1/system/cache", CacheStatusResponse>("/api/v1/system/cache");
      applyCacheResponse(next, false);
    } catch (error) {
      setStatusMsg({ text: errorMessage(error), error: true });
    }
  }, [applyCacheResponse]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function runAction(work: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await work();
    } catch (error) {
      setStatusMsg({ text: errorMessage(error), error: true });
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function handleClearCategory(category: CacheCategoryKey) {
    await runAction(async () => {
      const next = await apiPost<"/api/v1/system/cache/{category}/clear", CacheStatusResponse>(
        "/api/v1/system/cache/{category}/clear",
        { pathParams: { category } }
      );
      applyCacheResponse(next, false);
      const freed = next.result?.freed_bytes ?? 0;
      const removed = next.result?.removed_files ?? 0;
      setStatusMsg({
        text: `已回收 ${formatCacheBytes(freed)}，清理 ${removed} 个文件。`,
        error: false,
      });
    });
  }

  async function handleClearAll() {
    if (!clearAllConfirm) return;
    await runAction(async () => {
      const next = await apiPost<"/api/v1/system/cache/clear-all", CacheStatusResponse>(
        "/api/v1/system/cache/clear-all"
      );
      applyCacheResponse(next, false);
      setClearAllConfirm(false);
      const freed = next.result?.freed_bytes ?? 0;
      const removed = next.result?.removed_files ?? 0;
      setStatusMsg({
        text: `全部可回收缓存已清理：释放 ${formatCacheBytes(freed)}，共移除 ${removed} 个文件。`,
        error: false,
      });
    });
  }

  async function handleSaveLimits(event: FormEvent) {
    event.preventDefault();
    await runAction(async () => {
      const next = await apiPut<"/api/v1/system/cache", CacheStatusResponse, CachePolicy>(
        "/api/v1/system/cache",
        { body: limits }
      );
      applyCacheResponse(next, true);
      setStatusMsg({ text: "容量设置已保存，超额可回收缓存已清理。", error: false });
    });
  }

  const summary = data?.summary;

  return (
    <section
      className={["win-group-box", className].filter(Boolean).join(" ")}
      id="cache-panel"
      data-workspace-anchor=""
    >
      <div className="panel-heading">
        <h2>媒体与资产缓存管理</h2>
        <button type="button" data-refresh="" disabled={busy} onClick={() => void refresh()}>
          刷新容量
        </button>
      </div>
      <p className="hint">
        视频创作、音乐点歌、TTS 语音、浏览片段、临时产物与预览资产均在此统一设置缓存上限、查看占用并执行 LRU 回收。模型权重、原始角色立绘、上传表情包库、账号凭证及已发回执永不清理。
      </p>
      {summary && (
        <p data-cache-summary="" className="hint" style={{ fontWeight: 500 }}>
          {`总占用 ${formatCacheBytes(summary.bytes)} / 总上限 ${formatCacheLimit(summary.limit_bytes, summary.unlimited)} · 可回收总计 ${formatCacheBytes(summary.reclaimable_bytes)} · 受保护总计 ${formatCacheBytes(summary.protected_bytes)} · 文件总数 ${summary.files}`}
        </p>
      )}
      <form onSubmit={(event) => void handleSaveLimits(event)}>
        <div className={`cache-groups ${styles.cacheGroups}`}>
          {CACHE_CATEGORIES.map(({ key, field, label, desc, min, max }) => {
            const stats = data?.categories?.[key];
            return (
              <div key={key} className={`cache-group ${styles.cacheGroup}`} data-category={key} hidden={!stats}>
                <h3>{label}</h3>
                <p className="hint" style={{ marginTop: 0, marginBottom: 6 }}>
                  {desc}
                </p>
                <p data-stats="" className="hint">
                  {stats
                    ? `已用 ${formatCacheBytes(stats.bytes)} / ${formatCacheLimit(stats.limit_bytes, stats.unlimited)} · 文件 ${stats.files ?? 0}${
                        key === "meme_inbox" && typeof stats.max_items === "number"
                          ? `（上限 ${stats.max_items === 0 ? "不限" : `${stats.max_items} 条`}）`
                          : ""
                      } · 可回收 ${formatCacheBytes(stats.reclaimable_bytes)} · 受保护 ${formatCacheBytes(stats.protected_bytes)}${stats.writing ? " · 正在写入" : ""}`
                    : ""}
                </p>
                <label>
                  {`${label}最大缓存（MiB，${min}–${max}${min === 0 ? "，0 表示不限" : ""}）`}
                  <input
                    type="number"
                    min={min}
                    max={max}
                    step={1}
                    required
                    name={field}
                    value={limits[field] ?? DEFAULT_POLICY[field]}
                    onChange={(event) => {
                      dirtyRef.current = true;
                      setDirty(true);
                      setLimits((prev) => ({
                        ...prev,
                        [field]: event.target.value === "" ? prev[field] : Number(event.target.value),
                      }));
                    }}
                  />
                </label>
                {key === "meme_inbox" && (
                  <label style={{ marginTop: 8 }}>
                    {"表情包收件箱最大条数（0–10000，0 表示不限）"}
                    <input
                      type="number"
                      min={0}
                      max={10000}
                      step={1}
                      required
                      name="meme_inbox_max_items"
                      value={limits.meme_inbox_max_items ?? DEFAULT_POLICY.meme_inbox_max_items}
                      onChange={(event) => {
                        dirtyRef.current = true;
                        setDirty(true);
                        setLimits((prev) => ({
                          ...prev,
                          meme_inbox_max_items:
                            event.target.value === ""
                              ? prev.meme_inbox_max_items
                              : Number(event.target.value),
                        }));
                      }}
                    />
                  </label>
                )}
                <button
                  type="button"
                  style={{ marginTop: 8 }}
                  aria-label={`清理${label}可回收缓存`}
                  disabled={busy}
                  onClick={() => void handleClearCategory(key)}
                >
                  清理可回收缓存
                </button>
              </div>
            );
          })}
        </div>
        <div className="actions" style={{ marginTop: 8, flexWrap: "wrap", gap: 12 }}>
          <button type="submit" disabled={busy}>
            保存缓存上限
          </button>
          <label className={styles.inlineField}>
            <input
              id="cache-clear-all-confirm"
              type="checkbox"
              checked={clearAllConfirm}
              disabled={busy}
              onChange={(event) => setClearAllConfirm(event.target.checked)}
            />
            确认清理全部可回收媒体与资产缓存
          </label>
          <button
            id="cache-clear-all-btn"
            type="button"
            disabled={busy || !clearAllConfirm}
            onClick={() => void handleClearAll()}
          >
            一键清理全部可回收缓存
          </button>
        </div>
        <p data-state="" className={statusMsg.error ? "hint error-text" : "hint"} role="status">
          {statusMsg.text}
        </p>
      </form>
    </section>
  );
}
