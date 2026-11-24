import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import { ApiError, apiDelete, apiGet, apiPost } from "@/shared/api/client";
import { streamAgentChat, subscribeAgentChatEvents } from "@/shared/api/sse";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { useCharacterDisplay } from "@/shared/hooks/useCharacterDisplay";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import {
  calmChatMessage,
  createRequestId,
  dispatchAutomationSync,
  type AutomationControlStatus,
  type ChatHistoryResponse,
  type ChatRequestPayload,
  type ChatSubmissionPayload,
  type ChatTurn,
  type MemoryUpdatedItem,
  type OpenedArtifactRef,
  type PetCharacterInfo,
  type ToolActivityItem,
} from "./types";

const PAGE_SIZE = 20;

const STAGE_LABELS: Record<string, string> = {
  queued: "等待推理",
  preparing: "正在整理上下文",
  preflight: "正在检查模型",
  checking_model: "正在检查模型",
  counting: "正在准备模型输入",
  generating: "正在回复",
  working: "正在查询或安排任务",
  constrained: "需要调整或确认条件",
};

const ACTIVE_TASK_STATUSES = new Set([
  "queued",
  "scheduled",
  "running",
  "waiting",
  "needs_input",
  "awaiting_approval",
  "paused",
]);

const ACTIVE_VIDEO_STATUSES = new Set(["queued", "waiting", "running", "pending", "starting"]);

const LIVE_HANDOFF_ACTIVE = new Set([
  "queued",
  "generating",
  "synthesizing",
  "ready_to_play",
  "playing",
]);

const CONTINUOUS_REFRESH_CARDS = new Set([
  "video_activity",
  "public_experiences",
  "memory_summary",
  "live_studio_hud",
  "live_confirmation",
]);

export interface TransientTurn {
  request_id: string;
  message: string;
  stageText: string;
  partial: string;
  toolActivities: ToolActivityItem[];
  memoryUpdates: MemoryUpdatedItem[];
}

export interface ChatStatusMessage {
  text: string;
  isError: boolean;
  canRetry: boolean;
}

type ScrollIntent =
  | { kind: "bottom" }
  | { kind: "preserve"; prevHeight: number; anchorRequestId?: string; anchorTop?: number }
  | null;

interface InFlightState {
  request: ChatRequestPayload;
  stopping: boolean;
  controller: AbortController;
}

interface RetryState {
  key: string;
  request: ChatRequestPayload;
  payload: ChatSubmissionPayload;
  fromComposer: boolean;
  onAccepted?: () => void;
}

export interface SubmitChatOptions {
  fromComposer?: boolean;
  onAccepted?: () => void;
}

export interface UseOperatorChatStreamOptions {
  timelineRef: RefObject<HTMLDivElement | null>;
  controlsPending: boolean;
}

function buildScopeKey(res: ChatHistoryResponse): string {
  return `${res.character_id || res.character}/${res.character_version ?? ""}/${res.account_id || ""}/${res.conversation_id || ""}`;
}

function mergeLatestTurns(prev: ChatTurn[], incoming: ChatTurn[]): ChatTurn[] {
  const incomingIds = new Set(incoming.map((turn) => turn.request_id));
  const hiddenIds = new Set(
    incoming.filter((turn) => turn.hidden).map((turn) => turn.request_id)
  );
  const olderPrefix = prev.filter(
    (turn) => !incomingIds.has(turn.request_id) && !hiddenIds.has(turn.request_id) && !turn.hidden
  );
  const visibleIncoming = incoming.filter((turn) => !turn.hidden);
  return [...olderPrefix, ...visibleIncoming];
}

function prependOlderTurns(prev: ChatTurn[], older: ChatTurn[]): ChatTurn[] {
  const existingIds = new Set(prev.map((turn) => turn.request_id));
  const incomingIds = new Set(older.map((turn) => turn.request_id));
  const visibleOlder = older.filter((turn) => !turn.hidden && !existingIds.has(turn.request_id));
  const remainingPrev = prev.filter((turn) => {
    if (!incomingIds.has(turn.request_id)) return true;
    const match = older.find((item) => item.request_id === turn.request_id);
    return !match?.hidden;
  });
  return [...visibleOlder, ...remainingPrev];
}

