import { useEffect, useState } from "react";
import { apiGet, apiPost } from "@/shared/api/client";
import { useAppSession, type ContextStatusSnapshot } from "@/shared/hooks/useAppSession";
import { label, list, occupancyText, tokenCount } from "@/shared/lib/display";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { CachePanel } from "./CachePanel";
import styles from "./OverviewPage.module.css";
import { RetentionPanel } from "./RetentionPanel";
import type { VoiceOptionSummary } from "./types";

export function OverviewPage() {
  const { status, settings, refreshStatus } = useAppSession();
  const [voices, setVoices] = useState<VoiceOptionSummary[]>([]);
  const [videoId, setVideoId] = useState("");
  const [threadId, setThreadId] = useState("");
  const [threadContext, setThreadContext] = useState<ContextStatusSnapshot | null>(null);
  const [compactionResult, setCompactionResult] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    apiGet<"/api/v1/persona/voices", VoiceOptionSummary[]>("/api/v1/persona/voices")
      .then((data) => {
        if (mounted) setVoices(list<VoiceOptionSummary>(data));
      })
      .catch(() => {});
    return () => {
      mounted = false;
    };
  }, []);

  const voiceId = status?.character?.voice_profile || settings?.voice_id || undefined;
  const speechName = voices.find((v) => v.id === voiceId)?.name || voiceId || "待确认";
  const activeContext = threadContext || status?.context || null;

  async function compactContext(scope: "live" | "comments") {
    if (busyAction) return;
    const vid = videoId.trim();
    const tid = threadId.trim();
    if (scope === "comments" && (!vid || !tid)) {
      const msg = "请先选择要压缩的视频与单个楼ID；不同楼上下文不会合并。";
      setCompactionResult(`压缩失败：${msg}`);
      emitNotice(msg, true);
      return;
    }

    setBusyAction(scope === "live" ? "compact-live" : "compact-comments");
    setCompactionResult("正在使用当前文本模型压缩上下文…");
    emitNotice("上下文压缩已开始，请等待当前文本模型返回。");
    try {
      const result = await apiPost<
        "/api/v1/persona/context/compact",
        { message?: string; detail?: string; compacted?: boolean },
        { scope: "live" | "comments"; video_id?: string; thread_id?: string }
      >("/api/v1/persona/context/compact", {
        body: scope === "comments" ? { scope: "comments", video_id: vid, thread_id: tid } : { scope: "live" },
      });
      const message =
        result.message ||
        result.detail ||
        (result.compacted === false
          ? "没有可压缩的历史。"
          : "压缩请求已完成，请查看实际上下文占用与压缩次数。");
      setCompactionResult(message);
      emitNotice(message);
      await refreshStatus();
      if (scope === "comments") {
        const refreshed = await apiGet<"/api/v1/persona/context/status", ContextStatusSnapshot>(
          "/api/v1/persona/context/status",
          { query: { video_id: vid, thread_id: tid } }
        ).catch(() => null);
        if (refreshed) setThreadContext(refreshed);
      }
    } catch (error) {
      const msg = errorMessage(error);
      setCompactionResult(`压缩失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function inspectThreadContext() {
    if (busyAction) return;
    const vid = videoId.trim();
    const tid = threadId.trim();
    setBusyAction("inspect-context");
    try {
      const snapshot = await apiGet<"/api/v1/persona/context/status", ContextStatusSnapshot>(
        "/api/v1/persona/context/status",
        { query: vid || tid ? { video_id: vid, thread_id: tid } : undefined }
      );
      setThreadContext(snapshot);
      setCompactionResult(vid && tid ? `已读取楼 ${vid} / ${tid} 的上下文状态。` : "已读取当前活动上下文状态。");
    } catch (error) {
      const msg = errorMessage(error);
      setCompactionResult(`读取失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function rebuildThreadContext() {
    if (busyAction) return;
    const vid = videoId.trim();
    const tid = threadId.trim();
    if (!vid || !tid) {
      const msg = "请先填写评论视频 ID 与单个楼ID，再重建该楼上下文。";
      setCompactionResult(`重建失败：${msg}`);
      emitNotice(msg, true);
      return;
    }
    setBusyAction("rebuild-context");
    setCompactionResult(`正在使用本地现存原文重建 ${vid} / ${tid}…`);
    try {
      const result = await apiPost<
        "/api/v1/persona/context/rebuild",
        { message?: string; detail?: string; rebuilt?: boolean; prefilled?: boolean; input_tokens?: number },
        { scope: string; video_id: string; thread_id: string }
      >("/api/v1/persona/context/rebuild", {
        body: { scope: "comments", video_id: vid, thread_id: tid },
      });
      let msg =
        result.message ||
        result.detail ||
        (result.rebuilt === false
          ? "本次未重建上下文，请检查该楼现存原文。"
          : `楼 ${vid} / ${tid} 上下文已重建，请查看实际占用。`);
      if (result.prefilled === false) {
        msg += " 诊断：未执行真实模型 prefill，不能据此认定模型已预热。";
      } else if (result.prefilled === true) {
        msg += ` 模型 prefill 已完成${result.input_tokens != null ? `，输入 ${result.input_tokens} tokens` : ""}。`;
      }
      setCompactionResult(msg);
      emitNotice(msg);
      const refreshed = await apiGet<"/api/v1/persona/context/status", ContextStatusSnapshot>(
        "/api/v1/persona/context/status",
        { query: { video_id: vid, thread_id: tid } }
      ).catch(() => null);
      if (refreshed) setThreadContext(refreshed);
    } catch (error) {
      const msg = errorMessage(error);
      setCompactionResult(`重建失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <section data-section="overview" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>存储、上下文与数据维护</h1>
      </div>

      <div className="win-group-box">
        <div className="win-group-title">运行时状态与组件健康度</div>
        <dl className="workspace-overview win-metrics-grid">
          <div className="metric-card">
            <dt>直播会话</dt>
            <dd id="overview-live-state">{status ? label(status.state) : "待确认"}</dd>
          </div>
          <div className="metric-card">
            <dt>待播队列</dt>
            <dd id="overview-queue">{status?.queue_length ?? "—"}</dd>
          </div>
          <div className="metric-card">
            <dt>OBS 播放器</dt>
            <dd id="overview-player">
              {status ? (status.overlay_connected ? "已连接" : "未连接") : "待确认"}
            </dd>
          </div>
          <div className="metric-card">
            <dt>角色音色</dt>
            <dd id="overview-speech">{speechName}</dd>
          </div>
        </dl>
      </div>

      <CachePanel className={styles.groupSpacer} />

      <article className={`win-group-box ${styles.groupSpacer}`}>
        <div className="win-group-title">对话上下文管理与压缩诊断</div>
        <dl id="context-details" className="description-list win-desc-grid" style={{ marginTop: 10 }}>
          {activeContext && (
            <>
              <dt>活动范围</dt>
              <dd>{activeContext.scope || "暂无活动历史"}</dd>
              <dt>配置目标</dt>
              <dd>{tokenCount(activeContext.configured_tokens)}</dd>
              <dt>实际服务容量</dt>
              <dd>{tokenCount(activeContext.server_tokens)}</dd>
              <dt>服务报告上限</dt>
              <dd>{tokenCount(activeContext.native_tokens)}</dd>
              <dt>完整输入占用</dt>
              <dd>{`${tokenCount(activeContext.active_tokens ?? 0)}（${occupancyText(activeContext)}）`}</dd>
              <dt>累计已安装压缩轮数</dt>
              <dd>{activeContext.compact_count ?? 0}</dd>
              <dt>最近操作尝试轮数</dt>
              <dd>{activeContext.compaction_passes_last ?? 0}</dd>
              <dt>最近摘要预算</dt>
              <dd>
                {(activeContext.summary_budgets_last || []).map((v) => `${v} tokens`).join(" / ") ||
                  "尚未压缩"}
              </dd>
              <dt>二轮触发占用</dt>
              <dd>{`${activeContext.secondary_threshold_percent ?? 40}%`}</dd>
              <dt>最近压缩提示</dt>
              <dd>{activeContext.last_compaction_warning || "无"}</dd>
              <dt>最近压缩错误</dt>
              <dd>{activeContext.last_compaction_error || "无"}</dd>
            </>
          )}
        </dl>
        <p id="context-profile-note" className="hint">
          {activeContext?.error || ""}
        </p>
        <div className="actions" style={{ marginTop: 12 }}>
          <button
            id="compact-live"
            type="button"
            className="win-btn"
            disabled={busyAction !== null}
            aria-busy={busyAction === "compact-live" ? "true" : undefined}
            onClick={() => void compactContext("live")}
          >
            压缩直播上下文
          </button>
          <label className={styles.inlineField}>
            评论视频 ID
            <input
              id="compact-video-id"
              placeholder="BV…"
              value={videoId}
              onChange={(event) => setVideoId(event.target.value)}
            />
          </label>
          <label className={styles.inlineField}>
            楼ID
            <input
              id="compact-thread-id"
              placeholder="楼ID"
              value={threadId}
              onChange={(event) => setThreadId(event.target.value)}
            />
          </label>
          <button
            id="compact-comments-settings"
            type="button"
            className="win-btn"
            disabled={busyAction !== null}
            aria-busy={busyAction === "compact-comments" ? "true" : undefined}
            onClick={() => void compactContext("comments")}
          >
            压缩评论上下文
          </button>
          <button
            id="inspect-context-status"
            type="button"
            className="win-btn"
            disabled={busyAction !== null}
            onClick={() => void inspectThreadContext()}
          >
            查询上下文状态
          </button>
          <button
            id="rebuild-context-settings"
            type="button"
            className="win-btn"
            disabled={busyAction !== null}
            onClick={() => void rebuildThreadContext()}
          >
            重建评论上下文
          </button>
        </div>
        <p id="compaction-result" className="hint" role="status">
          {compactionResult}
        </p>
      </article>

      <RetentionPanel className={styles.groupSpacer} />
    </section>
  );
}
