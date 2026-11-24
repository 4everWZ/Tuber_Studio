import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { OperationContinuationCard } from "@/shared";
import { apiGet, apiPost } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import {
  badgeTone,
  label as displayLabel,
  occupancyText,
  tokenCount,
} from "@/shared/lib/display";
import { emitNotice, errorMessage, webCharacterName } from "@/shared/lib/errors";
import type { ParsedRoute } from "@/shell/navigation";
import {
  COMMENT_QUEUE_VIEWS,
  CommentQueueSection,
  type GenerationScope,
  type QueueViewName,
} from "./CommentQueueSection";
import { CommentsImportForm } from "./CommentsImportForm";
import { CommentsSourcesSection, type SourceFilters } from "./CommentsSourcesSection";
import { commentFinished, reconcileCommentSelection } from "./commentState";
import { useOperationsControl } from "@/shared/operations/operationsStore";
import type { CommentRecord, CommentTaskRecord } from "./types";
import { UserMemoryModal, type UserMemoryTarget } from "./UserMemoryModal";

interface ContextStatusResponse {
  active_tokens?: number | null;
  configured_tokens?: number | null;
  compact_count?: number | null;
  compaction_passes_last?: number | null;
  last_compaction_warning?: string | null;
}

function parseCommentsRoute(route: ParsedRoute): {
  view: QueueViewName;
  routeKey: string | null;
  inferRouteView: boolean;
  generation: GenerationScope | null;
} {
  const { anchor, routeKind, routeId, taskId } = route;
  if (
    routeKind === "review" &&
    routeId === "task" &&
    taskId &&
    /^[\w-]+$/.test(taskId)
  ) {
    return {
      view: "review",
      routeKey: null,
      inferRouteView: false,
      generation: { id: taskId, selectedFirst: false },
    };
  }
  if (anchor || routeKind === "sources") {
    return {
      view: "sources",
      routeKey: null,
      inferRouteView: false,
      generation: null,
    };
  }
  const matchedView =
    routeKind && COMMENT_QUEUE_VIEWS.includes(routeKind)
      ? (routeKind === "activity" ? "review" : (routeKind as QueueViewName))
      : null;
  const [channel, identity] = matchedView ? [routeId, taskId] : [routeKind, routeId];
  const validKey =
    channel &&
    ["comments", "browsing", "jobs"].includes(channel) &&
    identity &&
    /^[\w-]+$/.test(identity);

  return {
    view: matchedView || "review",
    routeKey: validKey ? `${channel}/${identity}` : null,
    inferRouteView: Boolean(validKey && !matchedView),
    generation: null,
  };
}

