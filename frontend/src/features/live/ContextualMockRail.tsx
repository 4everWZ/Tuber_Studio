import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { LivePollState } from "./consoleTypes";
import { formatSongQuality } from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import type { DockTabId } from "./ShowDock";
import type { LiveStudioData } from "./types";

export type FeedTabId = "chat" | "dialogue" | "queue";
export type MockEventType = "chat" | "gift" | "song" | "sfx" | "disconnect";

const JOB_LABELS: Record<string, string> = {
  received: "已收到",
  queued: "排队中",
  generating: "生成中",
  synthesizing: "准备音频",
  ready_to_play: "等待播放器",
  playing: "播出中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
  filtered: "已过滤",
  expired: "已过期",
  dropped: "未加入队列",
};

const KIND_LABELS: Record<string, string> = {
  chat: "弹幕",
  gift: "礼物",
  song: "歌曲",
  sfx: "音效",
};

const PREP_LABELS: Record<string, string> = {
  waiting: "等待准备",
  pending: "等待准备",
  ready: "准备就绪",
  preparing: "准备中",
  failed: "准备失败",
};

const DOCK_CONTEXT_LABELS: Record<DockTabId, string> = {
  rundown: "节目单与投票",
  draw_guess: "你画我猜 2.0",
  turtle_soup: "灵魂海龟汤",
  roast_radio: "锐评辩论",
  cold_director: "冷场导演",
  games: "实机游戏",
};