function upsertSingleTurn(prev: ChatTurn[], turn: ChatTurn): ChatTurn[] {
  if (turn.hidden) {
    return prev.filter((item) => item.request_id !== turn.request_id);
  }
  const index = prev.findIndex((item) => item.request_id === turn.request_id);
  if (index >= 0) {
    const existing = prev[index];
    const next = [...prev];
    next[index] = {
      ...turn,
      tool_activities: turn.tool_activities ?? existing.tool_activities,
      memory_updates: turn.memory_updates ?? existing.memory_updates,
    };
    return next;
  }
  return [...prev, turn];
}

function cardNeedsRefresh(turn: ChatTurn): boolean {
  if (turn.memory_dependent || turn.assistant_only) return true;
  const card = turn.card;
  if (!card) return false;
  const status = String(card.data.status || "");
  if (card.type === "agent_task") return ACTIVE_TASK_STATUSES.has(status);
  if (card.type === "video_browsing") return ACTIVE_VIDEO_STATUSES.has(status);
  if (card.type === "dynamic_draft") {
    return !["published", "rejected", "invalidated"].includes(status);
  }
  if (card.type === "live_handoff") return LIVE_HANDOFF_ACTIVE.has(status);
  return CONTINUOUS_REFRESH_CARDS.has(card.type);
}

