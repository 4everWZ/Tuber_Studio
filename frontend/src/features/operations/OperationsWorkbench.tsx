import { useEffect, useMemo, useRef, useState } from "react";
import { OperationContinuationCard, Paginator } from "@/shared";
import { apiPost } from "@/shared/api/client";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import {
  formatRelativeTime,
  humanizeKind,
  humanizeReason,
  humanizeRoundReport,
  humanizeStatus,
  humanizeTaskReason,
} from "@/shared/lib/formatters";
import styles from "./operations.module.css";
import type { BrowsingIntent, BrowsingRound, BrowsingTask, InboxEventItem } from "./types";

export type WorkbenchTab = "tasks" | "rounds" | "inbox" | "intents";

function parseTarget(raw: BrowsingIntent["target"]): Record<string, unknown> {
  if (!raw) return {};
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return typeof parsed === "object" && parsed ? (parsed as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }
  return raw as Record<string, unknown>;
}

function paginateItems<T>(items: readonly T[], page: number, pageSize: number): {
  currentPage: number;
  items: T[];
} {
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const currentPage = Math.min(Math.max(1, page), totalPages);
  const start = (currentPage - 1) * pageSize;
  return {
    currentPage,
    items: items.slice(start, start + pageSize),
  };
}

export interface OperationsWorkbenchProps {
  tasks: BrowsingTask[];
  rounds: BrowsingRound[];
  intents: BrowsingIntent[];
  inbox: InboxEventItem[];
  activeTab: WorkbenchTab;
  onTabChange: (tab: WorkbenchTab) => void;
  roundFilter: string | null;
  selectedTaskId: string | null;
  onSelectTaskId: (id: string | null) => void;
  onRefresh: () => void | Promise<void>;
}

