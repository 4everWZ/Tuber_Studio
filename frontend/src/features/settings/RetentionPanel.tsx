import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { dateTime, display } from "@/shared/lib/display";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import styles from "./OverviewPage.module.css";
import {
  buildRetentionRequest,
  type RetentionCleanupResponse,
  type RetentionPolicyResponse,
  type RetentionPreviewResponse,
} from "./types";

const PREVIEW_COUNT_LABELS: Record<string, string> = {
  context_windows: "上下文窗口",
  grounded_memory: "来源检索索引",
  conversations: "对话记录",
  jobs: "直播任务",
  events: "直播事件",
  sessions: "直播会话",
  dynamic_memory_summaries: "运营记忆摘要",
};

interface ActivePreviewState {
  signature: string;
  cutoff: string;
  counts: Record<string, number>;
}

export function RetentionPanel({ className }: { className?: string }) {
  const [policy, setPolicy] = useState<RetentionPolicyResponse | null>(null);
  const [daysInput, setDaysInput] = useState("30");
  const [neverClean, setNeverClean] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [pendingKind, setPendingKind] = useState<"save" | "preview" | "cleanup" | null>(null);
  const [policyState, setPolicyState] = useState<{ text: string; error: boolean }>({
    text: "正在读取服务留存策略…",
    error: false,
  });

  const [beforeDate, setBeforeDate] = useState("");
  const [preview, setPreview] = useState<ActivePreviewState | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [cleanupState, setCleanupState] = useState<{ text: string; error: boolean }>({
    text: "",
    error: false,
  });

  const dirtyRef = useRef(dirty);
  const pendingRef = useRef(pendingKind);
  const beforeDateRef = useRef(beforeDate);
  const policyRef = useRef(policy);
  dirtyRef.current = dirty;
  pendingRef.current = pendingKind;
  beforeDateRef.current = beforeDate;
  policyRef.current = policy;

  useUnsavedGuard("retention-panel", dirty);

  const loadRetention = useCallback(async () => {
    try {
      const next = await apiGet<"/api/v1/system/retention", RetentionPolicyResponse>(
        "/api/v1/system/retention"
      );
      policyRef.current = next;
      setPolicy(next);
      if (!dirtyRef.current) {
        setNeverClean(next.days == null);
        setDaysInput(String(next.days ?? 30));
      }
      setPolicyState({
        text: dirtyRef.current ? "有未保存修改，请保存后预览。" : "显示服务已保存策略。",
        error: false,
      });
    } catch (error) {
      setPolicyState({ text: `留存策略读取失败：${errorMessage(error)}`, error: true });
    }
  }, []);

  useEffect(() => {
    void loadRetention();
  }, [loadRetention]);

  function invalidatePreview(message = "请先预览待清理范围，再明确确认。", isError = false) {
    setPreview(null);
    setConfirmed(false);
    setCleanupState({ text: message, error: isError });
  }

  async function runRetentionAction(
    kind: "save" | "preview" | "cleanup",
    action: () => Promise<void>
  ) {
    if (pendingRef.current) return;
    pendingRef.current = kind;
    setPendingKind(kind);
    try {
      await action();
    } catch (error) {
      const msg = errorMessage(error);
      if (kind !== "save") {
        invalidatePreview(`${msg} 请重新预览后再清理。`, true);
      } else {
        setPolicyState({ text: `策略保存失败：${msg}`, error: true });
      }
      emitNotice(msg, true);
    } finally {
      pendingRef.current = null;
      setPendingKind(null);
    }
  }

  async function handleSavePolicy(event: FormEvent) {
    event.preventDefault();
    await runRetentionAction("save", async () => {
      const days = neverClean ? null : Number(daysInput);
      await apiPut<"/api/v1/system/retention", RetentionPolicyResponse, { days: number | null }>(
        "/api/v1/system/retention",
        { body: { days } }
      );
      dirtyRef.current = false;
      setDirty(false);
      await loadRetention();
      emitNotice("留存策略已保存。");
    });
  }

  async function handlePreviewClick() {
    await runRetentionAction("preview", async () => {
      const payload = buildRetentionRequest(policyRef.current, dirtyRef.current, beforeDateRef.current);
      const signature = JSON.stringify(payload);
      invalidatePreview("正在统计待清理记录；尚未删除…", false);
      const result = await apiPost<
        "/api/v1/system/retention/preview",
        RetentionPreviewResponse,
        { before?: string }
      >("/api/v1/system/retention/preview", { body: payload });

      const currentSig = JSON.stringify(
        buildRetentionRequest(policyRef.current, dirtyRef.current, beforeDateRef.current)
      );
      if (currentSig !== signature) return;
      if (result.cutoff == null) {
        throw new Error("服务未返回清理截止时间，请指定日期后重新预览。");
      }
      setPreview({
        signature,
        cutoff: result.cutoff,
        counts: result.counts || {},
      });
      setCleanupState({ text: "预览完成，尚未删除。请核对数量和范围后确认。", error: false });
    });
  }

  async function handleCleanupClick() {
    await runRetentionAction("cleanup", async () => {
      const currentSig = JSON.stringify(
        buildRetentionRequest(policyRef.current, dirtyRef.current, beforeDateRef.current)
      );
      if (!preview || !confirmed || currentSig !== preview.signature) {
        throw new Error("请先预览当前范围并明确确认清理。");
      }
      const result = await apiPost<
        "/api/v1/system/retention/cleanup",
        RetentionCleanupResponse,
        { before: string; confirm: boolean }
      >("/api/v1/system/retention/cleanup", {
        body: { before: preview.cutoff, confirm: true },
      });
      invalidatePreview(`清理已完成，实际结果：${JSON.stringify(result.counts || result)}`, false);
      await loadRetention();
    });
  }

  const loaded = Boolean(policy);
  const busy = pendingKind !== null;
  const lastCompleteUtcDay = (() => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - 1);
    return d.toISOString().slice(0, 10);
  })();

  return (
    <article
      className={["win-group-box", className].filter(Boolean).join(" ")}
      id="retention-panel"
    >
      <div className="panel-heading">
        <div className="win-group-title" style={{ margin: 0 }}>
          对话与流水数据留存策略
        </div>
        <span className="hint">按已配置策略自动在后台执行</span>
      </div>

      <form id="retention-form" style={{ marginTop: 10 }} onSubmit={(event) => void handleSavePolicy(event)}>
        <div className="retention-policy win-checks-bar">
          <label>
            保留天数
            <input
              id="retention-days"
              className={styles.retentionDaysInput}
              type="number"
              min={1}
              max={36500}
              step={1}
              required={!neverClean}
              disabled={!loaded || busy || neverClean}
              value={daysInput}
              onChange={(event) => {
                dirtyRef.current = true;
                setDirty(true);
                setDaysInput(event.target.value);
              }}
            />
          </label>
          <label className="check-field">
            <input
              id="retention-never"
              type="checkbox"
              disabled={!loaded || busy}
              checked={neverClean}
              onChange={(event) => {
                dirtyRef.current = true;
                setDirty(true);
                setNeverClean(event.target.checked);
              }}
            />
            {" 永不自动清理历史数据"}
          </label>
          <button
            id="retention-save"
            type="submit"
            className="win-btn"
            disabled={!loaded || busy || !dirty}
            aria-busy={pendingKind === "save" ? "true" : undefined}
          >
            保存留存策略
          </button>
        </div>
        <p
          id="retention-policy-state"
          className={policyState.error ? "hint error-text" : "hint"}
          role="status"
        >
          {policyState.text}
        </p>
      </form>

      <dl id="retention-status" className="description-list" style={{ marginTop: 10 }}>
        {policy && (
          <>
            <dt>已保存策略</dt>
            <dd>{policy.days == null ? "永不自动清理" : `保留 ${policy.days} 天`}</dd>
            <dt>自动清理</dt>
            <dd>{policy.enabled ? "已启用" : "已关闭"}</dd>
            <dt>最近执行（UTC）</dt>
            <dd>{dateTime(policy.last_run)}</dd>
            <dt>下次执行（UTC）</dt>
            <dd>{dateTime(policy.next_run)}</dd>
          </>
        )}
      </dl>

      <pre id="retention-last-result" className="code-small" hidden={policy?.last_result == null}>
        {policy?.last_result == null ? "" : JSON.stringify(policy.last_result, null, 2)}
      </pre>

      <div className="win-subgroup-box" style={{ marginTop: 16 }}>
        <div className="win-subgroup-title">手动预览与安全清理</div>
        <p className="hint">
          清理会永久删除到期的对话上下文、检索索引与直播记录。原始评论、私信与审核草稿予以保留。
        </p>
        <div className="actions" style={{ marginTop: 8 }}>
          <label className={styles.inlineField}>
            截止日期（可选）
            <input
              id="retention-before"
              type="date"
              max={lastCompleteUtcDay}
              aria-describedby="retention-date-help"
              disabled={!loaded || busy}
              value={beforeDate}
              onChange={(event) => {
                beforeDateRef.current = event.target.value;
                setBeforeDate(event.target.value);
                invalidatePreview();
              }}
            />
          </label>
          <button
            id="retention-preview"
            type="button"
            className="win-btn"
            disabled={!loaded || busy || dirty}
            aria-busy={pendingKind === "preview" ? "true" : undefined}
            onClick={() => void handlePreviewClick()}
          >
            预览待清理范围
          </button>
        </div>
        <p id="retention-date-help" className="hint" />

        <div id="retention-preview-panel" className={styles.retentionPreview} hidden={!preview}>
          <p id="retention-preview-cutoff" className="hint">
            {preview ? `删除时间早于 ${preview.cutoff} 的适用记录（UTC，边界不包含）。` : ""}
          </p>
          <dl id="retention-preview-counts" className="description-list">
            {preview &&
              Object.entries(preview.counts).map(([key, count]) => (
                <div key={key} style={{ display: "contents" }}>
                  <dt>{PREVIEW_COUNT_LABELS[key] || key}</dt>
                  <dd>{display(count)}</dd>
                </div>
              ))}
          </dl>
          <label className="check-field" style={{ marginTop: 8 }}>
            <input
              id="retention-confirm"
              type="checkbox"
              disabled={busy || !preview}
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            {" 我已核对以上清理范围与数量，确认永久执行清理"}
          </label>
          <button
            id="retention-cleanup"
            className="danger win-btn"
            type="button"
            style={{ marginTop: 8 }}
            disabled={busy || !preview || !confirmed}
            aria-busy={pendingKind === "cleanup" ? "true" : undefined}
            onClick={() => void handleCleanupClick()}
          >
            确认清理预览记录
          </button>
        </div>
        <p
          id="retention-cleanup-state"
          className={cleanupState.error ? "hint error-text" : "hint"}
          role="status"
        >
          {cleanupState.text}
        </p>
      </div>
    </article>
  );
}
