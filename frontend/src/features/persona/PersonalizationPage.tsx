import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { occupancyText, tokenCount } from "@/shared/lib/display";
import { errorMessage } from "@/shared/lib/errors";
import { RememberButton } from "@/shared/ui/RememberButton";
import { ManageMemoriesModal } from "./ManageMemoriesModal";
import { MemorySummaryDialog } from "./MemorySummaryDialog";
import styles from "./Personalization.module.css";
import { RecentEventsDialog } from "./RecentEventsDialog";
import {
  editableCard,
  MEMORIES_CHANGED_EVENT,
  OPEN_MANAGE_MEMORIES_EVENT,
  type CharacterCard,
  type MemoryPreferenceState,
  type PersonaContextStatus,
  type RecentEventItem,
  type RecentEventsResponse,
  type UnifiedMemoryListResponse,
} from "./types";

const DEFAULT_MEMORY_LIMIT = 100;

function instructionsTextFromCard(card: CharacterCard): string {
  const raw = card.style?.instructions;
  if (Array.isArray(raw)) {
    return raw.map((item) => String(item ?? "").trim()).filter(Boolean).join("\n");
  }
  return typeof raw === "string" ? raw : "";
}

export function PersonalizationPage() {
  const { status, refreshStatus } = useAppSession();
  const activeCharacterId = status?.character?.id ?? "";

  const [preference, setPreference] = useState<MemoryPreferenceState | null>(null);
  const [preferenceFailed, setPreferenceFailed] = useState(false);
  const [switchStatus, setSwitchStatus] = useState("");
  const [savingPreference, setSavingPreference] = useState(false);

  // Compact capacity stats for the "管理记忆 (N / 100)" entry button only (never inline list!)
  const [memoryCount, setMemoryCount] = useState(0);
  const [memoryLimit, setMemoryLimit] = useState(DEFAULT_MEMORY_LIMIT);
  const [overLimit, setOverLimit] = useState(false);
  const [legacyMigratedCount, setLegacyMigratedCount] = useState(0);
  const [fallbackModalOpen, setFallbackModalOpen] = useState(false);

  // Custom instructions & owner address preference on active character card
  const [characterCard, setCharacterCard] = useState<CharacterCard | null>(null);
  const [ownerAddress, setOwnerAddress] = useState("");
  const [customInstructions, setCustomInstructions] = useState("");
  const [savingInstructions, setSavingInstructions] = useState(false);
  const [instructionsStatus, setInstructionsStatus] = useState("");

  // Collapsed legacy / advanced context & memory tools
  const [statement, setStatement] = useState("");
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [recentOpen, setRecentOpen] = useState(false);
  const [scopeVersion, setScopeVersion] = useState(0);
  const [pollGeneration, setPollGeneration] = useState(0);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState("");
  const [recentItems, setRecentItems] = useState<RecentEventItem[]>([]);

  const [ctxVideoId, setCtxVideoId] = useState("");
  const [ctxThreadId, setCtxThreadId] = useState("");
  const [ctxStatus, setCtxStatus] = useState<PersonaContextStatus | null>(null);
  const [ctxFeedback, setCtxFeedback] = useState("");
  const [ctxBusy, setCtxBusy] = useState(false);

  const preferenceRef = useRef<MemoryPreferenceState | null>(null);
  const savingPrefRef = useRef(false);
  const readingPrefSeqRef = useRef(0);
  const readingRecentSeqRef = useRef(0);
  const recentScopeRef = useRef<string | null>(null);
  const pendingDraftRef = useRef<{ scope: string; text: string } | undefined>(undefined);
  const statementRef = useRef("");
  const scopeVersionRef = useRef(0);
  const cardReqSeqRef = useRef(0);

  const manageOpenerRef = useRef<HTMLButtonElement | null>(null);
  const summaryOpenerRef = useRef<HTMLButtonElement | null>(null);
  const recentOpenerRef = useRef<HTMLButtonElement | null>(null);

  preferenceRef.current = preference;
  savingPrefRef.current = savingPreference;
  statementRef.current = statement;
  scopeVersionRef.current = scopeVersion;

  const baselineOwnerAddress = characterCard?.owner_address || "";
  const baselineInstructions = characterCard ? instructionsTextFromCard(characterCard) : "";
  const instructionsDirty = Boolean(
    characterCard &&
      (ownerAddress !== baselineOwnerAddress || customInstructions !== baselineInstructions)
  );
  useUnsavedGuard("personalization-instructions", instructionsDirty);

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

  const refreshScope = useCallback((restoreSameScope = true) => {
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
  }, []);

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

  const loadMemoryStats = useCallback(async () => {
    try {
      const res = await apiGet<"/api/v1/persona/memories", Partial<UnifiedMemoryListResponse>>(
        "/api/v1/persona/memories",
        { query: { include_inactive: true } }
      );
      const items = Array.isArray(res.items) ? res.items : [];
      const nextCount =
        typeof res.count === "number" ? res.count : items.filter((item) => item.active).length;
      const nextLimit =
        typeof res.limit === "number" && res.limit > 0 ? res.limit : DEFAULT_MEMORY_LIMIT;
      setMemoryCount(nextCount);
      setMemoryLimit(nextLimit);
      setOverLimit(typeof res.over_limit === "boolean" ? res.over_limit : nextCount > nextLimit);
      setLegacyMigratedCount(
        typeof res.legacy_migrated_count === "number" ? res.legacy_migrated_count : 0
      );
    } catch {
      // Non-blocking: button remains clickable even if initial count lookup fails
    }
  }, []);

  const loadCharacterPreferences = useCallback(async (charId: string) => {
    if (!charId) {
      setCharacterCard(null);
      return;
    }
    const seq = ++cardReqSeqRef.current;
    try {
      const card = await apiGet<"/api/v1/persona/characters/{character_id}", CharacterCard>(
        "/api/v1/persona/characters/{character_id}",
        { pathParams: { character_id: charId } }
      );
      if (seq !== cardReqSeqRef.current) return;
      if (!card || !card.id || !card.name) {
        setCharacterCard(null);
        return;
      }
      setCharacterCard(card);
      setOwnerAddress(card.owner_address || "");
      setCustomInstructions(instructionsTextFromCard(card));
      setInstructionsStatus("");
    } catch {
      if (seq === cardReqSeqRef.current) {
        setCharacterCard(null);
      }
    }
  }, []);

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

  // Initial load + scopeVersion reload
  useEffect(() => {
    void loadPreference({ background: false });
    void loadMemoryStats();
  }, [scopeVersion, loadPreference, loadMemoryStats]);

  useEffect(() => {
    void loadCharacterPreferences(activeCharacterId);
  }, [activeCharacterId, scopeVersion, loadCharacterPreferences]);

  // 15s background polling
  useEffect(() => {
    const interval = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void loadPreference({ background: true });
      if (recentOpen) void loadRecent({ background: true });
      if (memoryOpen) setPollGeneration((prev) => prev + 1);
    }, 15_000);
    return () => clearInterval(interval);
  }, [loadPreference, loadRecent, recentOpen, memoryOpen]);

  // Refresh scope when character changes or memory updates globally
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
      void loadMemoryStats();
    };
    const onCharChanged = () => refreshScope(true);
    const onStatsChanged = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          count?: number;
          limit?: number;
          over_limit?: boolean;
          legacy_migrated_count?: number;
        }>
      ).detail;
      if (detail && typeof detail.count === "number") {
        setMemoryCount(detail.count);
        if (typeof detail.limit === "number") setMemoryLimit(detail.limit);
        if (typeof detail.over_limit === "boolean") setOverLimit(detail.over_limit);
        if (typeof detail.legacy_migrated_count === "number") {
          setLegacyMigratedCount(detail.legacy_migrated_count);
        }
      } else {
        void loadMemoryStats();
      }
    };
    document.addEventListener("owner-memory-changed", onMemoryChanged);
    document.addEventListener("studio-character-change", onCharChanged);
    window.addEventListener(MEMORIES_CHANGED_EVENT, onStatsChanged);
    return () => {
      document.removeEventListener("owner-memory-changed", onMemoryChanged);
      document.removeEventListener("studio-character-change", onCharChanged);
      window.removeEventListener(MEMORIES_CHANGED_EVENT, onStatsChanged);
    };
  }, [memoryOpen, loadPreference, loadMemoryStats, refreshScope]);

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

  async function handleSaveInstructions(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!characterCard || savingInstructions) return;
    const cleanAddress = ownerAddress.trim();
    if (cleanAddress.includes("\n")) {
      setInstructionsStatus("对你的称呼须为单行。");
      return;
    }
    setSavingInstructions(true);
    setInstructionsStatus("正在保存个性化指令…");
    try {
      const nextCard: CharacterCard = {
        ...editableCard(characterCard),
        style: {
          ...(characterCard.style || {}),
          instructions: customInstructions.trim(),
        },
      };
      if (cleanAddress) {
        nextCard.owner_address = cleanAddress;
      } else {
        delete nextCard.owner_address;
      }
      const saved = await apiPut<
        "/api/v1/persona/characters/{character_id}",
        CharacterCard,
        CharacterCard
      >("/api/v1/persona/characters/{character_id}", {
        pathParams: { character_id: characterCard.id },
        body: nextCard,
      });
      setCharacterCard(saved);
      setOwnerAddress(saved.owner_address || "");
      setCustomInstructions(instructionsTextFromCard(saved));
      setInstructionsStatus("已保存个性化偏好。");
      await refreshStatus();
    } catch (error) {
      setInstructionsStatus(`保存失败：${errorMessage(error)}`);
    } finally {
      setSavingInstructions(false);
    }
  }

  function handleOpenManageMemories() {
    if (typeof window !== "undefined") {
      const evt = new CustomEvent(OPEN_MANAGE_MEMORIES_EVENT, {
        cancelable: true,
        detail: { scope: "all" },
      });
      const unhandled = window.dispatchEvent(evt);
      if (unhandled) {
        setFallbackModalOpen(true);
      }
      return;
    }
    setFallbackModalOpen(true);
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
  const historyEnabled = preference
    ? preference.history_enabled !== false && preference.reference_chat_history !== false
    : false;

  return (
    <section data-section="personalization" className="win-page-view">
      <div className={`personalization-settings ${styles.settingsContainer}`}>
        <header className={styles.settingsHeader}>
          <h1 className={styles.settingsHeading}>个性化</h1>
          <p className={styles.settingsLead}>
            决定 Aqua 如何称呼你、如何回应，以及可以记住哪些长期信息。
          </p>
        </header>

        {/* Primary Memory Toggles + Single Manage Memories Entry */}
        <section
          className={`personalization-rows ${styles.rowsCard}`}
          id="daily-memory-card"
          data-workspace-anchor=""
          aria-label="记忆设置"
        >
          <label className={`personalization-row ${styles.row}`} htmlFor="memory-enabled">
            <span>
              <strong id="memory-toggle-label">引用已保存记忆</strong>
              <small id="memory-toggle-description">
                让 Aqua 在对话与互动中使用并自动保存长期偏好与事实。
              </small>
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
                void handlePreferenceToggle(event.target.checked, historyEnabled);
              }}
            />
          </label>

          <label className={`personalization-row ${styles.row}`} htmlFor="memory-history-enabled">
            <span>
              <strong id="history-toggle-label">引用历史对话</strong>
              <small id="history-toggle-description">
                在私聊回复中参考过往聊天上下文（公开互动不会读取私聊历史）。
              </small>
            </span>
            <input
              id="memory-history-enabled"
              type="checkbox"
              role="switch"
              className={styles.switchInput}
              aria-labelledby="history-toggle-label"
              aria-describedby="history-toggle-description"
              disabled={controlsDisabled}
              checked={historyEnabled}
              onChange={(event) => {
                void handlePreferenceToggle(Boolean(preference?.enabled), event.target.checked);
              }}
            />
          </label>

          <div className={`personalization-row ${styles.row}`}>
            <span>
              <strong>已保存的记忆</strong>
              <small>
                查看、搜索、增删或清理号主私聊与公开互动记忆。
                {legacyMigratedCount > 0 ? ` 检测到 ${legacyMigratedCount} 条历史遗留迁移记录待清理。` : ""}
              </small>
            </span>
            <div className={styles.entryActions}>
              {overLimit && <span className={styles.warningPill}>已超限</span>}
              <button
                ref={manageOpenerRef}
                type="button"
                id="open-manage-memories"
                className={styles.manageMemoriesBtn}
                onClick={handleOpenManageMemories}
              >
                {`管理记忆 (${memoryCount} / ${memoryLimit})`}
              </button>
            </div>
          </div>
        </section>

        <p id="memory-switch-status" className={`hint ${styles.statusMessage}`} role="status">
          {switchStatus}
        </p>

        <button
          type="button"
          className="win-btn"
          id="retry-memory-preference"
          hidden={!preferenceFailed}
          onClick={() => void loadPreference({ background: false })}
        >
          重新读取记忆设置
        </button>

        {/* Custom Instructions & Owner Address Preference */}
        {characterCard && (
          <section className={styles.rowsCard} aria-label="自定义指令与称呼">
            <div className={styles.cardHeader}>
              <h2 className={styles.cardTitle}>
                自定义指令 · {characterCard.display_name || characterCard.name}
              </h2>
            </div>
            <form className={styles.instructionsForm} onSubmit={(e) => void handleSaveInstructions(e)}>
              <label className={styles.fieldLabel}>
                Aqua 应该如何称呼你
                <input
                  id="personalization-owner-address"
                  type="text"
                  maxLength={40}
                  disabled={savingInstructions}
                  placeholder="例如：船长、老板（留空则默认称呼「你」）"
                  value={ownerAddress}
                  onChange={(event) => {
                    setOwnerAddress(event.target.value);
                    setInstructionsStatus("");
                  }}
                />
                <span className={styles.fieldHint}>私聊和实时通话中会优先使用这里设定的称呼。</span>
              </label>

              <label className={styles.fieldLabel}>
                表达风格与自定义指令
                <textarea
                  id="personalization-custom-instructions"
                  rows={3}
                  disabled={savingInstructions}
                  placeholder="描述你希望 Aqua 保持的语气、回复习惯或交互偏好…"
                  value={customInstructions}
                  onChange={(event) => {
                    setCustomInstructions(event.target.value);
                    setInstructionsStatus("");
                  }}
                />
              </label>

              <div className={styles.instructionsFooter}>
                <span className="hint" role="status">
                  {instructionsStatus}
                </span>
                <button
                  type="submit"
                  className="primary"
                  id="save-personalization-instructions"
                  disabled={savingInstructions || !instructionsDirty}
                >
                  {savingInstructions ? "正在保存…" : "保存指令"}
                </button>
              </div>
            </form>
          </section>
        )}

        {/* Collapsed Advanced Context & Legacy Tools Drawer */}
        <details className={styles.contextTools}>
          <summary>高级上下文与历史工具</summary>
          <div className={styles.advancedBody}>
            <div className={styles.advancedQuickRow}>
              <button
                ref={summaryOpenerRef}
                type="button"
                className={styles.pillButton}
                id="open-memory-summary"
                onClick={() => setMemoryOpen(true)}
              >
                旧版摘要与归档
              </button>
              <button
                ref={recentOpenerRef}
                type="button"
                className={styles.pillButton}
                id="open-recent-events"
                onClick={() => {
                  setRecentOpen(true);
                  void loadRecent({ background: false });
                }}
              >
                近期趣事
              </button>
            </div>

            <div className={styles.statementRow}>
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
                    void loadMemoryStats();
                  }}
                />
              </span>
            </div>

            <form
              onSubmit={(event: FormEvent) => {
                event.preventDefault();
                void runContextAction("status");
              }}
            >
              <div className="form-grid">
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
              <dl className="description-list">
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
          </div>
        </details>
      </div>

      {/* Standalone fallback when PersonalizationPage is rendered without routes.tsx ManageMemoriesHost */}
      {fallbackModalOpen && (
        <ManageMemoriesModal
          open
          onClose={() => {
            setFallbackModalOpen(false);
            manageOpenerRef.current?.focus({ preventScroll: true });
          }}
          onChanged={(nextStats) => {
            setMemoryCount(nextStats.count);
            setMemoryLimit(nextStats.limit);
            setOverLimit(nextStats.over_limit);
            setLegacyMigratedCount(nextStats.legacy_migrated_count);
          }}
        />
      )}

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
