import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/shared/api/client";
import { humanizeStatus } from "@/shared/lib/formatters";
import type { ParsedRoute } from "@/shell/navigation";
import { OperationsWorkbench, type WorkbenchTab } from "./OperationsWorkbench";
import styles from "./operations.module.css";
import type {
  ActivityItem,
  ActivityResponse,
  BrowsingIntent,
  BrowsingRound,
  BrowsingTask,
  DiscoveryPublishedItem,
  DiscoveryRecordsResponse,
  EvidenceSegment,
  InboxEventItem,
  InboxRecordsResponse,
  QueueDetail,
} from "./types";

function toArray<T>(value: T[] | { items?: T[] } | null | undefined): T[] {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.items)) return value.items;
  return [];
}

const OUTCOME_LABELS: Record<string, string> = {
  new_reply: "本轮新回",
  previous_reply: "此前已回",
  same_account: "同账号已回 · 归属未确认",
  pending_verification: "待核对",
  read: "已读",
  published: "已发送",
};

export function AutomationRecordsPage({ route }: { route: ParsedRoute }) {
  const routeRoundId =
    route.routeKind === "round" && /^[\w-]+$/.test(route.routeId || "")
      ? route.routeId || null
      : null;
  const routeTaskId =
    routeRoundId && /^[\w-]+$/.test(route.taskId || "") ? route.taskId || null : null;

  const [activeTab, setActiveTab] = useState<WorkbenchTab>(routeRoundId ? "tasks" : "rounds");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(routeTaskId);

  const [tasks, setTasks] = useState<BrowsingTask[]>([]);
  const [rounds, setRounds] = useState<BrowsingRound[]>([]);
  const [intents, setIntents] = useState<BrowsingIntent[]>([]);
  const [inbox, setInbox] = useState<InboxEventItem[]>([]);
  const [publishedItems, setPublishedItems] = useState<DiscoveryPublishedItem[]>([]);
  const [evidenceCache, setEvidenceCache] = useState<Record<string, EvidenceSegment[]>>({});

  // Round activity
  const [activityItems, setActivityItems] = useState<ActivityItem[]>([]);
  const [activityPage, setActivityPage] = useState(1);
  const [activityPages, setActivityPages] = useState(1);
  const [activityError, setActivityError] = useState("");
  const [openEvents, setOpenEvents] = useState<Set<string>>(() => new Set());

  const prevRoundRef = useRef<string | null | undefined>(undefined);
  const activeActivityRoundRef = useRef<string | undefined>(undefined);
  const activityRequestSeq = useRef(0);
  const refreshSeq = useRef(0);

  useEffect(() => {
    if (prevRoundRef.current !== routeRoundId) {
      prevRoundRef.current = routeRoundId;
      setActiveTab(routeRoundId ? "tasks" : "rounds");
      setActivityPage(1);
    }
    if (routeTaskId) {
      setActiveTab("tasks");
      setSelectedTaskId(routeTaskId);
    }
  }, [routeRoundId, routeTaskId]);

  const loadRoundActivity = useCallback(
    async (roundId: string | undefined, targetPage: number) => {
      if (!roundId) {
        setActivityItems([]);
        setActivityPages(1);
        setActivityError("");
        return;
      }
      const seq = ++activityRequestSeq.current;
      try {
        const data = await apiGet<"/api/v1/operations/activity", ActivityResponse>(
          "/api/v1/operations/activity",
          {
            query: {
              page_size: 20,
              page: targetPage,
              round_id: roundId,
            },
          }
        );
        if (seq !== activityRequestSeq.current) return;
        setActivityItems(data.items || []);
        setActivityPage(data.page || targetPage);
        setActivityPages(data.pages || 1);
        setActivityError("");
      } catch {
        if (seq === activityRequestSeq.current) {
          setActivityError("本轮事件读取失败，刷新后重试。");
        }
      }
    },
    []
  );

  // IMPORTANT: record refresh reads ONLY record collections; parent status stays with coordinator.
  const refreshRecords = useCallback(async () => {
    const seq = ++refreshSeq.current;
    const tasksQuery = routeRoundId ? { round_id: routeRoundId } : undefined;
    const results = await Promise.allSettled([
      apiGet<"/api/v1/operations/tasks", BrowsingTask[] | { items: BrowsingTask[] }>(
        "/api/v1/operations/tasks",
        tasksQuery ? { query: tasksQuery } : undefined
      ),
      apiGet<"/api/v1/operations/rounds", BrowsingRound[] | { items: BrowsingRound[] }>(
        "/api/v1/operations/rounds"
      ),
      apiGet<"/api/v1/operations/intents", BrowsingIntent[] | { items: BrowsingIntent[] }>(
        "/api/v1/operations/intents"
      ),
      apiGet<"/api/v1/operations/inbox", InboxRecordsResponse>("/api/v1/operations/inbox"),
      apiGet<"/api/v1/operations/discovery/records", DiscoveryRecordsResponse>(
        "/api/v1/operations/discovery/records"
      ),
    ]);
    if (seq !== refreshSeq.current) return;

    let nextRounds = rounds;
    if (results[0].status === "fulfilled") setTasks(toArray(results[0].value));
    if (results[1].status === "fulfilled") {
      nextRounds = toArray(results[1].value);
      setRounds(nextRounds);
    }
    if (results[2].status === "fulfilled") {
      setIntents(
        toArray(results[2].value).filter((row) => ["video_like", "video_fav"].includes(row.kind))
      );
    }
    if (results[3].status === "fulfilled") setInbox(toArray(results[3].value));
    if (results[4].status === "fulfilled") {
      setPublishedItems(results[4].value.sent || []);
    }

    const currentRound =
      routeRoundId ||
      nextRounds.find((item) => item.status === "running")?.id ||
      nextRounds[0]?.id;

    let nextPage = activityPage;
    if (currentRound !== activeActivityRoundRef.current) {
      activeActivityRoundRef.current = currentRound;
      nextPage = 1;
      setActivityPage(1);
    }
    await loadRoundActivity(currentRound, nextPage);
  }, [routeRoundId, activityPage, loadRoundActivity, rounds]);

  useEffect(() => {
    void refreshRecords();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeRoundId]);

  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void refreshRecords();
    }, 8000);
    return () => clearInterval(timer);
  }, [refreshRecords]);

  async function loadEvidenceForPublished(id: string) {
    if (evidenceCache[id]) return;
    try {
      const record = await apiGet<
        "/api/v1/operations/comments/workbench/{channel}/{identity}",
        QueueDetail
      >("/api/v1/operations/comments/workbench/{channel}/{identity}", {
        pathParams: { channel: "browsing", identity: id },
      });
      if (record.evidence_segments?.length) {
        setEvidenceCache((prev) => ({ ...prev, [id]: record.evidence_segments! }));
      }
    } catch {
      // Allow retry on next expand
    }
  }

  return (
    <section data-section="automation-records" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>运行记录</h1>
        <a href="#automation" className="button quiet">
          返回控制台
        </a>
      </div>

      <section className="win-group-box">
        <h2 id="automation-round-activity-title">本轮事件</h2>
        <div id="automation-round-activity">
          {activityError ? (
            <p role="status">{activityError}</p>
          ) : activityItems.length === 0 ? (
            <p className="hint">本轮暂无可展示的事件。</p>
          ) : (
            activityItems.map((item) => {
              const resultLabel =
                OUTCOME_LABELS[item.comment_outcome || item.status || ""] ||
                humanizeStatus(item.status).label;
              const ownership = [
                item.own_resource === true ? (item.bvid ? "自有投稿" : "自有动态") : "",
                item.own_thread === true
                  ? "号主的楼"
                  : item.own_thread === false
                    ? "观众的楼"
                    : "",
              ].filter(Boolean);
              const body = item.account_reply_text || item.confirmed_reply_text || item.text;
              const sourceLink = item.source_href || item.href;

              return (
                <article key={item.id} className={styles.activityRow}>
                  <div>
                    <strong>
                      {item.title} · {resultLabel}
                    </strong>
                    {ownership.length > 0 && <p>{ownership.join(" · ")}</p>}
                    {item.source_text && <blockquote>{item.source_text}</blockquote>}
                    {body && <p>{body}</p>}
                    <div className="actions">
                      {sourceLink && (
                        <a
                          href={sourceLink}
                          target={sourceLink.startsWith("https://") ? "_blank" : undefined}
                          rel={sourceLink.startsWith("https://") ? "noreferrer" : undefined}
                        >
                          {item.source_href ? "打开原评论" : "打开来源"}
                        </a>
                      )}
                      {item.sent_href && (
                        <a
                          href={item.sent_href}
                          target={item.sent_href.startsWith("https://") ? "_blank" : undefined}
                          rel={item.sent_href.startsWith("https://") ? "noreferrer" : undefined}
                        >
                          打开已确认回复
                        </a>
                      )}
                    </div>
                    {(item.reason || item.error) && (
                      <details
                        data-event={item.id}
                        open={openEvents.has(item.id)}
                        onToggle={(e) => {
                          const open = e.currentTarget.open;
                          setOpenEvents((prev) => {
                            if (prev.has(item.id) === open) return prev;
                            const next = new Set(prev);
                            if (open) next.add(item.id);
                            else next.delete(item.id);
                            return next;
                          });
                        }}
                      >
                        <summary>处理详情</summary>
                        {item.reason && <p>{item.reason}</p>}
                        {item.error && <p>{item.error}</p>}
                      </details>
                    )}
                  </div>
                </article>
              );
            })
          )}

          {activityPages > 1 && (
            <div className="actions" style={{ marginTop: 10 }}>
              <button
                type="button"
                disabled={activityPage <= 1}
                onClick={() => {
                  const target = Math.max(1, activityPage - 1);
                  setActivityPage(target);
                  void loadRoundActivity(activeActivityRoundRef.current, target);
                }}
              >
                上一页
              </button>
              <span>
                第 {activityPage} / {activityPages} 页
              </span>
              <button
                type="button"
                disabled={activityPage >= activityPages}
                onClick={() => {
                  const target = activityPage + 1;
                  setActivityPage(target);
                  void loadRoundActivity(activeActivityRoundRef.current, target);
                }}
              >
                下一页
              </button>
            </div>
          )}
        </div>
      </section>

      <details
        className="win-group-box"
        id="automation-diagnostics"
        open={Boolean(routeTaskId || routeRoundId)}
        style={{ marginTop: 16 }}
      >
        <summary>任务与诊断</summary>
        <div id="automation-panel" style={{ marginTop: 12 }}>
          <OperationsWorkbench
            tasks={tasks}
            rounds={rounds}
            intents={intents}
            inbox={inbox}
            activeTab={activeTab}
            onTabChange={setActiveTab}
            roundFilter={routeRoundId}
            selectedTaskId={selectedTaskId}
            onSelectTaskId={setSelectedTaskId}
            onRefresh={refreshRecords}
          />
        </div>
      </details>

      <details className="win-group-box" style={{ marginTop: 16 }}>
        <summary>近期公开发布</summary>
        <div className="panel-heading">
          <a href="#comments/history" className="win-link-btn">
            查看全部已执行评论 →
          </a>
        </div>
        <div id="discovery-published">
          {publishedItems.length === 0 ? (
            <p className="hint">暂无发送记录。</p>
          ) : (
            publishedItems.map((item) => (
              <details
                key={item.id}
                className={styles.issueRow}
                onToggle={(e) => {
                  if (e.currentTarget.open) {
                    void loadEvidenceForPublished(item.id);
                  }
                }}
              >
                <summary>
                  {item.kind === "top_level" ? "主评" : "跟帖"} · {item.bvid} ·{" "}
                  {new Date(item.updated_at * 1000).toLocaleString("zh-CN")}
                </summary>
                {item.text && <blockquote className="quoted-text">{item.text}</blockquote>}
                {item.receipt_id && <p className="hint">平台回执 {item.receipt_id}</p>}
                <a href={`#comments/history/browsing/${item.id}`}>查看来源与回执</a>
                {evidenceCache[item.id] && (
                  <div className="evidence-list" style={{ marginTop: 8 }}>
                    {evidenceCache[item.id].map((seg, idx) => {
                      const sec = seg.start ?? seg.time ?? seg.timestamp;
                      const meta = [
                        seg.id?.startsWith("frame:") ? "画面观察" : "字幕原文",
                        seg.cid && `CID ${seg.cid}`,
                        Number.isFinite(sec) && `${sec} 秒`,
                      ].filter(Boolean);
                      return (
                        <div key={seg.id || idx}>
                          {meta.length > 0 && <p className="hint">{meta.join(" · ")}</p>}
                          <blockquote className="quoted-text">
                            {seg.text || seg.observation || seg.content || "材料正文不可用"}
                          </blockquote>
                        </div>
                      );
                    })}
                  </div>
                )}
              </details>
            ))
          )}
        </div>
      </details>
    </section>
  );
}
