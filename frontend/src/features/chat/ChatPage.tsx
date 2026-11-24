import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { MarkdownText } from "@/shared/ui/MarkdownText";
import { inferenceDisplay } from "@/shell/InferenceStatusBadge";
import { AssistantAnswer, MemoryUpdatedPill, ToolActivityDisclosure } from "./AssistantAnswer";
import { ChatHeaderMenu, focusMenuButtonByKey } from "./ChatHeaderMenu";
import styles from "./ChatPage.module.css";
import { ChatPetFloat } from "./ChatPetFloat";
import { OperatorChatCards } from "./OperatorChatCards";
import { TaskActivityDrawer, type ActivitySection } from "./TaskActivityDrawer";
import { TemporaryChatDialog } from "./TemporaryChatDialog";
import { PrivateCallDialog } from "./call/PrivateCallDialog";
import { usePrivateCallSession } from "./call/usePrivateCallSession";
import {
  calmChatMessage,
  type ChatSubmissionPayload,
  type VideoRangeInput,
  type VideoSelectionInput,
} from "./types";
import { useOperatorChatStream } from "./useOperatorChatStream";

type ShortcutInputAction = "agent_task" | "dynamic" | "browse" | "full_video";

interface ShortcutInputMeta {
  label: string;
  placeholder?: string;
  command?: string;
}

const SHORTCUT_INPUTS: Record<ShortcutInputAction, ShortcutInputMeta> = {
  agent_task: { label: "交办任务", placeholder: "交代目标、材料和你需要的结果", command: "" },
  dynamic: { label: "写动态", placeholder: "输入动态主题", command: "发个动态" },
  browse: { label: "刷视频", placeholder: "输入话题、视频 BV 号或链接", command: "去刷视频" },
  full_video: { label: "看完整视频" },
};

const SHORTCUT_QUERIES: Record<string, string> = {
  public_experiences: "查看公开经历",
  video_activity: "今天刷了多少视频",
  operation_control: "自主运营",
  live_status: "查看直播状态",
  clock: "现在几点",
  activity: "查看运营动态",
};

const EMPTY_STARTER_PROMPTS: ReadonlyArray<{ label: string; prompt: string }> = [
  { label: "看看今天渠道动态", prompt: "今天有什么运营动态？" },
  { label: "帮我记住偏好", prompt: "记住我的沟通偏好：回答保持简洁清晰" },
  { label: "安排一次视频阅读", prompt: "去刷一个最近值得看的技术视频" },
];

interface VideoRangeDraft {
  id: number;
  part: string;
  start: string;
  end: string;
}

const DEFAULT_INPUT_PLACEHOLDER = "给 Aqua 发消息…";

