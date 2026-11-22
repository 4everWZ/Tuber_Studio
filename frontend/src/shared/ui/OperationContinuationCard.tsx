import { useEffect, useState } from "react";
import { apiPost } from "../api/client";
import { errorMessage } from "../lib/errors";
import { emitNotice } from "../lib/errors";

export interface ContinuationFeedback {
  kind?: string;
  code?: string | number | null;
  message?: string | null;
}

export interface ContinuationRevision {
  version?: number | string;
  phase?: string;
  action?: string;
  status?: string;
  output?: unknown;
  feedback?: ContinuationFeedback | string | null;
}

export interface ContinuationRecord {
  id: string;
  phase?: string;
  state: string;
  can_resume?: boolean;
  private_handoff?: boolean;
  output_visibility?: string;
  resume_target?: {
    channel?: string;
    local_id?: string;
  } | null;
  recoveries?: number;
  max_recoveries?: number;
  total_recoveries?: number;
  feedback?: ContinuationFeedback | string | null;
  trigger_feedback?: ContinuationFeedback | string | null;
  budget?: {
    model_calls?: number;
    input_tokens?: number;
    output_tokens?: number;
  } | null;
  limits?: {
    max_model_calls?: number;
    max_input_tokens?: number;
    max_output_tokens?: number;
  } | null;
  deadline?: number | null;
  original_output?: unknown;
  current_output?: unknown;
  revisions?: ContinuationRevision[];
}

export type ContinuationChannel = "comments" | "messages" | "dynamics" | "browsing";

export interface OperationContinuationCardProps {
  value: ContinuationRecord | null | undefined;
  channel: ContinuationChannel;
  localId: string | number | null | undefined;
  dirty?: boolean;
  entityStatus?: string | null;
  privateContext?: boolean;
  displayChannel?: string;
  onRefresh?: () => Promise<void> | void;
  onUpdated?: (continuation: ContinuationRecord) => void;
  onNotify?: (message: string, isError?: boolean) => void;
}

const STATE_LABELS: Record<string, string> = {
  pending: "等待处理",
  deciding: "Agent 处理中",
  awaiting_review: "等待审核",
  invalid: "生成无效",
  deferred: "本次已暂缓",
  waiting: "等待下一步",
  needs_material: "等待补充材料",
  checkpoint: "进度已保存",
  needs_reconciliation: "发送结果待核对",
  completed: "处理完成",
  abandoned: "已暂缓",
  cancelled: "已取消",
};

const FEEDBACK_KINDS: Record<string, string> = {
  platform_error: "平台反馈",
  style_diagnostic: "表达建议",
  resource_limit: "额度反馈",
  round_budget: "轮次额度",
  model_error: "模型反馈",
  platform_uncertain: "平台结果待核对",
};

const RECONCILIATION_LABELS: Record<string, string> = {
  visible: "已找到匹配的发送记录",
  not_visible: "已找到匹配记录，当前不可见",
  not_found: "本次读取未找到匹配记录",
  ambiguous: "存在多条候选，仍需核对",
  check_failed: "本次平台读取未成功",
  unsupported: "当前渠道尚不支持读回核对",
};

const REVISION_PHASES: Record<string, string> = {
  feedback: "收到反馈",
  decision: "Agent 判断",
  apply: "应用修订",
  checkpoint: "保存进度",
  resume: "继续处理",
  review: "审核",
  publish: "平台提交",
  reconciliation: "平台结果核对",
};

const REVISION_ACTIONS: Record<string, string> = {
  revise: "修订正文",
  defer: "稍后处理",
  abandon: "暂缓",
  wait: "等待",
  resume: "继续",
  received: "已收到",
  completed: "已完成",
  pending: "等待处理",
  failed: "失败",
  cancelled: "已取消",
  ...RECONCILIATION_LABELS,
};

function continuationStateLabel(state: string): string {
  return STATE_LABELS[state] || state || "状态未返回";
}

function extractBodyText(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  for (const key of ["text", "spoken_text", "content", "body"]) {
    if (typeof record[key] === "string") return record[key] as string;
  }
  return JSON.stringify(value, null, 2);
}

function formatContinuationFeedback(value: ContinuationFeedback | string | null | undefined): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const kind = (value.kind && FEEDBACK_KINDS[value.kind]) || value.kind;
  return [kind, value.code == null ? null : `错误码 ${value.code}`, value.message]
    .filter((item) => item != null && item !== "")
    .map(String)
    .join(" · ");
}

