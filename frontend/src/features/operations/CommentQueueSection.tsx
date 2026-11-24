import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  MemePickerModal,
  MentionLinksAndEditor,
  OperationContinuationCard,
  useUnsavedGuard,
  type MentionSaveItem,
} from "@/shared";
import { apiGet, apiPost } from "@/shared/api/client";
import { streamBatchPublish } from "@/shared/api/sse";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import { illustrationStatus, illustrationUrl } from "@/shared/lib/operationImage";
import { commentFinished } from "./commentState";
import { createReplyHandoff } from "./replyHandoff";
import { VideoContextTreeCard, buildVideoContextTree } from "./VideoContextTreeCard";
import styles from "./operations.module.css";
import type {
  ActivityItem,
  BrowsingIntent,
  BrowsingTask,
  CommentRecord,
  DiscoveryStatus,
  EvidenceSegment,
  QueueDetail,
  QueueListResponse,
  QueueRow,
} from "./types";

export type QueueViewName =
  | "sources"
  | "review"
  | "authorized"
  | "blocked"
  | "verify"
  | "processing"
  | "history";

export const COMMENT_QUEUE_VIEWS: readonly string[] = [
  "review",
  "authorized",
  "blocked",
  "verify",
  "processing",
  "history",
  "activity",
];

const PAGE_SIZE = 10;
const ACCEPT_ALL_MEMES = () => true;

const SOURCE_LABELS: Record<string, string> = {
  manual: "手动",
  interaction: "公开互动",
  discovery: "自主浏览",
};

const QUEUE_STATUS_LABELS: Record<string, string> = {
  pending_review: "待审核",
  approved: "已审核 · 待提交",
  publish_uncertain: "待核对",
  publishing: "提交中",
  published: "平台已受理",
  rejected: "已拒绝",
  copied: "已复制",
  operator_replied: "账号已在原楼回复",
  private_handoff: "已转私信",
  pending: "排队中",
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  partial: "部分完成",
  cancelled: "已取消",
  send_queued: "已授权待发",
  send_waiting: "等额度或恢复",
  send_blocked: "发布被拒／待处理",
  rejected_by_platform: "平台已拒绝",
  source_changed: "原楼已失效或变化",
  unverified: "原楼暂时无法核验",
  ready: "可排队",
};

const VISIBILITY_LABELS: Record<string, string> = {
  visible: "已查到公开记录",
  not_visible: "平台标记不可见",
  not_found: "暂未查到",
  check_failed: "可见性检查失败",
  not_checked: "尚未检查",
};

export function queueViewForStatus(status: string | null | undefined): QueueViewName {
  if (status === "pending_review" || status === "approved") return "review";
  if (status === "publish_uncertain") return "verify";
  if (status === "send_queued" || status === "send_waiting") return "authorized";
  if (status === "send_blocked" || status === "rejected_by_platform") return "blocked";
  if (["pending", "queued", "running", "publishing"].includes(status || "")) return "processing";
  return "history";
}

function keyOf(row: { channel: string; id: string | number } | null | undefined): string {
  return row ? `${row.channel}/${row.id}` : "";
}

function canBatchPublish(row: QueueRow): boolean {
  return row.can_publish === true && ["comments", "browsing"].includes(row.channel);
}

function receiptText(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function safeBilibiliSourceUrl(
  rawUrl: string | null | undefined,
  videoId: string | null | undefined
): string | null {
  if (rawUrl) {
    try {
      const parsed = new URL(rawUrl);
      if (
        parsed.protocol === "https:" &&
        ["www.bilibili.com", "t.bilibili.com"].includes(parsed.host)
      ) {
        return parsed.href;
      }
    } catch {
      // Fallback to video ID below
    }
  }
  if (videoId && /^BV[0-9A-Za-z]{10}$/.test(videoId)) {
    return `https://www.bilibili.com/video/${videoId}`;
  }
  return null;
}

export function EvidenceListView({ segments }: { segments: EvidenceSegment[] }) {
  return (
    <div className="evidence-list">
      {segments.map((segment, idx) => {
        const seconds = segment.start ?? segment.time ?? segment.timestamp;
        const sourceMeta = [
          segment.id?.startsWith("frame:") ? "画面观察" : "字幕原文",
          segment.cid && `CID ${segment.cid}`,
          Number.isFinite(seconds) && `${seconds} 秒`,
        ].filter(Boolean);
        return (
          <div key={segment.id || idx}>
            {sourceMeta.length > 0 && <p className="hint">{sourceMeta.join(" · ")}</p>}
            <blockquote className="quoted-text">
              {segment.text || segment.observation || segment.content || "材料正文不可用"}
            </blockquote>
          </div>
        );
      })}
    </div>
  );
}

export interface GenerationScope {
  id: string;
  commentIds?: number[];
  selectedFirst?: boolean;
  existing?: boolean;
}

interface BatchPreviewRow {
  channel: string;
  id: string;
  status: string;
  account_id?: string;
  updated_at?: number;
  source_url?: string;
  text?: string;
  illustration?: QueueRow["illustration"];
  error?: string;
}

interface BatchResultRow {
  channel: string;
  id: string;
  status: string;
  error?: string;
  receipt?: unknown;
}

export interface CommentQueueSectionProps {
  initialView?: QueueViewName;
  initialRouteKey?: string | null;
  inferRouteView?: boolean;
  generation?: GenerationScope | null;
  discoveryStatus?: DiscoveryStatus | null;
  selectedSourceCount?: number;
  sourceCommentPreview?: CommentRecord | null;
  sourceComments?: CommentRecord[];
  sourcesContent?: ReactNode;
  refreshTick?: number;
  onViewChanged?: (view: QueueViewName) => void;
  onShowAll?: () => void;
  onDraftUpdated?: (draft: Record<string, unknown>, key: string) => void;
  onSelectionCleared?: () => void;
  onUnsavedChange?: (dirty: boolean) => void;
}

function toArray<T>(value: T[] | { items?: T[] } | null | undefined): T[] {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.items)) return value.items;
  return [];
}