function parseVideoSelection(bvidInput: string, ranges: VideoRangeDraft[]): VideoSelectionInput {
  const bvid = /^(?:https:\/\/www\.bilibili\.com\/video\/)?(BV[0-9A-Za-z]{10})(?:\/?(?:[?#].*)?)$/.exec(
    bvidInput.trim()
  )?.[1];
  if (!bvid) {
    throw new Error("请输入有效的视频 BV 号或 B 站视频链接。");
  }
  const parsedRanges: VideoRangeInput[] = ranges.map((row) => {
    const part = Number(row.part);
    const start = Number(row.start);
    const end = Number(row.end);
    if (!Number.isFinite(part) || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      throw new Error("取材时段的终点须晚于起点。");
    }
    return { part, start, end };
  });
  return { bvid, ranges: parsedRanges };
}

export function ChatPage() {
  const { status, settings } = useAppSession();

  const sectionRef = useRef<HTMLElement | null>(null);
  const timelineRef = useRef<HTMLDivElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const videoIdRef = useRef<HTMLInputElement | null>(null);
  const activityToggleRef = useRef<HTMLButtonElement | null>(null);
  const modelDetailsRef = useRef<HTMLDetailsElement | null>(null);
  const shortcutsPopupRef = useRef<HTMLDivElement | null>(null);
  const shortcutsToggleRef = useRef<HTMLButtonElement | null>(null);
  const shortcutsPanelRef = useRef<HTMLDivElement | null>(null);
  const rangeRowIdRef = useRef(0);
  const lastRangeFocusIdRef = useRef<number | null>(null);

  const [controlsPending, setControlsPending] = useState(false);
  const [openMenu, setOpenMenu] = useState<"shortcuts" | "more" | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerSection, setDrawerSection] = useState<ActivitySection>("activity");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [tempChatOpen, setTempChatOpen] = useState(false);
  const [petSlotEl, setPetSlotEl] = useState<HTMLDivElement | null>(null);

  const [selectedAction, setSelectedAction] = useState<ShortcutInputAction | null>(null);
  const [inputValue, setInputValue] = useState("");
  const [videoId, setVideoId] = useState("");
  const [videoRanges, setVideoRanges] = useState<VideoRangeDraft[]>([]);

  const chat = useOperatorChatStream({
    timelineRef,
    controlsPending,
  });

  const latestTurnRequestId = chat.turns.length
    ? chat.turns[chat.turns.length - 1].request_id
    : undefined;

  const callSession = usePrivateCallSession({
    active: true,
    characterKey: chat.petCharacter?.character_id ?? "",
    latestTurnRequestId,
    onChatChanged: () => void chat.refresh(),
  });

  const resizeInput = useCallback(() => {
    const input = inputRef.current;
    if (!input || input.hidden) return;
    const timeline = timelineRef.current;
    const follow =
      timeline ? timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 36 : false;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
    if (follow && timeline) {
      timeline.scrollTop = timeline.scrollHeight;
    }
  }, []);

  // Size chat viewport to visualViewport height so soft keyboards keep composer pinned cleanly
  useEffect(() => {
    const viewport = typeof window !== "undefined" ? window.visualViewport : null;
    const shell = sectionRef.current?.closest<HTMLElement>(".win-main-shell") ?? null;

    const sizeChat = () => {
      if (shell && viewport?.scale === 1) {
        shell.style.setProperty("--operator-chat-height", `${viewport.height}px`);
      } else if (shell) {
        shell.style.removeProperty("--operator-chat-height");
      }
      resizeInput();
    };

    sizeChat();
    viewport?.addEventListener("resize", sizeChat);
    window.addEventListener("resize", resizeInput);
    return () => {
      viewport?.removeEventListener("resize", sizeChat);
      window.removeEventListener("resize", resizeInput);
      shell?.style.removeProperty("--operator-chat-height");
    };
  }, [resizeInput]);

  useEffect(() => {
    resizeInput();
  }, [inputValue, selectedAction, resizeInput]);

  // Focus newly added video range row end input
  useEffect(() => {
    const focusId = lastRangeFocusIdRef.current;
    if (focusId === null) return;
    lastRangeFocusIdRef.current = null;
    const endInput = formRef.current?.querySelector<HTMLInputElement>(
      `[data-range-id="${focusId}"] input[name="end"]`
    );
    endInput?.focus();
  }, [videoRanges]);

  // Focus first shortcut menu item when shortcuts menu opens
  useEffect(() => {
    if (openMenu === "shortcuts") {
      shortcutsPanelRef.current
        ?.querySelector<HTMLButtonElement>("button:not(:disabled)")
        ?.focus({ preventScroll: true });
    }
  }, [openMenu]);

  // Close shortcuts menu and model error details on outside pointer or focus move
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onOutside = (event: Event) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (
        openMenu === "shortcuts" &&
        shortcutsPopupRef.current &&
        !shortcutsPopupRef.current.contains(target)
      ) {
        setOpenMenu(null);
      }
      if (modelDetailsRef.current?.open && !modelDetailsRef.current.contains(target)) {
        modelDetailsRef.current.open = false;
      }
    };
    document.addEventListener("pointerdown", onOutside);
    document.addEventListener("focusin", onOutside);
    return () => {
      document.removeEventListener("pointerdown", onOutside);
      document.removeEventListener("focusin", onOutside);
    };
  }, [openMenu]);

  const focusComposer = useCallback(
    (actionOverride?: ShortcutInputAction | null) => {
      const targetAction = actionOverride === undefined ? selectedAction : actionOverride;
      window.requestAnimationFrame(() => {
        if (targetAction === "full_video") {
          videoIdRef.current?.focus({ preventScroll: true });
        } else {
          inputRef.current?.focus({ preventScroll: true });
        }
      });
    },
    [selectedAction]
  );

  const selectAction = useCallback((action: ShortcutInputAction | null) => {
    setSelectedAction(action);
  }, []);

  const addVideoRangeRow = useCallback(() => {
    if (chat.pending) return;
    const nextId = ++rangeRowIdRef.current;
    lastRangeFocusIdRef.current = nextId;
    setVideoRanges((prev) => [...prev, { id: nextId, part: "1", start: "0", end: "" }]);
  }, [chat.pending]);

  const removeVideoRangeRow = useCallback((id: number) => {
    setVideoRanges((prev) => prev.filter((row) => row.id !== id));
  }, []);

  const updateVideoRangeField = useCallback(
    (id: number, field: "part" | "start" | "end", value: string) => {
      setVideoRanges((prev) =>
        prev.map((row) => (row.id === id ? { ...row, [field]: value } : row))
      );
    },
    []
  );

  const buildSubmission = useCallback((): ChatSubmissionPayload => {
    if (selectedAction === "full_video") {
      const selection = parseVideoSelection(videoId, videoRanges);
      return {
        message: `看完整视频：${selection.bvid}`,
        intent: { action: "full_video" },
        video_selection: selection,
      };
    }

    const topic = inputValue.trim();
    const shortcut = selectedAction ? SHORTCUT_INPUTS[selectedAction] : null;
    if (!shortcut || !selectedAction) {
      return { message: topic };
    }

    const hasVideoFields =
      selectedAction === "agent_task" && (Boolean(videoId.trim()) || videoRanges.length > 0);
    const selection = hasVideoFields ? parseVideoSelection(videoId, videoRanges) : undefined;
    const command = shortcut.command || "";
    const message = command && topic ? `${command}：${topic}` : topic || command;
    const intent: ChatSubmissionPayload["intent"] =
      topic && selectedAction !== "agent_task"
        ? { action: selectedAction, topic }
        : { action: selectedAction };

    return {
      ...(selection ? { video_selection: selection } : {}),
      message,
      intent,
    };
  }, [inputValue, selectedAction, videoId, videoRanges]);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (chat.pending) return;
    let payload: ChatSubmissionPayload;
    try {
      payload = buildSubmission();
    } catch (error) {
      chat.setMessage(calmChatMessage(error instanceof Error ? error.message : String(error)), true);
      return;
    }

    const submittedValue = inputValue;
    const submittedAction = selectedAction;
    void chat.submit(payload, {
      fromComposer: true,
      onAccepted: () => {
        if (payload.video_selection) {
          if (submittedAction === "agent_task") {
            setInputValue((cur) => (cur === submittedValue ? "" : cur));
          }
          setVideoId("");
          setVideoRanges([]);
          selectAction(null);
        } else {
          setInputValue((cur) => (cur === submittedValue ? "" : cur));
          selectAction(null);
        }
        focusComposer(null);
      },
    });
  };

  const handleComposerKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing &&
      event.keyCode !== 229
    ) {
      event.preventDefault();
      formRef.current?.requestSubmit();
    }
  };

  const liveAuto = chat.automationStatus ?? (status?.automation
    ? {
        state: status.automation.state,
        policy: {
          enabled: status.automation.enabled,
          emergency: status.automation.emergency,
        },
      }
    : null);
  const liveAutoState = liveAuto?.state || "";
  const liveAutoEmergency = liveAuto?.policy?.emergency === true;
  const liveAutoStarting = liveAutoState === "starting";
  const liveAutoDraining = liveAutoState === "draining" || liveAutoState === "stopping";
  const liveAutoRunning =
    !liveAutoEmergency &&
    (liveAutoState === "running" ||
      (liveAuto?.policy?.enabled === true &&
        !liveAutoStarting &&
        !liveAutoDraining &&
        liveAutoState !== "off" &&
        liveAutoState !== "blocked"));

  const quickOpsDisabled =
    chat.pending || controlsPending || liveAutoStarting || liveAutoDraining || liveAutoEmergency;
  const quickOpsLabel = liveAutoEmergency
    ? "运营已紧停"
    : liveAutoStarting
      ? "运营启动中…"
      : liveAutoDraining
        ? "运营收尾中…"
        : liveAutoRunning
          ? "停止运营"
          : "开始运营";
  const quickOpsState = liveAutoEmergency
    ? "emergency"
    : liveAutoStarting
      ? "starting"
      : liveAutoDraining
        ? "stopping"
        : liveAutoRunning
          ? "running"
          : "off";

  const handleQuickOperationsToggle = () => {
    if (quickOpsDisabled) return;
    setOpenMenu(null);
    if (liveAutoRunning) {
      void chat.submit(
        { message: "停止自主运营", intent: { action: "stop" } },
        { fromComposer: false }
      );
    } else {
      void chat.submit(
        { message: "开始自主运营", intent: { action: "start_operations" } },
        { fromComposer: false }
      );
    }
  };

  const handleShortcutClick = (action: string) => {
    if (chat.pending) return;
    setOpenMenu(null);
    if (action === "toggle_operations") {
      handleQuickOperationsToggle();
      return;
    }
    if (Object.hasOwn(SHORTCUT_QUERIES, action)) {
      focusComposer();
      void chat.submit(
        { message: SHORTCUT_QUERIES[action], intent: { action } },
        { fromComposer: false }
      );
      return;
    }
    if (action in SHORTCUT_INPUTS) {
      const typed = action as ShortcutInputAction;
      selectAction(typed);
      focusComposer(typed);
    }
  };

  const handleClearChat = async () => {
    const cleared = await chat.clearChat();
    if (cleared) {
      setDrawerOpen(false);
      setInputValue("");
      setVideoId("");
      setVideoRanges([]);
      selectAction(null);
      focusComposer(null);
    }
  };

  const modelInfo = inferenceDisplay(status, settings);
  const modelFailed = modelInfo.connected === false;
  const modelDetail = modelFailed
    ? calmChatMessage(status?.context?.profile_error || "Omni API 暂未连接")
    : "";

  const videoMode = selectedAction === "full_video";
  const showVideoFields = videoMode || selectedAction === "agent_task";
  const currentShortcut = selectedAction ? SHORTCUT_INPUTS[selectedAction] : null;
  const inFlight = Boolean(chat.transient);
  const hasComposerInput = videoMode
    ? Boolean(videoId.trim())
    : Boolean(inputValue.trim() || selectedAction);
  const showEmptyState =
    chat.turns.length === 0 && !chat.transient && !chat.nextCursor && !chat.historyNote;

  return (
    <section
      ref={sectionRef}
      data-section="chat"
      className={`operator-chat ${styles.chatRoot}`}
    >
      <header className="operator-chat-heading">
        <h1 id="operator-chat-title">{chat.characterTitle}</h1>
        <div className="operator-chat-heading-actions">
          <button
            type="button"
            id="operator-chat-ops-quick"
            className="chat-ops-quick"
            data-state={quickOpsState}
            disabled={quickOpsDisabled}
            aria-busy={liveAutoStarting || liveAutoDraining}
            onClick={handleQuickOperationsToggle}
          >
            <span className="chat-ops-dot" aria-hidden="true" />
            <span>{quickOpsLabel}</span>
          </button>
          <ChatHeaderMenu
            open={openMenu === "more"}
            pending={chat.pending}
            character={chat.petCharacter}
            onToggleOpen={() => {
              if (modelDetailsRef.current) modelDetailsRef.current.open = false;
              setOpenMenu((prev) => (prev === "more" ? null : "more"));
            }}
            onClose={() => setOpenMenu((prev) => (prev === "more" ? null : prev))}
            onOpenTemporaryChat={() => setTempChatOpen(true)}
            onClearChat={() => void handleClearChat()}
            onNotify={(msg, isErr) => chat.setMessage(msg, Boolean(isErr))}
          />
          <button
            id="private-call-open"
            className="chat-activity-toggle"
            type="button"
            aria-label="私有语音通话"
            hidden={!callSession.available}
            disabled={!callSession.available}
            onClick={() => void callSession.openDialog()}
          >
            电话
          </button>
          <button
            ref={activityToggleRef}
            id="chat-activity-toggle"
            className="chat-activity-toggle"
            type="button"
            aria-expanded={drawerOpen}
            aria-controls="chat-activity"
            aria-label={chat.attentionCount ? `面板，${chat.attentionCount} 项更新` : "面板"}
            onClick={() => {
              setDrawerOpen((prev) => {
                const next = !prev;
                if (next) {
                  setSelectedTaskId(null);
                  setDrawerSection("activity");
                }
                return next;
              });
            }}
          >
            {chat.attentionCount ? `面板 · ${chat.attentionCount}` : "面板"}
          </button>
        </div>

        <span
          id="operator-chat-model-state"
          className="operator-chat-model-state"
          role="status"
          hidden={modelFailed}
        >
          {modelFailed ? "" : modelInfo.connection}
        </span>
        <details
          ref={modelDetailsRef}
          id="operator-chat-model-details"
          className="operator-chat-model-state operator-chat-model-details"
          hidden={!modelFailed}
          onKeyDown={(event) => {
            if (event.key === "Escape" && event.currentTarget.open) {
              event.currentTarget.open = false;
              event.currentTarget.querySelector("summary")?.focus({ preventScroll: true });
            }
          }}
        >
          <summary>连接异常</summary>
          <div className="operator-chat-menu">
            <p id="operator-chat-model-error">{modelDetail}</p>
            <a href="#settings">查看 Omni 服务能力</a>
          </div>
        </details>
      </header>

      <div id="operator-chat-timeline" ref={timelineRef} className="operator-chat-timeline">
        <ChatPetFloat
          character={chat.petCharacter}
          callDialogOpen={callSession.dialogOpen}
          petSlotEl={petSlotEl}
        />
        <div className="operator-chat-older">
          <button
            id="operator-chat-older"
            type="button"
            hidden={!chat.nextCursor}
            disabled={chat.olderPending}
            onClick={() => void chat.loadOlder()}
          >
            加载更早消息
          </button>
          <p
            id="operator-chat-history-note"
            className="hint"
            role="status"
            hidden={!chat.historyNote}
          >
            {chat.historyNote}
          </p>
        </div>

        {showEmptyState && (
          <div className="operator-chat-empty">
            <p className="operator-chat-empty-title">有什么可以帮你的？</p>
            <div className="operator-chat-starters">
              {EMPTY_STARTER_PROMPTS.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  className="operator-chat-starter"
                  onClick={() => {
                    setInputValue(item.prompt);
                    focusComposer();
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
        )}

        <div id="operator-chat-feed" className="operator-chat-feed" role="log" aria-label="私聊消息">
          {chat.turns.map((turn) => {
            const suppressCard = Boolean(
              (turn.sources?.length &&
                ["operation_events", "operation_event"].includes(turn.card?.type || "")) ||
                (turn.memory_updates?.length && turn.card?.type === "memory_change")
            );
            return (
              <article
                key={turn.request_id}
                className="operator-chat-turn"
                data-request-id={turn.request_id}
              >
                {!turn.assistant_only && <p className="operator-chat-user">{turn.message}</p>}
                <div
                  className="operator-chat-reply"
                  title={turn.character_name || undefined}
                >
                  <AssistantAnswer turn={turn} />
                </div>
                {turn.card && !suppressCard && (
                  <div className="operator-chat-card">
                    <OperatorChatCards
                      turn={turn}
                      sharedAutomation={chat.automationStatus}
                      controlsPending={controlsPending}
                      onControlsPendingChange={setControlsPending}
                      onReflectAutomation={chat.reflectAutomation}
                      onOpenTask={(taskId) => {
                        setDrawerSection("activity");
                        setSelectedTaskId(taskId);
                        setDrawerOpen(true);
                      }}
                      onOpenArtifact={chat.rememberOpenedArtifact}
                      onChatChanged={() => void chat.refresh()}
                    />
                  </div>
                )}
              </article>
            );
          })}

          {chat.transient && (
            <article className="operator-chat-turn" data-request-id={chat.transient.request_id}>
              <p className="operator-chat-user">{chat.transient.message}</p>
              <div className="operator-chat-reply">
                {chat.transient.toolActivities.length > 0 && (
                  <ToolActivityDisclosure
                    activities={chat.transient.toolActivities}
                    defaultOpen
                  />
                )}
                {chat.transient.partial ? (
                  <MarkdownText allowLinks={false} text={chat.transient.partial} />
                ) : chat.transient.stageText ? (
                  <span className="chat-thinking-stage">
                    <span className="chat-thinking-dot" aria-hidden="true" />
                    <span>{chat.transient.stageText}</span>
                  </span>
                ) : chat.transient.toolActivities.length === 0 ? (
                  <span className="chat-thinking-stage">
                    <span className="chat-thinking-dot" aria-hidden="true" />
                    <span>思考中…</span>
                  </span>
                ) : null}
                {chat.transient.memoryUpdates.length > 0 && (
                  <MemoryUpdatedPill updates={chat.transient.memoryUpdates} />
                )}
              </div>
            </article>
          )}
        </div>
      </div>

      <TaskActivityDrawer
        open={drawerOpen}
        section={drawerSection}
        selectedTaskId={selectedTaskId}
        onSelectView={(taskId, sec) => {
          setSelectedTaskId(taskId);
          if (sec) setDrawerSection(sec);
        }}
        onClose={(restoreFocus) => {
          setDrawerOpen(false);
          if (restoreFocus) {
            activityToggleRef.current?.focus({ preventScroll: true });
          }
        }}
        onArtifactOpened={chat.rememberOpenedArtifact}
        onAttentionChanged={() => void chat.refreshAttention()}
        onChatChanged={() => void chat.refresh()}
      />

      <form
        ref={formRef}
        id="operator-chat-form"
        className="operator-chat-compose"
        onSubmit={handleSubmit}
      >
        <fieldset
          id="operator-chat-video-fields"
          className="operator-chat-video-fields"
          hidden={!showVideoFields}
          disabled={chat.pending || !showVideoFields}
        >
          <legend>指定视频</legend>
          <label>
            视频 BV 号或链接
            <input
              ref={videoIdRef}
              id="operator-chat-video-id"
              required={videoMode || videoRanges.length > 0}
              placeholder="BV… 或视频链接"
              value={videoId}
              onChange={(event) => setVideoId(event.target.value)}
            />
          </label>
          <div id="operator-chat-video-ranges">
            {videoRanges.map((row) => (
              <div key={row.id} data-range-id={row.id} className="operator-chat-range">
                <label>
                  分 P
                  <input
                    type="number"
                    name="part"
                    min={1}
                    step={1}
                    required
                    value={row.part}
                    onChange={(event) => updateVideoRangeField(row.id, "part", event.target.value)}
                  />
                </label>
                <label>
                  起点（秒）
                  <input
                    type="number"
                    name="start"
                    min={0}
                    step="any"
                    required
                    value={row.start}
                    onChange={(event) => updateVideoRangeField(row.id, "start", event.target.value)}
                  />
                </label>
                <label>
                  终点（秒）
                  <input
                    type="number"
                    name="end"
                    min={0}
                    step="any"
                    required
                    value={row.end}
                    onChange={(event) => updateVideoRangeField(row.id, "end", event.target.value)}
                  />
                </label>
                <button type="button" onClick={() => removeVideoRangeRow(row.id)}>
                  移除
                </button>
              </div>
            ))}
          </div>
          <div className="actions">
            <button
              id="operator-chat-add-range"
              type="button"
              disabled={chat.pending}
              onClick={addVideoRangeRow}
            >
              限定分 P 和时段
            </button>
          </div>
        </fieldset>

        <span
          id="operator-chat-status"
          className={
            chat.statusMessage.isError
              ? "operator-chat-status operator-chat-status-error"
              : "operator-chat-status"
          }
          role="status"
        >
          {chat.statusMessage.text}
          {chat.statusMessage.canRetry && (
            <>
              {" "}
              <button
                type="button"
                className="quiet operator-chat-retry"
                onClick={chat.retryLast}
              >
                重试
              </button>
            </>
          )}
        </span>
        <span id="operator-chat-progress" className="operator-chat-sr-only" role="status">
          {chat.progressText}
        </span>

        <div className="operator-chat-pill">
          <div
            ref={shortcutsPopupRef}
            id="operator-chat-shortcuts"
            className="operator-chat-popup"
            onKeyDown={(event) => {
              if (openMenu === "shortcuts") {
                focusMenuButtonByKey(
                  shortcutsPanelRef.current,
                  event,
                  () => setOpenMenu(null),
                  shortcutsToggleRef.current
                );
              }
            }}
          >
            <button
              ref={shortcutsToggleRef}
              id="operator-chat-shortcuts-toggle"
              className="operator-chat-icon"
              type="button"
              aria-label="对话快捷操作"
              aria-expanded={openMenu === "shortcuts"}
              aria-controls="operator-chat-shortcuts-panel"
              disabled={chat.pending}
              onClick={() => {
                if (modelDetailsRef.current) modelDetailsRef.current.open = false;
                setOpenMenu((prev) => (prev === "shortcuts" ? null : "shortcuts"));
              }}
            >
              <span aria-hidden="true">+</span>
            </button>
            <div
              ref={shortcutsPanelRef}
              id="operator-chat-shortcuts-panel"
              className="operator-chat-menu operator-chat-menu-up"
              hidden={openMenu !== "shortcuts"}
            >
              <button type="button" data-chat-action="agent_task" onClick={() => handleShortcutClick("agent_task")}>
                交办任务
              </button>
              <button type="button" data-chat-action="dynamic" onClick={() => handleShortcutClick("dynamic")}>
                写动态
              </button>
              <button type="button" data-chat-action="browse" onClick={() => handleShortcutClick("browse")}>
                刷视频
              </button>
              <button type="button" data-chat-action="full_video" onClick={() => handleShortcutClick("full_video")}>
                看完整视频
              </button>
              <button
                type="button"
                data-chat-action="public_experiences"
                onClick={() => handleShortcutClick("public_experiences")}
              >
                公开经历
              </button>
              <button
                type="button"
                data-chat-action="video_activity"
                onClick={() => handleShortcutClick("video_activity")}
              >
                今日观看
              </button>
              <button
                type="button"
                data-chat-action="toggle_operations"
                disabled={quickOpsDisabled}
                onClick={() => handleShortcutClick("toggle_operations")}
              >
                {quickOpsLabel}
              </button>
              <button
                type="button"
                data-chat-action="operation_control"
                onClick={() => handleShortcutClick("operation_control")}
              >
                自主运营设置
              </button>
              <button type="button" data-chat-action="live_status" onClick={() => handleShortcutClick("live_status")}>
                直播状态
              </button>
              <button type="button" data-chat-action="clock" onClick={() => handleShortcutClick("clock")}>
                当前时间
              </button>
              <button type="button" data-chat-action="activity" onClick={() => handleShortcutClick("activity")}>
                运营动态
              </button>
            </div>
          </div>

          <button
            id="operator-chat-clear-action"
            className="operator-chat-selected-action"
            type="button"
            hidden={!currentShortcut}
            disabled={chat.pending}
            aria-label={currentShortcut ? `取消${currentShortcut.label}快捷操作` : undefined}
            onClick={() => {
              if (!chat.pending) {
                selectAction(null);
                focusComposer(null);
              }
            }}
          >
            {currentShortcut ? `${currentShortcut.label} ×` : ""}
          </button>

          <label
            id="operator-chat-input-label"
            className="operator-chat-sr-only"
            htmlFor="operator-chat-input"
            hidden={videoMode}
          >
            发消息
          </label>
          <textarea
            ref={inputRef}
            id="operator-chat-input"
            rows={1}
            placeholder={currentShortcut?.placeholder || DEFAULT_INPUT_PLACEHOLDER}
            hidden={videoMode}
            disabled={videoMode}
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            onKeyDown={handleComposerKeyDown}
          />
          <button
            className="operator-chat-icon operator-chat-send"
            id="operator-chat-send"
            type={inFlight ? "button" : "submit"}
            aria-label={
              inFlight
                ? chat.stopping
                  ? "正在停止生成"
                  : "停止生成"
                : videoMode
                  ? "安排只读取材"
                  : "发送"
            }
            data-generating={inFlight ? "true" : "false"}
            disabled={inFlight ? chat.stopping : chat.pending || !hasComposerInput}
            onClick={inFlight ? () => void chat.stopGeneration() : undefined}
          >
            <span className="operator-chat-arrow" aria-hidden="true" />
            <span className="operator-chat-stop" aria-hidden="true" />
          </button>
        </div>
      </form>

      <TemporaryChatDialog open={tempChatOpen} onClose={() => setTempChatOpen(false)} />

      <PrivateCallDialog
        open={callSession.dialogOpen}
        availability={callSession.availability}
        temporary={callSession.temporary}
        onTemporaryChange={callSession.setTemporary}
        topic={callSession.topic}
        onTopicChange={callSession.setTopic}
        optionsOpen={callSession.optionsOpen}
        onOptionsToggle={callSession.setOptionsOpen}
        inCall={callSession.inCall}
        callReady={callSession.callReady}
        callEphemeral={callSession.callEphemeral}
        statusText={callSession.statusText}
        muted={callSession.muted}
        showMute={callSession.showMute}
        avatarProfilesOffered={callSession.avatarProfilesOffered}
        selectedProfile={callSession.selectedProfile}
        onSelectedProfileChange={callSession.setSelectedProfile}
        portraitActive={callSession.portraitActive}
        portraitUi={callSession.portraitUi}
        videoElRef={callSession.videoElRef}
        transcriptRecords={callSession.transcriptRecords}
        reviewConfig={callSession.reviewConfig}
        confirmHandoff={callSession.callWork.handoff}
        workInput={callSession.workInput}
        onWorkInputChange={callSession.setWorkInput}
        workSending={callSession.workSending}
        workAnswer={callSession.workAnswer}
        workError={callSession.workError}
        onSubmitWork={() => void callSession.submitWork()}
        workTasks={callSession.callWork.tasks}
        taskNote={callSession.callWork.taskNote}
        onReadTask={callSession.callWork.readTask}
        onStartCall={callSession.startCall}
        onToggleMute={callSession.toggleMute}
        onSmoothingChange={callSession.setSmoothing}
        onInterruptAnswer={callSession.interruptAnswer}
        onEndOrClose={callSession.endOrCloseDialog}
        onNativeClose={callSession.handleDialogNativeClose}
        onPetSlotChange={setPetSlotEl}
      />
    </section>
  );
}
