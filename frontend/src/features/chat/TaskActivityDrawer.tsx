import { memo, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost, apiPut, studioPath } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import { illustrationUrl } from "@/shared/lib/operationImage";
import { CapabilityStrip, type OperationCapabilityStatus } from "@/shared/ui/CapabilityStrip";
import { MarkdownText } from "@/shared/ui/MarkdownText";
import { MentionLinks, type PublicMention } from "@/shared/ui/MentionLinksAndEditor";
import styles from "./ChatPage.module.css";
import { DynamicPublicationApproval } from "./DynamicPublicationApproval";
import { GoalEventsList } from "./GoalEventsList";
import { GoalFollowupForm } from "./GoalFollowupForm";
import { BrowserTaskCreateForm, TaskBrowserPanel } from "./TaskBrowserPanel";
import {
  createRequestId,
  safeHttpsUrl,
  type AgentTask,
  type AgentTaskActivityResponse,
  type AgentTaskStep,
  type CreativeEdition,
  type DynamicPublicationApprovalData,
  type OpenedArtifactRef,
} from "./types";

export type ActivitySection = "activity" | "results" | "permissions";

type SubView =
  | { kind: "main" }
  | { kind: "schedule" }
  | { kind: "attachment" }
  | { kind: "browser_create" }
  | { kind: "browser_panel" }
  | { kind: "steer" }
  | { kind: "editor"; mode: "next_task" | "edit_artifact"; title: string; initial: string; limit: number };

const TASK_STATES: Record<string, string> = {
  queued: "等待执行",
  scheduled: "已预约",
  running: "进行中",
  waiting: "等待工具结果",
  awaiting_approval: "等待确认",
  needs_input: "等待你的答复",
  completed: "已完成",
  cancelled: "已停止",
  failed: "未完成",
  paused: "已暂停",
};

const TOOL_LABELS: Record<string, string> = {
  web_search: "搜索网页",
  web_page: "打开来源",
  bilibili_owner: "查号主主页",
  operation_status: "查看运营",
  today_activity: "查看当日活动",
  inspect_video: "读取视频",
  live_status: "查看直播",
  game_status: "查看游戏",
  live_handoff: "交接直播素材",
  game_action: "执行模拟游戏动作",
  inspect_dynamic_draft: "核对动态草稿",
  publish_dynamic_draft: "发布指定动态",
  publish_dynamic_batch: "批量发布动态",
  delegate_text: "委派文本工作",
  owner_attachment: "号主提供的私有附件",
  owner_code_grant: "已允许本次隔离计算",
  private_code: "处理私有文件",
  private_browser: "阅读浏览器",
  recall_memory: "检索记忆",
  workshop_lookup: "查询工坊记录",
  authorized_inbox_text: "授权读取的来信",
  inbox_received: "来信入库回执",
  comment_received: "评论楼事件",
};

const STOPPABLE_STATES = new Set([
  "queued",
  "scheduled",
  "running",
  "waiting",
  "needs_input",
  "awaiting_approval",
  "paused",
]);

const ATTENTION_STATES = new Set(["needs_input", "awaiting_approval"]);

const ACCOUNT_STATE_LABELS: Record<string, string> = {
  not_configured: "未配置",
  unverified: "未验证",
  logged_in: "已登录",
  expired: "已过期",
  restricted: "受限",
  error: "异常",
};

