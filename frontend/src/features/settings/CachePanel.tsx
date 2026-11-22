import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./OverviewPage.module.css";
import { formatCacheBytes, type CachePolicy, type CacheStatusResponse } from "./types";

const CACHE_CATEGORIES: ReadonlyArray<{
  key: keyof CacheStatusResponse["categories"];
  field: keyof CachePolicy;
  label: string;
  min: number;
  max: number;
}> = [
  { key: "music", field: "music_limit_mib", label: "音乐", min: 96, max: 15360 },
  { key: "speech", field: "speech_limit_mib", label: "语音", min: 16, max: 15360 },
  { key: "temporary", field: "temporary_limit_mib", label: "临时产物", min: 16, max: 4096 },
  { key: "browsing", field: "browsing_limit_mib", label: "浏览材料", min: 16, max: 4096 },
];

export function CachePanel({ className }: { className?: string }) {
  const [data, setData] = useState<CacheStatusResponse | null>(null);
  const [limits, setLimits] = useState<CachePolicy>({
    music_limit_mib: 15360,
    speech_limit_mib: 512,
    temporary_limit_mib: 1024,
    browsing_limit_mib: 512,
  });
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
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
      setLimits(next.settings);
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

  async function handleClearCategory(category: keyof CacheStatusResponse["categories"]) {
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

  return (
    <section
      className={["win-group-box", className].filter(Boolean).join(" ")}
      id="cache-panel"
      data-workspace-anchor=""
    >
      <div className="panel-heading">
        <h2>媒体缓存</h2>
        <button type="button" data-refresh="" disabled={busy} onClick={() => void refresh()}>
          刷新容量
        </button>
      </div>
      <p className="hint">
        点歌音乐（最高15 GiB）与生成的语音音频均有独立缓存上限与自动 LRU 淘汰机制，可以在此统一查看与清理。曲库、当前／待播、进行中的浏览任务与待审核证据受保护。模型、音色包、账号、审计、关系和私信原文不在此清理。
      </p>
      <form onSubmit={(event) => void handleSaveLimits(event)}>
        <div className={`cache-groups ${styles.cacheGroups}`}>
          {CACHE_CATEGORIES.map(({ key, field, label, min, max }) => {
            const stats = data?.categories?.[key];
            return (
              <div key={key} className={`cache-group ${styles.cacheGroup}`} data-category={key} hidden={!stats}>
                <h3>{label}</h3>
                <p data-stats="" className="hint">
                  {stats
                    ? `已用 ${formatCacheBytes(stats.bytes)} / ${formatCacheBytes(stats.limit_bytes)} · 可回收 ${formatCacheBytes(stats.reclaimable_bytes)} · 受保护 ${formatCacheBytes(stats.protected_bytes)}${stats.writing ? " · 正在写入" : ""}`
                    : ""}
                </p>
                <label>
                  {`${label}最大缓存（MiB，最高${max}）`}
                  <input
                    type="number"
                    min={min}
                    max={max}
                    step={1}
                    required
                    name={field}
                    value={limits[field]}
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
        <button type="submit" disabled={busy}>
          保存缓存上限
        </button>
        <p data-state="" className={statusMsg.error ? "hint error-text" : "hint"} role="status">
          {statusMsg.text}
        </p>
      </form>
    </section>
  );
}
