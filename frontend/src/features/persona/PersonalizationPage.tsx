import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { occupancyText, tokenCount } from "@/shared/lib/display";
import { errorMessage } from "@/shared/lib/errors";
import { RememberButton } from "@/shared/ui/RememberButton";
import { MemorySummaryDialog } from "./MemorySummaryDialog";
import styles from "./Personalization.module.css";
import { RecentEventsDialog } from "./RecentEventsDialog";
import type {
  MemoryPreferenceState,
  PersonaContextStatus,
  RecentEventItem,
  RecentEventsResponse,
  UnifiedMemoryItem,
} from "./types";

export function PersonalizationPage() {
  const { status, refreshStatus } = useAppSession();
  const [preference, setPreference] = useState<MemoryPreferenceState | null>(null);
  const [preferenceFailed, setPreferenceFailed] = useState(false);
  const [switchStatus, setSwitchStatus] = useState("");
  const [statement, setStatement] = useState("");
  const [savingPreference, setSavingPreference] = useState(false);

  const [memoryOpen, setMemoryOpen] = useState(false);
  const [recentOpen, setRecentOpen] = useState(false);
  const [scopeVersion, setScopeVersion] = useState(0);
  const [pollGeneration, setPollGeneration] = useState(0);

  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState("");
  const [recentItems, setRecentItems] = useState<RecentEventItem[]>([]);

  // Context status / rebuild / compact tools (collapsed by default)
  const [ctxVideoId, setCtxVideoId] = useState("");
  const [ctxThreadId, setCtxThreadId] = useState("");
  const [ctxStatus, setCtxStatus] = useState<PersonaContextStatus | null>(null);
  const [ctxFeedback, setCtxFeedback] = useState("");
  const [ctxBusy, setCtxBusy] = useState(false);

  // Unified ChatGPT-Web-style long-term memory CRUD
  const [unifiedScope, setUnifiedScope] = useState<"all" | "owner" | "public">("all");
  const [unifiedItems, setUnifiedItems] = useState<UnifiedMemoryItem[]>([]);
  const [unifiedLoading, setUnifiedLoading] = useState(false);
  const [unifiedStatus, setUnifiedStatus] = useState("");
  const [newMemoryScope, setNewMemoryScope] = useState<"owner" | "public">("owner");
  const [newMemoryContent, setNewMemoryContent] = useState("");
  const [unifiedBusy, setUnifiedBusy] = useState(false);
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState("");

  const preferenceRef = useRef<MemoryPreferenceState | null>(null);
  const savingPrefRef = useRef(false);
  const readingPrefSeqRef = useRef(0);
  const readingRecentSeqRef = useRef(0);
  const recentScopeRef = useRef<string | null>(null);
  const pendingDraftRef = useRef<{ scope: string; text: string } | undefined>(undefined);
  const statementRef = useRef("");
  const scopeVersionRef = useRef(0);

  const summaryOpenerRef = useRef<HTMLButtonElement | null>(null);
  const recentOpenerRef = useRef<HTMLButtonElement | null>(null);

  preferenceRef.current = preference;
  savingPrefRef.current = savingPreference;
  statementRef.current = statement;
  scopeVersionRef.current = scopeVersion;

  const applyPreference = useCallback((value: MemoryPreferenceState) => {
    preferenceRef.current = value;
    setPreference(value);
    setPreferenceFailed(false);
    setSwitchStatus("");

    const draft = pendingDraftRef.current;
    pendingDraftRef.current = undefined;
    if (draft && draft.scope === value.scope && value.enabled && !statementRef.current) {
      statementRef.current = draft.text;
      setStatement(draft.text);
    }
  }, []);

  const refreshScope = useCallback(
    (restoreSameScope = true) => {
      const currentPref = preferenceRef.current;
      if (!restoreSameScope) {
        pendingDraftRef.current = undefined;
      } else if (currentPref) {
        pendingDraftRef.current = { scope: currentPref.scope, text: statementRef.current };
      }
      readingPrefSeqRef.current += 1;
      readingRecentSeqRef.current += 1;
      preferenceRef.current = null;
      recentScopeRef.current = null;
      statementRef.current = "";
      setStatement("");
      setPreference(null);
      setSwitchStatus("");
      setScopeVersion((prev) => prev + 1);
    },
    []
  );

  const loadPreference = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (savingPrefRef.current) return;
      const readSeq = ++readingPrefSeqRef.current;
      try {
        const value = await apiGet<"/api/v1/persona/daily-memory/preferences", MemoryPreferenceState>(
          "/api/v1/persona/daily-memory/preferences"
        );
        if (readSeq !== readingPrefSeqRef.current || savingPrefRef.current) return;
        const currentPref = preferenceRef.current;
        if (
          (currentPref?.scope && currentPref.scope !== value.scope) ||
          (recentScopeRef.current && recentScopeRef.current !== value.scope)
        ) {
          refreshScope(true);
          applyPreference(value);
          return;
        }
        applyPreference(value);
      } catch (error) {
        if (readSeq !== readingPrefSeqRef.current || (background && preferenceRef.current)) return;
        setSwitchStatus(`读取记忆设置失败：${errorMessage(error)}`);
        setPreferenceFailed(true);
      }
    },
    [applyPreference, refreshScope]
  );

  const loadRecent = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      const readSeq = ++readingRecentSeqRef.current;
      if (!background && recentItems.length === 0) {
        setRecentLoading(true);
        setRecentError("");
      }
      try {
        const result = await apiGet<"/api/v1/persona/daily-memory/recent-events", RecentEventsResponse>(
          "/api/v1/persona/daily-memory/recent-events"
        );
        if (readSeq !== readingRecentSeqRef.current) return;
        const currentPref = preferenceRef.current;
        if (
          (currentPref && currentPref.scope !== result.scope) ||
          (recentScopeRef.current && recentScopeRef.current !== result.scope)
        ) {
          refreshScope(true);
          void loadPreference({ background: false });
          return;
        }
        recentScopeRef.current = result.scope;
        setRecentItems(Array.isArray(result.items) ? result.items : []);
        setRecentError("");
      } catch (error) {
        if (readSeq !== readingRecentSeqRef.current || (background && recentItems.length > 0)) return;
        setRecentError(`读取近期趣事失败：${errorMessage(error)}`);
      } finally {
        if (readSeq === readingRecentSeqRef.current) {
          setRecentLoading(false);
        }
      }
    },
    [recentItems.length, refreshScope, loadPreference]
  );

  const loadUnifiedMemories = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (!background) setUnifiedLoading(true);
      try {
        const res = await apiGet<
          "/api/v1/persona/memories",
          { character_id?: string; items?: UnifiedMemoryItem[] }
        >("/api/v1/persona/memories", {
          query: {
            include_inactive: true,
            ...(unifiedScope !== "all" ? { scope: unifiedScope } : {}),
          },
        });
        setUnifiedItems(Array.isArray(res.items) ? res.items : []);
        if (!background) setUnifiedStatus("");
      } catch (error) {
        if (!background) setUnifiedStatus(`读取统一记忆失败：${errorMessage(error)}`);
      } finally {
        if (!background) setUnifiedLoading(false);
      }
    },
    [unifiedScope]
  );

  // Initial load + scopeVersion reload
  useEffect(() => {
    void loadPreference({ background: false });
    void loadUnifiedMemories({ background: false });
  }, [scopeVersion, loadPreference, loadUnifiedMemories]);

  // 15s background polling matching legacy app.js
  useEffect(() => {
    const interval = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void loadPreference({ background: true });
      if (recentOpen) void loadRecent({ background: true });
      if (memoryOpen) setPollGeneration((prev) => prev + 1);
    }, 15_000);
    return () => clearInterval(interval);
  }, [loadPreference, loadRecent, recentOpen, memoryOpen]);

  // Refresh scope when character changes or owner memory changes
  const activeCharKey = status?.character ? `${status.character.id ?? ""}:${status.character.version ?? ""}` : "";
  const prevCharKeyRef = useRef(activeCharKey);
  useEffect(() => {
    if (prevCharKeyRef.current && activeCharKey && prevCharKeyRef.current !== activeCharKey) {
      refreshScope(true);
    }
    prevCharKeyRef.current = activeCharKey;
  }, [activeCharKey, refreshScope]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onMemoryChanged = () => {
      if (memoryOpen) setPollGeneration((prev) => prev + 1);
      void loadPreference({ background: true });
    };
    const onCharChanged = () => refreshScope(true);
    document.addEventListener("owner-memory-changed", onMemoryChanged);
    document.addEventListener("studio-character-change", onCharChanged);
    return () => {
      document.removeEventListener("owner-memory-changed", onMemoryChanged);
      document.removeEventListener("studio-character-change", onCharChanged);
    };
  }, [memoryOpen, loadPreference, refreshScope]);

  const handleScopeConflict = useCallback(() => {
    refreshScope(true);
  }, [refreshScope]);

  async function handlePreferenceToggle(nextEnabled: boolean, nextHistoryEnabled: boolean) {
    const old = preferenceRef.current;
    if (!old || savingPrefRef.current) return;
    const version = scopeVersionRef.current;
    savingPrefRef.current = true;
    setSavingPreference(true);
    setSwitchStatus("正在保存…");

    // Optimistic checkbox feedback
    setPreference({ ...old, enabled: nextEnabled, history_enabled: nextHistoryEnabled });

    let failure: unknown = null;
    try {
      const value = await apiPut<
        "/api/v1/persona/daily-memory/preferences",
        MemoryPreferenceState,
        MemoryPreferenceState
      >("/api/v1/persona/daily-memory/preferences", {
        body: {
          enabled: nextEnabled,
          scope: old.scope,
          history_enabled: nextHistoryEnabled,
        },
      });
      if (version === scopeVersionRef.current) {
        applyPreference(value);
      }
    } catch (error) {
      failure = error;
    } finally {
      savingPrefRef.current = false;
      setSavingPreference(false);
    }

    if (
      version !== scopeVersionRef.current ||
      (failure && typeof failure === "object" && (failure as { status?: number }).status === 409)
    ) {
      refreshScope(true);
      return;
    }

    if (failure) {
      setPreference(old);
      setSwitchStatus(`保存失败：${errorMessage(failure)}。可再次切换重试。`);
    } else if (memoryOpen) {
      setPollGeneration((prev) => prev + 1);
    }
  }

  async function runContextAction(action: "status" | "rebuild" | "compact_live" | "compact_comments") {
    if (ctxBusy) return;
    setCtxBusy(true);
    setCtxFeedback("");
    try {
      const video_id = ctxVideoId.trim();
      const thread_id = ctxThreadId.trim();
      if (action === "status") {
        const snapshot = await apiGet<"/api/v1/persona/context/status", PersonaContextStatus>(
          "/api/v1/persona/context/status",
          {
            query: video_id || thread_id ? { video_id, thread_id } : undefined,
          }
        );
        setCtxStatus(snapshot);
        setCtxFeedback("已读取上下文状态。");
      } else if (action === "rebuild") {
        if (!video_id || !thread_id) throw new Error("请同时指定视频与对话楼ID");
        const result = await apiPost<
          "/api/v1/persona/context/rebuild",
          { message?: string; detail?: string; rebuilt?: boolean; prefilled?: boolean; input_tokens?: number },
          { scope: string; video_id: string; thread_id: string }
        >("/api/v1/persona/context/rebuild", {
          body: { scope: "comments", video_id, thread_id },
        });
        let msg =
          result.message ||
          result.detail ||
          (result.rebuilt === false ? "本次未重建上下文，请检查该楼现存原文。" : `楼 ${video_id} / ${thread_id} 上下文已重建。`);
        if (result.prefilled === false) {
          msg += " 诊断：未执行真实模型 prefill。";
        } else if (result.prefilled === true) {
          msg += ` 模型 prefill 已完成${result.input_tokens != null ? `，输入 ${result.input_tokens} tokens` : ""}。`;
        }
        setCtxFeedback(msg);
      } else {
        const isComments = action === "compact_comments";
        if (isComments && (!video_id || !thread_id)) {
          throw new Error("请先填写视频 ID 与单个楼ID；不同楼上下文不会合并。");
        }
        const result = await apiPost<
          "/api/v1/persona/context/compact",
          { message?: string; detail?: string; compacted?: boolean },
          { scope: string; video_id?: string; thread_id?: string }
        >("/api/v1/persona/context/compact", {
          body: isComments ? { scope: "comments", video_id, thread_id } : { scope: "live" },
        });
        setCtxFeedback(
          result.message ||
            result.detail ||
            (result.compacted === false ? "没有可压缩的历史。" : "压缩请求已完成。")
        );
        await refreshStatus();
      }
    } catch (error) {
      setCtxFeedback(errorMessage(error));
    } finally {
      setCtxBusy(false);
    }
  }

  const controlsDisabled = !preference || savingPreference;
  const statementDisabled = !preference?.enabled;

  const handleAddUnifiedMemory = async () => {
    const trimmed = newMemoryContent.trim();
    if (!trimmed || unifiedBusy) return;
    setUnifiedBusy(true);
    setUnifiedStatus("");
    try {
      await apiPost("/api/v1/persona/memories", {
        body: { content: trimmed, scope: newMemoryScope },
      });
      setNewMemoryContent("");
      setUnifiedStatus("已添加记忆");
      await loadUnifiedMemories({ background: false });
    } catch (err) {
      setUnifiedStatus(err instanceof Error ? err.message : "添加记忆失败");
    } finally {
      setUnifiedBusy(false);
    }
  };

  const handleToggleUnifiedActive = async (item: UnifiedMemoryItem) => {
    if (unifiedBusy) return;
    setUnifiedBusy(true);
    setUnifiedStatus("");
    try {
      await apiPatch("/api/v1/persona/memories/{memory_id}", {
        pathParams: { memory_id: item.id },
        body: { active: !item.active },
      });
      setUnifiedStatus(item.active ? "已停用该条记忆" : "已启用该条记忆");
      await loadUnifiedMemories({ background: false });
    } catch (err) {
      setUnifiedStatus(err instanceof Error ? err.message : "更新记忆状态失败");
    } finally {
      setUnifiedBusy(false);
    }
  };

  const handleSaveUnifiedEdit = async (memoryId: string) => {
    const trimmed = editingContent.trim();
    if (!trimmed || unifiedBusy) return;
    setUnifiedBusy(true);
    setUnifiedStatus("");
    try {
      await apiPatch("/api/v1/persona/memories/{memory_id}", {
        pathParams: { memory_id: memoryId },
        body: { content: trimmed },
      });
      setEditingMemoryId(null);
      setEditingContent("");
      setUnifiedStatus("已更新记忆");
      await loadUnifiedMemories({ background: false });
    } catch (err) {
      setUnifiedStatus(err instanceof Error ? err.message : "更新记忆失败");
    } finally {
      setUnifiedBusy(false);
    }
  };

  const handleDeleteUnifiedMemory = async (memoryId: string) => {
    if (unifiedBusy) return;
    if (typeof window !== "undefined" && !window.confirm("确认删除这条记忆吗？")) {
      return;
    }
    setUnifiedBusy(true);
    setUnifiedStatus("");
    try {
      await apiDelete("/api/v1/persona/memories/{memory_id}", {
        pathParams: { memory_id: memoryId },
      });
      if (editingMemoryId === memoryId) {
        setEditingMemoryId(null);
        setEditingContent("");
      }
      setUnifiedStatus("已删除记忆");
      await loadUnifiedMemories({ background: false });
    } catch (err) {
      setUnifiedStatus(err instanceof Error ? err.message : "删除记忆失败");
    } finally {
      setUnifiedBusy(false);
    }
  };

  return (
    <section data-section="personalization" className="win-page-view">
      <div className={`personalization-settings ${styles.settingsContainer}`}>
        <h1 className={styles.settingsHeading}>个性化</h1>

        <section
          className={`personalization-rows ${styles.rowsCard}`}
          id="unified-memories-card"
          aria-label="统一记忆库"
        >
          <div style={{ padding: "16px", display: "flex", flexDirection: "column", gap: "12px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px" }}>
              <div>
                <h2 style={{ margin: 0, fontSize: "15px" }}>统一记忆库</h2>
                <p style={{ margin: "4px 0 0", fontSize: "12px", opacity: 0.75 }}>
                  号主私聊记忆与公开互动记忆严格隔离，支持随时增删改与启停。
                </p>
              </div>
              <div role="tablist" aria-label="记忆作用域筛选" style={{ display: "flex", gap: "6px" }}>
                {(
                  [
                    ["all", "全部"],
                    ["owner", "仅号主私聊"],
                    ["public", "公开互动"],
                  ] as const
                ).map(([scopeKey, label]) => (
                  <button
                    key={scopeKey}
                    type="button"
                    role="tab"
                    aria-selected={unifiedScope === scopeKey}
                    disabled={unifiedBusy}
                    onClick={() => {
                      setUnifiedScope(scopeKey);
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
              <select
                aria-label="新增记忆作用域"
                value={newMemoryScope}
                disabled={unifiedBusy}
                onChange={(e) => setNewMemoryScope(e.target.value as "owner" | "public")}
              >
                <option value="owner">仅号主私聊 (Owner)</option>
                <option value="public">公开互动 (Public)</option>
              </select>
              <input
                type="text"
                aria-label="新增记忆内容"
                placeholder="输入希望小鲸鱼记住的长期事实或规则..."
                value={newMemoryContent}
                disabled={unifiedBusy}
                onChange={(e) => setNewMemoryContent(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void handleAddUnifiedMemory();
                  }
                }}
                style={{ flex: 1, minWidth: "220px" }}
              />
              <button
                type="button"
                disabled={unifiedBusy || !newMemoryContent.trim()}
                onClick={() => void handleAddUnifiedMemory()}
              >
                添加记忆
              </button>
            </div>

            {unifiedStatus ? (
              <div role="status" style={{ fontSize: "12px", opacity: 0.85 }}>
                {unifiedStatus}
              </div>
            ) : null}

            {unifiedLoading ? (
              <div style={{ fontSize: "13px", opacity: 0.7 }}>正在加载记忆...</div>
            ) : unifiedItems.length === 0 ? (
              <div style={{ fontSize: "13px", opacity: 0.7 }}>当前筛选下暂无记忆条目。</div>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "8px" }}>
                {unifiedItems.map((item) => {
                  const isEditing = editingMemoryId === item.id;
                  return (
                    <li
                      key={item.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: "8px",
                        padding: "8px 10px",
                        border: "1px solid rgba(128,128,128,0.22)",
                        borderRadius: "6px",
                        opacity: item.active ? 1 : 0.6,
                      }}
                    >
                      <span style={{ fontSize: "12px", padding: "2px 6px", borderRadius: "4px", border: "1px solid currentColor", flexShrink: 0 }}>
                        {item.scope === "owner" ? "号主私聊" : "公开互动"}
                      </span>
                      {isEditing ? (
                        <>
                          <input
                            type="text"
                            aria-label="编辑记忆内容"
                            value={editingContent}
                            disabled={unifiedBusy}
                            onChange={(e) => setEditingContent(e.target.value)}
                            style={{ flex: 1 }}
                          />
                          <button
                            type="button"
                            disabled={unifiedBusy || !editingContent.trim()}
                            onClick={() => void handleSaveUnifiedEdit(item.id)}
                          >
                            保存
                          </button>
                          <button
                            type="button"
                            disabled={unifiedBusy}
                            onClick={() => {
                              setEditingMemoryId(null);
                              setEditingContent("");
                            }}
                          >
                            取消
                          </button>
                        </>
                      ) : (
                        <>
                          <span style={{ flex: 1, fontSize: "13px", wordBreak: "break-word" }}>{item.content}</span>
                          <button
                            type="button"
                            disabled={unifiedBusy}
                            onClick={() => void handleToggleUnifiedActive(item)}
                          >
                            {item.active ? "已启用" : "已停用"}
                          </button>
                          <button
                            type="button"
                            disabled={unifiedBusy}
                            onClick={() => {
                              setEditingMemoryId(item.id);
                              setEditingContent(item.content);
                            }}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            disabled={unifiedBusy}
                            onClick={() => void handleDeleteUnifiedMemory(item.id)}
                          >
                            删除
                          </button>
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </section>

        <section
          className={`personalization-rows ${styles.rowsCard}`}
          id="daily-memory-card"
          data-workspace-anchor=""
          aria-label="记忆设置"
        >
          <label className={`personalization-row ${styles.row}`} htmlFor="memory-enabled">
            <span>
              <strong id="memory-toggle-label">启用记忆</strong>
              <small id="memory-toggle-description">为当前账号和角色自动整理并取用长期记忆。</small>
            </span>
            <input
              id="memory-enabled"
              type="checkbox"
              role="switch"
              className={styles.switchInput}
              aria-labelledby="memory-toggle-label"
              aria-describedby="memory-toggle-description"
              disabled={controlsDisabled}
              checked={Boolean(preference?.enabled)}
              onChange={(event) => {
                void handlePreferenceToggle(
                  event.target.checked,
                  preference?.history_enabled !== false
                );
              }}
            />
          </label>

          <div className={`personalization-row ${styles.row}`}>
            <label htmlFor="memory-history-enabled">回复时引用以前的私聊</label>
            <input
              id="memory-history-enabled"
              type="checkbox"
              role="switch"
              disabled={controlsDisabled}
              checked={preference ? preference.history_enabled !== false : false}
              onChange={(event) => {
                void handlePreferenceToggle(Boolean(preference?.enabled), event.target.checked);
              }}
            />
          </div>

          <div className={`personalization-row ${styles.row}`}>
            <span>
              <strong>记忆摘要</strong>
              <small>查看、更正或删除已记住的内容。</small>
            </span>
            <button
              ref={summaryOpenerRef}
              type="button"
              className={`personalization-button ${styles.pillButton}`}
              id="open-memory-summary"
              onClick={() => setMemoryOpen(true)}
            >
              管理
            </button>
          </div>

          <div className={`personalization-row ${styles.row}`}>
            <span>
              <strong>近期趣事</strong>
              <small>查看近期观看、评论互动和回复结果。</small>
            </span>
            <button
              ref={recentOpenerRef}
              type="button"
              className={`personalization-button ${styles.pillButton}`}
              id="open-recent-events"
              onClick={() => {
                setRecentOpen(true);
                void loadRecent({ background: false });
              }}
            >
              查看
            </button>
          </div>
        </section>

        <p id="memory-switch-status" className={`hint ${styles.statusMessage}`} role="status">
          {switchStatus}
        </p>

        <div className={`personalization-row ${styles.row}`}>
          <label className={styles.statementBox}>
            要明确记住的原话
            <input
              id="owner-memory-statement"
              maxLength={150}
              disabled={statementDisabled}
              value={statement}
              onChange={(event) => {
                statementRef.current = event.target.value;
                setStatement(event.target.value);
              }}
            />
          </label>
          <span id="owner-memory-save" inert={statementDisabled ? true : undefined}>
            <RememberButton
              label="保存私有记忆"
              disabled={statementDisabled}
              propose={() => ({
                source: "settings",
                text: statementRef.current.trim(),
              })}
              onConfirmed={() => {
                if (memoryOpen) setPollGeneration((prev) => prev + 1);
              }}
            />
          </span>
        </div>

        <button
          type="button"
          className="win-btn"
          id="retry-memory-preference"
          hidden={!preferenceFailed}
          onClick={() => void loadPreference({ background: false })}
        >
          重新读取记忆设置
        </button>

        <details className={`personalization-rows ${styles.contextTools}`}>
          <summary>上下文状态、重建与压缩工具</summary>
          <form
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              void runContextAction("status");
            }}
          >
            <div className="form-grid" style={{ marginTop: 10 }}>
              <label>
                评论视频 ID（可选）
                <input
                  id="persona-context-video-id"
                  placeholder="BV…"
                  value={ctxVideoId}
                  onChange={(event) => setCtxVideoId(event.target.value)}
                />
              </label>
              <label>
                楼ID（可选）
                <input
                  id="persona-context-thread-id"
                  placeholder="楼ID"
                  value={ctxThreadId}
                  onChange={(event) => setCtxThreadId(event.target.value)}
                />
              </label>
            </div>
            <div className="actions" style={{ marginTop: 10 }}>
              <button type="submit" className="win-btn" disabled={ctxBusy}>
                查看上下文状态
              </button>
              <button
                type="button"
                className="win-btn"
                disabled={ctxBusy}
                onClick={() => void runContextAction("rebuild")}
              >
                重建评论上下文
              </button>
              <button
                type="button"
                className="win-btn"
                disabled={ctxBusy}
                onClick={() => void runContextAction("compact_live")}
              >
                压缩直播上下文
              </button>
              <button
                type="button"
                className="win-btn"
                disabled={ctxBusy}
                onClick={() => void runContextAction("compact_comments")}
              >
                压缩评论上下文
              </button>
            </div>
          </form>
          {ctxStatus && (
            <dl className="description-list" style={{ marginTop: 10 }}>
              <dt>活动范围</dt>
              <dd>{ctxStatus.scope || "暂无活动历史"}</dd>
              <dt>完整输入占用</dt>
              <dd>{`${tokenCount(ctxStatus.active_tokens ?? 0)}（${occupancyText(ctxStatus)}）`}</dd>
              <dt>累计压缩轮数</dt>
              <dd>{ctxStatus.compact_count ?? 0}</dd>
            </dl>
          )}
          {ctxFeedback && (
            <p className="hint" role="status">
              {ctxFeedback}
            </p>
          )}
        </details>
      </div>

      <MemorySummaryDialog
        open={memoryOpen}
        expectedScope={preference?.scope}
        scopeVersion={scopeVersion}
        pollGeneration={pollGeneration}
        onClose={() => {
          setMemoryOpen(false);
          summaryOpenerRef.current?.focus({ preventScroll: true });
        }}
        onScopeConflict={handleScopeConflict}
      />

      <RecentEventsDialog
        open={recentOpen}
        loading={recentLoading}
        error={recentError}
        items={recentItems}
        onClose={() => {
          readingRecentSeqRef.current += 1;
          setRecentOpen(false);
          recentOpenerRef.current?.focus({ preventScroll: true });
        }}
        onRetry={() => void loadRecent({ background: false })}
      />
    </section>
  );
}