function formatTimeLabel(value: number | undefined): string {
  if (!value) return "";
  return new Date(value * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

function observedStatus(tool: string, result: Record<string, unknown> | null | undefined): string {
  if (!result || result.error) return "";
  if (tool === "operation_status") {
    const labels: Record<string, string> = {
      off: "未启动",
      starting: "正在启动",
      running: "运行中",
      draining: "正在收尾",
      stopping: "正在停止",
      blocked: "已阻塞",
      emergency: "紧急停止",
    };
    const state = typeof result.state === "string" ? result.state : "";
    return labels[state] || "";
  }
  if (tool === "live_status") {
    const platform: Record<string, string> = { stopped: "已停止", live: "直播中", paused: "已暂停" };
    const native: Record<string, string> = {
      idle: "空闲",
      starting: "正在启动",
      publishing: "正在发布",
      stopping: "正在停止",
      unavailable: "暂不可查",
    };
    const state = typeof result.state === "string" ? result.state : "";
    const program = (result.native_program as { state?: string } | undefined)?.state || "";
    const parts: string[] = [];
    if (platform[state]) parts.push(`平台直播${platform[state]}`);
    if (native[program]) parts.push(`Omni 节目${native[program]}`);
    return parts.join("，");
  }
  return "";
}

function downloadText(filename: string, text: string, mime = "text/plain;charset=utf-8"): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function SafeExternalAnchor({ url, label }: { url: unknown; label?: unknown }) {
  const href = safeHttpsUrl(url);
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {typeof label === "string" && label ? label : href}
    </a>
  );
}

const TaskStepItem = memo(
  function TaskStepItem({ taskId, step }: { taskId: string; step: AgentTaskStep }) {
    const videoRef = useRef<HTMLVideoElement | null>(null);
    const label = TOOL_LABELS[step.tool] || step.tool;
    const result = step.result || null;
    const observation = observedStatus(step.tool, result);
    const summary =
      observation || (result ? (result.error ? "工具返回错误" : "已取得结果") : "等待结果");

    const clip = (result?.clip as
      | { bvid: string; cid: string; part: number; start: number; end: number; frames: number }
      | undefined);
    const description =
      result &&
      (result.error ||
        result.spoken ||
        result.message ||
        result.excerpt ||
        (result.state as { summary?: string } | undefined)?.summary);
    const links = Array.isArray(result?.links) ? (result.links as string[]) : [];

    return (
      <li data-step-id={step.id}>
        {step.tool === "inspect_video" && clip && (
          <details
            onToggle={(event) => {
              if (!(event.currentTarget as HTMLDetailsElement).open) {
                videoRef.current?.pause();
              }
            }}
          >
            <summary>{`查看 P${clip.part} · ${clip.start}–${clip.end} 秒画面`}</summary>
            <video
              ref={videoRef}
              controls
              preload="none"
              playsInline
              className={styles.videoPreview}
              src={studioPath(
                `/api/v1/agent/tasks/${encodeURIComponent(taskId)}/clips/${encodeURIComponent(step.id)}`
              )}
            />
            <p className="hint">{`${clip.bvid} · CID ${clip.cid} · ${clip.frames} 个连续原帧 · 仅画面`}</p>
            {/^BV[0-9A-Za-z]{10}$/.test(clip.bvid) &&
              Number.isInteger(clip.part) &&
              clip.part > 0 &&
              Number.isFinite(clip.start) &&
              clip.start >= 0 && (
                <SafeExternalAnchor
                  url={`https://www.bilibili.com/video/${clip.bvid}/?p=${clip.part}&t=${Math.floor(clip.start)}`}
                  label="跳到原片"
                />
              )}
          </details>
        )}
        {step.tool === "private_code" &&
          typeof result?.text === "string" &&
          typeof result?.name === "string" && (
            <button
              type="button"
              onClick={() => downloadText(result.name as string, result.text as string)}
            >
              下载 {result.name}
            </button>
          )}
        <details>
          <summary>{`${label} · ${summary}`}</summary>
          {typeof description === "string" && description && <p>{description}</p>}
          {result?.url != null && (
            <SafeExternalAnchor
              url={result.url}
              label={result.title || result.name || "打开来源"}
            />
          )}
          {links.map((url, idx) => (
            <SafeExternalAnchor key={`${url}-${idx}`} url={url} />
          ))}
          <details>
            <summary>工具记录</summary>
            <pre>{JSON.stringify(result || step.arguments, null, 2)}</pre>
          </details>
        </details>
      </li>
    );
  },
  (prev, next) => prev.taskId === next.taskId && JSON.stringify(prev.step) === JSON.stringify(next.step)
);

function HistoricalEditionItem({
  taskId,
  entry,
  onOpened,
}: {
  taskId: string;
  entry: CreativeEdition;
  onOpened: (ref: OpenedArtifactRef) => void;
}) {
  const [loaded, setLoaded] = useState<CreativeEdition | null>(null);
  const [error, setError] = useState("");
  const label = [entry.author_name, `任务 r${entry.task_revision}`, `成果 v${entry.edition}`]
    .filter(Boolean)
    .join(" · ");

  async function handleToggle(open: boolean) {
    if (!open) return;
    const coord: OpenedArtifactRef = {
      task_id: taskId,
      task_revision: entry.task_revision,
      edition: entry.edition,
    };
    if (loaded) {
      onOpened(coord);
      return;
    }
    try {
      const saved = await apiGet<
        "/api/v1/agent/tasks/{identity}/artifacts/{revision}/{edition}",
        CreativeEdition
      >("/api/v1/agent/tasks/{identity}/artifacts/{revision}/{edition}", {
        pathParams: {
          identity: taskId,
          revision: entry.task_revision,
          edition: entry.edition,
        },
      });
      setLoaded(saved);
      onOpened(coord);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <details onToggle={(event) => void handleToggle((event.currentTarget as HTMLDetailsElement).open)}>
      <summary>{label}</summary>
      <div>
        {error ? (
          <p className="hint">{error}</p>
        ) : loaded ? (
          <MarkdownText allowLinks text={loaded.body} />
        ) : null}
      </div>
    </details>
  );
}

interface BilibiliAccountInfo {
  state?: string;
  profile?: {
    name?: string | null;
    avatar_url?: string | null;
  } | null;
}

export interface TaskActivityDrawerProps {
  open: boolean;
  section: ActivitySection;
  selectedTaskId: string | null;
  onSelectView: (taskId: string | null, section?: ActivitySection) => void;
  onClose: (restoreFocus?: boolean) => void;
  onArtifactOpened: (ref: OpenedArtifactRef | null) => void;
  onAttentionChanged: () => void;
  onChatChanged: () => void;
}

export function TaskActivityDrawer({
  open,
  section,
  selectedTaskId,
  onSelectView,
  onClose,
  onArtifactOpened,
  onAttentionChanged,
  onChatChanged,
}: TaskActivityDrawerProps) {
  const [activityData, setActivityData] = useState<AgentTaskActivityResponse | null>(null);
  const [taskDetail, setTaskDetail] = useState<AgentTask | null>(null);
  const [automationStatus, setAutomationStatus] = useState<OperationCapabilityStatus | null>(null);
  const [accountInfo, setAccountInfo] = useState<BilibiliAccountInfo | null>(null);
  const [statusText, setStatusText] = useState("");
  const [subView, setSubView] = useState<SubView>({ kind: "main" });
  const [nestedEditing, setNestedEditing] = useState(false);

  // Form states for schedule / attachment / steer / editor
  const [scheduleGoal, setScheduleGoal] = useState("");
  const [scheduleWhen, setScheduleWhen] = useState("");
  const [attachGoal, setAttachGoal] = useState("");
  const [attachAllowCode, setAttachAllowCode] = useState(false);
  const [attachFiles, setAttachFiles] = useState<File[]>([]);
  const [steerGoal, setSteerGoal] = useState("");
  const [editorText, setEditorText] = useState("");
  const [formBusy, setFormBusy] = useState(false);
  const [historyEditions, setHistoryEditions] = useState<CreativeEdition[] | null>(null);
  const [historyError, setHistoryError] = useState("");

  const closeBtnRef = useRef<HTMLButtonElement | null>(null);
  const scheduleRequestRef = useRef<{ key: string; id: string } | null>(null);
  const attachRequestRef = useRef<{ key: string; id: string } | null>(null);
  const nextTaskIdRef = useRef<string>(createRequestId());
  const versionRef = useRef(0);
  const readingRef = useRef(false);

  const editing = subView.kind !== "main" || nestedEditing;

  const loadData = useCallback(async () => {
    if (!open || editing || readingRef.current) return;
    const currentVersion = ++versionRef.current;
    readingRef.current = true;
    try {
      if (section === "permissions") {
        const auto = await apiGet<"/api/v1/operations/automation", OperationCapabilityStatus>(
          "/api/v1/operations/automation"
        );
        if (currentVersion !== versionRef.current) return;
        setAutomationStatus(auto);
        setStatusText("");
      } else if (selectedTaskId) {
        const task = await apiGet<"/api/v1/agent/tasks/{identity}", AgentTask>(
          "/api/v1/agent/tasks/{identity}",
          { pathParams: { identity: selectedTaskId } }
        );
        if (currentVersion !== versionRef.current) return;
        setTaskDetail(task);
        setActivityData(null);
        setStatusText("");
        if (section === "results" && task.artifact) {
          onArtifactOpened({
            task_id: task.id,
            task_revision: task.artifact.task_revision,
            edition: task.artifact.edition,
          });
        } else if (section === "activity" && task.artifact) {
          onArtifactOpened({
            task_id: task.id,
            task_revision: task.artifact.task_revision,
            edition: task.artifact.edition,
          });
        }
        if (section === "activity" && task.attention != null) {
          await apiDelete("/api/v1/agent/tasks/{identity}/attention", {
            pathParams: { identity: selectedTaskId },
            query: { updated: task.attention },
          }).catch(() => {});
          onAttentionChanged();
        }
      } else {
        const list = await apiGet<"/api/v1/agent/tasks", AgentTaskActivityResponse>(
          "/api/v1/agent/tasks"
        );
        if (currentVersion !== versionRef.current) return;
        setActivityData(list);
        setTaskDetail(null);
        setStatusText("");
      }
    } catch (error) {
      if (currentVersion !== versionRef.current) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
        setActivityData(null);
        setTaskDetail(null);
      }
      setStatusText(errorMessage(error));
    } finally {
      readingRef.current = false;
    }
  }, [open, editing, section, selectedTaskId, onArtifactOpened, onAttentionChanged]);

  // Reset subview & set initial loading message whenever drawer opens or navigation changes
  useEffect(() => {
    setSubView({ kind: "main" });
    setNestedEditing(false);
    setHistoryEditions(null);
    setHistoryError("");
    if (open) {
      const tabNames: Record<ActivitySection, string> = {
        activity: "活动",
        results: "成果",
        permissions: "权限",
      };
      setStatusText(`正在读取${tabNames[section]}…`);
      closeBtnRef.current?.focus();
      void apiGet<"/api/v1/operations/account/bilibili", BilibiliAccountInfo>(
        "/api/v1/operations/account/bilibili"
      )
        .then(setAccountInfo)
        .catch(() => {});
    } else {
      setStatusText("");
    }
  }, [open, section, selectedTaskId]);

  useEffect(() => {
    if (!open || editing) return;
    void loadData();
    if (section === "permissions") return;
    const timer = setInterval(() => {
      void loadData();
    }, 2000);
    return () => clearInterval(timer);
  }, [open, editing, section, loadData]);

  // Refresh when external chat/call mutations occur
  useEffect(() => {
    if (!open || typeof document === "undefined") return;
    const handler = () => {
      void loadData();
    };
    document.addEventListener("operator-chat-changed", handler);
    return () => document.removeEventListener("operator-chat-changed", handler);
  }, [open, loadData]);

  async function applyTaskControl(
    task: AgentTask,
    change: { action: "steer" | "cancel" | "approve" | "pause" | "resume"; goal?: string; step_id?: string }
  ) {
    setFormBusy(true);
    try {
      const updated = await apiPatch<
        "/api/v1/agent/tasks/{identity}",
        AgentTask,
        typeof change & { revision: number }
      >("/api/v1/agent/tasks/{identity}", {
        pathParams: { identity: task.id },
        body: { ...change, revision: task.revision },
      });
      setSubView({ kind: "main" });
      setTaskDetail(updated);
      setStatusText("");
    } catch (error) {
      setStatusText(errorMessage(error));
    } finally {
      setFormBusy(false);
    }
  }

  async function handleScheduleSubmit(event: FormEvent) {
    event.preventDefault();
    const runAt = new Date(scheduleWhen).getTime() / 1000;
    if (!scheduleGoal.trim() || !Number.isFinite(runAt) || runAt <= Date.now() / 1000) {
      setStatusText("请填写任务和未来的开始时间。");
      return;
    }
    const payload = { goal: scheduleGoal.trim(), run_at: runAt };
    const key = JSON.stringify(payload);
    if (scheduleRequestRef.current?.key !== key) {
      scheduleRequestRef.current = { key, id: createRequestId() };
    }
    setFormBusy(true);
    try {
      const created = await apiPost<
        "/api/v1/agent/tasks",
        AgentTask,
        { request_id: string; goal: string; run_at: number }
      >("/api/v1/agent/tasks", {
        body: { ...payload, request_id: scheduleRequestRef.current.id },
      });
      setSubView({ kind: "main" });
      onSelectView(created.id, "activity");
    } catch (error) {
      setStatusText(errorMessage(error));
    } finally {
      setFormBusy(false);
    }
  }

  async function handleAttachmentSubmit(event: FormEvent) {
    event.preventDefault();
    const objective = attachGoal.trim();
    setFormBusy(true);
    try {
      if (!objective || (!attachFiles.length && !attachAllowCode) || attachFiles.length > 3) {
        throw new Error("请填写任务并选择附件或启用隔离计算。");
      }
      const attachments = await Promise.all(
        attachFiles.map(async (file) => {
          if (!/\.(md|txt|csv|json|srt|vtt)$/.test(file.name) || file.size > 32000) {
            throw new Error("请选择小型文本、CSV、JSON 或字幕文件。");
          }
          const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
          if (!text.trim() || Array.from(text).length > 8000) {
            throw new Error("文本为空或超过 8000 字符。");
          }
          return { name: file.name, text };
        })
      );
      const payload = { goal: objective, attachments, allow_code: attachAllowCode };
      const key = JSON.stringify(payload);
      if (attachRequestRef.current?.key !== key) {
        attachRequestRef.current = { key, id: createRequestId() };
      }
      const created = await apiPost<
        "/api/v1/agent/tasks",
        AgentTask,
        typeof payload & { request_id: string }
      >("/api/v1/agent/tasks", {
        body: { ...payload, request_id: attachRequestRef.current.id },
      });
      setSubView({ kind: "main" });
      onSelectView(created.id, "activity");
    } catch (error) {
      setStatusText(errorMessage(error));
    } finally {
      setFormBusy(false);
    }
  }

  async function loadArtifactHistory(taskId: string) {
    if (historyEditions !== null) return;
    setHistoryError("");
    try {
      const rows = await apiGet<"/api/v1/agent/tasks/{identity}/artifacts", CreativeEdition[]>(
        "/api/v1/agent/tasks/{identity}/artifacts",
        { pathParams: { identity: taskId } }
      );
      setHistoryEditions(rows);
    } catch (error) {
      setHistoryError(errorMessage(error));
    }
  }

  async function promoteGoal(taskId: string) {
    setFormBusy(true);
    try {
      const updated = await apiPut<"/api/v1/agent/tasks/{identity}/goal", AgentTask>(
        "/api/v1/agent/tasks/{identity}/goal",
        { pathParams: { identity: taskId } }
      );
      setTaskDetail(updated);
      onSelectView(updated.id, "activity");
    } catch (error) {
      setStatusText(errorMessage(error));
    } finally {
      setFormBusy(false);
    }
  }

  async function closeGoal(task: AgentTask) {
    const goal = task.creative_goal;
    if (!goal) return;
    const scope = goal.stop_scope || {};
    const runs = goal.runs.filter((run) => Object.prototype.hasOwnProperty.call(scope, run.id));
    if (
      runs.length &&
      typeof window !== "undefined" &&
      !window.confirm(
        [
          `结束「${goal.title}」并停止当前授权的 ${runs.length} 项执行？`,
          ...runs.map((run) => `· ${run.goal}`),
          "周期和事件跟进也会停止。已提交的动作只核对原回执，历史和成果保留。",
        ].join("\n")
      )
    ) {
      return;
    }
    setFormBusy(true);
    try {
      const updated = await apiPost<
        "/api/v1/agent/tasks/{identity}/goal/close",
        AgentTask,
        { confirmed_runs: Record<string, number> }
      >("/api/v1/agent/tasks/{identity}/goal/close", {
        pathParams: { identity: goal.id },
        body: { confirmed_runs: scope },
      });
      setTaskDetail(updated);
      onSelectView(updated.id, "activity");
    } catch (error) {
      setStatusText(errorMessage(error));
    } finally {
      setFormBusy(false);
    }
  }

  const profile = accountInfo?.profile;
  const accountName =
    profile?.name || (accountInfo?.state === "not_configured" ? "未绑定 B站" : "B站账号");
  const accountState = accountInfo?.state
    ? ACCOUNT_STATE_LABELS[accountInfo.state] || accountInfo.state
    : "";

  const renderContent = () => {
    if (!open || section === "permissions") return null;

    if (section === "results") {
      if (!selectedTaskId) {
        const tasks = activityData?.tasks || [];
        if (activityData && !tasks.length) {
          return <p className="hint">暂无任务成果。</p>;
        }
        if (!activityData) return null;
        return (
          <label>
            按任务查看成果
            <select
              value=""
              onChange={(event) => {
                if (event.target.value) onSelectView(event.target.value, "results");
              }}
            >
              <option value="">选择任务</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.goal}
                </option>
              ))}
            </select>
          </label>
        );
      }
      if (!taskDetail) return null;
      return (
        <>
          <button type="button" onClick={() => onSelectView(null, "results")}>
            选择其他任务
          </button>
          <h3>{taskDetail.goal}</h3>
          {taskDetail.artifact ? (
            <MarkdownText allowLinks text={taskDetail.artifact.body} />
          ) : (
            <p className="hint">这项任务暂无成果。</p>
          )}
          <button type="button" onClick={() => onSelectView(taskDetail.id, "activity")}>
            查看任务
          </button>
        </>
      );
    }

    // section === "activity"
    if (subView.kind === "schedule") {
      return (
        <form className="chat-task-steer" onSubmit={(event) => void handleScheduleSubmit(event)}>
          <label>
            任务要求
            <textarea
              required
              rows={3}
              maxLength={2000}
              value={scheduleGoal}
              onChange={(event) => setScheduleGoal(event.target.value)}
            />
          </label>
          <label>
            开始时间
            <input
              type="datetime-local"
              required
              value={scheduleWhen}
              onChange={(event) => setScheduleWhen(event.target.value)}
            />
          </label>
          <button type="submit" disabled={formBusy}>
            保存预约
          </button>
          <button
            type="button"
            onClick={() => {
              setSubView({ kind: "main" });
              onSelectView(null, "activity");
            }}
          >
            返回活动
          </button>
        </form>
      );
    }

    if (subView.kind === "attachment") {
      return (
        <form className="chat-task-steer" onSubmit={(event) => void handleAttachmentSubmit(event)}>
          <label>
            希望用这些材料做什么？
            <textarea
              required
              maxLength={2000}
              value={attachGoal}
              onChange={(event) => setAttachGoal(event.target.value)}
            />
          </label>
          <label>
            私有附件（最多三份 UTF-8 文本、数据或字幕，每份最多 8000 字符）
            <input
              type="file"
              accept=".md,.txt,.csv,.json,.srt,.vtt"
              multiple
              required={!attachAllowCode}
              onChange={(event) => setAttachFiles(Array.from(event.target.files || []))}
            />
          </label>
          <label className="check-field">
            <input
              type="checkbox"
              checked={attachAllowCode}
              onChange={(event) => setAttachAllowCode(event.target.checked)}
            />
            <span>允许本次在隔离环境中运行 JavaScript 计算并生成文件</span>
          </label>
          <p className="hint">材料只供当前角色与后台创作使用。这项任务不能访问网页或平台。</p>
          <button type="submit" disabled={formBusy}>
            交办
          </button>
          <button
            type="button"
            onClick={() => {
              setSubView({ kind: "main" });
              onSelectView(null, "activity");
            }}
          >
            返回活动
          </button>
        </form>
      );
    }

    if (subView.kind === "browser_create") {
      return (
        <BrowserTaskCreateForm
          onCreated={(id) => {
            setSubView({ kind: "main" });
            onSelectView(id, "activity");
          }}
          onCancel={() => {
            setSubView({ kind: "main" });
            onSelectView(null, "activity");
          }}
        />
      );
    }

    if (!selectedTaskId) {
      if (!activityData) return null;
      const tasks = activityData.tasks || [];
      const goals = activityData.goals || [];
      const notices = activityData.attention || [];
      const groups: Array<[string, AgentTask[]]> = [
        ["需要处理", tasks.filter((t) => ATTENTION_STATES.has(t.status))],
        [
          "进行中",
          tasks.filter(
            (t) => STOPPABLE_STATES.has(t.status) && t.status !== "scheduled" && !ATTENTION_STATES.has(t.status)
          ),
        ],
        [
          "已预约",
          tasks.filter((t) => t.status === "scheduled").sort((a, b) => (a.due || 0) - (b.due || 0)),
        ],
        ["已结束", tasks.filter((t) => !STOPPABLE_STATES.has(t.status))],
      ];

      return (
        <>
          <button
            type="button"
            onClick={() => {
              setScheduleGoal("");
              setScheduleWhen("");
              setSubView({ kind: "schedule" });
            }}
          >
            预约任务
          </button>
          <button
            type="button"
            onClick={() => {
              setAttachGoal("");
              setAttachAllowCode(false);
              setAttachFiles([]);
              setSubView({ kind: "attachment" });
            }}
          >
            用文本附件交办
          </button>
          <button type="button" onClick={() => setSubView({ kind: "browser_create" })}>
            用浏览器交办
          </button>

          {notices.length > 0 && <h3 className="chat-activity-group">给号主的更新</h3>}
          {notices.map((item) => (
            <button
              key={`notice-${item.id}`}
              type="button"
              onClick={() => onSelectView(item.id, "activity")}
            >
              {`${TASK_STATES[item.status] || item.status} · ${item.goal}`}
            </button>
          ))}

          {goals.length > 0 && <h3 className="chat-activity-group">长期创作目标</h3>}
          {goals.map((goal) => (
            <button
              key={`goal-${goal.id}`}
              type="button"
              onClick={() => onSelectView(goal.id, "activity")}
            >
              {goal.title + (goal.closed ? " · 已结束" : "")}
            </button>
          ))}

          {groups.map(([heading, group]) => {
            if (!group.length) return null;
            return (
              <div key={heading}>
                <h3 className="chat-activity-group">{heading}</h3>
                <ul className="chat-activity-list">
                  {group.map((task) => (
                    <li key={task.id}>
                      <button
                        type="button"
                        className="chat-task-row"
                        onClick={() => onSelectView(task.id, "activity")}
                      >
                        {task.goal}
                        <small className="hint">
                          {task.status === "scheduled"
                            ? formatTimeLabel(task.due)
                            : TASK_STATES[task.status] || task.status}
                        </small>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}

          {!tasks.length && <p className="hint">你交办的工作会出现在这里。</p>}
        </>
      );
    }

    // Selected task detail
    if (!taskDetail) return null;
    const task = taskDetail;

    if (subView.kind === "editor") {
      return (
        <form
          className="chat-task-steer"
          onSubmit={(event) => {
            event.preventDefault();
            const text = editorText.trim();
            if (!text) return;
            setFormBusy(true);
            if (subView.mode === "next_task" && task.creative_goal) {
              void apiPost<
                "/api/v1/agent/tasks",
                AgentTask,
                { request_id: string; goal: string; creative_goal_id: string }
              >("/api/v1/agent/tasks", {
                body: {
                  request_id: nextTaskIdRef.current,
                  goal: text,
                  creative_goal_id: task.creative_goal.id,
                },
              })
                .then((updated) => {
                  setSubView({ kind: "main" });
                  onSelectView(updated.id, "activity");
                })
                .catch((err) => setStatusText(errorMessage(err)))
                .finally(() => setFormBusy(false));
            } else if (subView.mode === "edit_artifact" && task.artifact) {
              void apiPatch<
                "/api/v1/agent/tasks/{identity}/artifact",
                AgentTask,
                { task_revision: number; edition: number; body: string }
              >("/api/v1/agent/tasks/{identity}/artifact", {
                pathParams: { identity: task.id },
                body: {
                  task_revision: task.artifact.task_revision,
                  edition: task.artifact.edition,
                  body: text,
                },
              })
                .then((updated) => {
                  setSubView({ kind: "main" });
                  setTaskDetail(updated);
                })
                .catch((err) => setStatusText(errorMessage(err)))
                .finally(() => setFormBusy(false));
            }
          }}
        >
          <label>
            {subView.title}
            <textarea
              required
              rows={6}
              maxLength={subView.limit}
              value={editorText}
              onChange={(event) => setEditorText(event.target.value)}
            />
          </label>
          <button type="submit" disabled={formBusy}>
            保存
          </button>
          <button type="button" onClick={() => setSubView({ kind: "main" })}>
            返回
          </button>
        </form>
      );
    }

    if (subView.kind === "browser_panel") {
      return (
        <TaskBrowserPanel
          task={task}
          isOpen
          onOpen={() => {}}
          onClose={() => setSubView({ kind: "main" })}
        />
      );
    }

    const canStopOrPause = task.can_control !== false && STOPPABLE_STATES.has(task.status);
    const resumable = task.status === "paused" || task.pause_requested;
    const goal = task.creative_goal;
    const artifact = task.artifact;
    const approvalStep =
      task.can_control !== false && task.status === "awaiting_approval"
        ? task.steps[task.steps.length - 1]
        : null;
    const approval = approvalStep?.approval as
      | (DynamicPublicationApprovalData & {
          target?: string;
          text?: string;
          channel?: string;
          draft_id?: number;
          post_id?: number;
          payload?: {
            mentions?: PublicMention[];
            meme_id?: string;
            artifact_id?: string;
            message_kind?: string;
            comment_card_sha256?: string;
            comment_card_page_sha256?: string[];
          };
          sources?: Array<{ source_url?: string; public_sources?: Array<{ url?: string }> }>;
        })
      | undefined;

    return (
      <>
        <button type="button" onClick={() => onSelectView(null, "activity")}>
          全部活动
        </button>
        <h3>{task.goal}</h3>
        <p className="hint" role="status">
          {task.status === "scheduled"
            ? `已预约 · ${formatTimeLabel(task.due)}`
            : TASK_STATES[task.status] || task.status}
        </p>
        <div className="chat-task-result">
          {task.message && (!artifact || task.status !== "completed") && (
            <>
              {(task.status === "completed" || task.status === "failed") &&
                task.steps.some((step) => step.result) && (
                  <p className="hint">按工具回执交付</p>
                )}
              <MarkdownText text={task.message} />
            </>
          )}
        </div>
        <ol className="chat-task-steps">
          {task.steps.map((step) => (
            <TaskStepItem key={step.id} taskId={task.id} step={step} />
          ))}
        </ol>

        {task.can_control !== false &&
          (subView.kind === "steer" ? (
            <form
              className="chat-task-steer"
              onSubmit={(event) => {
                event.preventDefault();
                if (steerGoal.trim()) {
                  void applyTaskControl(task, { action: "steer", goal: steerGoal.trim() });
                }
              }}
            >
              <label>
                新的任务要求
                <textarea
                  required
                  maxLength={2000}
                  rows={4}
                  value={steerGoal}
                  onChange={(event) => setSteerGoal(event.target.value)}
                />
              </label>
              <button type="submit" disabled={formBusy}>
                继续任务
              </button>
              <button type="button" onClick={() => setSubView({ kind: "main" })}>
                取消修改
              </button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => {
                setSteerGoal("");
                setSubView({ kind: "steer" });
              }}
            >
              {task.status === "needs_input" ? "补充要求" : "调整任务"}
            </button>
          ))}

        {canStopOrPause && (
          <>
            <button
              type="button"
              disabled={formBusy}
              onClick={() => void applyTaskControl(task, { action: resumable ? "resume" : "pause" })}
            >
              {resumable ? (task.pause_requested ? "取消待暂停" : "恢复任务") : "暂停任务"}
            </button>
            <button
              type="button"
              disabled={formBusy}
              onClick={() => void applyTaskControl(task, { action: "cancel" })}
            >
              停止任务
            </button>
          </>
        )}

        {task.can_control === false && !goal?.closed && (
          <p className="hint">原执行授权已失效。记录与成果仍可查看；如需继续，请重新交办。</p>
        )}

        <GoalFollowupForm goal={goal} onEditingChange={setNestedEditing} />
        <GoalEventsList goal={goal} onEditingChange={setNestedEditing} />

        {!goal ? (
          <button type="button" disabled={formBusy} onClick={() => void promoteGoal(task.id)}>
            设为长期创作目标
          </button>
        ) : (
          <>
            <h3>{goal.title}</h3>
            <p className="hint">{goal.closed ? "长期目标已结束" : "长期目标 · 执行记录"}</p>
            {goal.runs.map((run) =>
              run.id === task.id ? null : (
                <button
                  key={run.id}
                  type="button"
                  onClick={() => onSelectView(run.id, "activity")}
                >
                  {`${run.goal} · ${TASK_STATES[run.status] || run.status}`}
                </button>
              )
            )}
            {!goal.closed && (
              <>
                <button
                  type="button"
                  onClick={() => {
                    nextTaskIdRef.current = createRequestId();
                    setEditorText("");
                    setSubView({
                      kind: "editor",
                      mode: "next_task",
                      title: "新任务要求",
                      initial: "",
                      limit: 2000,
                    });
                  }}
                >
                  交办下一项工作
                </button>
                <button type="button" disabled={formBusy} onClick={() => void closeGoal(task)}>
                  结束长期目标
                </button>
              </>
            )}
          </>
        )}

        {artifact && (
          <section className="chat-task-result">
            <h3>私有成果</h3>
            <small className="hint">
              {[artifact.author_name, `任务 r${artifact.task_revision}`, `成果 v${artifact.edition}`]
                .filter(Boolean)
                .join(" · ")}
            </small>
            <MarkdownText allowLinks text={artifact.body} />
            <button
              type="button"
              onClick={() => {
                setEditorText(artifact.body);
                setSubView({
                  kind: "editor",
                  mode: "edit_artifact",
                  title: "私有成果正文",
                  initial: artifact.body,
                  limit: 64000,
                });
              }}
            >
              编辑成果
            </button>
            <button
              type="button"
              onClick={() =>
                downloadText(
                  `工坊成果-${task.id}-r${artifact.task_revision}-v${artifact.edition}.md`,
                  artifact.body,
                  "text/markdown;charset=utf-8"
                )
              }
            >
              下载 Markdown
            </button>
            {(artifact.edition > 1 || artifact.task_revision > 1) && (
              <details
                onToggle={(event) => {
                  if ((event.currentTarget as HTMLDetailsElement).open) {
                    void loadArtifactHistory(task.id);
                  }
                }}
              >
                <summary>历史成果</summary>
                <div>
                  {historyError ? (
                    <p className="hint">{historyError}</p>
                  ) : historyEditions ? (
                    (() => {
                      const older = historyEditions.filter(
                        (e) =>
                          e.task_revision !== artifact.task_revision || e.edition !== artifact.edition
                      );
                      if (!older.length) return <p className="hint">当前为首份成果。</p>;
                      return older.map((entry) => (
                        <HistoricalEditionItem
                          key={`${entry.task_revision}-${entry.edition}`}
                          taskId={task.id}
                          entry={entry}
                          onOpened={onArtifactOpened}
                        />
                      ));
                    })()
                  ) : null}
                </div>
              </details>
            )}
          </section>
        )}

        <TaskBrowserPanel
          task={task}
          isOpen={false}
          onOpen={() => setSubView({ kind: "browser_panel" })}
          onClose={() => setSubView({ kind: "main" })}
        />

        {approvalStep && approval && (
          approval.kind === "dynamic_publication" ? (
            <DynamicPublicationApproval
              taskId={task.id}
              stepId={approvalStep.id}
              revision={task.revision}
              approval={approval}
              onUpdated={(updated) => {
                setTaskDetail(updated);
                onChatChanged();
              }}
            />
          ) : (
            <>
              <p>
                {`${approval.target || ""} · ${
                  approval.text || String(approvalStep.arguments?.action || "")
                }`}
                {(approvalStep.tool === "publish_dynamic_draft" ||
                  approvalStep.tool === "submit_selected_reply") &&
                  approval.payload && (
                    <>
                      {approval.payload.mentions?.length ? (
                        <MentionLinks mentions={approval.payload.mentions} />
                      ) : null}
                      {(() => {
                        const ref = {
                          asset_id: approval.channel && approval.payload.meme_id,
                          artifact_id: approval.payload.artifact_id,
                        };
                        const url = illustrationUrl(
                          approval.channel || "dynamics",
                          approval.draft_id || approval.post_id || "",
                          ref
                        );
                        return url ? (
                          <img
                            className="dynamic-comment-card"
                            src={studioPath(url)}
                            alt="本次审批绑定的图片"
                          />
                        ) : null;
                      })()}
                      {approval.payload.message_kind === "text_image" && (
                        <small>先发图片，再发正文，各有一条平台回执。</small>
                      )}
                    </>
                  )}
                {approvalStep.tool === "publish_dynamic_draft" && (
                  <>
                    {(approval.sources || []).map((src, sIdx) => (
                      <span key={`app-src-${sIdx}`}>
                        <SafeExternalAnchor url={src.source_url} label="查看原始来源" />
                        {(src.public_sources || []).map((origin, oIdx) => (
                          <SafeExternalAnchor key={`orig-${oIdx}`} url={origin.url} />
                        ))}
                      </span>
                    ))}
                    {(() => {
                      const p = approval.payload || {};
                      const previews = p.meme_id
                        ? [`/api/v1/persona/memes/${encodeURIComponent(p.meme_id)}/preview`]
                        : p.comment_card_sha256
                          ? Array.from(
                              { length: p.comment_card_page_sha256?.length || 1 },
                              (_, idx) =>
                                `/api/v1/operations/dynamics/posts/${encodeURIComponent(
                                  String(approval.post_id || "")
                                )}/comment-card?page=${idx + 1}`
                            )
                          : [];
                      return previews.map((url, pIdx) => (
                        <img
                          key={`prev-${pIdx}`}
                          className="dynamic-comment-card"
                          src={studioPath(url)}
                          alt="将随正文一起发布的配图"
                        />
                      ));
                    })()}
                  </>
                )}
              </p>
              <button
                type="button"
                disabled={formBusy}
                onClick={() =>
                  void applyTaskControl(task, { action: "approve", step_id: approvalStep.id })
                }
              >
                {approvalStep.tool === "publish_dynamic_draft"
                  ? "确认公开发布这条动态"
                  : approvalStep.tool === "live_handoff"
                    ? "确认交接给本场直播"
                    : "确认执行这个动作"}
              </button>
            </>
          )
        )}
      </>
    );
  };

  return (
    <aside
      id="chat-activity"
      className="chat-activity"
      aria-labelledby="chat-activity-title"
      hidden={!open}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose(true);
      }}
    >
      <header className="chat-activity-header">
        <h2 id="chat-activity-title">工坊面板</h2>
        <a href="#accounts" id="chat-account-badge" className="account-badge" aria-label="B站账号">
          <span className="account-avatar">
            {profile?.avatar_url ? (
              <img
                src={studioPath("/api/v1/operations/account/bilibili/avatar")}
                alt=""
                referrerPolicy="no-referrer"
              />
            ) : (
              <span aria-hidden="true">{profile?.name ? Array.from(profile.name)[0] : "B站"}</span>
            )}
          </span>
          <span className="account-badge-name">{accountName}</span>
          {accountState && (
            <span className="account-state" data-state={accountInfo?.state || "unknown"}>
              {accountState}
            </span>
          )}
        </a>
        <button
          ref={closeBtnRef}
          id="chat-activity-close"
          className="operator-chat-icon"
          type="button"
          aria-label="关闭面板"
          onClick={() => onClose(true)}
        >
          ×
        </button>
      </header>
      <nav className="chat-panel-sections" aria-label="工坊面板">
        <button
          type="button"
          id="chat-panel-permissions"
          aria-pressed={section === "permissions"}
          onClick={() => onSelectView(null, "permissions")}
        >
          权限
        </button>
        <button
          type="button"
          id="chat-panel-activity"
          aria-pressed={section === "activity"}
          onClick={() => onSelectView(null, "activity")}
        >
          活动
        </button>
        <button
          type="button"
          id="chat-panel-results"
          aria-pressed={section === "results"}
          onClick={() => onSelectView(null, "results")}
        >
          成果
        </button>
      </nav>
      <section id="chat-panel-permission-summary" hidden={section !== "permissions"}>
        <CapabilityStrip status={automationStatus} />
        <a href="#operations-permission-matrix" className="button" aria-label="角色权限">
          管理角色权限
        </a>
      </section>
      <p id="chat-activity-status" className="hint" role="status" hidden={!statusText}>
        {statusText}
      </p>
      <div id="chat-activity-content">{renderContent()}</div>
    </aside>
  );
}