function formatTime(timestamp: number | undefined): string {
  if (typeof timestamp !== "number" || !Number.isFinite(timestamp)) return "";
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export interface ContextualMockRailProps {
  view: LiveStudioData | null;
  poll: LivePollState | null;
  activeDockTab: DockTabId;
  canAct: boolean;
  enabled: boolean;
  clearDisabled: boolean;
  onQueueChange: (id: string, action: "up" | "down" | "remove") => void;
  onClearQueue: () => void;
  onInjectMock: (payload: {
    type: MockEventType;
    display_name: string;
    text: string;
    metadata?: Record<string, unknown>;
  }) => Promise<boolean>;
  onSettleVote: () => void;
  musicLinkProps: {
    href: string;
    onClick?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
  };
  queueListRef: React.RefObject<HTMLOListElement | null>;
  queueHeadingRef: React.RefObject<HTMLSpanElement | null>;
}

export function ContextualMockRail({
  view,
  poll,
  activeDockTab,
  canAct,
  enabled,
  clearDisabled,
  onQueueChange,
  onClearQueue,
  onInjectMock,
  onSettleVote,
  musicLinkProps,
  queueListRef,
  queueHeadingRef,
}: ContextualMockRailProps) {
  const [feedTab, setFeedTab] = useState<FeedTabId>("chat");
  const [follow, setFollow] = useState(true);
  const [unread, setUnread] = useState(0);

  const [mockType, setMockType] = useState<MockEventType>("chat");
  const [mockName, setMockName] = useState("测试观众");
  const [mockText, setMockText] = useState("");
  const [mockAsset, setMockAsset] = useState("");

  const sessionIdRef = useRef<string | null | undefined>(undefined);
  const chatIdsRef = useRef<Set<string | number>>(new Set());
  const followRef = useRef(follow);
  followRef.current = follow;

  const chatPaneRef = useRef<HTMLDivElement | null>(null);
  const dialoguePaneRef = useRef<HTMLDivElement | null>(null);
  const chatTabRef = useRef<HTMLButtonElement | null>(null);
  const dialogueTabRef = useRef<HTMLButtonElement | null>(null);
  const queueTabRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!view) return;
    const changedSession =
      sessionIdRef.current === undefined || sessionIdRef.current !== view.session_id;
    sessionIdRef.current = view.session_id;
    const nextChat = view.chat ?? [];
    if (changedSession) {
      chatIdsRef.current = new Set(nextChat.map((item, idx) => item.id ?? idx));
      setFollow(true);
      setUnread(0);
      if (dialoguePaneRef.current) {
        dialoguePaneRef.current.scrollTop = 0;
      }
    } else {
      if (!followRef.current) {
        const added = nextChat.filter((item, idx) => !chatIdsRef.current.has(item.id ?? idx)).length;
        if (added > 0) setUnread((count) => count + added);
      }
      chatIdsRef.current = new Set(nextChat.map((item, idx) => item.id ?? idx));
    }
  }, [view]);

  useEffect(() => {
    if (follow && feedTab === "chat" && chatPaneRef.current) {
      chatPaneRef.current.scrollTop = chatPaneRef.current.scrollHeight;
      setUnread(0);
    }
  }, [follow, feedTab, view?.chat]);

  const songs = useMemo(
    () => (view?.queue ?? []).filter((item) => item.type === "song"),
    [view?.queue]
  );
  const otherQueueCount = (view?.queue.length ?? 0) - songs.length;

  const switchFeedTab = (tab: FeedTabId) => {
    setFeedTab(tab);
    if (tab === "chat" && follow && chatPaneRef.current) {
      chatPaneRef.current.scrollTop = chatPaneRef.current.scrollHeight;
      setUnread(0);
    }
  };

  const handleFeedTabKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const order: FeedTabId[] = ["chat", "dialogue", "queue"];
    const idx = order.indexOf(feedTab);
    const next: FeedTabId =
      event.key === "Home"
        ? "chat"
        : event.key === "End"
          ? "queue"
          : event.key === "ArrowRight"
            ? order[(idx + 1) % order.length]
            : order[(idx - 1 + order.length) % order.length];
    switchFeedTab(next);
    if (next === "chat") chatTabRef.current?.focus();
    else if (next === "dialogue") dialogueTabRef.current?.focus();
    else queueTabRef.current?.focus();
  };

  const quickSend = (
    type: "chat" | "gift" | "song",
    text: string,
    metadata: Record<string, unknown> = {}
  ) => {
    void onInjectMock({
      type,
      display_name: mockName.trim() || "测试观众",
      text,
      metadata,
    });
  };

  const handleCustomSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const ok = await onInjectMock({
      type: mockType,
      display_name: mockName.trim() || "测试观众",
      text: mockText,
      metadata: mockType === "sfx" ? { asset_id: mockAsset.trim() } : {},
    });
    if (ok) {
      setMockText("");
    }
  };

  const pollActive = Boolean(poll && poll.phase === "started");
  const mockDisabled = !canAct || !enabled;

  return (
    <aside className={`panel ${styles.deskPanel}`} aria-label="直播流与场景模拟器">
      <div className={styles.deskPanelHeading}>
        <h2>直播流与队列</h2>
        <button
          type="button"
          className={styles.deskFollowBtn}
          data-desk-follow
          aria-pressed={follow}
          hidden={feedTab !== "chat"}
          onClick={() => {
            const next = !follow;
            setFollow(next);
            if (next && chatPaneRef.current) {
              chatPaneRef.current.scrollTop = chatPaneRef.current.scrollHeight;
              setUnread(0);
            }
          }}
        >
          {follow ? "跟随最新：开" : unread ? `跳到最新 · ${unread} 条` : "跟随最新：关"}
        </button>
      </div>

      <div className={styles.deskFeedTabs} role="tablist" aria-label="直播间记录">
        <button
          ref={chatTabRef}
          type="button"
          role="tab"
          id="desk-chat-tab"
          aria-controls="desk-chat"
          aria-selected={feedTab === "chat"}
          tabIndex={feedTab === "chat" ? 0 : -1}
          data-desk-feed="chat"
          onClick={() => switchFeedTab("chat")}
          onKeyDown={handleFeedTabKeyDown}
        >
          弹幕{unread > 0 && !follow ? ` (${unread})` : ""}
        </button>
        <button
          ref={dialogueTabRef}
          type="button"
          role="tab"
          id="desk-dialogue-tab"
          aria-controls="desk-dialogue"
          aria-selected={feedTab === "dialogue"}
          tabIndex={feedTab === "dialogue" ? 0 : -1}
          data-desk-feed="dialogue"
          onClick={() => switchFeedTab("dialogue")}
          onKeyDown={handleFeedTabKeyDown}
        >
          台词
        </button>
        <button
          ref={queueTabRef}
          type="button"
          role="tab"
          id="desk-queue-tab"
          aria-controls="desk-queue-panel"
          aria-selected={feedTab === "queue"}
          tabIndex={feedTab === "queue" ? 0 : -1}
          data-desk-feed="queue"
          onClick={() => switchFeedTab("queue")}
          onKeyDown={handleFeedTabKeyDown}
        >
          待播队列 ({view?.queue.length ?? 0})
        </button>
      </div>

      <div
        ref={chatPaneRef}
        data-desk-chat
        id="desk-chat"
        role="tabpanel"
        aria-labelledby="desk-chat-tab"
        tabIndex={0}
        className={styles.deskFeed}
        hidden={feedTab !== "chat"}
        onScroll={() => {
          const el = chatPaneRef.current;
          if (!el || feedTab !== "chat") return;
          const atBottom = el.scrollHeight - el.clientHeight - el.scrollTop < 24;
          setFollow(atBottom);
          if (atBottom) setUnread(0);
        }}
      >
        <ol className={styles.deskList}>
          {(view?.chat ?? []).map((item, idx) => (
            <li key={item.id ?? idx} className={styles.deskFeedRow}>
              <div className={styles.deskFeedMeta}>
                <strong>{item.requester || "直播间"}</strong>
                <span>{KIND_LABELS[item.type] || "互动"}</span>
                <span>{(item.status ? JOB_LABELS[item.status] : "") || item.status || ""}</span>
                <time>{formatTime(item.received_at)}</time>
              </div>
              <p>{item.text || "（无文字）"}</p>
              {item.reason && <small>{item.reason}</small>}
            </li>
          ))}
        </ol>
        <p data-feed-empty className={styles.deskEmpty} hidden={(view?.chat?.length ?? 0) > 0}>
          本会话暂无弹幕或礼物。
        </p>
      </div>

      <div
        ref={dialoguePaneRef}
        data-desk-dialogue
        id="desk-dialogue"
        role="tabpanel"
        aria-labelledby="desk-dialogue-tab"
        tabIndex={0}
        className={styles.deskFeed}
        hidden={feedTab !== "dialogue"}
      >
        <ol className={styles.deskList}>
          {(view?.dialogue ?? []).map((item) => {
            const stateText =
              item.status === "completed" ? "已确认播放" : JOB_LABELS[item.status] || item.status;
            return (
              <li key={item.id} className={styles.deskFeedRow}>
                <div className={styles.deskFeedMeta}>
                  <strong>{item.requester || "直播间"}</strong>
                  <span>{KIND_LABELS[item.type] || "互动"}</span>
                  <span>{stateText}</span>
                  <time>{formatTime(item.created_at)}</time>
                </div>
                {item.status === "completed" && item.text && <p>{item.text}</p>}
                {item.type === "song" && item.song_title && <p>{item.song_title}</p>}
                {item.error && item.status !== "completed" && (
                  <small className={styles.deskError}>{item.error}</small>
                )}
              </li>
            );
          })}
        </ol>
        <p data-feed-empty className={styles.deskEmpty} hidden={(view?.dialogue?.length ?? 0) > 0}>
          本会话暂无台词记录。
        </p>
      </div>

      <div
        id="desk-queue-panel"
        role="tabpanel"
        aria-labelledby="desk-queue-tab"
        hidden={feedTab !== "queue"}
      >
        <div className={styles.deskPanelHeading}>
          <span className={styles.deskChip} data-desk-song-count ref={queueHeadingRef} tabIndex={-1}>
            {songs.length} 首待播
          </span>
          <span className={styles.deskHint} data-desk-other-count>
            {otherQueueCount > 0 ? `另有 ${otherQueueCount} 条待播互动` : ""}
          </span>
        </div>
        <ol
          ref={queueListRef}
          data-desk-queue
          className={`${styles.deskList} ${styles.deskQueue}`}
          aria-label="待播歌曲"
        >
          {songs.map((item, index) => {
            const queuedSong = item.media?.song;
            const title = queuedSong?.title || item.text || "未命名歌曲";
            const quality = formatSongQuality(queuedSong?.quality);
            const subtitle = [queuedSong?.artist, item.requester && `点歌 · ${item.requester}`, quality]
              .filter(Boolean)
              .join(" · ");
            const prep = item.preparation;
            const prepLabel = prep ? PREP_LABELS[prep[0]] || "等待准备" : "";
            const prepError = prep?.[1] ?? "";
            return (
              <li key={item.id} className={styles.deskQueueRow}>
                <span className={styles.deskIndex}>{index + 1}</span>
                <div className={styles.deskRowContent}>
                  <strong>{title}</strong>
                  <small>{subtitle}</small>
                  {prep && (
                    <small className={prepError ? styles.deskError : undefined}>
                      {prepError || prepLabel}
                    </small>
                  )}
                </div>
                <div className={styles.deskRowActions}>
                  {(
                    [
                      ["up", "上移", index === 0],
                      ["down", "下移", index === songs.length - 1],
                      ["remove", "移除", false],
                    ] as const
                  ).map(([action, label, isEdge]) => (
                    <button
                      key={action}
                      type="button"
                      data-queue-action={action}
                      data-queue-id={item.id}
                      data-edge={String(isEdge)}
                      aria-label={`${label} ${title}`}
                      disabled={!canAct || !enabled || isEdge}
                      onClick={() => onQueueChange(item.id, action)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </li>
            );
          })}
        </ol>
        <p data-desk-queue-empty className={styles.deskEmpty} hidden={songs.length > 0}>
          暂无待播歌曲。
        </p>
        <div className={styles.deskQueueFooter}>
          <a data-desk-link="music" {...musicLinkProps}>
            曲库与点歌 →
          </a>
          <button
            type="button"
            data-desk-queue-clear
            disabled={clearDisabled}
            onClick={onClearQueue}
          >
            清空全部待播
          </button>
        </div>
      </div>

      <div className={styles.simSection} data-desk-quick-mock>
        <div className={styles.deskPanelHeading} style={{ marginBottom: 0 }}>
          <strong style={{ fontSize: 13 }}>场景模拟器</strong>
          <span className={styles.deskChip} data-simulator-context>
            {pollActive ? "投票进行中" : DOCK_CONTEXT_LABELS[activeDockTab]}
          </span>
        </div>

        <div className={styles.quickActions}>
          {pollActive ? (
            <>
              {(poll?.options ?? []).slice(0, 4).map((opt, idx) => (
                <button
                  key={`${opt}-${idx}`}
                  type="button"
                  className={styles.compactBtn}
                  data-quick-mock={`poll-vote-${idx + 1}`}
                  disabled={mockDisabled}
                  onClick={() => quickSend("chat", String(idx + 1))}
                >
                  投 {idx + 1} ({opt})
                </button>
              ))}
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="poll-settle-empty"
                disabled={!canAct}
                onClick={onSettleVote}
              >
                空票结算测兜底
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="poll-cmd"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "#投票选游戏")}
              >
                模拟 #投票选游戏
              </button>
            </>
          ) : activeDockTab === "draw_guess" ? (
            <>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-guess"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "西瓜")}
              >
                猜：西瓜
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-guess-keyboard"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "机械键盘")}
              >
                猜：机械键盘
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-guess-miss"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "外星飞船")}
              >
                故意猜偏测吐槽
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="gift-brush"
                disabled={mockDisabled}
                onClick={() => quickSend("gift", "画笔", { gift_name: "画笔", count: 1, price: 10 })}
              >
                送画笔开局 (10币)
              </button>
            </>
          ) : activeDockTab === "turtle_soup" ? (
            <>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-soup"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "这件事发生在太空里吗？")}
              >
                问：在太空里吗？
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-soup-work"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "主人公当时在工作吗？")}
              >
                问：在工作吗？
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-soup-hint"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "#提示")}
              >
                弹幕 #提示
              </button>
            </>
          ) : activeDockTab === "roast_radio" ? (
            <>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-vote"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "1")}
              >
                投正方 (1)
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-vote-2"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "2")}
              >
                投反方 (2)
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-roast-cmd"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "#吐槽")}
              >
                模拟 #吐槽
              </button>
            </>
          ) : activeDockTab === "cold_director" ? (
            <>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="cold-wakeup"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "Aqua 我回来啦！刚才在忙啥呢？")}
              >
                观众回来了 (唤醒)
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-hello"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "Aqua晚上好，今天吃白饭了吗？")}
              >
                刷弹幕重置 T0
              </button>
            </>
          ) : activeDockTab === "games" ? (
            <>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="gift-farm"
                disabled={mockDisabled}
                onClick={() => quickSend("gift", "助力农活", { gift_name: "小心心", count: 1, price: 10 })}
              >
                送礼助力游戏 (10币)
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-game-fish"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "Aqua 快去钓鱼！")}
              >
                弹幕建议：去钓鱼
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="poll-cmd"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "#投票选游戏")}
              >
                模拟 #投票选游戏
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="continue-chat-cmd"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "#继续聊")}
              >
                模拟 #继续聊
              </button>
              <button
                type="button"
                className={styles.compactBtn}
                data-quick-mock="chat-hello"
                disabled={mockDisabled}
                onClick={() => quickSend("chat", "Aqua晚上好，今天吃白饭了吗？")}
              >
                打招呼弹幕
              </button>
            </>
          )}
        </div>

        <div className={styles.simPinnedRow}>
          <button
            type="button"
            className={styles.compactBtn}
            data-quick-mock="gift-rice"
            disabled={mockDisabled}
            onClick={() =>
              quickSend("gift", "白饭应援", { gift_name: "白饭应援", count: 1, price: 100 })
            }
          >
            🎁 送礼冲加班条 (100币)
          </button>
          <button
            type="button"
            className={styles.compactBtn}
            data-quick-mock="chat-energy"
            disabled={mockDisabled}
            onClick={() => quickSend("chat", "Aqua今天也要干两大碗白饭！冲冲冲！")}
          >
            🍚 活跃弹幕充能
          </button>
        </div>

        <details className={styles.deskTools} style={{ marginTop: 4 }}>
          <summary>自定义事件注入 (chat / gift / song / sfx / disconnect)</summary>
          <form data-desk-mock onSubmit={(e) => void handleCustomSubmit(e)}>
            <fieldset disabled={mockDisabled}>
              <div className={styles.fieldsGrid}>
                <label>
                  类型
                  <select
                    name="type"
                    value={mockType}
                    onChange={(e) => setMockType(e.target.value as MockEventType)}
                  >
                    <option value="chat">弹幕</option>
                    <option value="gift">礼物</option>
                    <option value="song">点歌</option>
                    <option value="sfx">预录音效</option>
                    <option value="disconnect">模拟断线</option>
                  </select>
                </label>
                <label>
                  昵称
                  <input
                    name="name"
                    maxLength={80}
                    required
                    value={mockName}
                    onChange={(e) => setMockName(e.target.value)}
                  />
                </label>
              </div>
              <label>
                消息
                <input
                  name="text"
                  maxLength={500}
                  required={!["sfx", "disconnect"].includes(mockType)}
                  value={mockText}
                  onChange={(e) => setMockText(e.target.value)}
                />
              </label>
              <label data-desk-asset-label hidden={mockType !== "sfx"}>
                音效素材 ID
                <input
                  name="asset"
                  maxLength={100}
                  required={mockType === "sfx"}
                  value={mockAsset}
                  onChange={(e) => setMockAsset(e.target.value)}
                />
              </label>
              <button type="submit" className={styles.compactBtn}>
                注入 Mock 事件
              </button>
            </fieldset>
          </form>
        </details>
      </div>
    </aside>
  );
}
