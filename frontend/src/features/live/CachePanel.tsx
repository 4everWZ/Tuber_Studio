import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./LiveWorkbench.module.css";
import type { CacheStatusData } from "./types";

const CACHE_GROUPS = [
  { key: "music", label: "音乐", min: 96, max: 15360 },
  { key: "speech", label: "语音", min: 16, max: 15360 },
  { key: "temporary", label: "临时产物", min: 16, max: 4096 },
  { key: "browsing", label: "浏览材料", min: 16, max: 4096 },
] as const;

function formatBytes(value: number): string {
  if (value >= 1073741824) return `${(value / 1073741824).toFixed(2)} GiB`;
  if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value} B`;
}

export interface CachePanelProps {
  visible?: boolean;
}

export function CachePanel({ visible = true }: CachePanelProps) {
  const [cacheData, setCacheData] = useState<CacheStatusData | null>(null);
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stateMessage, setStateMessage] = useState("");
  const [stateError, setStateError] = useState(false);

  const initializedRef = useRef(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  const applyCacheData = useCallback((next: CacheStatusData, forceFields = false) => {
    setCacheData(next);
    if (!initializedRef.current || forceFields || !dirtyRef.current) {
      if (!initializedRef.current || forceFields) {
        const nextLimits: Record<string, string> = {};
        for (const group of CACHE_GROUPS) {
          const val = next.settings?.[`${group.key}_limit_mib`];
          if (val !== undefined) nextLimits[`${group.key}_limit_mib`] = String(val);
        }
        setLimits(nextLimits);
        initializedRef.current = true;
      }
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!visible || busyRef.current) return;
    if (typeof document !== "undefined" && document.hidden) return;
    try {
      const next = await apiGet<"/api/v1/system/cache", CacheStatusData>("/api/v1/system/cache");
      applyCacheData(next, false);
    } catch (error) {
      setStateMessage(errorMessage(error));
      setStateError(true);
    }
  }, [visible, applyCacheData]);

  useEffect(() => {
    if (!visible) return undefined;
    void refresh();
    const timer = setInterval(() => {
      void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [visible, refresh]);

  const runAction = async (work: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try {
      await work();
    } catch (error) {
      setStateMessage(errorMessage(error));
      setStateError(true);
    } finally {
      setBusy(false);
    }
  };

  const handleClear = (key: string) => {
    void runAction(async () => {
      const next = await apiPost<"/api/v1/system/cache/{category}/clear", CacheStatusData>(
        "/api/v1/system/cache/{category}/clear",
        { pathParams: { category: key } }
      );
      applyCacheData(next, false);
      const freed = next.result?.freed_bytes ?? 0;
      const removed = next.result?.removed_files ?? 0;
      setStateMessage(`已回收 ${formatBytes(freed)}，清理 ${removed} 个文件。`);
      setStateError(false);
    });
  };

  const handleSave = (event: FormEvent) => {
    event.preventDefault();
    void runAction(async () => {
      const payload: Record<string, number> = {};
      for (const group of CACHE_GROUPS) {
        if (!cacheData?.categories?.[group.key]) continue;
        const fieldName = `${group.key}_limit_mib`;
        payload[fieldName] = Number(limits[fieldName] ?? cacheData.settings?.[fieldName] ?? group.max);
      }
      const next = await apiPut<"/api/v1/system/cache", CacheStatusData>("/api/v1/system/cache", {
        body: payload as never,
      });
      setDirty(false);
      applyCacheData(next, true);
      setStateMessage("容量设置已保存，超额可回收缓存已清理。");
      setStateError(false);
    });
  };

  return (
    <section
      className={`panel ${styles.onlineSection}`}
      id="cache-panel"
      data-live-page="settings"
    >
      <div className="panel-heading">
        <h2>媒体缓存</h2>
        <button
          type="button"
          data-refresh=""
          disabled={busy}
          onClick={() => void refresh()}
        >
          刷新容量
        </button>
      </div>
      <p className="hint">
        点歌音乐（最高15 GiB）与生成的语音音频均有独立缓存上限与自动 LRU
        淘汰机制，可以在此统一查看与清理。曲库、当前／待播、进行中的浏览任务与待审核证据受保护。模型、音色包、账号、审计、关系和私信原文不在此清理。
      </p>
      <form onSubmit={handleSave}>
        <div className={styles.cacheGroups}>
          {CACHE_GROUPS.map((group) => {
            const stats = cacheData?.categories?.[group.key];
            const fieldName = `${group.key}_limit_mib`;
            return (
              <div
                key={group.key}
                className={styles.cacheGroup}
                data-category={group.key}
                hidden={!stats}
              >
                <h3>{group.label}</h3>
                <p data-stats="" className="hint">
                  {stats
                    ? `已用 ${formatBytes(stats.bytes)} / ${formatBytes(stats.limit_bytes)} · 可回收 ${formatBytes(stats.reclaimable_bytes)} · 受保护 ${formatBytes(stats.protected_bytes)}${stats.writing ? " · 正在写入" : ""}`
                    : ""}
                </p>
                <label>
                  {group.label}最大缓存（MiB，最高{group.max}）
                  <input
                    type="number"
                    min={group.min}
                    max={group.max}
                    step={1}
                    required={Boolean(stats)}
                    name={fieldName}
                    value={limits[fieldName] ?? ""}
                    onChange={(e) => {
                      setDirty(true);
                      setLimits((prev) => ({ ...prev, [fieldName]: e.target.value }));
                    }}
                  />
                </label>
                <button
                  type="button"
                  aria-label={`清理${group.label}可回收缓存`}
                  disabled={busy}
                  onClick={() => handleClear(group.key)}
                >
                  清理可回收缓存
                </button>
              </div>
            );
          })}
        </div>
        <button type="submit" disabled={busy || !cacheData}>
          保存缓存上限
        </button>
        <p
          data-state=""
          role="status"
          className={stateError ? "error-text" : undefined}
        >
          {stateMessage}
        </p>
      </form>
    </section>
  );
}