export function CommentQueueSection({
  initialView = "review",
  initialRouteKey = null,
  inferRouteView: initialInferView = false,
  generation: externalGeneration = null,
  discoveryStatus = null,
  selectedSourceCount = 0,
  sourceCommentPreview = null,
  sourceComments,
  sourcesContent,
  refreshTick = 0,
  onViewChanged,
  onShowAll,
  onDraftUpdated,
  onSelectionCleared,
  onUnsavedChange,
}: CommentQueueSectionProps) {
  const [view, setView] = useState<QueueViewName>(initialView);
  const [queueDisplayMode, setQueueDisplayMode] = useState<"tree" | "flat">("tree");
  const [offset, setOffset] = useState(0);
  const [routeKey, setRouteKey] = useState<string | null>(initialRouteKey);
  const [inferRouteView, setInferRouteView] = useState(initialInferView);
  const [generation, setGeneration] = useState<GenerationScope | null>(externalGeneration);

  const [rows, setRows] = useState<QueueRow[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detailRow, setDetailRow] = useState<QueueDetail | null>(null);
  const [detailsByKey, setDetailsByKey] = useState<Record<string, QueueDetail>>({});
  const [contextTasks, setContextTasks] = useState<BrowsingTask[]>([]);
  const [contextIntents, setContextIntents] = useState<BrowsingIntent[]>([]);
  const [contextActivities, setContextActivities] = useState<ActivityItem[]>([]);
  const [contextComments, setContextComments] = useState<CommentRecord[]>([]);
  const [detailPlaceholder, setDetailPlaceholder] = useState("选择一条记录查看来源和下一步操作。");
  const [selectionCleared, setSelectionCleared] = useState(false);

  const [feedback, setFeedback] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refreshingBtn, setRefreshingBtn] = useState(false);

  // Edits & cross-page checked drafts
  const [draftEdits, setDraftEdits] = useState<Map<string, string>>(() => new Map());
  const [checkedDrafts, setCheckedDrafts] = useState<Map<string, QueueRow>>(() => new Map());
  const [batchMode, setBatchMode] = useState(false);
  const [allSelected, setAllSelected] = useState(false);
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [batchProgress, setBatchProgress] = useState("");
  const [batchResults, setBatchResults] = useState<{
    mode: "save" | "queue" | "publish" | "reject";
    items: BatchResultRow[];
    submitted: Map<string, QueueRow>;
  } | null>(null);

  // Batch preview approval dialog
  const [approvalDialogData, setApprovalDialogData] = useState<{
    scopeLabel: string;
    checked: BatchPreviewRow[];
    sendItems: Array<{
      channel: string;
      id: string;
      updated_at?: number;
      expected_image: QueueRow["illustration"] | null;
      text?: string;
    }>;
  } | null>(null);
  const approvalDialogRef = useRef<HTMLDialogElement | null>(null);
  const approvalResolverRef = useRef<((confirmed: boolean) => void) | null>(null);

  // Meme Picker state
  const [memePickerOpen, setMemePickerOpen] = useState(false);

  const delegateRef = useRef(createReplyHandoff());
  const versionRef = useRef(0);
  const selectionSeqRef = useRef(0);
  const sourceDetailSeqRef = useRef(0);
  const batchScopeRef = useRef<string | null>(null);
  const selectedKeyRef = useRef<string | null>(selectedKey);
  selectedKeyRef.current = selectedKey;
  const draftEditsRef = useRef(draftEdits);
  draftEditsRef.current = draftEdits;

  const hasUnsaved = draftEdits.size > 0;
  useUnsavedGuard("comment-queue-edits", hasUnsaved);
  useEffect(() => {
    onUnsavedChange?.(hasUnsaved);
  }, [hasUnsaved, onUnsavedChange]);

  // Sync external route props
  useEffect(() => {
    setView(initialView);
    setRouteKey(initialRouteKey);
    setInferRouteView(initialInferView);
    setOffset(0);
    setSelectionCleared(false);
    if (initialView === "sources") {
      setGeneration(null);
    }
  }, [initialView, initialRouteKey, initialInferView]);

  useEffect(() => {
    setGeneration(externalGeneration);
    if (externalGeneration) {
      setView("review");
      setRouteKey(null);
      setSelectedKey(null);
      setDetailRow(null);
      setOffset(0);
      setSelectionCleared(false);
    }
  }, [externalGeneration]);

  // Reset batch scope when generation scope or review view changes
  const scopeKey = useMemo(() => {
    if (view !== "review") return null;
    if (!generation) return "";
    const params = new URLSearchParams();
    if (!generation.existing) params.set("task_id", generation.id);
    if (generation.commentIds?.length) params.set("comment_ids", generation.commentIds.join(","));
    return params.toString();
  }, [view, generation]);

  useEffect(() => {
    if (batchScopeRef.current !== scopeKey) {
      batchScopeRef.current = scopeKey;
      selectionSeqRef.current += 1;
      setSelectionLoading(false);
      setAllSelected(false);
      setBatchMode(false);
      setCheckedDrafts(new Map());
      setBatchProgress("");
    }
  }, [scopeKey]);

  const fetchDetail = useCallback(async (key: string): Promise<QueueDetail> => {
    const [rawChannel, identity] = key.split("/");
    const channel = rawChannel as "comments" | "browsing" | "jobs";
    const detail = await apiGet<
      "/api/v1/operations/comments/workbench/{channel}/{identity}",
      QueueDetail
    >("/api/v1/operations/comments/workbench/{channel}/{identity}", {
      pathParams: { channel, identity },
    });
    setDetailsByKey((prev) => ({ ...prev, [key]: detail }));
    return detail;
  }, []);

  const refreshTreeContext = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        apiGet<"/api/v1/operations/tasks", BrowsingTask[] | { items?: BrowsingTask[] }>(
          "/api/v1/operations/tasks"
        ),
        apiGet<"/api/v1/operations/intents", BrowsingIntent[] | { items?: BrowsingIntent[] }>(
          "/api/v1/operations/intents"
        ),
        apiGet<"/api/v1/operations/activity", { items?: ActivityItem[] }>(
          "/api/v1/operations/activity",
          { query: { kind: "videos", page: 1, page_size: 50 } }
        ),
        ...(sourceComments
          ? []
          : [
              apiGet<"/api/v1/operations/comments", CommentRecord[] | { items?: CommentRecord[] }>(
                "/api/v1/operations/comments",
                { query: { limit: 200 } }
              ),
            ]),
      ]);
      if (results[0].status === "fulfilled") setContextTasks(toArray(results[0].value));
      if (results[1].status === "fulfilled") setContextIntents(toArray(results[1].value));
      if (results[2].status === "fulfilled") {
        setContextActivities(toArray(results[2].value as ActivityItem[] | { items?: ActivityItem[] }));
      }
      if (results[3] && results[3].status === "fulfilled") {
        setContextComments(toArray(results[3].value as CommentRecord[] | { items?: CommentRecord[] }));
      }
    } catch {
      // Optional enrichment never blocks queue list
    }
  }, [sourceComments]);

  const clearProcessedSelection = useCallback(() => {
    setSelectedKey(null);
    setRouteKey(null);
    setInferRouteView(false);
    setDetailRow(null);
    setSelectionCleared(true);
    setDetailPlaceholder("本条已处理，可在「已执行与历史」中查看。请选择下一条记录。");
    onSelectionCleared?.();
    if (typeof window !== "undefined" && typeof window.history !== "undefined") {
      const targetHash = generation
        ? `#comments/review/task/${encodeURIComponent(generation.id)}`
        : `#comments/${view}`;
      window.history.replaceState(null, "", targetHash);
    }
  }, [generation, onSelectionCleared, view]);

  const refreshQueue = useCallback(
    async (_forceDetail = false) => {
      if (view === "sources") {
        return;
      }
      const seq = ++versionRef.current;
      if (!loaded) setFeedback("正在读取评论记录…");

      try {
        let activeView: QueueViewName = view;
        let routedDetail: QueueDetail | null = null;
        if (routeKey && inferRouteView) {
          routedDetail = await fetchDetail(routeKey);
          if (seq !== versionRef.current) return;
          activeView = queueViewForStatus(routedDetail.send_queue?.status || routedDetail.status);
          setView(activeView);
          setInferRouteView(false);
          onViewChanged?.(activeView);
        }

        const query: Record<string, string | number> = {
          view: activeView,
          limit: PAGE_SIZE,
          offset,
        };
        if (generation?.id) query.task_id = generation.id;
        if (generation?.commentIds?.length) query.comment_ids = generation.commentIds.join(",");

        const data = await apiGet<"/api/v1/operations/comments/workbench", QueueListResponse>(
          "/api/v1/operations/comments/workbench",
          { query }
        );
        if (seq !== versionRef.current) return;

        // Existing drafts fallback when task completed
        let effectiveData = data;
        if (
          generation &&
          !data.items.length &&
          data.task?.status === "completed" &&
          data.task.existing_drafts?.length
        ) {
          const existing = data.task.existing_drafts.filter(
            (row) => queueViewForStatus(row.status) === activeView
          );
          const nextOffset = offset < existing.length ? offset : 0;
          effectiveData = {
            ...data,
            total: existing.length,
            offset: nextOffset,
            items: existing.slice(nextOffset, nextOffset + PAGE_SIZE),
          };
          setGeneration((prev) =>
            prev
              ? {
                  ...prev,
                  commentIds: prev.commentIds || data.task?.comment_ids,
                  existing: true,
                }
              : prev
          );
        }

        setLoaded(true);
        setOffset(effectiveData.offset ?? offset);
        setTotal(effectiveData.total);
        setRows(effectiveData.items);
        if (effectiveData.items.length > 0) {
          void refreshTreeContext();
        }

        const prevKey = selectedKeyRef.current;
        const currentRow = effectiveData.items.find((r) => keyOf(r) === prevKey);
        let departedDetail: QueueDetail | null = null;
        let keepLocalEdit = false;

        if (
          prevKey &&
          !currentRow &&
          activeView !== "history" &&
          (!routeKey || routeKey === prevKey)
        ) {
          departedDetail = await fetchDetail(prevKey);
          if (seq !== versionRef.current) return;
          if (commentFinished(departedDetail)) {
            onDraftUpdated?.(departedDetail as unknown as Record<string, unknown>, prevKey);
            keepLocalEdit = draftEditsRef.current.has(prevKey);
            if (!keepLocalEdit) {
              clearProcessedSelection();
            }
          }
        }

        let nextSelected: string | null =
          routeKey ||
          (keepLocalEdit
            ? prevKey
            : keyOf(currentRow || (!selectionCleared ? effectiveData.items[0] : null))) ||
          null;

        if (generation && !generation.selectedFirst && !selectionCleared && effectiveData.items.length) {
          nextSelected = keyOf(effectiveData.items[0]);
          setGeneration((prev) => (prev ? { ...prev, selectedFirst: true } : prev));
        }

        setSelectedKey(nextSelected);

        if (generation) {
          const task = effectiveData.task;
          if (generation.existing) {
            setFeedback("所选评论已有草稿，已打开原草稿。");
          } else if (task) {
            setFeedback(
              `本次生成：${task.done || 0} / ${task.total || 0}${task.errors ? ` · ${task.errors} 条未完成` : ""} · ${QUEUE_STATUS_LABELS[task.status] || task.status}${task.error ? ` · ${task.error}` : ""}`
            );
          } else {
            setFeedback("正在读取本次生成结果…");
          }
        } else if (!effectiveData.total) {
          setFeedback("此视图暂无记录。");
        } else {
          setFeedback("");
        }

        if (!nextSelected) {
          setDetailRow(null);
          setDetailPlaceholder(
            selectionCleared
              ? "本条已处理，可在「已执行与历史」中查看。请选择下一条记录。"
              : generation
                ? ["completed", "failed", "cancelled"].includes(effectiveData.task?.status || "")
                  ? "本次任务已结束，未生成可审核的回复；请查看任务进度中的原因。"
                  : "正在生成回复，完成后会在这里显示并自动选中。"
                : "选择一条记录查看来源和下一步操作。"
          );
        } else {
          const detail =
            routedDetail ||
            (nextSelected === prevKey ? departedDetail : null) ||
            (await fetchDetail(nextSelected));
          if (seq !== versionRef.current) return;
          setDetailRow(detail);
          setActionMessage(detail.send_queue?.error || detail.error || "");
        }
      } catch (err) {
        if (seq !== versionRef.current) return;
        setFeedback(`读取失败 · ${errorMessage(err)}${loaded ? "（保留上次结果）" : ""}`);
      }
    },
    [
      view,
      loaded,
      routeKey,
      inferRouteView,
      offset,
      generation,
      selectionCleared,
      fetchDetail,
      refreshTreeContext,
      onViewChanged,
      onDraftUpdated,
      clearProcessedSelection,
    ]
  );

  useEffect(() => {
    void refreshQueue();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, offset, routeKey, generation?.id, refreshTick]);

  // Handle source comment click preview when view === "sources"
  useEffect(() => {
    if (view !== "sources") return;
    const seq = ++sourceDetailSeqRef.current;
    if (!sourceCommentPreview) {
      setSelectedKey(null);
      setDetailRow(null);
      setDetailPlaceholder("选择一条评论查看来源和草稿。");
      return;
    }
    setSelectionCleared(false);
    if (!sourceCommentPreview.draft) {
      setSelectedKey(null);
      setDetailRow(null);
      return;
    }
    const key = `comments/${sourceCommentPreview.draft.id}`;
    void fetchDetail(key)
      .then((row) => {
        if (seq === sourceDetailSeqRef.current) {
          setSelectedKey(key);
          setDetailRow(row);
          setActionMessage(row.send_queue?.error || row.error || "");
        }
      })
      .catch((err) => {
        if (seq === sourceDetailSeqRef.current) {
          setFeedback(errorMessage(err));
        }
      });
  }, [view, sourceCommentPreview, fetchDetail]);

  function handleViewSwitch(nextView: QueueViewName) {
    setGeneration(null);
    setOffset(0);
    setRouteKey(null);
    setInferRouteView(false);
    setSelectedKey(null);
    setDetailRow(null);
    setSelectionCleared(false);
    setDetailPlaceholder("选择一条记录查看来源和下一步操作。");
    onSelectionCleared?.();
    if (typeof window !== "undefined" && typeof window.history !== "undefined") {
      window.history.pushState(null, "", `#comments/${nextView}`);
    }
    setView(nextView);
    onViewChanged?.(nextView);
  }

  async function handleSelectRow(row: QueueRow) {
    const id = keyOf(row);
    setSelectionCleared(false);
    setRouteKey(id);
    setSelectedKey(id);
    try {
      const detail = await fetchDetail(id);
      setDetailRow(detail);
      setActionMessage(detail.send_queue?.error || detail.error || "");
    } catch (err) {
      setFeedback(errorMessage(err));
    }
  }

  function updateEditorText(key: string, nextValue: string, serverText: string) {
    setDraftEdits((prev) => {
      const next = new Map(prev);
      if (nextValue === serverText) next.delete(key);
      else next.set(key, nextValue);
      return next;
    });
  }

  async function postRowEndpoint(
    row: QueueDetail,
    endpoint: string,
    body?: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    const channel = row.channel || (selectedKey?.split("/")[0] ?? "comments");
    if (channel === "comments") {
      if (endpoint === "review") {
        return apiPost("/api/v1/operations/comments/drafts/{draft_id}/review", {
          pathParams: { draft_id: Number(row.id) },
          body: body as never,
        }) as Promise<Record<string, unknown>>;
      }
      if (endpoint === "publish") {
        return apiPost("/api/v1/operations/comments/drafts/{draft_id}/publish", {
          pathParams: { draft_id: Number(row.id) },
        }) as Promise<Record<string, unknown>>;
      }
      return apiPost("/api/v1/operations/comments/drafts/{draft_id}/copy", {
        pathParams: { draft_id: Number(row.id) },
      }) as Promise<Record<string, unknown>>;
    }
    if (endpoint === "review") {
      return apiPost("/api/v1/operations/intents/{identity}/review", {
        pathParams: { identity: row.id },
        body: body as never,
      }) as Promise<Record<string, unknown>>;
    }
    return apiPost("/api/v1/operations/intents/{identity}/publish", {
      pathParams: { identity: row.id },
    }) as Promise<Record<string, unknown>>;
  }

  async function handleQuickBranchReview(row: QueueRow, action: "approve" | "reject") {
    if (busy || row.channel === "jobs") return;
    const key = keyOf(row);
    setBusy(true);
    versionRef.current += 1;
    setFeedback(action === "approve" ? "正在保存审核通过…" : "正在拒绝草稿…");
    try {
      const detail = detailsByKey[key] || (await fetchDetail(key));
      const body: Record<string, unknown> =
        action === "approve"
          ? {
              action: "approve",
              text: draftEdits.get(key) ?? detail.text ?? "",
              updated_at: detail.updated_at ?? row.updated_at,
              expected_image: detail.illustration ?? row.illustration ?? null,
            }
          : { action: "reject" };
      const result = await postRowEndpoint(detail, "review", body);
      const updated = result && result.status ? { ...detail, ...result } : detail;
      onDraftUpdated?.(updated as unknown as Record<string, unknown>, key);
      setDraftEdits((prev) => {
        const next = new Map(prev);
        next.delete(key);
        return next;
      });
      if (commentFinished(updated as { status?: string }) && selectedKeyRef.current === key) {
        clearProcessedSelection();
      }
      setBusy(false);
      await refreshQueue(true);
      setFeedback(action === "approve" ? "已保存，草稿审核通过。" : "已退回，草稿已保存。");
    } catch (err) {
      setBusy(false);
      await refreshQueue(true);
      setFeedback(errorMessage(err));
    }
  }

  const videoTree = useMemo(
    () =>
      buildVideoContextTree({
        rows,
        tasks: contextTasks,
        intents: contextIntents,
        activities: contextActivities,
        comments: sourceComments ?? contextComments,
        detailsByKey,
      }),
    [rows, contextTasks, contextIntents, contextActivities, sourceComments, contextComments, detailsByKey]
  );

  async function runRowAction(
    labelText: string,
    handler: () => Promise<Record<string, unknown> | void>,
    successText?: string,
    clearEdits = true
  ) {
    if (busy || !detailRow || !selectedKey) return;
    const key = selectedKey;
    const rowSnapshot = detailRow;
    setBusy(true);
    versionRef.current += 1;
    setActionMessage(`${labelText}中…`);
    setFeedback(`${labelText}中…`);
    try {
      const result = await handler();
      const updated = result && result.status ? { ...rowSnapshot, ...result } : rowSnapshot;
      onDraftUpdated?.(updated as unknown as Record<string, unknown>, key);
      if (clearEdits) {
        setDraftEdits((prev) => {
          const next = new Map(prev);
          next.delete(key);
          return next;
        });
      }
      if (commentFinished(updated as { status?: string }) && selectedKeyRef.current === key) {
        setDraftEdits((prev) => {
          const next = new Map(prev);
          next.delete(key);
          return next;
        });
        clearProcessedSelection();
      }
      setBusy(false);
      await refreshQueue(true);
      const statusStr = String((result as { status?: string } | undefined)?.status || "");
      const statusDoneMsg =
        statusStr === "published"
          ? "已发送"
          : statusStr === "publish_uncertain"
            ? "发布结果待核对"
            : statusStr === "rejected"
              ? "已退回，草稿已保存。"
              : "";
      setFeedback(successText || statusDoneMsg);
    } catch (err) {
      setBusy(false);
      await refreshQueue(true);
      setFeedback(errorMessage(err));
    }
  }

  async function handleBatchToggleAll() {
    if (busy || selectionLoading || view !== "review") return;
    if (allSelected) {
      setCheckedDrafts(new Map());
      setAllSelected(false);
      return;
    }
    const seq = ++selectionSeqRef.current;
    setSelectionLoading(true);
    setBatchProgress("正在读取所有页的可发布草稿…");
    try {
      const query: Record<string, string> = {};
      if (generation && !generation.existing) query.task_id = generation.id;
      if (generation?.commentIds?.length) query.comment_ids = generation.commentIds.join(",");
      const data = await apiGet<
        "/api/v1/operations/comments/workbench/selection",
        { items: QueueRow[]; total: number }
      >("/api/v1/operations/comments/workbench/selection", { query });
      if (seq !== selectionSeqRef.current) return;
      const next = new Map<string, QueueRow>();
      for (const row of data.items) {
        const k = keyOf(row);
        next.set(k, checkedDrafts.get(k) || row);
      }
      setCheckedDrafts(next);
      setAllSelected(next.size > 0);
      setBatchProgress(next.size > 0 ? "" : "当前筛选没有可发布的草稿。");
    } catch (err) {
      if (seq === selectionSeqRef.current) {
        setBatchProgress(`全选失败：${errorMessage(err)}`);
      }
    } finally {
      if (seq === selectionSeqRef.current) setSelectionLoading(false);
    }
  }

  function promptApprovalModal(data: NonNullable<typeof approvalDialogData>): Promise<boolean> {
    setApprovalDialogData(data);
    return new Promise((resolve) => {
      approvalResolverRef.current = resolve;
      setTimeout(() => {
        const dlg = approvalDialogRef.current;
        if (dlg && !dlg.open) {
          if (typeof dlg.showModal === "function") dlg.showModal();
          else dlg.setAttribute("open", "");
        }
      }, 0);
    });
  }

  function resolveApprovalModal(confirmed: boolean) {
    const dlg = approvalDialogRef.current;
    if (dlg?.open) {
      if (typeof dlg.close === "function") dlg.close(confirmed ? "confirm" : "cancel");
      else dlg.removeAttribute("open");
    }
    setApprovalDialogData(null);
    const resolve = approvalResolverRef.current;
    approvalResolverRef.current = null;
    resolve?.(confirmed);
  }

  async function handleFinishBatchReview(mode: "save" | "queue") {
    if (busy || selectionLoading || view !== "review" || checkedDrafts.size === 0) return;
    const submitted = new Map(checkedDrafts);
    const items = [...submitted].map(([key, row]) => ({
      channel: row.channel,
      id: row.id,
      updated_at: row.updated_at,
      expected_image: row.illustration ?? null,
      ...(draftEdits.has(key) ? { text: draftEdits.get(key) } : {}),
    }));

    setBusy(true);
    versionRef.current += 1;
    setBatchResults(null);
    setBatchProgress(mode === "save" ? "正在保存审核结果…" : "正在核对所选草稿的原楼…");

    try {
      let checked: BatchPreviewRow[] = [];
      let send = items;
      if (mode === "queue") {
        const previewRes = await apiPost<
          "/api/v1/operations/comments/workbench/batch-review/{action}",
          { items: BatchPreviewRow[] },
          { items: typeof items }
        >("/api/v1/operations/comments/workbench/batch-review/{action}", {
          pathParams: { action: "preview" },
          body: { items },
        });
        checked = previewRes.items;
        const readyKeys = new Set(checked.filter((r) => r.status === "ready").map(keyOf));
        send = items.filter((r) => readyKeys.has(keyOf(r)));
        const accountId = checked.find((r) => r.account_id)?.account_id ?? "待核验";
        const confirmed = await promptApprovalModal({
          scopeLabel: `B 站账号 UID ${accountId} · ${send.length} 条可排队`,
          checked,
          sendItems: send,
        });
        if (!confirmed) {
          setBatchProgress("");
          return;
        }
      }

      const result = await apiPost<
        "/api/v1/operations/comments/workbench/batch-review/{action}",
        { items: BatchResultRow[] },
        { items: typeof send }
      >("/api/v1/operations/comments/workbench/batch-review/{action}", {
        pathParams: { action: mode },
        body: { items: send },
      });

      const combined = [...result.items, ...checked.filter((r) => r.status !== "ready")];
      setBatchResults({ mode, items: combined, submitted });

      const nextEdits = new Map(draftEdits);
      const nextChecked = new Map(checkedDrafts);
      for (const resRow of result.items) {
        if (resRow.status === "approved" || resRow.status === "send_queued") {
          const k = keyOf(resRow);
          nextEdits.delete(k);
          nextChecked.delete(k);
        }
      }
      setDraftEdits(nextEdits);
      setCheckedDrafts(nextChecked);
      setAllSelected(false);
      setBatchProgress("");
    } catch (err) {
      setBatchProgress(errorMessage(err));
    } finally {
      setBusy(false);
      await refreshQueue(true);
    }
  }

  async function handleBatchStreamPublish() {
    if (busy || selectionLoading || view !== "review" || checkedDrafts.size === 0) return;
    const submitted = new Map(checkedDrafts);
    const items = [...submitted].map(([key, row]) => ({
      channel: row.channel,
      id: row.id,
      updated_at: row.updated_at,
      expected_image: row.illustration ?? null,
      ...(draftEdits.has(key) ? { text: draftEdits.get(key) } : {}),
    }));

    setBusy(true);
    versionRef.current += 1;
    setBatchResults(null);
    setBatchProgress(`正在逐条发布 ${items.length} 条评论…`);

    let doneCount = 0;
    const liveItems: BatchResultRow[] = [];
    try {
      const doneRes = await streamBatchPublish<
        { items: BatchResultRow[] },
        { items: typeof items },
        BatchResultRow
      >({
        body: { items },
        onProgress(item) {
          doneCount += 1;
          liveItems.push(item);
          setBatchProgress(`已处理 ${doneCount} / ${items.length} 条…`);
          setBatchResults({ mode: "publish", items: [...liveItems], submitted });
        },
      });
      const finalItems = doneRes.items || liveItems;
      setBatchResults({ mode: "publish", items: finalItems, submitted });

      const nextEdits = new Map(draftEdits);
      const nextChecked = new Map(checkedDrafts);
      for (const resRow of finalItems) {
        if (resRow.status === "published") {
          const k = keyOf(resRow);
          nextEdits.delete(k);
          nextChecked.delete(k);
        }
      }
      setDraftEdits(nextEdits);
      setCheckedDrafts(nextChecked);
      setAllSelected(false);
      setBatchProgress("");
    } catch (err) {
      setBatchProgress(errorMessage(err));
    } finally {
      setBusy(false);
      await refreshQueue(true);
    }
  }

  async function handleBatchDelegate() {
    if (busy || selectionLoading || checkedDrafts.size === 0) return;
    const selectedRows = [...checkedDrafts.values()];
    const items = selectedRows.map((row) => ({
      channel: "comments" as const,
      draft_id: Number(row.id),
      source_ids: [Number(row.comment_id)],
    }));
    setBusy(true);
    try {
      await delegateRef.current(items);
      setBatchProgress(`已交办 ${items.length} 条回复，可在对话的活动中逐条审批。`);
      setCheckedDrafts(new Map());
      setAllSelected(false);
    } catch (err) {
      setBatchProgress(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleBatchReject(itemsOverride?: Array<{ channel: string; id: string; updated_at?: number }>, submittedOverride?: Map<string, QueueRow>) {
    if (busy || selectionLoading || view !== "review") return;
    const submitted = submittedOverride || new Map(checkedDrafts);
    const items =
      itemsOverride ||
      [...submitted].map(([, row]) => ({
        channel: row.channel,
        id: row.id,
        updated_at: row.updated_at,
      }));
    if (!items.length) return;
    if (!window.confirm(`确认批量拒绝这 ${items.length} 条草稿？拒绝后将移入历史记录，不会发送到 B 站。`)) {
      return;
    }
    setBusy(true);
    versionRef.current += 1;
    setBatchResults(null);
    setBatchProgress(`正在批量拒绝 ${items.length} 条草稿…`);
    try {
      const result = await apiPost<
        "/api/v1/operations/comments/workbench/batch-review/{action}",
        { items: BatchResultRow[] },
        { items: typeof items }
      >("/api/v1/operations/comments/workbench/batch-review/{action}", {
        pathParams: { action: "reject" },
        body: { items },
      });
      setBatchResults({ mode: "reject", items: result.items, submitted });
      const nextEdits = new Map(draftEdits);
      const nextChecked = new Map(checkedDrafts);
      for (const resRow of result.items) {
        if (resRow.status === "rejected") {
          const k = keyOf(resRow);
          nextEdits.delete(k);
          nextChecked.delete(k);
        }
      }
      setDraftEdits(nextEdits);
      setCheckedDrafts(nextChecked);
      setAllSelected(false);
      setBatchProgress("");
    } catch (err) {
      setBatchProgress(errorMessage(err));
    } finally {
      setBusy(false);
      await refreshQueue(true);
    }
  }

  async function handleRejectErrored() {
    if (busy || selectionLoading || view !== "review") return;
    setSelectionLoading(true);
    setBatchProgress("正在扫描报错或来源失效的待审草稿…");
    try {
      const query: Record<string, string> = { errored_only: "true" };
      if (generation && !generation.existing) query.task_id = generation.id;
      if (generation?.commentIds?.length) query.comment_ids = generation.commentIds.join(",");
      const data = await apiGet<
        "/api/v1/operations/comments/workbench/selection",
        { items: QueueRow[]; total: number }
      >("/api/v1/operations/comments/workbench/selection", { query });
      if (!data.items.length) {
        setBatchProgress("当前筛选没有报错的待审草稿。");
        return;
      }
      const submitted = new Map<string, QueueRow>(data.items.map((row) => [keyOf(row), row]));
      const items = data.items.map((row) => ({
        channel: row.channel,
        id: row.id,
        updated_at: row.updated_at,
      }));
      setSelectionLoading(false);
      await handleBatchReject(items, submitted);
    } catch (err) {
      setBatchProgress(`扫描报错草稿失败：${errorMessage(err)}`);
    } finally {
      setSelectionLoading(false);
    }
  }

  const selectedRowsList = [...checkedDrafts.values()];
  const hasUnpublishableChecked = selectedRowsList.some((row) => !canBatchPublish(row));
  const batchDelegateDisabled =
    busy ||
    selectionLoading ||
    checkedDrafts.size === 0 ||
    selectedRowsList.length > 100 ||
    selectedRowsList.some(
      (row) => row.channel !== "comments" || !row.comment_id || draftEdits.has(keyOf(row))
    );

  // Detail pane computed variables
  const detailChannel = selectedKey?.split("/")[0] || detailRow?.channel || "comments";
  const detailStatus = detailRow?.status || "";
  const sendQueue = detailRow?.send_queue;
  const editable = ["pending_review", "approved", "copied"].includes(detailStatus);
  const currentEditorText = selectedKey
    ? draftEdits.get(selectedKey) ?? detailRow?.text ?? ""
    : "";
  const detailDirty = Boolean(selectedKey && draftEdits.has(selectedKey));
  const awaitingImage = detailRow?.illustration_choice === "awaiting_choice";
  const hasImageJob = Boolean(detailRow?.image_ref?.job_id || detailRow?.target?.image_ref?.job_id);
  const canPublish = Boolean(detailRow?.can_publish && !sendQueue);
  const needsImagePreview = awaitingImage || (detailDirty && hasImageJob);
  const imageRef = detailRow?.illustration;

  const idleDiscovery =
    view === "review" &&
    total === 0 &&
    !generation &&
    discoveryStatus?.state === "off" &&
    !discoveryStatus?.overlay_enabled;

  return (
    <section id="comment-queue">
      <div className={styles.queueToolbar}>
        <fieldset id="queue-status-options" className={styles.queueStatusOptions}>
          <legend>评论处理</legend>
          {(
            [
              ["sources", "选择评论"],
              ["review", "待审核"],
              ["authorized", "已授权待发"],
              ["blocked", "发布被拒／待处理"],
              ["verify", "待核对"],
              ["processing", "处理中"],
              ["history", "已执行与历史"],
            ] as const
          ).map(([val, label]) => (
            <label key={val}>
              <input
                type="radio"
                name="queue-status"
                value={val}
                checked={view === val}
                onChange={() => handleViewSwitch(val)}
              />
              <span>{label}</span>
            </label>
          ))}
        </fieldset>

        <div className="actions">
          {view === "review" && (
            <button
              id="queue-reject-errored"
              type="button"
              disabled={busy || selectionLoading}
              onClick={() => void handleRejectErrored()}
            >
              清理异常草稿 (仅异常)
            </button>
          )}
          {generation && (
            <button
              id="queue-show-all"
              type="button"
              onClick={() => {
                setGeneration(null);
                onShowAll?.();
                handleViewSwitch("review");
              }}
            >
              全部草稿
            </button>
          )}
          <button
            id="refresh-queue"
            type="button"
            disabled={busy || refreshingBtn}
            onClick={async () => {
              if (busy) return;
              setRefreshingBtn(true);
              try {
                await refreshQueue(true);
              } finally {
                setRefreshingBtn(false);
              }
            }}
          >
            刷新
          </button>
        </div>
      </div>

      <p id="queue-feedback" className="hint" role="status">
        {feedback}
        {total === 0 && !generation && view !== "sources" && loaded && (
          <>
            {" "}
            <a href="#automation">
              {idleDiscovery ? "前往开始自动刷视频" : "查看自动刷视频状态"}
            </a>
          </>
        )}
      </p>

      <div id="queue-records" className={styles.reviewLayout}>
        {/* Left List Column */}
        <article className="win-group-box">
          <div className="panel-heading">
            <h2 id="queue-list-title">
              {view === "sources"
                ? "选择要回复的评论"
                : generation
                  ? "本次生成的回复"
                  : "评论草稿"}
            </h2>
            {view === "sources" ? (
              <span id="selected-comment-count" className="hint">
                已选 {selectedSourceCount} 条
              </span>
            ) : (
              <div className={styles.viewModeToggle} role="group" aria-label="列表展示模式">
                <button
                  type="button"
                  className={`${styles.viewModeBtn} ${
                    queueDisplayMode === "tree" ? styles.viewModeBtnActive : ""
                  }`.trim()}
                  aria-pressed={queueDisplayMode === "tree"}
                  onClick={() => setQueueDisplayMode("tree")}
                >
                  树形视图
                </button>
                <button
                  type="button"
                  className={`${styles.viewModeBtn} ${
                    queueDisplayMode === "flat" ? styles.viewModeBtnActive : ""
                  }`.trim()}
                  aria-pressed={queueDisplayMode === "flat"}
                  onClick={() => setQueueDisplayMode("flat")}
                >
                  平铺列表
                </button>
              </div>
            )}
          </div>

          {view === "sources" ? (
            sourcesContent
          ) : (
            <>
              {view === "review" && batchMode && (
                <div id="queue-batch-actions" className="actions" style={{ marginBlock: 8 }}>
                  <button
                    id="queue-batch-toggle"
                    type="button"
                    disabled={busy || selectionLoading}
                    onClick={() => void handleBatchToggleAll()}
                  >
                    {allSelected ? "全不选" : "全选所有页"}
                  </button>
                  <span id="queue-batch-count" className="hint" aria-live="polite">
                    已选 {checkedDrafts.size} 条
                  </span>
                  <button
                    id="queue-batch-save"
                    type="button"
                    disabled={busy || selectionLoading || checkedDrafts.size === 0 || hasUnpublishableChecked}
                    onClick={() => void handleFinishBatchReview("save")}
                  >
                    保存审核结果
                  </button>
                  <button
                    id="queue-batch-publish"
                    className="primary"
                    type="button"
                    disabled={busy || selectionLoading || checkedDrafts.size === 0 || hasUnpublishableChecked}
                    onClick={() => void handleFinishBatchReview("queue")}
                  >
                    审核并排队发送
                  </button>
                  <button
                    id="queue-batch-stream-publish"
                    type="button"
                    disabled={busy || selectionLoading || checkedDrafts.size === 0 || hasUnpublishableChecked}
                    onClick={() => void handleBatchStreamPublish()}
                  >
                    立即逐条发布
                  </button>
                  <button
                    id="queue-batch-reject"
                    type="button"
                    disabled={busy || selectionLoading || checkedDrafts.size === 0}
                    onClick={() => void handleBatchReject()}
                  >
                    批量拒绝所选
                  </button>
                  <button
                    id="queue-batch-delegate"
                    type="button"
                    disabled={batchDelegateDisabled || hasUnpublishableChecked}
                    onClick={() => void handleBatchDelegate()}
                  >
                    交办逐条送审
                  </button>
                </div>
              )}

              {batchProgress && (
                <p id="queue-batch-progress" className="hint" role="status">
                  {batchProgress}
                </p>
              )}

              {batchResults && (
                <details id="queue-batch-result" open>
                  <summary id="queue-batch-summary" aria-live="polite">
                    {(() => {
                      const targetStatus =
                        batchResults.mode === "save"
                          ? "approved"
                          : batchResults.mode === "publish"
                            ? "published"
                            : batchResults.mode === "reject"
                              ? "rejected"
                              : "send_queued";
                      const done = batchResults.items.filter(
                        (r) => r.status === targetStatus
                      ).length;
                      const verb =
                        batchResults.mode === "save"
                          ? "已保存审核"
                          : batchResults.mode === "publish"
                            ? "已直接发布"
                            : batchResults.mode === "reject"
                              ? "已批量拒绝"
                              : "已入发送队列";
                      return `${verb} ${done} 条 · 另有 ${batchResults.items.length - done} 条待处理`;
                    })()}
                  </summary>
                  <ul id="queue-batch-results">
                    {batchResults.items.map((res) => {
                      const key = keyOf(res);
                      const orig = batchResults.submitted.get(key);
                      return (
                        <li key={key}>
                          <a href={`#comments/${key}`}>
                            {orig?.excerpt?.slice(0, 80) || "查看草稿"}
                          </a>
                          {" · "}
                          {QUEUE_STATUS_LABELS[res.status] || res.status}
                          {res.error ? ` · ${safeMessage(res.error)}` : ""}
                        </li>
                      );
                    })}
                  </ul>
                </details>
              )}

              {queueDisplayMode === "tree" ? (
                <VideoContextTreeCard
                  tree={videoTree}
                  view={view}
                  selectedKey={selectedKey}
                  checkedDrafts={checkedDrafts}
                  busy={busy}
                  selectionLoading={selectionLoading}
                  onSelectRow={handleSelectRow}
                  onToggleCheck={(row, checked) => {
                    if (busy || selectionLoading) return;
                    const id = keyOf(row);
                    setBatchMode(true);
                    setAllSelected(false);
                    setCheckedDrafts((prev) => {
                      const next = new Map(prev);
                      if (checked) next.set(id, row);
                      else next.delete(id);
                      return next;
                    });
                  }}
                  onQuickApprove={(row) => handleQuickBranchReview(row, "approve")}
                  onQuickReject={(row) => handleQuickBranchReview(row, "reject")}
                />
              ) : (
                <div id="queue-list">
                  {rows.map((row) => {
                    const id = keyOf(row);
                    const isCurrent = id === selectedKey;
                    const illUrl = row.illustration
                      ? illustrationUrl(row.channel, row.id, row.illustration)
                      : "";
                    const excerpt =
                      (row.excerpt || QUEUE_STATUS_LABELS[row.status] || "评论记录").slice(0, 80) +
                      ((row.excerpt?.length || 0) > 80 ? "…" : "");
                    const isChecked = checkedDrafts.has(id);

                    const linkEl = (
                      <a
                        href={`#comments/${view}/${id}`}
                        data-queue-key={id}
                        className={styles.queueRow}
                        aria-current={isCurrent ? "true" : undefined}
                        onClick={(event) => {
                          if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
                            return;
                          }
                          event.preventDefault();
                          void handleSelectRow(row);
                        }}
                      >
                        {row.illustration &&
                          (illUrl ? (
                            <img src={illUrl} alt="回复配图" width={52} height={52} />
                          ) : (
                            <small>{illustrationStatus(row.illustration)}</small>
                          ))}
                        <span className="hint">
                          {row.source ? SOURCE_LABELS[row.source] || row.source : "来源"} ·{" "}
                          {QUEUE_STATUS_LABELS[row.status] || row.status}
                        </span>
                        <strong>{excerpt}</strong>
                        <span className="hint">
                          {row.video_id || (row.channel === "jobs" ? "生成任务" : "")}
                        </span>
                        {row.send_error && (
                          <small className="hint">{safeMessage(row.send_error)}</small>
                        )}
                        {row.status === "send_waiting" && row.send_wake_at && (
                          <small className="hint">
                            预计 {new Date(row.send_wake_at * 1000).toLocaleString("zh-CN")} 恢复
                          </small>
                        )}
                      </a>
                    );

                    if (view === "review" && row.channel !== "jobs") {
                      return (
                        <div key={id} className={styles.queueCheckRow} data-queue-key={id}>
                          <label>
                            <input
                              type="checkbox"
                              data-queue-key={id}
                              aria-label={`选择草稿：${(row.excerpt || row.video_id || "评论草稿").slice(0, 80)}`}
                              checked={isChecked}
                              disabled={busy || selectionLoading}
                              onChange={(e) => {
                                if (busy || selectionLoading) return;
                                setBatchMode(true);
                                setAllSelected(false);
                                setCheckedDrafts((prev) => {
                                  const next = new Map(prev);
                                  if (e.target.checked) next.set(id, row);
                                  else next.delete(id);
                                  return next;
                                });
                              }}
                            />
                          </label>
                          {linkEl}
                        </div>
                      );
                    }
                    return <div key={id}>{linkEl}</div>;
                  })}
                </div>
              )}

              {total > PAGE_SIZE && (
                <div className="actions" style={{ marginTop: 12 }}>
                  <button
                    id="queue-prev"
                    type="button"
                    disabled={offset <= 0}
                    onClick={() => {
                      setSelectedKey(null);
                      setDetailRow(null);
                      onSelectionCleared?.();
                      setOffset(Math.max(0, offset - PAGE_SIZE));
                    }}
                  >
                    上一页
                  </button>
                  <button
                    id="queue-next"
                    type="button"
                    disabled={offset + rows.length >= total}
                    onClick={() => {
                      setSelectedKey(null);
                      setDetailRow(null);
                      onSelectionCleared?.();
                      setOffset(offset + PAGE_SIZE);
                    }}
                  >
                    下一页
                  </button>
                  <span id="queue-count-label" className="hint">
                    {total ? `${offset + 1}–${offset + rows.length} / ${total}` : ""}
                  </span>
                </div>
              )}
            </>
          )}
        </article>

        {/* Right Detail Column */}
        <article
          className="win-group-box review-detail draft-panel"
          id="queue-detail"
          aria-label="记录详情"
        >
          {view === "sources" && sourceCommentPreview && !sourceCommentPreview.draft ? (
            <>
              <h2>{sourceCommentPreview.display_name || "匿名观众"}</h2>
              <blockquote className="quoted-text">{sourceCommentPreview.content}</blockquote>
              <p className="hint">勾选这条评论并生成回复，即可在这里直接审核。</p>
            </>
          ) : !detailRow ? (
            <>
              <h2>记录详情</h2>
              <p className="hint">{detailPlaceholder}</p>
            </>
          ) : detailStatus === "private_handoff" ? (
            <>
              <h2>{QUEUE_STATUS_LABELS.private_handoff}</h2>
              <a href="#messages">到私信工作台处理</a>
            </>
          ) : detailChannel === "jobs" ? (
            <>
              <h2>{QUEUE_STATUS_LABELS[detailStatus] || "生成任务"}</h2>
              <p>
                已处理 {detailRow.done ?? 0} / {detailRow.total ?? 0} 条 · 失败{" "}
                {detailRow.errors ?? 0} 条
              </p>
              {detailRow.error && <p className="error-text">{detailRow.error}</p>}
            </>
          ) : (
            (() => {
              const target = detailRow.target || {};
              const video = detailRow.video_id || target.bvid;
              const original = detailRow.source_text || target.source_text;
              const summaryMeta = [
                video,
                detailRow.author || target.recipient_name,
                detailRow.thread_id && `楼 ${detailRow.thread_id}`,
              ].filter(Boolean);
              const traceMeta = [
                detailRow.account_id && `账号 ${detailRow.account_id}`,
                (detailRow.platform_user_id || target.recipient) &&
                  `对方 UID ${detailRow.platform_user_id || target.recipient}`,
                (detailRow.platform_comment_id || target.parent) &&
                  `父评 ${detailRow.platform_comment_id || target.parent}`,
                detailRow.character_id,
              ].filter(Boolean);
              const hasDebugDetails = Boolean(
                traceMeta.length > 0 ||
                  detailRow.round_id ||
                  detailRow.evidence_segments?.length ||
                  detailRow.unavailable_materials ||
                  detailRow.answer_check ||
                  detailRow.receipt ||
                  detailRow.image_uploads?.length
              );
              const sourceUrl = safeBilibiliSourceUrl(detailRow.source_url, video);
              const illUrl = imageRef ? illustrationUrl(detailChannel, detailRow.id, imageRef) : "";
              const fieldLabel =
                detailDirty && commentFinished(detailRow)
                  ? "本地未保存修改"
                  : detailStatus === "published"
                    ? "回复正文"
                    : "草稿正文";

              let fallbackInfo = "";
              if (editable && !detailRow.can_publish) {
                if (imageRef && !detailRow.can_attach_image) {
                  fallbackInfo = "此楼使用文字回复，可移除原配图。";
                } else if (detailRow.unavailable_materials) {
                  fallbackInfo = "先重新取材并核对来源，再提交。";
                } else {
                  fallbackInfo = "配置 B 站写入凭证后可发布。";
                }
              } else if (detailStatus === "publish_uncertain" && !detailRow.continuation) {
                fallbackInfo = "请打开对应 B 站评论楼核对。";
              }
              const displayMsg = actionMessage || sendQueue?.error || detailRow.error || fallbackInfo;

              return (
                <>
                  <h2>{QUEUE_STATUS_LABELS[sendQueue?.status || detailStatus] || "记录详情"}</h2>
                  {summaryMeta.length > 0 && <p className="hint">{summaryMeta.join(" · ")}</p>}
                  {sendQueue?.status === "send_waiting" && sendQueue.wake_at && (
                    <p className="hint">
                      预计 {new Date(sendQueue.wake_at * 1000).toLocaleString("zh-CN")} 恢复
                    </p>
                  )}
                  {original && <blockquote className="quoted-text">{original}</blockquote>}
                  {sourceUrl && (
                    <a href={sourceUrl} target="_blank" rel="noreferrer">
                      在 B 站查看来源
                    </a>
                  )}

                  {detailRow.illustration_choice === "text_only" && <small>纯文字</small>}
                  {imageRef && (
                    <div>
                      {illUrl && (
                        <img
                          src={illUrl}
                          alt="将随这条评论发布的图片"
                          className={styles.commentCardImage}
                        />
                      )}
                      <small>{illustrationStatus(imageRef)}</small>
                    </div>
                  )}

                  <label>
                    {fieldLabel}
                    <textarea
                      rows={5}
                      readOnly={!editable || busy}
                      value={currentEditorText}
                      onChange={(e) =>
                        selectedKey &&
                        updateEditorText(selectedKey, e.target.value, detailRow.text ?? "")
                      }
                    />
                  </label>

                  <MentionLinksAndEditor
                    mentions={detailRow.mentions}
                    pending={detailRow.pending_mentions}
                    editable={editable}
                    onSave={async (mentions: MentionSaveItem[]) => {
                      if (busy) return;
                      setBusy(true);
                      try {
                        await postRowEndpoint(detailRow, "review", {
                          action: "edit",
                          text: currentEditorText,
                          updated_at: detailRow.updated_at,
                          mentions,
                        });
                        if (selectedKey) {
                          setDraftEdits((prev) => {
                            const next = new Map(prev);
                            next.delete(selectedKey);
                            return next;
                          });
                        }
                        setBusy(false);
                        await refreshQueue(true);
                        setFeedback("提及已保存，草稿待审核。");
                      } catch (err) {
                        setFeedback(errorMessage(err));
                      } finally {
                        setBusy(false);
                      }
                    }}
                  />

                  <div className="actions" style={{ marginBlock: 12 }}>
                    {editable &&
                      (detailRow.can_attach_image ||
                        detailRow.image_ref ||
                        detailRow.target?.image_ref) && (
                        <>
                          {detailRow.can_attach_image && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => setMemePickerOpen(true)}
                            >
                              选择配图
                            </button>
                          )}
                          {imageRef && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                void runRowAction("使用纯文字", () =>
                                  postRowEndpoint(detailRow, "review", {
                                    action: "edit",
                                    text: currentEditorText,
                                    image_ref: null,
                                    updated_at: detailRow.updated_at,
                                  })
                                )
                              }
                            >
                              使用纯文字
                            </button>
                          )}
                          {detailRow.can_attach_image && hasImageJob && (
                            <>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() =>
                                  void runRowAction("保存正文", () =>
                                    postRowEndpoint(detailRow, "review", {
                                      action: "edit",
                                      text: currentEditorText,
                                      updated_at: detailRow.updated_at,
                                    })
                                  )
                                }
                              >
                                保存正文
                              </button>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() =>
                                  void runRowAction("重新选图", async () => {
                                    if (currentEditorText !== (detailRow.text ?? "")) {
                                      throw new Error("请先保存正文");
                                    }
                                    return postRowEndpoint(detailRow, "review", {
                                      action: "reselect_image",
                                      updated_at: detailRow.updated_at,
                                      expected_image: imageRef ?? null,
                                    });
                                  })
                                }
                              >
                                重新选图
                              </button>
                            </>
                          )}
                        </>
                      )}

                    {editable && (
                      <>
                        <button
                          type="button"
                          className="primary"
                          disabled={busy || needsImagePreview}
                          onClick={() =>
                            void runRowAction(
                              sendQueue ? "保存并撤回排队" : "保存",
                              () =>
                                postRowEndpoint(detailRow, "review", {
                                  action: "approve",
                                  text: currentEditorText,
                                  updated_at: detailRow.updated_at,
                                  expected_image: imageRef ?? null,
                                }),
                              "已保存，草稿审核通过。"
                            )
                          }
                        >
                          {sendQueue ? "保存并撤回排队" : "保存"}
                        </button>

                        {sendQueue && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void runRowAction(
                                "取消排队",
                                () =>
                                  postRowEndpoint(detailRow, "review", {
                                    action: "approve",
                                    updated_at: detailRow.updated_at,
                                    expected_image: imageRef ?? null,
                                  }),
                                "已取消排队。",
                                false
                              )
                            }
                          >
                            取消排队
                          </button>
                        )}

                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void runRowAction("拒绝", () =>
                              postRowEndpoint(detailRow, "review", { action: "reject" })
                            )
                          }
                        >
                          拒绝
                        </button>

                        <button
                          type="button"
                          disabled={!canPublish || busy || needsImagePreview}
                          onClick={() =>
                            void runRowAction("发布", async () => {
                              if (detailStatus !== "approved" || detailDirty) {
                                await postRowEndpoint(detailRow, "review", {
                                  action: "approve",
                                  text: currentEditorText,
                                  updated_at: detailRow.updated_at,
                                  expected_image: imageRef ?? null,
                                });
                                if (selectedKey) {
                                  setDraftEdits((prev) => {
                                    const next = new Map(prev);
                                    next.delete(selectedKey);
                                    return next;
                                  });
                                }
                              }
                              return postRowEndpoint(detailRow, "publish");
                            })
                          }
                        >
                          发布
                        </button>

                        <button
                          type="button"
                          disabled={busy || Boolean(imageRef)}
                          onClick={() =>
                            void runRowAction(
                              "复制",
                              async () => {
                                await navigator.clipboard.writeText(currentEditorText);
                                if (
                                  detailChannel === "comments" &&
                                  detailStatus === "approved" &&
                                  !detailDirty
                                ) {
                                  return postRowEndpoint(detailRow, "copy");
                                }
                                setActionMessage("已复制回复");
                              },
                              "已复制回复。",
                              false
                            )
                          }
                        >
                          复制
                        </button>
                      </>
                    )}

                    {!editable && detailRow.text && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void runRowAction(
                            "复制",
                            async () => {
                              await navigator.clipboard.writeText(currentEditorText);
                            },
                            "已复制回复。",
                            false
                          )
                        }
                      >
                        复制
                      </button>
                    )}
                  </div>

                  {displayMsg && (
                    <p
                      className={sendQueue?.error || detailRow.error ? "error-text" : "hint"}
                      role="status"
                    >
                      {displayMsg}
                    </p>
                  )}

                  <OperationContinuationCard
                    value={detailRow.continuation}
                    channel={detailChannel === "browsing" ? "browsing" : "comments"}
                    localId={detailRow.id}
                    dirty={detailDirty}
                    entityStatus={detailStatus}
                    onRefresh={() => refreshQueue(true)}
                  />

                  {hasDebugDetails && (
                    <details className={styles.debugDisclosure}>
                      <summary>技术追踪与原始回执</summary>
                      <div className={styles.debugDisclosureBody}>
                        {traceMeta.length > 0 && <p className="hint">{traceMeta.join(" · ")}</p>}
                        {detailRow.round_id && (
                          <p>
                            <a href={`#automation/round/${encodeURIComponent(detailRow.round_id)}`}>
                              查看来源轮次
                            </a>
                          </p>
                        )}
                        {Boolean(detailRow.image_uploads?.length) && (
                          <details className="subsection">
                            <summary>图片上传回执</summary>
                            <pre className="code-small">{receiptText(detailRow.image_uploads)}</pre>
                          </details>
                        )}
                        {Boolean(detailRow.evidence_segments?.length) && (
                          <details className="subsection">
                            <summary>引用原文（{detailRow.evidence_segments!.length}）</summary>
                            <EvidenceListView segments={detailRow.evidence_segments!} />
                          </details>
                        )}
                        {Boolean(detailRow.unavailable_materials) && (
                          <p className="hint">{detailRow.unavailable_materials} 项原材料已失效或清理。</p>
                        )}
                        {Boolean(detailRow.answer_check) && (
                          <details className="subsection">
                            <summary>来源与完整性检查</summary>
                            <pre className="operation-revision-text">
                              {receiptText(detailRow.answer_check)}
                            </pre>
                          </details>
                        )}
                        {Boolean(detailRow.receipt) && (
                          <details className="subsection">
                            <summary>平台回执</summary>
                            <pre className="operation-revision-text">{receiptText(detailRow.receipt)}</pre>
                          </details>
                        )}
                      </div>
                    </details>
                  )}

                  {detailRow.visibility && detailStatus === "published" && (
                    <p className="hint">
                      平台可见性 · {VISIBILITY_LABELS[detailRow.visibility] || detailRow.visibility}
                    </p>
                  )}
                </>
              );
            })()
          )}
        </article>
      </div>

      {/* Batch Queue Approval Dialog */}
      <dialog
        ref={approvalDialogRef}
        id="queue-approval"
        aria-labelledby="queue-approval-title"
        onCancel={(event) => {
          event.preventDefault();
          resolveApprovalModal(false);
        }}
      >
        <h2 id="queue-approval-title">确认排队发送</h2>
        <p id="queue-approval-scope">{approvalDialogData?.scopeLabel || ""}</p>
        <ol id="queue-approval-items">
          {(approvalDialogData?.checked || []).map((row) => {
            const illUrl = row.illustration
              ? illustrationUrl(row.channel, row.id, row.illustration)
              : "";
            return (
              <li key={keyOf(row)}>
                <strong>{QUEUE_STATUS_LABELS[row.status] || row.status}</strong>
                {row.source_url && (
                  <>
                    {" · "}
                    <a href={row.source_url} target="_blank" rel="noreferrer">
                      查看原楼
                    </a>
                  </>
                )}
                {row.updated_at && ` · ${new Date(row.updated_at * 1000).toLocaleString("zh-CN")}`}
                {row.text && <p className="quoted-text">{row.text}</p>}
                {illUrl && (
                  <a href={illUrl} target="_blank" rel="noreferrer">
                    查看配图
                  </a>
                )}
                {row.error && <p className="hint">{safeMessage(row.error)}</p>}
              </li>
            );
          })}
        </ol>
        <div className="actions">
          <button type="button" onClick={() => resolveApprovalModal(false)}>
            取消
          </button>
          <button
            id="queue-approval-submit"
            type="button"
            className="primary"
            disabled={!approvalDialogData?.sendItems.length}
            onClick={() => resolveApprovalModal(true)}
          >
            确认排队
          </button>
        </div>
      </dialog>

      {/* Meme Picker for Comment Illustration */}
      <MemePickerModal
        open={memePickerOpen}
        selectedAssetId={imageRef?.asset_id}
        title="选择评论配图"
        description="选图后与正文一同送审。"
        showSources
        accept={ACCEPT_ALL_MEMES}
        onClose={() => setMemePickerOpen(false)}
        onSelect={async (_id, asset) => {
          setMemePickerOpen(false);
          if (!detailRow || !asset) return;
          await postRowEndpoint(detailRow, "review", {
            action: "edit",
            text: currentEditorText,
            image_ref: {
              asset_id: asset.asset_id,
              sha256: (asset as { sha256?: string }).sha256,
            },
            updated_at: detailRow.updated_at,
          });
          if (selectedKey) {
            setDraftEdits((prev) => {
              const next = new Map(prev);
              next.delete(selectedKey);
              return next;
            });
          }
          await refreshQueue(true);
        }}
      />
    </section>
  );
}
