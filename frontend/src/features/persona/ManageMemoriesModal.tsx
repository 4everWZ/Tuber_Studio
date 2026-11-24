import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./Personalization.module.css";
import {
  MEMORIES_CHANGED_EVENT,
  type ClearUnifiedMemoriesResponse,
  type PurgeMigratedMemoriesResponse,
  type UnifiedMemoryItem,
  type UnifiedMemoryListResponse,
} from "./types";

export type MemoryScopeFilter = "all" | "owner" | "public";

export interface ManageMemoriesModalProps {
  open: boolean;
  initialScope?: MemoryScopeFilter;
  highlightMemoryId?: string | null;
  highlightSnippet?: string | null;
  onClose: () => void;
  onChanged?: (stats: {
    count: number;
    total_count?: number;
    limit: number;
    over_limit: boolean;
    legacy_migrated_count: number;
  }) => void;
}

const DEFAULT_LIMIT = 100;

const SOURCE_LABELS: Record<string, string> = {
  manual: "手动添加",
  tool: "对话记住",
  chat: "对话记住",
  operation: "互动记住",
};

function normalizeScopeFilter(value: string | null | undefined): MemoryScopeFilter {
  if (value === "owner" || value === "public") return value;
  return "all";
}

function isMigratedRecord(item: UnifiedMemoryItem): boolean {
  return item.id.startsWith("mig:") || Boolean(item.source_key && item.source_key.startsWith("mig:"));
}

function formatMemoryTime(seconds: number | undefined): string {
  if (!seconds || !Number.isFinite(seconds)) return "";
  return new Date(seconds * 1000).toLocaleDateString("zh-CN", {
    month: "short",
    day: "numeric",
  });
}

function emitMemoriesChanged(stats: {
  count: number;
  total_count?: number;
  limit: number;
  over_limit: boolean;
  legacy_migrated_count: number;
}) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(MEMORIES_CHANGED_EVENT, { detail: stats }));
  }
  if (typeof document !== "undefined") {
    document.dispatchEvent(new Event("owner-memory-changed"));
  }
}