export function OperationsWorkbench({
  tasks,
  rounds,
  intents,
  inbox,
  activeTab,
  onTabChange,
  roundFilter,
  selectedTaskId,
  onSelectTaskId,
  onRefresh,
}: OperationsWorkbenchProps) {
  const [intentFilter, setIntentFilter] = useState("pending_review");
  const [taskFilter, setTaskFilter] = useState("");
  const [selectedIntentId, setSelectedIntentId] = useState<string | null>(null);

  const [taskPage, setTaskPage] = useState(1);
  const [roundPage, setRoundPage] = useState(1);
  const [intentPage, setIntentPage] = useState(1);

  const [intentPending, setIntentPending] = useState(false);
  const [intentError, setIntentError] = useState("");
  const [rereading, setRereading] = useState(false);
  const [refreshSpinning, setRefreshSpinning] = useState(false);

  // Preserve open <details> across task detail updates for the same task
  const [taskDetailsOpen, setTaskDetailsOpen] = useState<Set<string>>(() => new Set());
  const prevDetailTaskIdRef = useRef<string | null>(selectedTaskId);

  useEffect(() => {
    if (prevDetailTaskIdRef.current !== selectedTaskId) {
      prevDetailTaskIdRef.current = selectedTaskId;
      setTaskDetailsOpen(new Set());
    }
  }, [selectedTaskId]);

  // Reset task filter when round filter changes
  useEffect(() => {
    setTaskFilter("");
    setTaskPage(1);
  }, [roundFilter]);

  const filteredIntents = useMemo(
    () =>
      !intentFilter || intentFilter === "all"
        ? intents
        : intents.filter((item) => item.status === intentFilter),
    [intents, intentFilter]
  );

  // Auto-select first matching intent when tab is intents
  useEffect(() => {
    if (activeTab !== "intents") return;
    if (!selectedIntentId || !filteredIntents.some((item) => item.id === selectedIntentId)) {
      setSelectedIntentId(filteredIntents[0]?.id ?? null);
    }
  }, [activeTab, filteredIntents, selectedIntentId]);

  // Clear selected task if it no longer matches current filter
  useEffect(() => {
    if (
      selectedTaskId &&
      !tasks.some((t) => t.id === selectedTaskId && (!taskFilter || t.status === taskFilter))
    ) {
      onSelectTaskId(null);
    }
  }, [tasks, taskFilter, selectedTaskId, onSelectTaskId]);

  const filteredTasks = useMemo(
    () => (taskFilter ? tasks.filter((t) => t.status === taskFilter) : tasks),
    [tasks, taskFilter]
  );

  const taskPageSlice = paginateItems(filteredTasks, taskPage, 10);
  const roundPageSlice = paginateItems(rounds, roundPage, 6);
  const intentPageSlice = paginateItems(filteredIntents, intentPage, 8);

  const pendingIntentsCount = intents.filter((i) => i.status === "pending_review").length;
  const pendingInboxCount = inbox.filter((i) =>
    ["pending", "received"].includes(i.status)
  ).length;

  const selectedTask = tasks.find((t) => t.id === selectedTaskId) || null;
  const selectedIntent = intents.find((i) => i.id === selectedIntentId) || null;

  function intentReviewNote(intent: BrowsingIntent): string {
    if (intent.status !== "pending_review") return "";
    const round = rounds.find((r) => r.id === intent.round_id);
    if (round?.preview) return "来自预览轮，需人工审核。";
    return intent.auto_authorized ? "" : "这条草稿生成时未获得自动发布授权，需人工审核。";
  }

  async function handleIntentAction(intent: BrowsingIntent, action?: "approve" | "reject") {
    if (intentPending) return;
    setIntentPending(true);
    setIntentError("");
    try {
      if (action) {
        await apiPost<
          "/api/v1/operations/intents/{identity}/review",
          unknown,
          { action: string }
        >("/api/v1/operations/intents/{identity}/review", {
          pathParams: { identity: intent.id },
          body: { action },
        });
      } else {
        await apiPost<"/api/v1/operations/intents/{identity}/publish", unknown, never>(
          "/api/v1/operations/intents/{identity}/publish",
          { pathParams: { identity: intent.id } }
        );
      }
    } catch (err) {
      setIntentError(errorMessage(err));
    } finally {
      setIntentPending(false);
      await onRefresh();
    }
  }

  async function handleRereadTask(task: BrowsingTask) {
    if (rereading) return;
    setRereading(true);
    try {
      await apiPost<"/api/v1/operations/tasks/{identity}/reread", unknown, never>(
        "/api/v1/operations/tasks/{identity}/reread",
        { pathParams: { identity: task.id } }
      );
      emitNotice("已排队重新分析；生成结果将存为待审核草稿。");
      await onRefresh();
    } catch (err) {
      emitNotice(errorMessage(err), true);
    } finally {
      setRereading(false);
    }
  }

  async function handleBatchRejectErroredIntents() {
    if (intentPending) return;
    const errored = intents.filter(
      (i) => ["pending_review", "approved"].includes(i.status) && Boolean(i.error)
    );
    if (!errored.length) {
      emitNotice("当前没有报错的待审点赞或收藏。");
      return;
    }
    if (!window.confirm(`确认批量拒绝 ${errored.length} 条报错的待审操作？`)) return;
    setIntentPending(true);
    setIntentError("");
    try {
      const res = await apiPost<
        "/api/v1/operations/intents/batch-reject",
        { items: Array<{ id: string; status: string }> },
        { errored_only: boolean }
      >("/api/v1/operations/intents/batch-reject", { body: { errored_only: true } });
      const count = res.items.filter((it) => it.status === "rejected").length;
      emitNotice(`已批量拒绝 ${count} 条报错操作。`);
      await onRefresh();
    } catch (err) {
      setIntentError(errorMessage(err));
    } finally {
      setIntentPending(false);
    }
  }

  return (
    <div id="op-workbench-mount">
      <div className={styles.workbenchNav}>
        <div className={styles.segmentedControl} role="tablist" aria-label="记录视图">
          {(
            [
              ["tasks", "浏览任务", 0],
              ["rounds", "轮次记录", 0],
              ["inbox", "互动收件箱", pendingInboxCount],
              ["intents", "点赞与收藏", pendingIntentsCount],
            ] as const
          ).map(([tabKey, label, badgeCount]) => (
            <button
              key={tabKey}
              type="button"
              role="tab"
              aria-selected={activeTab === tabKey}
              data-tab={tabKey}
              className={activeTab === tabKey ? styles.navBtnActive : undefined}
              onClick={() => onTabChange(tabKey)}
            >
              <span>{label}</span>
              {badgeCount > 0 && <span className={styles.badgeCounter}>{badgeCount}</span>}
            </button>
          ))}
        </div>

        <div className="actions">
          <button
            type="button"
            id="op-btn-refresh-all"
            title="刷新工作台数据"
            disabled={refreshSpinning}
            onClick={async () => {
              setRefreshSpinning(true);
              try {
                await onRefresh();
                emitNotice("工作台数据已更新。");
              } finally {
                setRefreshSpinning(false);
              }
            }}
          >
            🔄 刷新
          </button>
        </div>
      </div>

      <div className={styles.splitWorkspace}>
        {/* Left Master Column */}
        <div className={styles.masterPane}>
          <div id="op-master-subbar" className={styles.segmentedControl}>
            {activeTab === "intents" && (
              <>
                {[
                  { key: "pending_review", label: `⏳ 待审核 (${intents.filter((i) => i.status === "pending_review").length})` },
                  { key: "approved", label: `✓ 已通过 (${intents.filter((i) => i.status === "approved").length})` },
                  { key: "publish_uncertain", label: "待核对" },
                  { key: "published", label: `🚀 已发布 (${intents.filter((i) => i.status === "published").length})` },
                  { key: "rejected", label: "✕ 已放弃" },
                  { key: "all", label: `全部 (${intents.length})` },
                ].map((opt) => (
                  <button
                    key={opt.key}
                    type="button"
                    className={intentFilter === opt.key ? styles.pillActive : undefined}
                    onClick={() => {
                      if (intentFilter === opt.key) return;
                      setIntentFilter(opt.key);
                      setIntentPage(1);
                      const nextList =
                        opt.key === "all"
                          ? intents
                          : intents.filter((i) => i.status === opt.key);
                      setSelectedIntentId(nextList[0]?.id ?? null);
                    }}
                  >
                    {opt.label}
                  </button>
                ))}
                {intents.some(
                  (i) => ["pending_review", "approved"].includes(i.status) && Boolean(i.error)
                ) && (
                  <button
                    type="button"
                    id="op-btn-reject-errored-intents"
                    disabled={intentPending}
                    onClick={() => void handleBatchRejectErroredIntents()}
                  >
                    清理异常草稿 (仅异常)
                  </button>
                )}
              </>
            )}

            {activeTab === "tasks" && (
              <>
                {roundFilter && <a href="#automation">本轮任务 · 查看全部</a>}
                <select
                  aria-label="筛选浏览任务状态"
                  value={taskFilter}
                  onChange={(event) => {
                    setTaskFilter(event.target.value);
                    onSelectTaskId(null);
                    setTaskPage(1);
                  }}
                >
                  <option value="">全部任务 ({tasks.length})</option>
                  <option value="selected">✨ 已生成互动</option>
                  <option value="viewed_without_comment">👀 已阅未发</option>
                  <option value="checkpoint">💾 进度已存</option>
                  <option value="no_subtitles">🔇 缺少可用字幕</option>
                  <option value="failed">🔴 失败/异常</option>
                </select>
              </>
            )}

            {activeTab === "rounds" && (
              <span className="hint">共 {rounds.length} 轮历史自主巡航</span>
            )}

            {activeTab === "inbox" && (
              <span className="hint">收到的互动与 @ 提及 ({inbox.length})</span>
            )}
          </div>

          <div id="op-master-list" className={styles.masterList}>
            {activeTab === "tasks" &&
              (taskPageSlice.items.length === 0 ? (
                <p className="hint">暂无符合筛选的浏览任务。</p>
              ) : (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>视频 / 来源</th>
                        <th>处理进度</th>
                        <th>状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {taskPageSlice.items.map((task) => {
                        const title =
                          task.title ||
                          task.metadata?.title ||
                          task.bvid ||
                          `任务 ${task.id.slice(0, 8)}`;
                        const statusInfo = humanizeStatus(task.status, task.material_mode);
                        const reasonText = humanizeTaskReason(task);
                        return (
                          <tr
                            key={task.id}
                            className={task.id === selectedTaskId ? "selected-row" : undefined}
                          >
                            <td>
                              <button
                                type="button"
                                className={styles.commentLink}
                                style={{ fontWeight: 600 }}
                                onClick={() => onSelectTaskId(task.id)}
                              >
                                {title}
                              </button>
                              <small className={styles.cellSub}>
                                {task.bvid || "—"} · {formatRelativeTime(task.created_at)}
                              </small>
                            </td>
                            <td>{reasonText}</td>
                            <td>
                              <span className={`badge ${statusInfo.className}`}>
                                {statusInfo.label}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ))}

            {activeTab === "rounds" &&
              (roundPageSlice.items.length === 0 ? (
                <p className="hint">暂无轮次记录。</p>
              ) : (
                roundPageSlice.items.map((round) => {
                  const report = humanizeRoundReport(round as unknown as Record<string, unknown>);
                  return (
                    <div key={round.id} className={styles.roundCard}>
                      <div className={styles.roundHeader}>
                        <strong>{report.title}</strong>
                        <span className={`badge ${report.statusInfo.className}`}>
                          {report.statusInfo.label}
                        </span>
                      </div>
                      <p>{report.narrative}</p>
                      {report.interactionOnly ? (
                        <button
                          type="button"
                          data-open-inbox=""
                          onClick={() => onTabChange("inbox")}
                        >
                          查看互动收件箱
                        </button>
                      ) : (
                        <a href={`#automation/round/${encodeURIComponent(round.id)}`}>
                          查看本轮视频
                        </a>
                      )}
                      <details style={{ marginTop: 8 }}>
                        <summary>读取和模型用量</summary>
                        <div className={styles.roundMetricsGrid}>
                          <div className={styles.metricChip}>
                            读取请求 <strong>{report.metrics.reads} 次</strong>
                          </div>
                          <div className={styles.metricChip}>
                            模型调用 <strong>{report.metrics.modelCalls} 次</strong>
                          </div>
                          <div className={styles.metricChip}>
                            评论草稿 <strong>{report.metrics.drafts ?? "—"}</strong>
                          </div>
                          <div className={styles.metricChip}>
                            Token 消耗 <strong>{report.metrics.tokens}</strong>
                          </div>
                        </div>
                      </details>
                    </div>
                  );
                })
              ))}

            {activeTab === "inbox" &&
              (inbox.length === 0 ? (
                <div>
                  <h4>暂无收到互动或 @ 提及</h4>
                  <p className="hint">全渠道运营读取到的公开回复和 @ 提及会出现在这里。</p>
                </div>
              ) : (
                inbox.map((event) => {
                  const payload = event.payload || {};
                  const sender =
                    event.sender_name || event.author_name || payload.author_name || "B 站观众";
                  const timeAgo = formatRelativeTime(event.timestamp || event.created_at);
                  const text =
                    event.source_text || payload.source_text || payload.text || "暂无公开原文";
                  return (
                    <div key={event.id} className={styles.inboxItem}>
                      <div className="panel-heading">
                        <strong>{sender}</strong>
                        <span className="hint">
                          {timeAgo} · {humanizeStatus(event.status).label}
                        </span>
                      </div>
                      <p className="quoted-text" style={{ margin: "6px 0" }}>
                        {text}
                      </p>
                      {event.resource_title && (
                        <small className="hint">来源视频：{event.resource_title}</small>
                      )}
                      {event.continuation && (
                        <OperationContinuationCard
                          value={event.continuation}
                          channel="browsing"
                          localId={`event:${event.id}`}
                          entityStatus={event.status}
                          onRefresh={onRefresh}
                        />
                      )}
                    </div>
                  );
                })
              ))}

            {activeTab === "intents" &&
              (intentPageSlice.items.length === 0 ? (
                <div>
                  <h4>
                    {intentFilter === "pending_review"
                      ? "暂无待审核的点赞或收藏"
                      : "暂无相关候选草稿"}
                  </h4>
                  <p className="hint">
                    {intentFilter === "pending_review"
                      ? "公开评论草稿在「评论与草稿」处理。"
                      : "可以切换至其他过滤标签查看历史记录。"}
                  </p>
                </div>
              ) : (
                intentPageSlice.items.map((intent) => {
                  const target = parseTarget(intent.target);
                  const bvid = String(intent.bvid || target.bvid || intent.source_id || "B站视频");
                  const relatedTask = tasks.find((t) => t.bvid === bvid);
                  const videoTitle = String(
                    relatedTask?.title || relatedTask?.metadata?.title || target.title || bvid
                  );
                  const kindInfo = humanizeKind(intent.kind);
                  const statusInfo = humanizeStatus(intent.status);
                  const isSelected = intent.id === selectedIntentId;
                  return (
                    <div
                      key={intent.id}
                      role="button"
                      tabIndex={0}
                      data-id={intent.id}
                      className={`${styles.candidateCard} ${isSelected ? styles.candidateCardSelected : ""}`.trim()}
                      onClick={() => {
                        setSelectedIntentId(intent.id);
                        setIntentError("");
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelectedIntentId(intent.id);
                          setIntentError("");
                        }
                      }}
                    >
                      <div className="panel-heading">
                        <div className="actions">
                          <span className="badge">{kindInfo.label}</span>
                          <span className={`badge ${statusInfo.className}`}>
                            {statusInfo.label}
                          </span>
                        </div>
                        <span className="hint">{formatRelativeTime(intent.created_at)}</span>
                      </div>
                      <div style={{ margin: "6px 0", fontWeight: 600 }} title={videoTitle}>
                        {videoTitle}
                      </div>
                      <div>{intent.text || "（点赞或收藏操作，无需文字）"}</div>
                      <div className="hint" style={{ marginTop: 4 }}>
                        💡 {humanizeReason(intent.reason)}
                      </div>
                    </div>
                  );
                })
              ))}
          </div>

          <div id="op-master-pagination" style={{ marginTop: 10 }}>
            {activeTab === "tasks" && (
              <Paginator
                currentPage={taskPageSlice.currentPage}
                totalItems={filteredTasks.length}
                pageSize={10}
                onPageChange={(next) => {
                  onSelectTaskId(null);
                  setTaskPage(next);
                }}
              />
            )}
            {activeTab === "rounds" && (
              <Paginator
                currentPage={roundPageSlice.currentPage}
                totalItems={rounds.length}
                pageSize={6}
                onPageChange={setRoundPage}
              />
            )}
            {activeTab === "intents" && (
              <Paginator
                currentPage={intentPageSlice.currentPage}
                totalItems={filteredIntents.length}
                pageSize={8}
                onPageChange={setIntentPage}
              />
            )}
          </div>
        </div>

        {/* Right Detail Inspector */}
        <div className={styles.detailPane} id="op-detail-pane">
          {activeTab === "tasks" &&
            (!selectedTask ? (
              <div>
                <h3>请选择左侧浏览任务</h3>
                <p className="hint">查看本轮取材与判断。</p>
              </div>
            ) : (
              (() => {
                const title =
                  selectedTask.title ||
                  selectedTask.metadata?.title ||
                  selectedTask.bvid ||
                  `任务 ${selectedTask.id}`;
                const statusInfo = humanizeStatus(selectedTask.status, selectedTask.material_mode);
                const bvidUrl = selectedTask.bvid
                  ? `https://www.bilibili.com/video/${encodeURIComponent(selectedTask.bvid)}`
                  : "#";
                const tracks = selectedTask.subtitle_tracks || [];
                const evidence = selectedTask.evidence || [];
                const drafts = [
                  selectedTask.intent_id,
                  ...(selectedTask.reply_intent_ids || []),
                ].filter((v): v is string => Boolean(v));
                const coverageLabels: Record<string, string> = {
                  all_available_cc: "已读可用字幕",
                  partial: "部分分 P 有材料",
                  sampled_visual: "已读取抽样画面",
                };
                const coverage =
                  (selectedTask.coverage && coverageLabels[selectedTask.coverage]) ||
                  selectedTask.coverage;
                const materialMap: Record<string, string> = {
                  subtitle_visual: "字幕＋画面",
                  subtitle: "仅字幕",
                  visual: "仅画面",
                  audio: "语音转写",
                  unavailable: "取材失败",
                };
                const material =
                  selectedTask.material_mode ? materialMap[selectedTask.material_mode] : undefined;

                return (
                  <div>
                    <div className="panel-heading">
                      <div className="actions">
                        <span className={`badge ${statusInfo.className}`}>{statusInfo.label}</span>
                        <span className="hint">{formatRelativeTime(selectedTask.created_at)}</span>
                      </div>
                      {selectedTask.bvid && (
                        <a href={bvidUrl} target="_blank" rel="noreferrer" className="win-link-btn">
                          🎬 在 B 站打开 ↗
                        </a>
                      )}
                    </div>

                    <div className={styles.videoContextCard}>
                      <h3>{title}</h3>
                      <p className="hint">{selectedTask.bvid || "—"}</p>
                    </div>

                    <div className={styles.aiReasonCard}>
                      <h4>处理进度与内容结果</h4>
                      {material && <p>{material}</p>}
                      <p>
                        {selectedTask.material_mode === "unavailable" && selectedTask.error
                          ? selectedTask.error
                          : humanizeTaskReason(selectedTask)}
                      </p>
                      {selectedTask.visual_error && (
                        <p className="hint">画面：{selectedTask.visual_error}</p>
                      )}
                    </div>

                    {Boolean(selectedTask.consecutive_failures) && (
                      <p className="hint">
                        连续失败 {selectedTask.consecutive_failures} 次
                        {selectedTask.consecutive_failures === 3 ? " · 已跳过" : ""}
                      </p>
                    )}

                    <details
                      className="subsection"
                      open={taskDetailsOpen.has("materials")}
                      onToggle={(e) => {
                        const open = e.currentTarget.open;
                        setTaskDetailsOpen((prev) => {
                          if (prev.has("materials") === open) return prev;
                          const next = new Set(prev);
                          if (open) next.add("materials");
                          else next.delete("materials");
                          return next;
                        });
                      }}
                    >
                      <summary>分 P 覆盖与引用材料</summary>
                      <p className="hint">
                        {selectedTask.cache_status === "missing"
                          ? "原材料已失效或清理"
                          : coverage || "暂无覆盖信息"}
                      </p>
                      {tracks.map((track, idx) => (
                        <p key={idx} className="hint">
                          CID {track.cid} · {track.language || "—"} ·{" "}
                          {track.is_ai == null
                            ? "类型未声明"
                            : track.is_ai
                              ? "平台 AI 字幕"
                              : "人工字幕"}{" "}
                          · {track.track_id || "无字幕轨"}
                        </p>
                      ))}
                      {Boolean(selectedTask.missing_parts?.length) && (
                        <p className="hint">
                          缺失分 P · {JSON.stringify(selectedTask.missing_parts)}
                        </p>
                      )}
                      {evidence.length === 0 ? (
                        <p className="hint">暂无可展示的引用片段。</p>
                      ) : (
                        evidence.map((segment, idx) => (
                          <div key={segment.id || idx}>
                            <p className="hint">
                              {segment.id?.startsWith("frame:") ? "画面观察" : "字幕原文"} · CID{" "}
                              {segment.cid || "—"} ·{" "}
                              {segment.start ?? segment.time ?? segment.timestamp ?? "—"} 秒
                            </p>
                            <blockquote className="quoted-text">
                              {segment.text || segment.observation || "材料正文不可用"}
                            </blockquote>
                          </div>
                        ))
                      )}
                    </details>

                    {drafts.map((id, index) => (
                      <p key={id}>
                        <a href={`#comments/browsing/${encodeURIComponent(id)}`}>
                          查看关联回应 {index + 1}
                        </a>
                      </p>
                    ))}

                    <OperationContinuationCard
                      value={selectedTask.autonomous ? null : selectedTask.continuation}
                      channel="browsing"
                      localId={selectedTask.id}
                      entityStatus={selectedTask.status}
                      onRefresh={onRefresh}
                    />

                    {!selectedTask.autonomous && (
                      <div className="actions" style={{ marginTop: 16 }}>
                        <button
                          type="button"
                          className="primary"
                          id="op-btn-reread"
                          disabled={rereading}
                          onClick={() => void handleRereadTask(selectedTask)}
                        >
                          🎬 重新深度分析视频并生成草稿
                        </button>
                      </div>
                    )}
                  </div>
                );
              })()
            ))}

          {activeTab === "intents" &&
            (!selectedIntent ? (
              <div>
                <h3>选择一条点赞或收藏记录</h3>
              </div>
            ) : (
              (() => {
                const target = parseTarget(selectedIntent.target);
                const bvid = String(target.bvid || "");
                const dynamicId = target.dynamic_id ? String(target.dynamic_id) : "";
                const statusInfo = humanizeStatus(selectedIntent.status);
                const editable = ["pending_review", "approved"].includes(selectedIntent.status);
                return (
                  <div>
                    <div className="panel-heading">
                      <h3>{humanizeKind(selectedIntent.kind).label}</h3>
                      <span className={`badge ${statusInfo.className}`}>{statusInfo.label}</span>
                    </div>
                    <p>
                      {String(target.title || bvid || (dynamicId ? `动态 ${dynamicId}` : ""))}
                    </p>
                    {bvid ? (
                      <a
                        href={`https://www.bilibili.com/video/${encodeURIComponent(bvid)}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        在 B 站查看来源
                      </a>
                    ) : dynamicId ? (
                      <a
                        href={`https://t.bilibili.com/${encodeURIComponent(dynamicId)}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        在 B 站查看来源
                      </a>
                    ) : null}
                    <blockquote className="quoted-text">
                      {String(
                        ["comment_like", "dynamic_like"].includes(selectedIntent.kind)
                          ? target.source_text || ""
                          : selectedIntent.text || ""
                      )}
                    </blockquote>
                    <p className="hint">{intentReviewNote(selectedIntent)}</p>
                    <div className="actions">
                      {selectedIntent.status === "pending_review" && (
                        <button
                          type="button"
                          className="primary"
                          disabled={intentPending}
                          onClick={() => void handleIntentAction(selectedIntent, "approve")}
                        >
                          审核通过
                        </button>
                      )}
                      {editable && (
                        <button
                          type="button"
                          disabled={intentPending}
                          onClick={() => void handleIntentAction(selectedIntent, "reject")}
                        >
                          拒绝
                        </button>
                      )}
                      {selectedIntent.status === "approved" && (
                        <button
                          type="button"
                          disabled={intentPending}
                          onClick={() => void handleIntentAction(selectedIntent)}
                        >
                          提交已审核操作
                        </button>
                      )}
                    </div>
                    <p className="error-text" data-intent-error="" role="status">
                      {intentError || selectedIntent.error || ""}
                    </p>
                    {(selectedIntent.receipt || selectedIntent.round_id) && (
                      <details className={styles.debugDisclosure}>
                        <summary>技术追踪与平台回执</summary>
                        <div className={styles.debugDisclosureBody}>
                          {selectedIntent.round_id && (
                            <a href={`#automation/round/${encodeURIComponent(selectedIntent.round_id)}`}>
                              查看来源轮次 ({selectedIntent.round_id})
                            </a>
                          )}
                          {selectedIntent.receipt && (
                            <details className="subsection">
                              <summary>
                                {(selectedIntent.receipt as { observed?: unknown }).observed
                                  ? "平台核对结果"
                                  : "平台回执"}
                              </summary>
                              <pre className="operation-revision-text">
                                {JSON.stringify(selectedIntent.receipt, null, 2)}
                              </pre>
                            </details>
                          )}
                        </div>
                      </details>
                    )}
                    <OperationContinuationCard
                      value={selectedIntent.continuation}
                      channel="browsing"
                      localId={selectedIntent.id}
                      entityStatus={selectedIntent.status}
                      onRefresh={onRefresh}
                    />
                  </div>
                );
              })()
            ))}

          {(activeTab === "rounds" || activeTab === "inbox") && (
            <div>
              <h3>运行记录与简报概览</h3>
              <p className="hint">
                左侧正在展示全量自主巡航与互动记录，您可翻页浏览或展开技术明细。
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