export function OperationContinuationCard({
  value,
  channel,
  localId,
  dirty = false,
  entityStatus,
  privateContext = false,
  displayChannel = channel,
  onRefresh,
  onUpdated,
  onNotify = (msg, isError) => emitNotice(msg, Boolean(isError)),
}: OperationContinuationCardProps) {
  const [overrideValue, setOverrideValue] = useState<{ key: string; record: ContinuationRecord } | null>(null);
  const [pending, setPending] = useState(false);
  const [resumeError, setResumeError] = useState<{ key: string; state: string; message: string } | null>(null);

  const effective = value && overrideValue?.record.id === value.id ? overrideValue.record : value;

  useEffect(() => {
    setOverrideValue(null);
  }, [value]);

  if (!effective) {
    return null;
  }

  const currentRecord = effective;

  const privateOutput =
    currentRecord.private_handoff === true ||
    ["private", "unresolved"].includes(currentRecord.output_visibility || "");
  const generation =
    currentRecord.phase === "generation" &&
    currentRecord.state !== "needs_reconciliation" &&
    entityStatus !== "publish_uncertain";
  const target = generation ? currentRecord.resume_target : null;
  const targetChannelAllowed =
    target &&
    (target.channel === channel ||
      (privateContext &&
        displayChannel === "messages" &&
        privateOutput &&
        ["comments", "browsing"].includes(target.channel || "")));
  const validTarget =
    Boolean(targetChannelAllowed) &&
    typeof target?.local_id === "string" &&
    target.local_id.startsWith("generation:") &&
    target.local_id.length > "generation:".length;

  let resolvedChannel: ContinuationChannel = channel;
  let resolvedLocalId: string | number | null | undefined = localId;
  if (generation) {
    resolvedLocalId = validTarget ? target!.local_id : null;
    if (
      validTarget &&
      target?.channel &&
      (["comments", "messages", "dynamics", "browsing"] as const).includes(
        target.channel as ContinuationChannel
      )
    ) {
      resolvedChannel = target.channel as ContinuationChannel;
    }
  }

  const bindingKey = `${resolvedChannel}:${resolvedLocalId}:${currentRecord.id}`;
  const publicHandoff = privateOutput && displayChannel !== "messages";
  const awaitingRound =
    currentRecord.state === "checkpoint" &&
    typeof currentRecord.feedback === "object" &&
    currentRecord.feedback?.kind === "round_budget";

  const stateBadgeText = publicHandoff
    ? "生成原稿在私信工作台"
    : awaitingRound
      ? "等待下一轮"
      : continuationStateLabel(currentRecord.state);

  const summaryText =
    publicHandoff || generation
      ? ""
      : `本轮恢复 ${currentRecord.recoveries ?? "—"} / ${currentRecord.max_recoveries ?? "—"} 次 · 累计 ${currentRecord.total_recoveries ?? currentRecord.recoveries ?? "—"} 次`;

  const feedbackTextRaw = formatContinuationFeedback(currentRecord.feedback);
  const triggerTextRaw = formatContinuationFeedback(currentRecord.trigger_feedback);
  const combinedFeedback =
    feedbackTextRaw === triggerTextRaw || !triggerTextRaw
      ? feedbackTextRaw
      : `${triggerTextRaw}\n当前：${feedbackTextRaw || continuationStateLabel(currentRecord.state)}`;
  const feedbackText = publicHandoff
    ? ""
    : currentRecord.state === "completed" && combinedFeedback
      ? `此前反馈：${combinedFeedback}`
      : combinedFeedback;

  const revisions = Array.isArray(currentRecord.revisions) ? currentRecord.revisions : [];
  const lastReconcile = [...revisions].reverse().find((row) => row.phase === "reconciliation");
  const reconciliationText =
    !publicHandoff && lastReconcile
      ? `平台核对：${RECONCILIATION_LABELS[lastReconcile.status || ""] || lastReconcile.status || ""}`
      : "";

  const budget = currentRecord.budget || {};
  const limits = currentRecord.limits || {};
  const budgetParts = (
    [
      ["调用", "model_calls"],
      ["输入 tokens", "input_tokens"],
      ["输出 tokens", "output_tokens"],
    ] as const
  )
    .filter(([, key]) => budget[key] != null)
    .map(([name, key]) => {
      const limitKey = `max_${key}` as const;
      const limit = limits[limitKey];
      const suffix = limit != null ? ` / ${limit === 0 ? "不限" : limit}` : "";
      return `${name} ${budget[key]}${suffix}`;
    });
  if (typeof currentRecord.deadline === "number" && Number.isFinite(currentRecord.deadline)) {
    budgetParts.push(`本轮期限 ${new Date(currentRecord.deadline * 1000).toLocaleString("zh-CN")}`);
  }
  const budgetText = publicHandoff ? "" : budgetParts.join(" · ");

  const reconciling =
    currentRecord.state === "needs_reconciliation" || entityStatus === "publish_uncertain";
  let noteText = "";
  if (!publicHandoff) {
    if (dirty && !reconciling) noteText = "保存修改后再继续处理。";
    if (awaitingRound && !dirty) noteText = "已保存进度，等下一轮额度恢复后继续。";
    if (generation && !dirty && currentRecord.state === "invalid") noteText = "可展开记录查看原稿。";
    if (resumeError?.key === bindingKey && resumeError.state === currentRecord.state) {
      noteText = resumeError.message;
    }
    if (generation && !validTarget && currentRecord.can_resume) {
      noteText = "刷新后可继续生成。";
    }
  }

  const showResume = !publicHandoff && Boolean(currentRecord.can_resume) && !reconciling;
  const resumeDisabled =
    pending || dirty || !currentRecord.can_resume || reconciling || resolvedLocalId == null;
  const resumeLabel = pending
    ? "正在继续…"
    : generation && ["invalid", "awaiting_review", "deferred"].includes(currentRecord.state)
      ? "重新生成"
      : "继续处理";

  const showReconcile = !publicHandoff && reconciling;
  const reconcileLabel = pending ? "正在核对…" : "核对发送结果";

  const original = extractBodyText(currentRecord.original_output);
  const current = extractBodyText(currentRecord.current_output);

  async function handleResume(checkOnly = false) {
    if (
      pending ||
      !currentRecord.id ||
      publicHandoff ||
      (checkOnly ? !reconciling : !currentRecord.can_resume || dirty || reconciling) ||
      resolvedLocalId == null
    ) {
      return;
    }
    setPending(true);
    setResumeError(null);
    try {
      const result = await apiPost<
        "/api/v1/operations/continuations/{channel}/{local_id}/resume",
        { continuation?: ContinuationRecord },
        { continuation_id: string }
      >("/api/v1/operations/continuations/{channel}/{local_id}/resume", {
        pathParams: {
          channel: resolvedChannel,
          local_id: String(resolvedLocalId),
        },
        body: { continuation_id: currentRecord.id },
      });
      if (result.continuation) {
        setOverrideValue({ key: bindingKey, record: result.continuation });
        onUpdated?.(result.continuation);
      }
      await onRefresh?.();
    } catch (error) {
      const msg = errorMessage(error);
      onNotify(msg, true);
      setResumeError({ key: bindingKey, state: currentRecord.state, message: msg });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="operation-continuation subsection">
      <div className="panel-heading">
        <h3>处理进度与修订</h3>
        <span data-continuation-state="" className="badge">
          {stateBadgeText}
        </span>
      </div>
      <p data-continuation-summary="" className="hint">
        {summaryText}
      </p>
      <p data-continuation-feedback="" className="operation-feedback">
        {feedbackText}
      </p>
      <p data-continuation-reconciliation="" className="hint" role="status">
        {reconciliationText}
      </p>
      <p data-continuation-budget="" className="hint">
        {budgetText}
      </p>
      <p data-continuation-note="" className="hint" role="status">
        {noteText}
      </p>
      <button
        type="button"
        data-continuation-resume=""
        hidden={!showResume}
        disabled={resumeDisabled}
        onClick={() => void handleResume(false)}
      >
        {resumeLabel}
      </button>
      <button
        type="button"
        data-continuation-reconcile=""
        hidden={!showReconcile}
        disabled={pending}
        onClick={() => void handleResume(true)}
      >
        {reconcileLabel}
      </button>
      <p data-continuation-private="" hidden={!publicHandoff}>
        <a href="#messages">到私信工作台查看生成进度</a>
      </p>
      <details data-continuation-history="" hidden={publicHandoff}>
        <summary>查看原稿与修订记录</summary>
        <div data-continuation-versions="">
          {original && (
            <>
              <h4>原稿</h4>
              <pre className="operation-revision-text">{original}</pre>
            </>
          )}
          {current && current !== original && (
            <>
              <h4>当前修订</h4>
              <pre className="operation-revision-text">{current}</pre>
            </>
          )}
          {revisions.map((revision, index) => {
            const phase =
              (revision.phase && REVISION_PHASES[revision.phase]) || revision.phase || "修订";
            const rawAction = revision.action || revision.status || "";
            const action = REVISION_ACTIONS[rawAction] || rawAction || "已记录";
            const text = extractBodyText(revision.output);
            const revFeedback = formatContinuationFeedback(revision.feedback);
            return (
              <details key={`${revision.version ?? index}-${index}`} className="operation-revision">
                <summary>
                  记录 {revision.version ?? index + 1} · {phase} · {action}
                </summary>
                {text && <pre className="operation-revision-text">{text}</pre>}
                {revFeedback && <p className="operation-feedback">{revFeedback}</p>}
              </details>
            );
          })}
          {!original && !current && revisions.length === 0 && (
            <p className="hint">暂无正文修订记录。</p>
          )}
        </div>
      </details>
    </div>
  );
}
