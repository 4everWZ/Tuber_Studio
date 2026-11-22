import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import { VoiceSourceButton } from "@/shared/ui/RememberButton";
import styles from "./Personalization.module.css";
import { safeBilibiliSourceHref } from "./RecentEventsDialog";
import {
  combineAbortSignals,
  type DailyHighlightItem,
  type DailyMemoryReport,
  type MemorySummaryAskResponse,
  type OrganizedMemoryChange,
  type OrganizedMemoryItem,
} from "./types";

export const MEMORY_VISIBILITY: Record<string, string> = {
  owner: "仅号主",
  shared: "可跨端使用",
  live_rule: "直播规则",
};

export const MEMORY_GROUP_NAMES: Record<string, string> = {
  preference: "长期偏好",
  decision: "持续决定",
  experience: "可复用经验",
};

const ORGANIZATION_STATUS: Record<string, string> = {
  running: "正在后台整理",
  waiting: "等待模型空闲",
  disabled: "自动整理已停用",
};

function formatMemoryDate(value: number): string {
  return new Date(value * 1000).toLocaleString("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function ItemSourcesContent({ item }: { item: OrganizedMemoryItem }) {
  const isVoice = item.source.key?.startsWith("confirmed_voice:");
  const when = item.source.at ? ` · ${formatMemoryDate(item.source.at)}` : "";

  return (
    <>
      {item.memory_kind === "private" && (
        <>
          <p className="hint">{(isVoice ? "私有通话" : "号主原话") + when}</p>
          {isVoice && item.source.key && <VoiceSourceButton id={item.id} sourceKey={item.source.key} />}
        </>
      )}
      {item.quote && <blockquote>{item.quote}</blockquote>}
      {(item.source.references || []).map((ref, idx) => {
        const href = safeBilibiliSourceHref(ref.href);
        if (!href) return null;
        return (
          <p key={`${href}-${idx}`}>
            <a href={href} target="_blank" rel="noopener noreferrer">
              {ref.text}
            </a>
          </p>
        );
      })}
    </>
  );
}

function SummaryNoteEditForm({
  item,
  initialText,
  disabled,
  onSave,
  onDelete,
}: {
  item: OrganizedMemoryItem;
  initialText: string;
  disabled: boolean;
  onSave: (note: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [noteValue, setNoteValue] = useState(initialText);

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void onSave(noteValue.trim());
      }}
    >
      <label>
        {`${item.headline} · ${MEMORY_VISIBILITY[item.visibility || ""] || ""}`}
        {item.choices ? (
          <select
            required
            value={noteValue}
            onChange={(event) => setNoteValue(event.target.value)}
          >
            {item.choices.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        ) : (
          <textarea
            required
            maxLength={150}
            value={noteValue}
            onChange={(event) => setNoteValue(event.target.value)}
          />
        )}
      </label>
      <div className="actions">
        <button type="submit" className="win-btn" disabled={disabled}>
          保存更正
        </button>
        <button
          type="button"
          className="win-btn"
          disabled={disabled}
          onClick={() => {
            if (window.confirm("确认删除这条记忆吗？")) {
              void onDelete();
            }
          }}
        >
          删除
        </button>
      </div>
    </form>
  );
}

function OrganizedEntryCard({
  item,
  disabled,
  onToggleOwnerOnly,
  onSave,
  onDelete,
}: {
  item: OrganizedMemoryItem;
  disabled: boolean;
  onToggleOwnerOnly: (ownerOnly: boolean) => Promise<boolean>;
  onSave: (headline: string, note: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [headline, setHeadline] = useState(item.headline);
  const [note, setNote] = useState(item.anecdote);
  const [ownerOnly, setOwnerOnly] = useState(Boolean(item.owner_only));

  const archived = item.status === "archived";
  const metaParts = [
    MEMORY_VISIBILITY[item.visibility || ""],
    archived ? "已归档" : "",
    new Date(item.updated_at * 1000).toLocaleString("zh-CN"),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <article data-memory-id={String(item.id)}>
      <strong>{item.headline}</strong>
      <p>{item.anecdote}</p>
      <p className="hint">{metaParts}</p>
      {item.can_share && (
        <label className={`memory-sharing ${styles.sharingToggle}`}>
          <input
            type="checkbox"
            role="switch"
            disabled={disabled}
            checked={ownerOnly}
            onChange={(event) => {
              const nextVal = event.target.checked;
              setOwnerOnly(nextVal);
              void onToggleOwnerOnly(nextVal).then((saved) => {
                if (!saved) setOwnerOnly(Boolean(item.owner_only));
              });
            }}
          />
          仅号主可用
        </label>
      )}
      <details>
        <summary>来源与编辑</summary>
        <ItemSourcesContent item={item} />
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void onSave(headline.trim(), note.trim());
          }}
        >
          <label>
            记忆标题
            <input
              maxLength={40}
              required
              value={headline}
              onChange={(event) => setHeadline(event.target.value)}
            />
          </label>
          <label>
            记忆内容
            {item.choices ? (
              <select required value={note} onChange={(event) => setNote(event.target.value)}>
                {item.choices.map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            ) : (
              <textarea
                maxLength={150}
                required
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
            )}
          </label>
          <button type="submit" className="win-btn" disabled={disabled}>
            保存修改
          </button>
        </form>
      </details>
      <div className="actions">
        <button
          type="button"
          className="win-btn"
          disabled={disabled}
          onClick={() => {
            if (window.confirm("确认删除这条记忆吗？")) {
              void onDelete();
            }
          }}
        >
          删除
        </button>
      </div>
    </article>
  );
}

function HighlightEntryCard({
  item,
  disabled,
  onSave,
  onDelete,
}: {
  item: DailyHighlightItem;
  disabled: boolean;
  onSave: (headline: string, anecdote: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [headline, setHeadline] = useState(item.headline);
  const [anecdote, setAnecdote] = useState(item.anecdote);

  return (
    <article>
      <strong>{item.headline}</strong>
      <p>{item.anecdote}</p>
      <p className="hint">{item.highlight_date}</p>
      <blockquote>{item.quote || ""}</blockquote>
      {(item.public_sources || []).map((ref, idx) => {
        const href = safeBilibiliSourceHref(
          `https://www.bilibili.com/video/${ref.bvid}#reply${ref.comment_id ?? ""}`
        );
        if (!href) return null;
        return (
          <p key={`${ref.bvid}-${idx}`}>
            <a href={href} target="_blank" rel="noopener noreferrer">
              {ref.quote}
            </a>
          </p>
        );
      })}
      <details>
        <summary>更正</summary>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void onSave(headline.trim(), anecdote.trim());
          }}
        >
          <label>
            记忆标题
            <input value={headline} onChange={(event) => setHeadline(event.target.value)} />
          </label>
          <label>
            记忆内容
            <textarea value={anecdote} onChange={(event) => setAnecdote(event.target.value)} />
          </label>
          <button type="submit" className="win-btn" disabled={disabled}>
            保存修改
          </button>
        </form>
      </details>
      <div className="actions">
        <button
          type="button"
          className="win-btn"
          disabled={disabled}
          onClick={() => {
            if (window.confirm("确认删除这条记忆吗？")) {
              void onDelete();
            }
          }}
        >
          删除
        </button>
      </div>
    </article>
  );
}

export interface MemorySummaryDialogProps {
  open: boolean;
  expectedScope?: string;
  scopeVersion: number;
  pollGeneration: number;
  onClose: () => void;
  onScopeConflict: () => void;
}

export function MemorySummaryDialog({
  open,
  expectedScope,
  scopeVersion,
  pollGeneration,
  onClose,
  onScopeConflict,
}: MemorySummaryDialogProps) {
  const [report, setReport] = useState<DailyMemoryReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [showManager, setShowManager] = useState(false);
  const [summaryFeedback, setSummaryFeedback] = useState("");
  const [organizerFeedback, setOrganizerFeedback] = useState("");

  const [questionText, setQuestionText] = useState("");
  const [requestStatus, setRequestStatus] = useState("");
  const [requestResult, setRequestResult] = useState<MemorySummaryAskResponse | null>(null);
  const [requestBusy, setRequestBusy] = useState(false);
  const [mutating, setMutating] = useState(false);

  const [searchQuery, setSearchQuery] = useState("");
  const [filterStatus, setFilterStatus] = useState("all");

  // Public incident form state
  const [incidentKey, setIncidentKey] = useState("");
  const [incidentDate, setIncidentDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [incidentHeadline, setIncidentHeadline] = useState("");
  const [incidentAnecdote, setIncidentAnecdote] = useState("");
  const [incidentBvid, setIncidentBvid] = useState("");
  const [incidentQuote, setIncidentQuote] = useState("");
  const [incidentTags, setIncidentTags] = useState("");
  const [incidentStatus, setIncidentStatus] = useState("");

  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const summaryRef = useRef<HTMLDivElement | null>(null);
  const managerRef = useRef<HTMLDivElement | null>(null);
  const backBtnRef = useRef<HTMLButtonElement | null>(null);
  const manageBtnRef = useRef<HTMLButtonElement | null>(null);
  const questionInputRef = useRef<HTMLTextAreaElement | null>(null);

  const reportRequestRef = useRef<AbortController | null>(null);
  const summaryRequestRef = useRef<AbortController | null>(null);
  const submitActionRef = useRef<"ask" | "update">("ask");
  const openRef = useRef(open);
  const scopeVersionRef = useRef(scopeVersion);
  const expectedScopeRef = useRef(expectedScope);
  const onScopeConflictRef = useRef(onScopeConflict);
  const reportRef = useRef<DailyMemoryReport | null>(report);
  const mutatingRef = useRef(mutating);
  const lastLoadedScopeVersionRef = useRef<number>(-1);
  const lastHandledPollRef = useRef(pollGeneration);

  openRef.current = open;
  scopeVersionRef.current = scopeVersion;
  expectedScopeRef.current = expectedScope;
  onScopeConflictRef.current = onScopeConflict;
  reportRef.current = report;
  mutatingRef.current = mutating;

  const cancelReportLoad = useCallback(() => {
    if (reportRequestRef.current) {
      reportRequestRef.current.abort();
      reportRequestRef.current = null;
    }
  }, []);

  const resetSummaryRequest = useCallback(() => {
    if (summaryRequestRef.current) {
      summaryRequestRef.current.abort();
      summaryRequestRef.current = null;
    }
    setQuestionText("");
    setRequestStatus("");
    setRequestResult(null);
    setRequestBusy(false);
  }, []);

  const loadReport = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (background && (mutatingRef.current || reportRequestRef.current)) return;
      cancelReportLoad();
      const requestController = new AbortController();
      const deadline =
        typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
          ? AbortSignal.timeout(30_000)
          : new AbortController().signal;
      const { signal, cleanup } = combineAbortSignals([requestController.signal, deadline]);
      reportRequestRef.current = requestController;

      if (!background && !reportRef.current) {
        setLoading(true);
        setLoadError("");
      }

      try {
        const value = await apiGet<"/api/v1/persona/daily-memory", DailyMemoryReport>(
          "/api/v1/persona/daily-memory",
          { query: { limit: 100 }, signal }
        );
        if (reportRequestRef.current !== requestController || !openRef.current) return;
        const currentExpected = expectedScopeRef.current;
        const currentReport = reportRef.current;
        if (
          (currentExpected && currentExpected !== value.scope) ||
          (currentReport && currentReport.scope !== value.scope)
        ) {
          onScopeConflictRef.current();
          return;
        }
        const first = !currentReport;
        const redraw =
          first ||
          JSON.stringify([currentReport.summary, currentReport.organized, currentReport.highlights]) !==
            JSON.stringify([value.summary, value.organized, value.highlights]);
        const activeEl = typeof document !== "undefined" ? document.activeElement : null;
        const deferRedraw =
          redraw &&
          background &&
          Boolean(
            activeEl &&
              (managerRef.current?.contains(activeEl) || summaryRef.current?.contains(activeEl))
          );

        if (!deferRedraw) {
          reportRef.current = value;
          setReport(value);
        }
        setLoading(false);
        setLoadError("");
        if (first) {
          setShowManager(false);
        }
      } catch (error) {
        if (reportRequestRef.current !== requestController || !openRef.current || (background && reportRef.current)) {
          return;
        }
        setLoading(false);
        const failure = deadline.aborted ? "读取记忆摘要超时。" : `读取记忆摘要失败：${errorMessage(error)}`;
        setLoadError(failure);
      } finally {
        cleanup();
        if (reportRequestRef.current === requestController) {
          reportRequestRef.current = null;
        }
      }
    },
    [cancelReportLoad]
  );

  // Manage native dialog open/close + scope resets + clean up controllers on close
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      if (lastLoadedScopeVersionRef.current !== scopeVersion) {
        lastLoadedScopeVersionRef.current = scopeVersion;
        cancelReportLoad();
        resetSummaryRequest();
        reportRef.current = null;
        setReport(null);
        setShowManager(false);
        setSummaryFeedback("");
        setOrganizerFeedback("");
        setSearchQuery("");
        setFilterStatus("all");
      }
      void loadReport({ background: false });
    } else {
      if (dialog.open || dialog.hasAttribute("open")) {
        if (typeof dialog.close === "function") {
          dialog.close();
        } else {
          dialog.removeAttribute("open");
        }
      }
      cancelReportLoad();
      resetSummaryRequest();
    }
  }, [open, scopeVersion, cancelReportLoad, resetSummaryRequest, loadReport]);

  // Handle background poll ticks while open
  useEffect(() => {
    if (pollGeneration === lastHandledPollRef.current) return;
    lastHandledPollRef.current = pollGeneration;
    if (open) {
      void loadReport({ background: true });
    }
  }, [pollGeneration, open, loadReport]);

  useEffect(() => {
    return () => {
      cancelReportLoad();
      summaryRequestRef.current?.abort();
    };
  }, [cancelReportLoad]);

  async function mutateMemory(
    kind: "organized_patch" | "organized_delete" | "highlight_put" | "highlight_delete",
    itemId: number,
    options: {
      body?: OrganizedMemoryChange | { headline: string; anecdote: string };
      revision?: number;
      expectedRevision?: number | null;
      fromSummary?: boolean;
      fromRequestForm?: boolean;
      successMessage: string;
    }
  ): Promise<boolean> {
    if (!reportRef.current || mutatingRef.current || summaryRequestRef.current) return false;
    const scope = reportRef.current.scope;
    const verAtStart = scopeVersionRef.current;
    mutatingRef.current = true;
    setMutating(true);
    const setTargetFeedback = options.fromRequestForm
      ? setRequestStatus
      : options.fromSummary
        ? setSummaryFeedback
        : setOrganizerFeedback;
    setTargetFeedback("");

    try {
      if (kind === "organized_patch") {
        await apiPatch("/api/v1/persona/daily-memory/organized/{item_id}", {
          pathParams: { item_id: itemId },
          query: {
            scope,
            ...(options.revision !== undefined ? { revision: options.revision } : {}),
            ...(options.expectedRevision ? { expected_revision: options.expectedRevision } : {}),
          },
          body: (options.body as OrganizedMemoryChange) || {},
        });
      } else if (kind === "organized_delete") {
        await apiDelete("/api/v1/persona/daily-memory/organized/{item_id}", {
          pathParams: { item_id: itemId },
          query: {
            scope,
            ...(options.expectedRevision ? { expected_revision: options.expectedRevision } : {}),
          },
        });
      } else if (kind === "highlight_put") {
        await apiPut("/api/v1/persona/daily-memory/{item_id}", {
          pathParams: { item_id: itemId },
          query: { scope },
          body: (options.body as Record<string, unknown>) || {},
        });
      } else {
        await apiDelete("/api/v1/persona/daily-memory/{item_id}", {
          pathParams: { item_id: itemId },
          query: { scope },
        });
      }

      if (verAtStart !== scopeVersionRef.current) return false;
      await loadReport({ background: false });
      if (verAtStart !== scopeVersionRef.current || !openRef.current) return false;
      setTargetFeedback(options.successMessage);
      return true;
    } catch (error) {
      if (verAtStart !== scopeVersionRef.current) return false;
      if ((error as { status?: number }).status === 409) {
        onScopeConflict();
      } else {
        setTargetFeedback(`操作失败：${errorMessage(error)}`);
      }
      return false;
    } finally {
      mutatingRef.current = false;
      setMutating(false);
    }
  }

  async function handleSummaryAskSubmit(event: FormEvent) {
    event.preventDefault();
    const text = questionText.trim();
    if (!text || !report || summaryRequestRef.current || mutating) return;

    const submitter = (event.nativeEvent as SubmitEvent | undefined)?.submitter as HTMLButtonElement | null;
    const mode =
      submitter?.value === "update" || (!submitter?.value && submitActionRef.current === "update")
        ? "update"
        : "ask";

    const controller = new AbortController();
    summaryRequestRef.current = controller;
    const verAtStart = scopeVersionRef.current;
    const scope = report.scope;

    setRequestBusy(true);
    setRequestResult(null);
    setRequestStatus(mode === "ask" ? "正在查找记忆…" : "正在识别要更新的记忆…");

    try {
      const result = await apiPost<
        "/api/v1/persona/daily-memory/summary",
        MemorySummaryAskResponse,
        { scope: string; action: "ask" | "update"; text: string }
      >("/api/v1/persona/daily-memory/summary", {
        body: { scope, action: mode, text },
        signal: controller.signal,
      });

      if (controller.signal.aborted || verAtStart !== scopeVersionRef.current || !openRef.current) return;
      if (result.scope !== scope) {
        onScopeConflict();
        return;
      }

      if (result.status === "found") {
        setRequestResult(result);
        setRequestStatus(`找到 ${result.items?.length ?? 0} 条相关记忆。`);
      } else if (result.status === "no_match") {
        setRequestStatus("没有找到相关的已保存记忆。");
      } else if (result.status === "updated") {
        await loadReport({ background: false });
        if (controller.signal.aborted || verAtStart !== scopeVersionRef.current || !openRef.current) return;
        setRequestStatus(`已更新「${result.item?.headline ?? ""}」。`);
        setQuestionText((prev) => (prev.trim() === text ? "" : prev));
      } else if (result.status === "proposal") {
        setRequestResult(result);
        setRequestStatus("请核对要更新的条目。");
      } else {
        setRequestStatus("请写出一条记忆的标题和更正后的内容，或从条目中直接编辑。");
      }
    } catch (error) {
      if (controller.signal.aborted || verAtStart !== scopeVersionRef.current || !openRef.current) return;
      if ((error as { status?: number }).status === 409) {
        onScopeConflict();
      } else {
        setRequestStatus(`操作失败：${errorMessage(error)}`);
      }
    } finally {
      if (summaryRequestRef.current === controller) {
        summaryRequestRef.current = null;
        setRequestBusy(false);
      }
    }
  }

  async function handleRecordIncident(event: FormEvent) {
    event.preventDefault();
    if (mutating) return;
    setMutating(true);
    setIncidentStatus("正在登记公开趣事…");
    try {
      const bvid = incidentBvid.trim();
      const quote = incidentQuote.trim();
      const tags = incidentTags
        .split(/[,，、\n]/u)
        .map((t) => t.trim())
        .filter(Boolean);
      await apiPost("/api/v1/persona/daily-memory/incidents", {
        body: {
          incident_key: incidentKey.trim(),
          highlight_date: incidentDate.trim(),
          headline: incidentHeadline.trim(),
          anecdote: incidentAnecdote.trim(),
          public_sources: bvid ? [{ bvid, quote: quote || incidentHeadline.trim() }] : [],
          tags,
        },
      });
      setIncidentKey("");
      setIncidentHeadline("");
      setIncidentAnecdote("");
      setIncidentBvid("");
      setIncidentQuote("");
      setIncidentTags("");
      await loadReport({ background: false });
      setIncidentStatus("公开趣事已登记。");
    } catch (error) {
      setIncidentStatus(`登记失败：${errorMessage(error)}`);
    } finally {
      setMutating(false);
    }
  }

  const orgState = report?.organization;
  const orgNotice =
    orgState?.status === "retry"
      ? orgState.error
        ? `后台整理失败：${orgState.error}`
        : "后台整理失败，稍后重试"
      : orgState?.status
        ? ORGANIZATION_STATUS[orgState.status] || ""
        : "";

  const normalizedSearch = searchQuery.trim().toLocaleLowerCase();
  const visibleOrganized = (report?.organized || []).filter(
    (item) =>
      (filterStatus === "all" || item.status === filterStatus) &&
      (!normalizedSearch ||
        [item.headline, item.anecdote, item.quote].some((v) => v?.toLocaleLowerCase().includes(normalizedSearch)))
  );

  const showLoadState = !report && (loading || Boolean(loadError));

  return (
    <dialog
      ref={dialogRef}
      id="memory-summary-dialog"
      className={`personalization-dialog ${styles.dialog}`}
      aria-labelledby="memory-summary-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
    >
      <header className={styles.dialogHeader}>
        <div className={`memory-summary-heading ${styles.summaryHeading}`}>
          <h2 id="memory-summary-title" className={styles.dialogTitle}>
            记忆摘要
          </h2>
          <time
            id="memory-summary-updated"
            className="hint"
            dateTime={
              report?.summary_updated_at ? new Date(report.summary_updated_at * 1000).toISOString() : undefined
            }
            hidden={!report?.summary_updated_at}
          >
            {report?.summary_updated_at ? `最近更新 ${formatMemoryDate(report.summary_updated_at)}` : ""}
          </time>
        </div>
        <button
          type="button"
          className={`personalization-close ${styles.closeButton}`}
          aria-label="关闭记忆摘要"
          autoFocus
          onClick={onClose}
        >
          ×
        </button>
      </header>

      <div className={`personalization-dialog-body ${styles.dialogBody}`}>
        <div
          id="memory-load-state"
          className={`personalization-load ${styles.loadState}`}
          role="status"
          hidden={!showLoadState}
        >
          {loading ? (
            <p>正在读取记忆摘要…</p>
          ) : loadError ? (
            <>
              <p>{loadError}</p>
              <button
                type="button"
                className={`personalization-button ${styles.pillButton}`}
                onClick={() => void loadReport({ background: false })}
              >
                重试
              </button>
            </>
          ) : null}
        </div>

        <div ref={summaryRef} id="memory-summary" hidden={showLoadState || showManager}>
          <div id="memory-summary-list" className={`memory-detail-list ${styles.detailList} ${styles.summaryList}`}>
            {!report?.summary?.length ? (
              <p className="hint">暂无已保存的长期记忆。</p>
            ) : (
              report.summary.map((group) => {
                const resolvedPairs = group.notes
                  .map((note) => ({
                    note,
                    item: report.organized.find((entry) => entry.id === note.id),
                  }))
                  .filter((pair): pair is { note: typeof pair.note; item: OrganizedMemoryItem } => Boolean(pair.item));

                return (
                  <article key={group.kind}>
                    <h3>{MEMORY_GROUP_NAMES[group.kind] || group.kind}</h3>
                    {group.notes.map((note) => (
                      <p key={note.id}>
                        {`${note.text} `}
                        {note.visibility && MEMORY_VISIBILITY[note.visibility] && (
                          <small className="hint">{MEMORY_VISIBILITY[note.visibility]}</small>
                        )}
                      </p>
                    ))}
                    {resolvedPairs.length > 0 && (
                      <details>
                        <summary>来源</summary>
                        {resolvedPairs.map(({ item }) => (
                          <details key={item.id}>
                            <summary>{item.headline}</summary>
                            <ItemSourcesContent item={item} />
                          </details>
                        ))}
                      </details>
                    )}
                    <details>
                      <summary>更正或删除</summary>
                      {resolvedPairs.map(({ note, item }) => (
                        <SummaryNoteEditForm
                          key={`${item.id}-${item.updated_at}-${item.revision ?? 0}`}
                          item={item}
                          initialText={note.text}
                          disabled={mutating || requestBusy}
                          onSave={async (newNote) => {
                            await mutateMemory("organized_patch", note.id, {
                              body: { note: newNote },
                              expectedRevision: item.revision,
                              fromSummary: true,
                              successMessage: "记忆已更新。",
                            });
                          }}
                          onDelete={async () => {
                            await mutateMemory("organized_delete", note.id, {
                              expectedRevision: item.revision,
                              fromSummary: true,
                              successMessage: "记忆已删除。",
                            });
                          }}
                        />
                      ))}
                    </details>
                  </article>
                );
              })
            )}
          </div>

          <p id="memory-summary-feedback" className="hint" role="status">
            {summaryFeedback}
          </p>
          <button
            ref={manageBtnRef}
            type="button"
            id="open-memory-items"
            className={`personalization-button ${styles.pillButton}`}
            onClick={() => {
              setShowManager(true);
              setTimeout(() => backBtnRef.current?.focus(), 0);
            }}
          >
            管理记忆条目
          </button>

          <form
            id="memory-summary-request"
            className={styles.summaryRequestForm}
            onSubmit={(event) => void handleSummaryAskSubmit(event)}
          >
            <label htmlFor="memory-summary-question">询问或更新记忆</label>
            <textarea
              ref={questionInputRef}
              id="memory-summary-question"
              className={styles.summaryQuestion}
              rows={2}
              maxLength={500}
              required
              placeholder="例：你记得我对运营汇报有什么要求？"
              value={questionText}
              onChange={(event) => setQuestionText(event.target.value)}
            />
            <div className="actions">
              <button
                type="submit"
                className="win-btn"
                value="ask"
                disabled={requestBusy}
                onClick={() => {
                  submitActionRef.current = "ask";
                }}
              >
                询问
              </button>
              <button
                type="submit"
                className="win-btn"
                value="update"
                disabled={requestBusy}
                onClick={() => {
                  submitActionRef.current = "update";
                }}
              >
                更新
              </button>
            </div>
            <p id="memory-summary-request-status" className="hint" role="status">
              {requestStatus}
            </p>
            <div id="memory-summary-request-result" className={`memory-detail-list ${styles.detailList}`}>
              {requestResult?.status === "found" &&
                (requestResult.items || []).map((item) => (
                  <article key={item.id}>
                    <strong>{item.headline}</strong>
                    <p>{item.anecdote}</p>
                    <details>
                      <summary>查看来源</summary>
                      <ItemSourcesContent item={item} />
                    </details>
                  </article>
                ))}
              {requestResult?.status === "proposal" && requestResult.item && (
                <article>
                  <strong>{requestResult.item.headline}</strong>
                  <p>{`当前内容 · ${requestResult.item.anecdote}`}</p>
                  <p>{`更新为 · ${requestResult.note ?? ""}`}</p>
                  <button
                    type="button"
                    className="win-btn"
                    disabled={mutating}
                    onClick={() => {
                      const target = requestResult.item!;
                      const nextNote = requestResult.note ?? "";
                      void mutateMemory("organized_patch", target.id, {
                        body: { note: nextNote },
                        revision: target.updated_at,
                        expectedRevision: target.revision,
                        fromRequestForm: true,
                        successMessage: "记忆已更新。",
                      }).then((saved) => {
                        if (saved && openRef.current) {
                          setRequestResult(null);
                          setRequestStatus(`已更新「${target.headline}」。`);
                          questionInputRef.current?.focus({ preventScroll: true });
                        }
                      });
                    }}
                  >
                    应用更正
                  </button>
                </article>
              )}
            </div>
          </form>
        </div>

        <div ref={managerRef} id="memory-items" hidden={showLoadState || !showManager}>
          <div className={`memory-toolbar ${styles.toolbar}`}>
            <button
              ref={backBtnRef}
              type="button"
              id="back-to-memory-summary"
              className="win-btn"
              onClick={() => {
                setShowManager(false);
                setTimeout(() => manageBtnRef.current?.focus(), 0);
              }}
            >
              返回摘要
            </button>
          </div>
          <p id="daily-memory-notice" className={`hint ${styles.statusMessage}`} role="status">
            {organizerFeedback || orgNotice}
          </p>
          <div className="form-grid">
            <label>
              搜索记忆
              <input
                id="search-memory"
                type="search"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
              />
            </label>
            <label>
              状态
              <select
                id="filter-memory"
                value={filterStatus}
                onChange={(event) => setFilterStatus(event.target.value)}
              >
                <option value="all">全部</option>
                <option value="active">已保存</option>
                <option value="archived">已归档</option>
              </select>
            </label>
          </div>

          <div id="organized-memory-list" className={`memory-detail-list ${styles.detailList}`}>
            {visibleOrganized.length === 0 ? (
              <p className="hint">暂无符合条件的记忆。</p>
            ) : (
              visibleOrganized.map((item) => (
                <OrganizedEntryCard
                  key={`${item.id}-${item.updated_at}-${item.revision ?? 0}`}
                  item={item}
                  disabled={mutating || requestBusy}
                  onToggleOwnerOnly={(ownerOnly) =>
                    mutateMemory("organized_patch", item.id, {
                      body: { owner_only: ownerOnly },
                      expectedRevision: item.revision,
                      successMessage: "记忆使用范围已更新。",
                    })
                  }
                  onSave={async (headline, note) => {
                    await mutateMemory("organized_patch", item.id, {
                      body: { headline, note },
                      expectedRevision: item.revision,
                      successMessage: "记忆已更新。",
                    });
                  }}
                  onDelete={async () => {
                    await mutateMemory("organized_delete", item.id, {
                      expectedRevision: item.revision,
                      successMessage: "记忆已删除。",
                    });
                  }}
                />
              ))
            )}
          </div>

          <details className={`older-memory-list ${styles.olderList}`}>
            <summary>早期记忆记录</summary>
            <div id="daily-memory-list" className={`memory-detail-list ${styles.detailList}`}>
              {!report?.highlights?.length ? (
                <p className="hint">暂无早期记忆记录。</p>
              ) : (
                report.highlights.map((item) => (
                  <HighlightEntryCard
                    key={item.id}
                    item={item}
                    disabled={mutating || requestBusy}
                    onSave={async (headline, anecdote) => {
                      await mutateMemory("highlight_put", item.id, {
                        body: { headline, anecdote },
                        successMessage: "记忆已更新。",
                      });
                    }}
                    onDelete={async () => {
                      await mutateMemory("highlight_delete", item.id, {
                        successMessage: "记忆已删除。",
                      });
                    }}
                  />
                ))
              )}
            </div>

            <details className={styles.olderList}>
              <summary>登记公开趣事</summary>
              <form id="record-incident-form" onSubmit={(event) => void handleRecordIncident(event)}>
                <div className="form-grid">
                  <label>
                    事件编号
                    <input
                      id="incident-key"
                      required
                      placeholder="例如 operator-confirmed-20261122"
                      value={incidentKey}
                      onChange={(event) => setIncidentKey(event.target.value)}
                    />
                  </label>
                  <label>
                    日期
                    <input
                      id="incident-date"
                      type="date"
                      required
                      value={incidentDate}
                      onChange={(event) => setIncidentDate(event.target.value)}
                    />
                  </label>
                </div>
                <label>
                  趣事标题
                  <input
                    id="incident-headline"
                    required
                    value={incidentHeadline}
                    onChange={(event) => setIncidentHeadline(event.target.value)}
                  />
                </label>
                <label>
                  趣事内容
                  <textarea
                    id="incident-anecdote"
                    rows={2}
                    required
                    value={incidentAnecdote}
                    onChange={(event) => setIncidentAnecdote(event.target.value)}
                  />
                </label>
                <div className="form-grid">
                  <label>
                    来源视频 BV 号
                    <input
                      id="incident-bvid"
                      required
                      placeholder="BV..."
                      value={incidentBvid}
                      onChange={(event) => setIncidentBvid(event.target.value)}
                    />
                  </label>
                  <label>
                    来源原话
                    <input
                      id="incident-quote"
                      value={incidentQuote}
                      onChange={(event) => setIncidentQuote(event.target.value)}
                    />
                  </label>
                </div>
                <label>
                  标签（逗号分隔，可选）
                  <input
                    id="incident-tags"
                    value={incidentTags}
                    onChange={(event) => setIncidentTags(event.target.value)}
                  />
                </label>
                <div className="actions" style={{ marginTop: 8 }}>
                  <button type="submit" className="win-btn" disabled={mutating}>
                    保存公开趣事
                  </button>
                  <span className="hint" role="status">
                    {incidentStatus}
                  </span>
                </div>
              </form>
            </details>
          </details>
        </div>
      </div>
    </dialog>
  );
}