export function CommentsPage({ route }: { route: ParsedRoute }) {
  const { status: sysStatus } = useAppSession();
  const control = useOperationsControl();
  const parsedRoute = useMemo(() => parseCommentsRoute(route), [route]);

  const [queueView, setQueueView] = useState<QueueViewName>(parsedRoute.view);
  const [generation, setGeneration] = useState<GenerationScope | null>(parsedRoute.generation);
  const [sourcesOpen, setSourcesOpen] = useState(parsedRoute.view === "sources");

  const [comments, setComments] = useState<CommentRecord[]>([]);
  const [tasks, setTasks] = useState<CommentTaskRecord[]>([]);
  const [filters, setFilters] = useState<SourceFilters>({
    videoId: "",
    threadId: "",
    category: "",
    status: "",
  });
  const [sourcePage, setSourcePage] = useState(0);
  const [selectedCommentId, setSelectedCommentId] = useState<number | null>(null);
  const [selectedComments, setSelectedComments] = useState<Set<number>>(() => new Set());
  const [threadContextState, setThreadContextState] = useState("");
  const [threadRebuildState, setThreadRebuildState] = useState("");
  const [contextBusy, setContextBusy] = useState(false);
  const [queueRefreshTick, setQueueRefreshTick] = useState(0);
  const [userMemoryTarget, setUserMemoryTarget] = useState<UserMemoryTarget | null>(null);

  const commentsReqSeq = useRef(0);
  const threadContextKeyRef = useRef<string | null>(null);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  useEffect(() => {
    setQueueView(parsedRoute.view);
    setGeneration(parsedRoute.generation);
    if (parsedRoute.view === "sources") {
      setSourcesOpen(true);
    }
  }, [parsedRoute]);

  const resolveSelectedThread = useCallback(
    (overrideComments?: CommentRecord[], overrideSelectedId?: number | null) => {
      const list = overrideComments ?? comments;
      const selId = overrideSelectedId !== undefined ? overrideSelectedId : selectedCommentId;
      const selected = list.find((c) => c.id === selId);
      let videoId = filtersRef.current.videoId.trim();
      let threadId = filtersRef.current.threadId.trim();
      if ((!videoId || !threadId) && selected) {
        const selectedThreadId = selected.thread_id || selected.comment_id;
        if (
          (!videoId || videoId === selected.video_id) &&
          (!threadId || threadId === selectedThreadId)
        ) {
          videoId = selected.video_id;
          threadId = selectedThreadId;
        }
      }
      return videoId && threadId
        ? { videoId, threadId, key: JSON.stringify([videoId, threadId]) }
        : null;
    },
    [comments, selectedCommentId]
  );

  const refreshThreadContext = useCallback(
    async (overrideComments?: CommentRecord[], overrideSelectedId?: number | null) => {
      const target = resolveSelectedThread(overrideComments, overrideSelectedId);
      if (!target) {
        threadContextKeyRef.current = null;
        setThreadContextState("选择评论或筛选单楼，查看该楼上下文；不同楼不会合并。");
        return;
      }
      if (threadContextKeyRef.current === target.key) return;
      threadContextKeyRef.current = target.key;
      setThreadContextState(`正在读取 ${target.videoId} / 楼 ${target.threadId} 的上下文…`);
      try {
        const context = await apiGet<
          "/api/v1/persona/context/status",
          ContextStatusResponse
        >("/api/v1/persona/context/status", {
          query: { video_id: target.videoId, thread_id: target.threadId },
        });
        if (threadContextKeyRef.current !== target.key) return;
        setThreadContextState(
          `所选楼 ${target.videoId} / ${target.threadId}：${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)}（${occupancyText(context)}），累计压缩 ${context.compact_count ?? 0} 轮、最近 ${context.compaction_passes_last ?? 0} 轮。${context.last_compaction_warning || ""}`
        );
      } catch (err) {
        if (threadContextKeyRef.current === target.key) {
          setThreadContextState(`所选楼上下文读取失败：${errorMessage(err)}`);
        }
      } finally {
        if (threadContextKeyRef.current === target.key) {
          threadContextKeyRef.current = null;
        }
      }
    },
    [resolveSelectedThread]
  );

  const refreshComments = useCallback(
    async (explicitFilters?: SourceFilters) => {
      const activeFilters = explicitFilters ?? filtersRef.current;
      const query: Record<string, string> = {};
      if (activeFilters.videoId.trim()) query.video_id = activeFilters.videoId.trim();
      if (activeFilters.threadId.trim()) query.thread_id = activeFilters.threadId.trim();
      if (activeFilters.category.trim()) query.category = activeFilters.category.trim();
      if (activeFilters.status.trim() && activeFilters.status.trim() !== "all") {
        query.status = activeFilters.status.trim();
      }

      const seq = ++commentsReqSeq.current;
      try {
        const [commentsRes, tasksRes] = await Promise.all([
          apiGet<"/api/v1/operations/comments", CommentRecord[] | { items?: CommentRecord[] }>(
            "/api/v1/operations/comments",
            { query }
          ),
          apiGet<
            "/api/v1/operations/comments/tasks",
            CommentTaskRecord[] | { items?: CommentTaskRecord[] }
          >("/api/v1/operations/comments/tasks"),
        ]);
        if (seq !== commentsReqSeq.current) return;

        const rawComments = Array.isArray(commentsRes) ? commentsRes : commentsRes.items || [];
        const visible = rawComments.filter(
          (comment) => Boolean(activeFilters.status) || !commentFinished(comment)
        );
        const rawTasks = Array.isArray(tasksRes) ? tasksRes : tasksRes.items || [];

        setComments(visible);
        setTasks(rawTasks);
        setSelectedComments((prev) => reconcileCommentSelection(visible, prev));

        let nextSelectedId = selectedCommentId;
        if (!visible.some((c) => c.id === selectedCommentId)) {
          nextSelectedId = null;
          setSelectedCommentId(null);
        }
        await refreshThreadContext(visible, nextSelectedId);
      } catch (err) {
        if (seq === commentsReqSeq.current) {
          emitNotice(errorMessage(err), true);
        }
      }
    },
    [selectedCommentId, refreshThreadContext]
  );

  useEffect(() => {
    void control.refreshDiscovery();
    if (queueView === "sources") {
      void refreshComments();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void control.refreshDiscovery();
      setQueueRefreshTick((t) => t + 1);
    }, 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleScopeChanged(videoId: string) {
    const nextFilters: SourceFilters = {
      videoId,
      threadId: "",
      category: "",
      status: "",
    };
    setFilters(nextFilters);
    setSelectedComments(new Set());
    setSelectedCommentId(null);
    setSourcePage(0);
    setQueueView("sources");
    setGeneration(null);
    await refreshComments(nextFilters);
  }

  async function handleCompactContext() {
    const target = resolveSelectedThread();
    if (!target) {
      emitNotice("请先选择要压缩的视频与单个楼ID；不同楼上下文不会合并。", true);
      return;
    }
    if (contextBusy) return;
    setContextBusy(true);
    emitNotice("上下文压缩已开始，请等待当前文本模型返回。");
    try {
      const result = await apiPost<
        "/api/v1/persona/context/compact",
        { message?: string; detail?: string; compacted?: boolean },
        { scope: "comments"; video_id: string; thread_id: string }
      >("/api/v1/persona/context/compact", {
        body: { scope: "comments", video_id: target.videoId, thread_id: target.threadId },
      });
      const msg =
        result.message ||
        result.detail ||
        (result.compacted === false
          ? "没有可压缩的历史。"
          : "压缩请求已完成，请查看实际上下文占用与压缩次数。");
      emitNotice(msg);
      await refreshThreadContext();
    } catch (err) {
      emitNotice(errorMessage(err), true);
    } finally {
      setContextBusy(false);
    }
  }

  async function handleRebuildContext() {
    const target = resolveSelectedThread();
    if (!target) {
      emitNotice("请先选择评论或筛选视频与单个楼ID，再重建该楼上下文。", true);
      return;
    }
    if (contextBusy) return;
    setContextBusy(true);
    setThreadRebuildState(`正在使用本地现存原文重建 ${target.videoId} / ${target.threadId}…`);
    try {
      const result = await apiPost<
        "/api/v1/persona/context/rebuild",
        {
          message?: string;
          detail?: string;
          rebuilt?: boolean;
          prefilled?: boolean;
          input_tokens?: number | null;
        },
        { scope: "comments"; video_id: string; thread_id: string }
      >("/api/v1/persona/context/rebuild", {
        body: { scope: "comments", video_id: target.videoId, thread_id: target.threadId },
      });
      let msg =
        result.message ||
        result.detail ||
        (result.rebuilt === false
          ? "本次未重建上下文，请检查该楼现存原文。"
          : `楼 ${target.videoId} / ${target.threadId} 上下文已重建，请查看实际占用。`);
      if (result.prefilled === false) {
        msg += " 诊断：未执行真实模型 prefill，不能据此认定模型已预热。";
      } else if (result.prefilled === true) {
        msg += ` 模型 prefill 已完成${result.input_tokens != null ? `，输入 ${result.input_tokens} tokens` : ""}。`;
      }
      setThreadRebuildState(msg);
      emitNotice(msg);
      await refreshThreadContext();
    } catch (err) {
      const msg = errorMessage(err);
      setThreadRebuildState(`重建失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setContextBusy(false);
    }
  }

  const selectedComment = comments.find((c) => c.id === selectedCommentId) || null;
  const charName = webCharacterName(control.status?.character || sysStatus?.character);
  const selectedDraft = selectedComment?.draft;
  const contextBadgeValue =
    selectedDraft?.status || selectedComment?.continuation?.state || "选择评论";

  return (
    <section data-section="comments" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>评论与草稿</h1>
      </div>

      <CommentsImportForm
        sourcesOpen={sourcesOpen}
        onSourcesOpenChange={setSourcesOpen}
        onScopeChanged={handleScopeChanged}
      />

      <CommentQueueSection
        initialView={queueView}
        initialRouteKey={parsedRoute.routeKey}
        inferRouteView={parsedRoute.inferRouteView}
        generation={generation}
        discoveryStatus={control.discovery}
        selectedSourceCount={reconcileCommentSelection(comments, selectedComments).size}
        sourceCommentPreview={selectedComment}
        sourceComments={comments}
        refreshTick={queueRefreshTick}
        onViewChanged={(nextView) => {
          setQueueView(nextView);
          if (nextView === "sources") {
            setSourcesOpen(true);
            void refreshComments();
          }
        }}
        onShowAll={() => setGeneration(null)}
        onDraftUpdated={(draft, key) => {
          if (!key.startsWith("comments/")) return;
          setComments((prev) => {
            const updated = prev.map((c) => {
              if (
                String(c.draft?.id) === String(draft.id) ||
                String(c.id) === String(draft.comment_id)
              ) {
                return {
                  ...c,
                  draft: { ...c.draft, ...draft } as CommentRecord["draft"],
                };
              }
              return c;
            });
            if (!filters.status) {
              return updated.filter((c) => !commentFinished(c));
            }
            return updated;
          });
          setSelectedComments((prev) => {
            const next = new Set(prev);
            for (const c of comments) {
              if (
                String(c.draft?.id) === String(draft.id) ||
                String(c.id) === String(draft.comment_id)
              ) {
                next.delete(c.id);
              }
            }
            return next;
          });
        }}
        onSelectionCleared={() => {
          setSelectedCommentId(null);
          setThreadContextState("");
          setThreadRebuildState("");
        }}
        sourcesContent={
          <CommentsSourcesSection
            comments={comments}
            selectedComments={selectedComments}
            selectedCommentId={selectedCommentId}
            sourcePage={sourcePage}
            filters={filters}
            onFiltersChange={(nextFilters) => {
              setFilters(nextFilters);
              void refreshComments(nextFilters);
            }}
            onPageChange={setSourcePage}
            onSelectedCommentsChange={setSelectedComments}
            onSelectComment={(comment) => {
              setSelectedCommentId(comment?.id ?? null);
              void refreshThreadContext(comments, comment?.id ?? null);
            }}
            onCommentUpdated={(updated) => {
              setComments((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
            }}
            onRefreshComments={() => refreshComments()}
            onDraftsRequested={(taskId, ids) => {
              setGeneration({ id: taskId, commentIds: ids, selectedFirst: false });
              setQueueView("review");
              if (typeof window !== "undefined" && typeof window.history !== "undefined") {
                window.history.replaceState(
                  null,
                  "",
                  `#comments/review/task/${encodeURIComponent(taskId)}`
                );
              }
            }}
            onFocusThread={async (videoId, threadId) => {
              const nextFilters: SourceFilters = { ...filters, videoId, threadId };
              setFilters(nextFilters);
              setSourcePage(0);
              await refreshComments(nextFilters);
            }}
          />
        }
      />

      {/* Selected Comment Context */}
      <details
        id="comment-review"
        data-workspace-anchor=""
        style={{ marginTop: 16, borderTop: "1px solid var(--line)", paddingBlock: 10 }}
      >
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>所选评论上下文</summary>
        <div style={{ marginTop: 8 }}>
          <span id="draft-status" className={`badge ${badgeTone(contextBadgeValue)}`}>
            {displayLabel(contextBadgeValue)}
          </span>
          <p id="draft-source" className="quoted-text">
            {selectedComment
              ? `${selectedComment.display_name || "匿名评论者"}：${selectedComment.content}`
              : "选择一条评论，查看原文和所在楼。"}
          </p>

          {selectedComment && (
            <dl id="draft-meta" className="description-list">
              <dt>来源评论</dt>
              <dd>
                {selectedComment.video_id} / {selectedComment.comment_id}
              </dd>
              <dt>{charName ? `与 ${charName} 的关系` : "关系"}</dt>
              <dd>
                {typeof selectedComment.relationship?.affinity === "number"
                  ? `${selectedComment.relationship.affinity} / 100 · ${selectedComment.relationship.stage}`
                  : selectedComment.platform_user_id
                    ? "个性化未启用"
                    : "匿名评论者"}
              </dd>
              <dt>楼 ID</dt>
              <dd>{selectedComment.thread_id || selectedComment.comment_id}</dd>
              <dt>父评论 ID</dt>
              <dd>{selectedComment.parent_comment_id || "顶层"}</dd>
            </dl>
          )}

          <div className="actions" style={{ marginTop: 8 }}>
            {selectedComment?.platform === "bilibili" &&
              selectedComment.platform_user_id &&
              selectedComment.relationship_account_id && (
                <button
                  id="open-comment-memory"
                  type="button"
                  onClick={() => {
                    const characterId =
                      control.status?.character?.id || sysStatus?.character?.id || "";
                    if (!characterId || !selectedComment.platform_user_id) return;
                    setUserMemoryTarget({
                      platform: "bilibili",
                      userId: selectedComment.platform_user_id,
                      accountId: selectedComment.relationship_account_id!,
                      characterId,
                      characterName: charName,
                      name: selectedComment.display_name,
                      onChange: () => refreshComments(),
                    });
                  }}
                >
                  查看档案
                </button>
              )}

            {selectedDraft && (
              <a
                id="open-comment-draft"
                href={
                  selectedDraft.status === "private_handoff"
                    ? "#messages"
                    : `#comments/comments/${selectedDraft.id}`
                }
              >
                {selectedDraft.status === "private_handoff"
                  ? "到私信工作台处理"
                  : "到待办处理草稿"}
              </a>
            )}
          </div>

          <OperationContinuationCard
            value={selectedDraft ? null : selectedComment?.continuation}
            channel="comments"
            localId={selectedComment?.id}
            onRefresh={() => refreshComments()}
          />

          <details style={{ marginTop: 10 }}>
            <summary style={{ cursor: "pointer", color: "var(--muted)" }}>
              上下文调试与重建维护
            </summary>
            <p id="thread-context-state" className="hint" style={{ marginTop: 8 }}>
              {threadContextState}
            </p>

            <div className="actions">
              <button
                id="compact-comments"
                type="button"
                disabled={contextBusy}
                onClick={() => void handleCompactContext()}
              >
                压缩当前楼
              </button>
              <button
                id="rebuild-comments"
                type="button"
                disabled={contextBusy}
                onClick={() => void handleRebuildContext()}
              >
                重建上下文
              </button>
            </div>
            {threadRebuildState && (
              <p id="thread-rebuild-state" className="hint" role="status">
                {threadRebuildState}
              </p>
            )}
          </details>
        </div>
      </details>

      {/* Comment Generation Tasks */}
      <details style={{ borderTop: "1px solid var(--line)", paddingBlock: 10 }}>
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>生成任务进度</summary>
        <div className="table-scroll" style={{ marginTop: 8 }}>
          <table>
            <thead>
              <tr>
                <th>任务</th>
                <th>状态</th>
                <th>进度</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody id="comment-tasks">
              {tasks.length === 0 ? (
                <tr>
                  <td colSpan={4} className="hint">
                    暂无评论任务。选中评论后生成角色草稿。
                  </td>
                </tr>
              ) : (
                tasks.map((task) => (
                  <tr key={task.id}>
                    <td>
                      {task.kind || task.type || "生成回复"}
                      <small style={{ display: "block", color: "var(--muted)" }}>{task.id}</small>
                    </td>
                    <td>
                      <span className={`badge ${badgeTone(task.status)}`}>
                        {displayLabel(task.status)}
                      </span>
                    </td>
                    <td>
                      {task.total != null
                        ? `${task.done || 0} / ${task.total}${task.errors ? ` · ${task.errors} 失败` : ""}`
                        : task.progress ?? task.result ?? task.completed_count ?? "—"}
                    </td>
                    <td>{task.error || task.reason || "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </details>

      <UserMemoryModal
        target={userMemoryTarget}
        onClose={() => setUserMemoryTarget(null)}
      />
    </section>
  );
}