export function ManageMemoriesModal({
  open,
  initialScope = "all",
  highlightMemoryId = null,
  highlightSnippet = null,
  onClose,
  onChanged,
}: ManageMemoriesModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const loadSeqRef = useRef(0);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  const [scopeTab, setScopeTab] = useState<MemoryScopeFilter>(normalizeScopeFilter(initialScope));
  const [searchQuery, setSearchQuery] = useState("");
  const [items, setItems] = useState<UnifiedMemoryItem[]>([]);
  const [count, setCount] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [overLimit, setOverLimit] = useState(false);
  const [legacyMigratedCount, setLegacyMigratedCount] = useState(0);

  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [errorText, setErrorText] = useState("");

  const [newContent, setNewContent] = useState("");
  const [newScope, setNewScope] = useState<"owner" | "public">(
    initialScope === "public" ? "public" : "owner"
  );

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [confirmClearOpen, setConfirmClearOpen] = useState(false);
  const [activeHighlightId, setActiveHighlightId] = useState<string | null>(highlightMemoryId);

  const applyServerSnapshot = useCallback(
    (res: UnifiedMemoryListResponse, { notifyGlobal = false }: { notifyGlobal?: boolean } = {}) => {
      const nextItems = Array.isArray(res.items) ? res.items : [];
      const activeFallback = nextItems.filter((item) => item.active).length;
      const nextCount = typeof res.count === "number" ? res.count : activeFallback;
      const nextTotal = typeof res.total_count === "number" ? res.total_count : nextItems.length;
      const nextLimit = typeof res.limit === "number" && res.limit > 0 ? res.limit : DEFAULT_LIMIT;
      const nextOverLimit =
        typeof res.over_limit === "boolean" ? res.over_limit : nextCount > nextLimit;
      const nextLegacy =
        typeof res.legacy_migrated_count === "number"
          ? res.legacy_migrated_count
          : nextItems.filter(isMigratedRecord).length;

      setItems(nextItems);
      setCount(nextCount);
      setTotalCount(nextTotal);
      setLimit(nextLimit);
      setOverLimit(nextOverLimit);
      setLegacyMigratedCount(nextLegacy);

      const snapshot = {
        count: nextCount,
        total_count: nextTotal,
        limit: nextLimit,
        over_limit: nextOverLimit,
        legacy_migrated_count: nextLegacy,
      };
      onChangedRef.current?.(snapshot);
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent(MEMORIES_CHANGED_EVENT, { detail: snapshot }));
      }
      if (notifyGlobal) {
        emitMemoriesChanged(snapshot);
      }
    },
    []
  );

  const loadMemories = useCallback(
    async ({ notifyGlobal = false }: { notifyGlobal?: boolean } = {}) => {
      const seq = ++loadSeqRef.current;
      setLoading(true);
      setErrorText("");
      try {
        const res = await apiGet<"/api/v1/persona/memories", UnifiedMemoryListResponse>(
          "/api/v1/persona/memories",
          {
            query: { include_inactive: true },
          }
        );
        if (seq !== loadSeqRef.current) return;
        applyServerSnapshot(res, { notifyGlobal });
      } catch (err) {
        if (seq !== loadSeqRef.current) return;
        setErrorText(`读取记忆失败：${errorMessage(err)}`);
      } finally {
        if (seq === loadSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [applyServerSnapshot]
  );

  // Sync dialog open/close and incoming open props
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      const resolvedScope = normalizeScopeFilter(initialScope);
      setScopeTab(resolvedScope);
      if (resolvedScope === "owner" || resolvedScope === "public") {
        setNewScope(resolvedScope);
      }
      setActiveHighlightId(highlightMemoryId || null);
      setConfirmClearOpen(false);
      setStatusMessage("");
      setErrorText("");

      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      void loadMemories();
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
      setEditingId(null);
      setEditingText("");
      setConfirmClearOpen(false);
    }
  }, [open, initialScope, highlightMemoryId, loadMemories]);

  // Resolve highlight target from snippet when memoryId wasn't provided
  useEffect(() => {
    if (!open) return;
    if (highlightMemoryId) {
      setActiveHighlightId(highlightMemoryId);
      return;
    }
    const needle = (highlightSnippet || "").trim().toLowerCase();
    if (!needle) {
      setActiveHighlightId(null);
      return;
    }
    const found = items.find((item) => item.content.toLowerCase().includes(needle));
    setActiveHighlightId(found ? found.id : null);
  }, [open, highlightMemoryId, highlightSnippet, items]);

  const scopeCounts = useMemo(() => {
    let owner = 0;
    let pub = 0;
    for (const item of items) {
      if (item.scope === "owner") owner += 1;
      else if (item.scope === "public") pub += 1;
    }
    return { all: items.length, owner, public: pub };
  }, [items]);

  const filteredItems = useMemo(() => {
    const needle = searchQuery.trim().toLowerCase();
    return items.filter((item) => {
      if (scopeTab !== "all" && item.scope !== scopeTab) return false;
      if (!needle) return true;
      const tagMatch = (item.tags || []).some((tag) => tag.toLowerCase().includes(needle));
      return item.content.toLowerCase().includes(needle) || tagMatch;
    });
  }, [items, scopeTab, searchQuery]);

  async function handleAddMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = newContent.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setErrorText("");
    setStatusMessage("");
    try {
      await apiPost("/api/v1/persona/memories", {
        body: { content: trimmed, scope: newScope, tags: [] },
      });
      setNewContent("");
      setStatusMessage("已添加记忆。");
      await loadMemories({ notifyGlobal: true });
    } catch (err) {
      setErrorText(`添加记忆失败：${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleActive(item: UnifiedMemoryItem) {
    if (busy) return;
    setBusy(true);
    setErrorText("");
    setStatusMessage("");
    try {
      await apiPatch("/api/v1/persona/memories/{memory_id}", {
        pathParams: { memory_id: item.id },
        body: { active: !item.active },
      });
      setStatusMessage(item.active ? "已停用该条记忆。" : "已启用该条记忆。");
      await loadMemories({ notifyGlobal: true });
    } catch (err) {
      setErrorText(`更新记忆状态失败：${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveEdit(memoryId: string) {
    const trimmed = editingText.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setErrorText("");
    setStatusMessage("");
    try {
      await apiPatch("/api/v1/persona/memories/{memory_id}", {
        pathParams: { memory_id: memoryId },
        body: { content: trimmed },
      });
      setEditingId(null);
      setEditingText("");
      setStatusMessage("已更新记忆内容。");
      await loadMemories({ notifyGlobal: true });
    } catch (err) {
      setErrorText(`更新记忆失败：${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(memoryId: string) {
    if (busy) return;
    setBusy(true);
    setErrorText("");
    setStatusMessage("");
    try {
      await apiDelete("/api/v1/persona/memories/{memory_id}", {
        pathParams: { memory_id: memoryId },
      });
      if (editingId === memoryId) {
        setEditingId(null);
        setEditingText("");
      }
      setStatusMessage("已删除记忆。");
      await loadMemories({ notifyGlobal: true });
    } catch (err) {
      setErrorText(`删除记忆失败：${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handlePurgeMigrated() {
    if (busy || legacyMigratedCount <= 0) return;
    setBusy(true);
    setErrorText("");
    setStatusMessage("");
    try {
      const res = await apiPost<
        "/api/v1/persona/memories/purge-migrated",
        PurgeMigratedMemoriesResponse
      >("/api/v1/persona/memories/purge-migrated");
      setStatusMessage(`已清理 ${res.purged ?? 0} 条历史遗留迁移记录。`);
      await loadMemories({ notifyGlobal: true });
    } catch (err) {
      setErrorText(`清理迁移记录失败：${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirmClearAll() {
    if (busy) return;
    setBusy(true);
    setErrorText("");
    setStatusMessage("");
    try {
      const res = await apiPost<
        "/api/v1/persona/memories/clear",
        ClearUnifiedMemoriesResponse
      >("/api/v1/persona/memories/clear");
      setConfirmClearOpen(false);
      setEditingId(null);
      setEditingText("");
      setStatusMessage(`已清空 ${res.deleted ?? 0} 条记忆。`);
      await loadMemories({ notifyGlobal: true });
    } catch (err) {
      setErrorText(`清空记忆失败：${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const ratioPercent = Math.min(100, Math.round((count / Math.max(1, limit)) * 100));
  const nearLimit = !overLimit && ratioPercent >= 85;

  return (
    <dialog
      ref={dialogRef}
      id="manage-memories-dialog"
      className={styles.memoryModal}
      aria-labelledby="manage-memories-title"
      onClick={(event) => {
        if (event.target === dialogRef.current) {
          onClose();
        }
      }}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
    >
      <header className={styles.memoryModalHeader}>
        <div>
          <h2 id="manage-memories-title" className={styles.dialogTitle}>
            已保存的记忆
          </h2>
          <p className={styles.modalSubtitle}>
            Aqua 会按作用域读取启用中的记忆：号主记忆仅私聊可见，公开互动记忆用于评论与动态。
          </p>
        </div>
        <button
          type="button"
          className={styles.closeButton}
          aria-label="关闭管理记忆"
          onClick={onClose}
        >
          ×
        </button>
      </header>

      <div className={styles.memoryModalBody}>
        {/* Capacity Meter */}
        <section
          className={`${styles.capacityCard} ${overLimit ? styles.capacityCardWarning : ""}`.trim()}
          aria-label="记忆容量"
        >
          <div className={styles.capacityHeader}>
            <span className={styles.capacityLabel}>
              容量使用情况
              {totalCount > count ? ` · 另有 ${totalCount - count} 条已停用` : ""}
            </span>
            <strong
              id="memory-capacity-counter"
              className={overLimit ? styles.capacityCountWarning : styles.capacityCount}
            >
              {count} / {limit}
            </strong>
          </div>
          <div
            className={styles.capacityTrack}
            role="progressbar"
            aria-label="已保存记忆容量"
            aria-valuenow={count}
            aria-valuemin={0}
            aria-valuemax={limit}
          >
            <div
              className={[
                styles.capacityFill,
                overLimit ? styles.capacityFillDanger : nearLimit ? styles.capacityFillWarn : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{ width: `${ratioPercent}%` }}
            />
          </div>
          {overLimit && (
            <p className={styles.capacityWarningText} role="alert">
              已超过建议上限（{limit} 条），建议精简或删除不再需要的旧记忆，避免背景提示过长。
            </p>
          )}
        </section>

        {/* Legacy Migrated Banner */}
        {legacyMigratedCount > 0 && (
          <div className={styles.legacyBanner} role="region" aria-label="历史遗留迁移记录">
            <div className={styles.legacyBannerText}>
              <strong>检测到 {legacyMigratedCount} 条历史遗留迁移记录</strong>
              <small>这些是由早期版本迁移导入的条目，清理后不会影响当前手动或对话新增记忆。</small>
            </div>
            <button
              type="button"
              id="purge-migrated-memories"
              className={styles.legacyPurgeButton}
              disabled={busy}
              onClick={() => void handlePurgeMigrated()}
            >
              清理历史遗留迁移记录 ({legacyMigratedCount}条)
            </button>
          </div>
        )}

        {/* Filter & Search Bar */}
        <div className={styles.modalControlsRow}>
          <div className={styles.pillTabList} role="tablist" aria-label="记忆作用域筛选">
            {(
              [
                ["all", `全部 (${scopeCounts.all})`],
                ["owner", `仅号主 (${scopeCounts.owner})`],
                ["public", `公开互动 (${scopeCounts.public})`],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={scopeTab === key}
                className={`${styles.pillTab} ${scopeTab === key ? styles.pillTabActive : ""}`.trim()}
                onClick={() => {
                  setScopeTab(key);
                  if (key === "owner" || key === "public") {
                    setNewScope(key);
                  }
                }}
              >
                {label}
              </button>
            ))}
          </div>

          <input
            ref={searchInputRef}
            type="search"
            className={styles.searchInput}
            aria-label="搜索记忆"
            placeholder="搜索记忆内容或标签…"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
          />
        </div>

        {/* Inline Add Form */}
        <form className={styles.addMemoryForm} onSubmit={(e) => void handleAddMemory(e)}>
          <select
            aria-label="新增记忆作用域"
            className={styles.scopeSelect}
            value={newScope}
            disabled={busy}
            onChange={(event) => setNewScope(event.target.value as "owner" | "public")}
          >
            <option value="owner">仅号主</option>
            <option value="public">公开互动</option>
          </select>
          <input
            type="text"
            className={styles.addMemoryInput}
            aria-label="新增记忆内容"
            placeholder="输入希望 Aqua 长期记住的偏好或事实…"
            value={newContent}
            disabled={busy}
            onChange={(event) => setNewContent(event.target.value)}
          />
          <button
            type="submit"
            className="primary"
            disabled={busy || !newContent.trim()}
          >
            添加记忆
          </button>
        </form>

        {highlightSnippet && !activeHighlightId && !loading && (
          <p className={styles.highlightHint}>
            来自对话更新：{highlightSnippet}
          </p>
        )}

        {statusMessage && (
          <p className={styles.modalStatus} role="status">
            {statusMessage}
          </p>
        )}
        {errorText && (
          <p className="error-text" role="alert">
            {errorText}
          </p>
        )}

        {/* Memory Items List */}
        {loading && items.length === 0 ? (
          <div className={styles.emptyState}>正在读取记忆…</div>
        ) : filteredItems.length === 0 ? (
          <div className={styles.emptyState}>
            {items.length === 0
              ? "还没有已保存的记忆。可以在上方直接添加，或在对话中让 Aqua 帮你记住。"
              : "当前筛选条件下没有匹配的记忆。"}
          </div>
        ) : (
          <ul className={styles.memoryList} aria-label="记忆条目列表">
            {filteredItems.map((item) => {
              const isEditing = editingId === item.id;
              const isHighlighted = activeHighlightId === item.id;
              const migrated = isMigratedRecord(item);
              const metaLabel = [
                migrated ? "历史迁移" : SOURCE_LABELS[item.source] || item.source,
                formatMemoryTime(item.updated_at),
              ]
                .filter(Boolean)
                .join(" · ");

              return (
                <li
                  key={item.id}
                  data-memory-id={item.id}
                  data-highlighted={isHighlighted ? "true" : undefined}
                  className={[
                    styles.memoryItem,
                    !item.active ? styles.memoryItemInactive : "",
                    isHighlighted ? styles.memoryItemHighlighted : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  <div className={styles.memoryItemMain}>
                    <div className={styles.memoryItemBadges}>
                      <span
                        className={`${styles.scopeBadge} ${
                          item.scope === "owner" ? styles.scopeBadgeOwner : styles.scopeBadgePublic
                        }`}
                      >
                        {item.scope === "owner" ? "仅号主" : "公开互动"}
                      </span>
                      {isHighlighted && <span className={styles.recentBadge}>刚更新</span>}
                      {metaLabel && <span className={styles.memoryMeta}>{metaLabel}</span>}
                    </div>

                    {isEditing ? (
                      <div className={styles.editRow}>
                        <input
                          type="text"
                          aria-label="编辑记忆内容"
                          value={editingText}
                          disabled={busy}
                          onChange={(event) => setEditingText(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              void handleSaveEdit(item.id);
                            } else if (event.key === "Escape") {
                              setEditingId(null);
                              setEditingText("");
                            }
                          }}
                        />
                        <button
                          type="button"
                          className="primary"
                          disabled={busy || !editingText.trim()}
                          onClick={() => void handleSaveEdit(item.id)}
                        >
                          保存
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            setEditingId(null);
                            setEditingText("");
                          }}
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <p className={styles.memoryContent}>{item.content}</p>
                    )}

                    {!isEditing && item.tags && item.tags.length > 0 && (
                      <div className={styles.tagRow}>
                        {item.tags.map((tag) => (
                          <span key={tag} className={styles.memoryTag}>
                            #{tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  {!isEditing && (
                    <div className={styles.memoryItemActions}>
                      <button
                        type="button"
                        className={styles.subtleActionBtn}
                        disabled={busy}
                        aria-pressed={item.active}
                        onClick={() => void handleToggleActive(item)}
                      >
                        {item.active ? "已启用" : "已停用"}
                      </button>
                      <button
                        type="button"
                        className={styles.subtleActionBtn}
                        disabled={busy}
                        onClick={() => {
                          setEditingId(item.id);
                          setEditingText(item.content);
                        }}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        className={`${styles.subtleActionBtn} ${styles.deleteActionBtn}`}
                        disabled={busy}
                        onClick={() => void handleDelete(item.id)}
                      >
                        删除
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <footer className={styles.memoryModalFooter}>
        {confirmClearOpen ? (
          <div className={styles.clearConfirmBar} role="alertdialog" aria-label="确认清空记忆">
            <span>确定要清空 Aqua 的全部已保存记忆吗？此操作无法撤销。</span>
            <div className={styles.clearConfirmActions}>
              <button
                type="button"
                className="danger"
                id="confirm-clear-memories"
                disabled={busy}
                onClick={() => void handleConfirmClearAll()}
              >
                确认清空
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirmClearOpen(false)}
              >
                取消
              </button>
            </div>
          </div>
        ) : (
          <div className={styles.footerBar}>
            <button
              type="button"
              id="clear-all-memories"
              className="danger"
              disabled={busy || items.length === 0}
              onClick={() => setConfirmClearOpen(true)}
            >
              清空记忆
            </button>
            <button type="button" onClick={onClose}>
              完成
            </button>
          </div>
        )}
      </footer>
    </dialog>
  );
}