function parseToolActivity(raw: unknown): ToolActivityItem | null {
  if (typeof raw !== "object" || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  const id = typeof rec.id === "string" && rec.id ? rec.id : "";
  const tool = typeof rec.tool === "string" && rec.tool ? rec.tool : "tool";
  const label = typeof rec.label === "string" && rec.label ? rec.label : tool;
  const status = typeof rec.status === "string" && rec.status ? rec.status : "running";
  if (!id && !label) return null;
  return {
    id: id || `${tool}-${label}`,
    tool,
    action: typeof rec.action === "string" ? rec.action : undefined,
    label,
    status,
    summary: typeof rec.summary === "string" ? calmChatMessage(rec.summary) : undefined,
    detail: typeof rec.detail === "string" ? calmChatMessage(rec.detail) : undefined,
    data: typeof rec.data === "object" && rec.data !== null ? (rec.data as Record<string, unknown>) : undefined,
  };
}

function parseMemoryUpdated(raw: unknown): MemoryUpdatedItem | null {
  if (typeof raw !== "object" || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  const snippet = typeof rec.snippet === "string" ? rec.snippet.trim() : "";
  if (!snippet) return null;
  const action = typeof rec.action === "string" && rec.action ? rec.action : "updated";
  const scope = typeof rec.scope === "string" && rec.scope ? rec.scope : "owner";
  return {
    action,
    scope,
    character_id: typeof rec.character_id === "string" ? rec.character_id : undefined,
    memory_id:
      typeof rec.memory_id === "string" || typeof rec.memory_id === "number"
        ? rec.memory_id
        : null,
    snippet,
    count: typeof rec.count === "number" ? rec.count : undefined,
    limit: typeof rec.limit === "number" ? rec.limit : undefined,
    soft_limit_reached: typeof rec.soft_limit_reached === "boolean" ? rec.soft_limit_reached : undefined,
  };
}

function upsertToolActivity(
  list: ToolActivityItem[],
  item: ToolActivityItem
): ToolActivityItem[] {
  const index = list.findIndex((entry) => entry.id === item.id);
  if (index >= 0) {
    const next = [...list];
    next[index] = { ...list[index], ...item };
    return next;
  }
  return [...list, item];
}

function appendMemoryUpdate(
  list: MemoryUpdatedItem[],
  item: MemoryUpdatedItem
): MemoryUpdatedItem[] {
  const key = `${item.action}:${item.memory_id ?? item.snippet}`;
  if (list.some((entry) => `${entry.action}:${entry.memory_id ?? entry.snippet}` === key)) {
    return list;
  }
  return [...list, item];
}

export function useOperatorChatStream({
  timelineRef,
  controlsPending,
}: UseOperatorChatStreamOptions) {
  const { status, refreshStatus, onUnauthorized } = useAppSession();
  const display = useCharacterDisplay();

  const [characterTitle, setCharacterTitle] = useState("Aqua");
  const [petCharacter, setPetCharacter] = useState<PetCharacterInfo | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(null);
  const [olderPending, setOlderPending] = useState(false);
  const [historyNote, setHistoryNote] = useState("");
  const [pending, setPending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [transient, setTransient] = useState<TransientTurn | null>(null);
  const [statusMessage, setStatusMessage] = useState<ChatStatusMessage>({
    text: "",
    isError: false,
    canRetry: false,
  });
  const [progressText, setProgressText] = useState("");
  const [attentionCount, setAttentionCount] = useState(0);
  const [automationStatus, setAutomationStatus] = useState<AutomationControlStatus | null>(null);

  const turnsRef = useRef<ChatTurn[]>([]);
  turnsRef.current = turns;

  const pendingRef = useRef(false);
  pendingRef.current = pending;

  const updatePending = useCallback((next: boolean) => {
    pendingRef.current = next;
    setPending(next);
  }, []);

  const controlsPendingRef = useRef(false);
  controlsPendingRef.current = controlsPending;

  const epochRef = useRef(0);
  const scopeRef = useRef<string | null>(null);
  const conversationIdRef = useRef<string | null>(null);
  const recentRequestRef = useRef(0);
  const historyPendingRef = useRef(false);
  const cardRefreshPendingRef = useRef(false);
  const inFlightRef = useRef<InFlightState | null>(null);
  const retryRef = useRef<RetryState | null>(null);
  const openedArtifactRef = useRef<OpenedArtifactRef | null>(null);
  const scrollIntentRef = useRef<ScrollIntent>(null);
  const mountedRef = useRef(true);

  const nearEnd = useCallback((): boolean => {
    const timeline = timelineRef.current;
    if (!timeline) return true;
    return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 36;
  }, [timelineRef]);

  const queueScrollBottomIfNear = useCallback(() => {
    if (nearEnd()) {
      scrollIntentRef.current = { kind: "bottom" };
    }
  }, [nearEnd]);

  useLayoutEffect(() => {
    const intent = scrollIntentRef.current;
    const timeline = timelineRef.current;
    if (!intent || !timeline) return;
    scrollIntentRef.current = null;
    if (intent.kind === "bottom") {
      timeline.scrollTop = timeline.scrollHeight;
    } else if (intent.kind === "preserve") {
      if (intent.anchorRequestId && intent.anchorTop !== undefined) {
        const anchorEl = timeline.querySelector<HTMLElement>(
          `[data-request-id="${CSS.escape(intent.anchorRequestId)}"]`
        );
        if (anchorEl) {
          timeline.scrollTop += anchorEl.getBoundingClientRect().top - intent.anchorTop;
          return;
        }
      }
      timeline.scrollTop += timeline.scrollHeight - intent.prevHeight;
    }
  }, [
    turns,
    transient?.partial,
    transient?.stageText,
    transient?.toolActivities.length,
    transient?.memoryUpdates.length,
    statusMessage.text,
    timelineRef,
  ]);

  const setMessage = useCallback(
    (text: string, isError = false, canRetry = false) => {
      queueScrollBottomIfNear();
      setStatusMessage({ text, isError, canRetry });
    },
    [queueScrollBottomIfNear]
  );

  const rememberOpenedArtifact = useCallback((ref: OpenedArtifactRef | null) => {
    openedArtifactRef.current = ref;
  }, []);

  const reflectAutomation = useCallback((value: AutomationControlStatus) => {
    setAutomationStatus(value);
  }, []);

  // Keep chat automation state in lockstep with AppShell live WS / status automation snapshot
  const liveSessionAutomation = status?.automation;
  useEffect(() => {
    if (!liveSessionAutomation) return;
    setAutomationStatus((prev) => ({
      ...(prev || {}),
      state: liveSessionAutomation.state ?? prev?.state,
      policy: {
        ...(prev?.policy || {}),
        enabled: liveSessionAutomation.enabled ?? prev?.policy?.enabled,
        emergency: liveSessionAutomation.emergency ?? prev?.policy?.emergency,
      },
    }));
  }, [
    liveSessionAutomation,
    liveSessionAutomation?.state,
    liveSessionAutomation?.enabled,
    liveSessionAutomation?.emergency,
  ]);

  const refreshAttention = useCallback(async () => {
    try {
      const result = await apiGet<"/api/v1/agent/attention", { count?: number }>(
        "/api/v1/agent/attention"
      );
      if (mountedRef.current) {
        setAttentionCount(Number(result?.count) || 0);
      }
    } catch {
      // Attention count stays on its last verified value if transient polling fails.
    }
  }, []);

  const resetLocalState = useCallback(() => {
    epochRef.current += 1;
    inFlightRef.current?.controller.abort();
    inFlightRef.current = null;
    openedArtifactRef.current = null;
    retryRef.current = null;
    scopeRef.current = null;
    conversationIdRef.current = null;
    historyPendingRef.current = false;
    setPetCharacter(null);
    setTurns([]);
    setNextCursor(null);
    setOlderPending(false);
    setHistoryNote("");
    updatePending(false);
    setStopping(false);
    setTransient(null);
    setStatusMessage({ text: "", isError: false, canRetry: false });
    setProgressText("");
  }, [updatePending]);

  const refresh = useCallback(async () => {
    if (pendingRef.current) return;
    const request = ++recentRequestRef.current;
    const currentEpoch = epochRef.current;
    try {
      const result = await apiGet<"/api/v1/agent/chat", ChatHistoryResponse>("/api/v1/agent/chat", {
        query: { limit: PAGE_SIZE },
      });
      if (
        !mountedRef.current ||
        request !== recentRequestRef.current ||
        currentEpoch !== epochRef.current ||
        pendingRef.current
      ) {
        return;
      }

      const nextScope = buildScopeKey(result);
      const scopeChanged = scopeRef.current !== null && scopeRef.current !== nextScope;
      if (scopeChanged) {
        openedArtifactRef.current = null;
        retryRef.current = null;
      }
      const first = scopeRef.current === null || scopeChanged;
      const follow = first || nearEnd();
      scopeRef.current = nextScope;
      conversationIdRef.current = result.conversation_id || null;

      setCharacterTitle(result.character || "Aqua");
      setPetCharacter(
        result.character_id || result.character
          ? {
              character_id: result.character_id || result.character,
              character_version: result.character_version ?? null,
              avatar: result.avatar ?? null,
              avatar_still: result.avatar_still ?? null,
              pet: result.pet ?? null,
            }
          : null
      );

      if (follow) {
        scrollIntentRef.current = { kind: "bottom" };
      }

      setTurns((prev) =>
        scopeChanged
          ? (result.turns || []).filter((turn) => !turn.hidden)
          : mergeLatestTurns(prev, result.turns || [])
      );
      if (first) {
        setNextCursor(result.next_cursor ?? null);
      }
      setHistoryNote("");
    } catch (error) {
      if (
        mountedRef.current &&
        currentEpoch === epochRef.current &&
        request === recentRequestRef.current
      ) {
        setHistoryNote(`对话读取暂不可用：${calmChatMessage(errorMessage(error))}`);
      }
    }
  }, [nearEnd]);

  const loadOlder = useCallback(async () => {
    if (!nextCursor || historyPendingRef.current) return;
    const firstTurn = timelineRef.current?.querySelector<HTMLElement>(
      ".operator-chat-turn[data-request-id]"
    );
    const anchorRequestId = firstTurn?.dataset.requestId;
    const anchorTop = firstTurn?.getBoundingClientRect().top;
    const prevHeightBeforeLoad = timelineRef.current?.scrollHeight ?? 0;
    const currentEpoch = epochRef.current;
    historyPendingRef.current = true;
    setOlderPending(true);
    setHistoryNote("正在读取更早消息…");
    try {
      const result = await apiGet<"/api/v1/agent/chat", ChatHistoryResponse>("/api/v1/agent/chat", {
        query: { limit: PAGE_SIZE, before: nextCursor },
      });
      if (!mountedRef.current || currentEpoch !== epochRef.current) return;
      const resultScope = buildScopeKey(result);
      if (resultScope !== scopeRef.current) {
        await refresh();
        return;
      }
      setHistoryNote("");
      const prevHeight = timelineRef.current?.scrollHeight ?? prevHeightBeforeLoad;
      scrollIntentRef.current = { kind: "preserve", prevHeight, anchorRequestId, anchorTop };
      setTurns((prev) => prependOlderTurns(prev, result.turns || []));
      setNextCursor(result.next_cursor ?? null);
    } catch (error) {
      if (mountedRef.current && currentEpoch === epochRef.current) {
        setHistoryNote(`更早消息读取暂不可用：${calmChatMessage(errorMessage(error))}`);
      }
    } finally {
      if (mountedRef.current && currentEpoch === epochRef.current) {
        historyPendingRef.current = false;
        setOlderPending(false);
      }
    }
  }, [nextCursor, refresh, timelineRef]);

  const refreshCards = useCallback(async () => {
    if (!mountedRef.current || cardRefreshPendingRef.current) return;
    void refreshAttention();
    void refresh();

    const currentTurns = turnsRef.current;
    const timeline = timelineRef.current;
    const bounds = timeline?.getBoundingClientRect();

    const hasVisibleAutomationCard = currentTurns.some((turn) => {
      if (turn.card?.type !== "operation_control") return false;
      if (!timeline || !bounds) return true;
      const el = timeline.querySelector<HTMLElement>(`[data-request-id="${CSS.escape(turn.request_id)}"]`);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      return rect.top < bounds.bottom && rect.bottom > bounds.top;
    });

    if (hasVisibleAutomationCard) {
      try {
        const auto = await apiGet<"/api/v1/operations/automation", AutomationControlStatus>(
          "/api/v1/operations/automation"
        );
        if (mountedRef.current) {
          setAutomationStatus(auto);
        }
      } catch {
        // Ignore transient automation poll failure.
      }
    }

    const candidates = currentTurns
      .filter((turn) => {
        if (!cardNeedsRefresh(turn)) return false;
        if (turn.card?.type === "agent_task") return true;
        if (!timeline || !bounds) return true;
        const el = timeline.querySelector<HTMLElement>(`[data-request-id="${CSS.escape(turn.request_id)}"]`);
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        return rect.top < bounds.bottom && rect.bottom > bounds.top;
      })
      .slice(-4);

    if (!candidates.length) return;
    const currentEpoch = epochRef.current;
    cardRefreshPendingRef.current = true;
    try {
      await Promise.allSettled(
        candidates.map(async (entry) => {
          if (controlsPendingRef.current) return;
          const updated = await apiGet<"/api/v1/agent/chat/turns/{request_id}", ChatTurn>(
            "/api/v1/agent/chat/turns/{request_id}",
            { pathParams: { request_id: entry.request_id } }
          );
          if (!mountedRef.current || currentEpoch !== epochRef.current || controlsPendingRef.current) {
            return;
          }
          if (nearEnd()) {
            scrollIntentRef.current = { kind: "bottom" };
          }
          setTurns((prev) => upsertSingleTurn(prev, updated));
          if (
            entry.card?.data?.status !== updated.card?.data?.status &&
            updated.card?.data?.delivered_in_chat
          ) {
            void refresh();
          }
        })
      );
    } finally {
      cardRefreshPendingRef.current = false;
    }
  }, [nearEnd, refresh, refreshAttention, timelineRef]);

  const submit = useCallback(
    async (payload: ChatSubmissionPayload, options: SubmitChatOptions = {}) => {
      if (!payload.message || pendingRef.current) return;
      const fromComposer = options.fromComposer ?? true;
      const key = JSON.stringify(payload);
      const reuseRetry = retryRef.current?.key === key ? retryRef.current : null;
      const request: ChatRequestPayload = reuseRetry
        ? reuseRetry.request
        : {
            request_id: createRequestId(),
            ...payload,
            ...(openedArtifactRef.current && !payload.opened_artifact && !payload.intent
              ? { opened_artifact: openedArtifactRef.current }
              : {}),
            ...(conversationIdRef.current ? { conversation_id: conversationIdRef.current } : {}),
          };

      const currentEpoch = epochRef.current;
      const controller = new AbortController();
      inFlightRef.current = { request, stopping: false, controller };
      recentRequestRef.current += 1;

      updatePending(true);
      setStopping(false);
      setMessage("");
      setProgressText(payload.video_selection ? "正在安排取材…" : "正在生成回复…");

      if (nearEnd()) {
        scrollIntentRef.current = { kind: "bottom" };
      }
      setTransient({
        request_id: request.request_id,
        message: payload.message,
        stageText: "",
        partial: "",
        toolActivities: [],
        memoryUpdates: [],
      });

      let partial = "";
      let renderFrame = 0;
      let result: ChatTurn | undefined;
      let collectedTools: ToolActivityItem[] = [];
      let collectedMemories: MemoryUpdatedItem[] = [];

      const flushDelta = () => {
        renderFrame = 0;
        if (!mountedRef.current || currentEpoch !== epochRef.current) return;
        if (nearEnd()) {
          scrollIntentRef.current = { kind: "bottom" };
        }
        setTransient((prev) => (prev ? { ...prev, partial } : prev));
      };

      try {
        result = await streamAgentChat<ChatTurn, ChatRequestPayload>({
          body: request,
          signal: controller.signal,
          onUnauthorized,
          onStage(stage) {
            if (!mountedRef.current || currentEpoch !== epochRef.current || partial) return;
            const label = STAGE_LABELS[stage] || "";
            setTransient((prev) => (prev ? { ...prev, stageText: label } : prev));
            setProgressText(label);
          },
          onDelta(delta) {
            if (!mountedRef.current || currentEpoch !== epochRef.current) return;
            partial += delta;
            if (!renderFrame) {
              renderFrame = window.requestAnimationFrame(flushDelta);
            }
          },
          onEvent(event, data) {
            if (!mountedRef.current || currentEpoch !== epochRef.current) return;
            if (event === "stage" && typeof data === "object" && data !== null) {
              const stageVal = String((data as { stage?: unknown }).stage || "");
              if (stageVal && !partial) {
                const label = STAGE_LABELS[stageVal] || "";
                setTransient((prev) => (prev ? { ...prev, stageText: label } : prev));
                setProgressText(label);
              }
            } else if (event === "delta" && typeof data === "object" && data !== null) {
              const chunk = (data as { delta?: unknown; text?: unknown }).delta ??
                (data as { text?: unknown }).text;
              if (typeof chunk === "string") {
                partial += chunk;
                if (!renderFrame) {
                  renderFrame = window.requestAnimationFrame(flushDelta);
                }
              }
            } else if (event === "tool_activity") {
              const item = parseToolActivity(data);
              if (item) {
                collectedTools = upsertToolActivity(collectedTools, item);
                if (nearEnd()) {
                  scrollIntentRef.current = { kind: "bottom" };
                }
                setTransient((prev) =>
                  prev ? { ...prev, toolActivities: collectedTools } : prev
                );
                const autoState = item.data?.automation_state;
                if (typeof autoState === "string" && autoState) {
                  const isActive = autoState === "running" || autoState === "starting";
                  setAutomationStatus((prev) => ({
                    ...(prev || {}),
                    state: autoState,
                    policy: { ...(prev?.policy || {}), enabled: isActive },
                  }));
                  dispatchAutomationSync({
                    automation: { state: autoState, enabled: isActive },
                    refresh: true,
                  });
                }
              }
            } else if (event === "memory_updated") {
              const mem = parseMemoryUpdated(data);
              if (mem) {
                collectedMemories = appendMemoryUpdate(collectedMemories, mem);
                if (nearEnd()) {
                  scrollIntentRef.current = { kind: "bottom" };
                }
                setTransient((prev) =>
                  prev ? { ...prev, memoryUpdates: collectedMemories } : prev
                );
              }
            }
          },
        });

        if (!mountedRef.current || currentEpoch !== epochRef.current) return;
        if (result?.stopped) {
          retryRef.current = null;
          setProgressText("已停止生成。");
          return;
        }

        if (fromComposer) {
          options.onAccepted?.();
        }
        retryRef.current = null;
        if (
          request.opened_artifact &&
          openedArtifactRef.current &&
          request.opened_artifact.task_id === openedArtifactRef.current.task_id &&
          request.opened_artifact.task_revision === openedArtifactRef.current.task_revision &&
          request.opened_artifact.edition === openedArtifactRef.current.edition
        ) {
          openedArtifactRef.current = null;
        }

        if (nearEnd()) {
          scrollIntentRef.current = { kind: "bottom" };
        }
        const doneTools = Array.isArray(result?.tool_activities)
          ? result.tool_activities.map(parseToolActivity).filter((item): item is ToolActivityItem => Boolean(item))
          : [];
        const rawDoneMemories = result?.memory_updates ?? result?.memory_events;
        const doneMemories = Array.isArray(rawDoneMemories)
          ? rawDoneMemories.map(parseMemoryUpdated).filter((item): item is MemoryUpdatedItem => Boolean(item))
          : [];
        const finalTools = doneTools.length > 0 ? doneTools : collectedTools;
        const finalMemories = doneMemories.length > 0 ? doneMemories : collectedMemories;

        const mergedTurn: ChatTurn = {
          message: payload.message,
          ...result,
          request_id: result?.request_id || request.request_id,
          ...(finalTools.length > 0 ? { tool_activities: finalTools } : {}),
          ...(finalMemories.length > 0 ? { memory_updates: finalMemories } : {}),
        };
        setTurns((prev) => upsertSingleTurn(prev, mergedTurn));
        setMessage("");
        setProgressText("");

        if (mergedTurn.card?.type === "operation_control" && mergedTurn.card.data) {
          const cardAuto = mergedTurn.card.data as AutomationControlStatus;
          setAutomationStatus(cardAuto);
          dispatchAutomationSync({
            automation: {
              state: cardAuto.state ?? undefined,
              enabled: cardAuto.policy?.enabled,
              emergency: cardAuto.policy?.emergency,
            },
            refresh: true,
          });
        }
      } catch (error) {
        if (!mountedRef.current || currentEpoch !== epochRef.current) return;
        if (request.opened_artifact && error instanceof ApiError && error.status === 409) {
          openedArtifactRef.current = null;
        }
        retryRef.current = {
          key,
          request,
          payload,
          fromComposer,
          onAccepted: options.onAccepted,
        };
        setProgressText("");
        setMessage(calmChatMessage(errorMessage(error)), true, true);
      } finally {
        if (renderFrame) {
          window.cancelAnimationFrame(renderFrame);
        }
        if (mountedRef.current) {
          setTransient(null);
        }
        if (mountedRef.current && currentEpoch === epochRef.current) {
          inFlightRef.current = null;
          updatePending(false);
          setStopping(false);
          if (result?.tool_receipt?.group === "pets") {
            await Promise.allSettled([refresh(), display.refresh()]);
          } else if (scopeRef.current === null) {
            await refresh();
          }
          if (
            result?.card?.type === "operation_control" ||
            result?.task_delivery ||
            payload.intent?.action === "start_operations" ||
            payload.intent?.action === "stop"
          ) {
            dispatchAutomationSync({ refresh: true });
            void refreshStatus().catch(() => {});
          }
        }
      }
    },
    [display, nearEnd, onUnauthorized, refresh, refreshStatus, setMessage, updatePending]
  );

  const retryLast = useCallback(() => {
    const saved = retryRef.current;
    if (!saved || pendingRef.current) return;
    void submit(saved.payload, {
      fromComposer: saved.fromComposer,
      onAccepted: saved.onAccepted,
    });
  }, [submit]);

  const stopGeneration = useCallback(async () => {
    const current = inFlightRef.current;
    if (!current || current.stopping) return;
    current.stopping = true;
    setStopping(true);
    try {
      const result = await apiPost<
        "/api/v1/agent/chat/turns/{request_id}/stop-generation",
        { stopping?: boolean }
      >("/api/v1/agent/chat/turns/{request_id}/stop-generation", {
        pathParams: { request_id: current.request.request_id },
      });
      if (inFlightRef.current !== current || result?.stopping) return;
      setProgressText("当前请求正在收尾。");
    } catch (error) {
      if (inFlightRef.current === current) {
        setMessage(`未能停止：${calmChatMessage(errorMessage(error))}`, true, false);
      }
    }
    if (inFlightRef.current === current) {
      current.stopping = false;
      setStopping(false);
    }
  }, [setMessage]);

  const clearChat = useCallback(async (): Promise<boolean> => {
    if (
      pendingRef.current ||
      !window.confirm("清空当前角色的私聊和对话上下文？已保存的记忆和任务可在各自入口查看。")
    ) {
      return false;
    }
    const currentEpoch = epochRef.current;
    updatePending(true);
    try {
      const cleared = await apiDelete<
        "/api/v1/agent/chat",
        { ok?: boolean; conversation_id?: string | null }
      >("/api/v1/agent/chat");
      if (!mountedRef.current || currentEpoch !== epochRef.current) return true;
      resetLocalState();
      conversationIdRef.current = cleared?.conversation_id || null;
      await refresh();
      return true;
    } catch (error) {
      if (mountedRef.current && currentEpoch === epochRef.current) {
        setMessage(safeMessage(errorMessage(error)), true, false);
        updatePending(false);
      }
      return false;
    }
  }, [refresh, resetLocalState, setMessage, updatePending]);

  // Initial load + cleanup
  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    void refreshAttention();
    return () => {
      mountedRef.current = false;
      inFlightRef.current?.controller.abort();
      inFlightRef.current = null;
    };
  }, [refresh, refreshAttention]);

  // Refresh when active character or version changes in workspace status
  const activeCharacterId = status?.character?.id ?? "";
  const activeCharacterVersion = status?.character?.version ?? "";
  const prevCharKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const nextKey = `${activeCharacterId}/${activeCharacterVersion}`;
    if (prevCharKeyRef.current === null) {
      prevCharKeyRef.current = nextKey;
      return;
    }
    if (prevCharKeyRef.current !== nextKey) {
      prevCharKeyRef.current = nextKey;
      void refresh();
    }
  }, [activeCharacterId, activeCharacterVersion, refresh]);

  // Push-back SSE listener for completed background / voice-call tasks (`GET /api/v1/agent/chat/events`)
  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let streamController: AbortController | null = null;

    const connect = async () => {
      while (!cancelled) {
        streamController = new AbortController();
        try {
          await subscribeAgentChatEvents({
            signal: streamController.signal,
            onUnauthorized,
            onEvent(event, data) {
              if (cancelled || event !== "delivery") return;
              if (typeof data === "object" && data !== null && "request_id" in data) {
                const turn = data as ChatTurn;
                if (nearEnd()) {
                  scrollIntentRef.current = { kind: "bottom" };
                }
                setTurns((prev) => upsertSingleTurn(prev, turn));
                void refreshAttention();
                dispatchAutomationSync({ refresh: true });
                if (!pendingRef.current) {
                  void refresh();
                }
              }
            },
          });
        } catch (error) {
          if (cancelled || streamController.signal.aborted) break;
          if (
            error instanceof ApiError &&
            (error.status === 401 ||
              error.status === 403 ||
              error.status === 404 ||
              error.status === 405)
          ) {
            break;
          }
        }
        if (cancelled) break;
        await new Promise<void>((resolve) => {
          reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            resolve();
          }, 2000);
        });
      }
    };

    void connect();

    return () => {
      cancelled = true;
      streamController?.abort();
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
    };
  }, [nearEnd, onUnauthorized, refresh, refreshAttention]);

  // Periodic card refresh (2.5s) and history refresh (10s) when tab is visible + external event bridge
  useEffect(() => {
    const onChatChanged = () => {
      void refresh();
      void refreshAttention();
    };
    const onVisibilityChange = () => {
      if (typeof document !== "undefined" && !document.hidden) {
        void refresh();
        void refreshCards();
      }
    };
    document.addEventListener("operator-chat-changed", onChatChanged);
    document.addEventListener("visibilitychange", onVisibilityChange);

    const cardTimer = setInterval(() => {
      if (typeof document === "undefined" || !document.hidden) {
        void refreshCards();
      }
    }, 2500);
    const historyTimer = setInterval(() => {
      if (typeof document === "undefined" || !document.hidden) {
        void refresh();
      }
    }, 10000);

    return () => {
      document.removeEventListener("operator-chat-changed", onChatChanged);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearInterval(cardTimer);
      clearInterval(historyTimer);
    };
  }, [refresh, refreshAttention, refreshCards]);

  return {
    characterTitle,
    petCharacter,
    turns,
    transient,
    nextCursor,
    olderPending,
    historyNote,
    pending,
    stopping,
    statusMessage,
    progressText,
    attentionCount,
    automationStatus,
    setMessage,
    reflectAutomation,
    rememberOpenedArtifact,
    refresh,
    loadOlder,
    submit,
    retryLast,
    stopGeneration,
    clearChat,
    refreshAttention,
    refreshCards,
  };
}
