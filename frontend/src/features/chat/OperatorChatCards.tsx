import { useEffect, useState } from "react";
import { ApiError, apiGet, apiPost, studioPath } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import { illustrationStatus, illustrationUrl, type IllustrationRef } from "@/shared/lib/operationImage";
import { MarkdownText } from "@/shared/ui/MarkdownText";
import { MentionLinks, type PublicMention } from "@/shared/ui/MentionLinksAndEditor";
import { StatusPill, type StatusPillTone } from "@/shared/ui/StatusPill";
import { AssistantAnswer, MemoryUpdatedPill } from "./AssistantAnswer";
import { DynamicPublicationApproval } from "./DynamicPublicationApproval";
import { OperationControlCard } from "./OperationControlCard";
import {
  calmChatMessage,
  isPlatformLink,
  type AutomationControlStatus,
  type ChatTurn,
  type DynamicPublicationApprovalData,
  type OpenedArtifactRef,
} from "./types";

const CARD_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  waiting: "等待资源",
  running: "进行中",
  completed: "已完成",
  paused: "已暂停",
  partial: "部分完成",
  failed: "未完成",
  cancelled: "已停止",
  pending_review: "待审核",
  approved: "已审核",
  rejected: "已拒绝",
  published: "已发布",
  publish_uncertain: "待核对",
  publishing: "提交中",
  invalidated: "来源已变化",
  unavailable: "草稿已不可用",
  stopped: "已停止",
  live: "直播中",
  mock: "模拟中",
  idle: "待机",
  selected: "已读材料",
  viewed_without_comment: "已看过",
  reading: "正在读取",
};

const ACTIVE_VIDEO_STATUSES = new Set(["queued", "waiting", "running", "pending", "starting"]);

const MEMORY_GROUP_NAMES: Record<string, string> = {
  preference: "长期偏好",
  decision: "持续决定",
  experience: "可复用经验",
};

const MEMORY_VISIBILITY_LABELS: Record<string, string> = {
  owner: "仅本人",
  shared: "可跨端使用",
  live_rule: "直播规则",
};

function statusLabel(value: unknown): string {
  if (typeof value !== "string" || !value) return "待确认";
  return CARD_STATUS_LABELS[value] || value;
}

function statusTone(value: unknown): StatusPillTone {
  if (typeof value !== "string" || !value) return "neutral";
  if (["completed", "approved", "published", "live", "selected"].includes(value)) return "good";
  if (["running", "reading", "publishing", "queued", "waiting"].includes(value)) return "blue";
  if (["failed", "publish_uncertain", "pending_review", "awaiting_approval", "needs_input", "invalidated"].includes(value)) {
    return "amber";
  }
  return "neutral";
}

function formatTimecode(seconds: number): string {
  const millis = Math.round(seconds * 1000);
  const minutes = String(Math.floor(millis / 60000)).padStart(2, "0");
  const remainder = ((millis % 60000) / 1000).toFixed(3).padStart(6, "0");
  return `${minutes}:${remainder}`;
}

function videoHref(bvid: string | undefined, suffix = ""): string {
  return bvid && /^BV[0-9A-Za-z]{10}$/.test(bvid) ? `https://www.bilibili.com/video/${bvid}${suffix}` : "";
}

function ExternalLink({ text, href }: { text: string; href: string }) {
  if (href.startsWith("https://")) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {text}
      </a>
    );
  }
  return <a href={href}>{text}</a>;
}

function ActionInteractionBlock({
  interaction,
}: {
  interaction?: { kind?: string; href?: string; target?: string } | null;
}) {
  if (interaction?.kind === "login" && interaction.href === "#accounts") {
    return (
      <>
        {interaction.target && <p>{calmChatMessage(interaction.target)}</p>}
        <a href={interaction.href}>打开 B 站账号登录</a>
      </>
    );
  }
  return null;
}

interface EventSourceBlock {
  kind?: string;
  author?: string;
  uid?: string | number;
  start?: number;
  href?: string;
  text?: string;
}

interface OperationEventItem {
  title: string;
  source?: EventSourceBlock;
  material?: EventSourceBlock;
  reply?: { state?: string; text?: string; href?: string };
}

function EventContent({ item, heading }: { item: OperationEventItem; heading: "h2" | "h3" }) {
  const entries = [item.source, item.material].filter((entry): entry is EventSourceBlock => Boolean(entry));
  const typeNames: Record<string, string> = { comment: "原评论", subtitle: "字幕", visual: "画面观察" };

  return (
    <section className="operator-chat-memory">
      {heading === "h2" ? <h2>{item.title}</h2> : <h3>{item.title}</h3>}
      {entries.map((entry, idx) => {
        const type = (entry.kind && typeNames[entry.kind]) || "来源";
        const description = [
          type,
          entry.author,
          entry.uid ? `UID ${entry.uid}` : "",
          entry.start != null ? `${Math.floor(entry.start)} 秒` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        return (
          <div key={`evt-src-${idx}`}>
            {isPlatformLink(entry.href) ? (
              <ExternalLink text={description} href={entry.href} />
            ) : (
              <p className="hint">{description}</p>
            )}
            {entry.text && <blockquote className="quoted-text">{entry.text}</blockquote>}
          </div>
        );
      })}
      {item.reply?.state === "confirmed" && item.reply.text && (
        <>
          {isPlatformLink(item.reply.href) ? (
            <ExternalLink text="已送达的回复" href={item.reply.href} />
          ) : (
            <strong>已送达的回复</strong>
          )}
          <blockquote className="quoted-text">{item.reply.text}</blockquote>
        </>
      )}
    </section>
  );
}

function HistoryArtifactCard({
  turn,
  data,
  onOpenArtifact,
}: {
  turn: ChatTurn;
  data: { task_revision: number; edition: number };
  onOpenArtifact: (ref: OpenedArtifactRef) => void;
}) {
  const [loaded, setLoaded] = useState<{ author?: string; body: string } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleToggle(open: boolean) {
    if (!open || loaded || loading) return;
    setLoading(true);
    setError("");
    try {
      const edition = await apiGet<
        "/api/v1/agent/chat/turns/{request_id}/artifact",
        { author?: string; body: string }
      >("/api/v1/agent/chat/turns/{request_id}/artifact", {
        pathParams: { request_id: turn.request_id },
      });
      setLoaded(edition);
      if (turn.task_reference) {
        onOpenArtifact({
          task_id: turn.task_reference,
          task_revision: data.task_revision,
          edition: data.edition,
        });
      }
    } catch (err) {
      setError(calmChatMessage(errorMessage(err)));
    } finally {
      setLoading(false);
    }
  }

  return (
    <details onToggle={(event) => void handleToggle((event.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        查看引用原稿 · r{data.task_revision} v{data.edition}
      </summary>
      <div>
        {error ? (
          <p className="hint">{error}</p>
        ) : loaded ? (
          <>
            <small className="hint">{loaded.author === "owner" ? "你的编辑" : "角色交付"}</small>
            <MarkdownText allowLinks text={loaded.body} />
          </>
        ) : loading ? (
          <p className="hint">正在读取原稿…</p>
        ) : null}
      </div>
    </details>
  );
}

interface DynamicDraftCardData {
  id?: number;
  status?: string;
  content?: string;
  account_id?: string | number;
  illustration?: IllustrationRef | null;
  meme_id?: string | null;
  comment_card?: { page_count?: number } | null;
  image_uploads?: unknown[];
  review_key?: string;
  error?: string | null;
  receipt?: Record<string, unknown> | null;
}

function DynamicDraftCard({ initial }: { initial: DynamicDraftCardData }) {
  const [override, setOverride] = useState<DynamicDraftCardData | null>(null);
  const [draftText, setDraftText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);

  useEffect(() => {
    setOverride(null);
  }, [initial]);

  const data = override || initial;
  const editable = data.status === "pending_review" || data.status === "approved";
  const textValue = draftText !== null ? draftText : data.content || "";
  const ref: IllustrationRef | null = data.illustration || (data.meme_id ? { asset_id: data.meme_id } : null);
  const imageUrls = data.id
    ? data.comment_card
      ? Array.from(
          { length: data.comment_card.page_count || 1 },
          (_, i) => `/api/v1/operations/dynamics/posts/${data.id}/comment-card?page=${i + 1}`
        )
      : [illustrationUrl("dynamics", data.id, ref)].filter(Boolean)
    : [];

  const receiptId =
    data.receipt?.id_str ||
    data.receipt?.dynamic_id_str ||
    data.receipt?.dynamic_id ||
    data.receipt?.id;

  async function submit() {
    if (busy || !data.id) return;
    setBusy(true);
    setOutcome("正在处理…");
    try {
      const updated = await apiPost<
        "/api/v1/operations/dynamics/posts/{post_id}/publish",
        DynamicDraftCardData,
        { text?: string; review_key?: string } | undefined
      >("/api/v1/operations/dynamics/posts/{post_id}/publish", {
        pathParams: { post_id: data.id },
        ...(editable ? { body: { text: textValue.trim(), review_key: data.review_key } } : {}),
      });
      setOverride(updated);
      setDraftText(null);
      setOutcome(null);
    } catch (error) {
      setOutcome(calmChatMessage(errorMessage(error)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="chat-card-header">
        <h2>动态草稿</h2>
        <StatusPill
          label={outcome || statusLabel(data.status)}
          tone={outcome ? "blue" : statusTone(data.status)}
          role="status"
        />
      </div>
      {data.id ? (
        <>
          {editable ? (
            <textarea
              rows={4}
              aria-label="动态正文"
              readOnly={busy}
              value={textValue}
              onChange={(event) => setDraftText(event.target.value)}
            />
          ) : (
            <p>{data.content}</p>
          )}
          {data.account_id && <p className="hint">发布账号 UID {data.account_id}</p>}
          {ref && <p className="hint">{illustrationStatus(ref)}</p>}
          {imageUrls.map((url, idx) => (
            <img
              key={`${url}-${idx}`}
              className="dynamic-comment-card"
              src={studioPath(url)}
              alt="随正文发布的动态配图"
            />
          ))}
          {data.image_uploads?.length ? (
            <p className="hint">已收到 {data.image_uploads.length} 条图片上传回执</p>
          ) : null}
          <div className="actions">
            {(editable || data.status === "publish_uncertain") && (
              <button
                type="button"
                className="primary"
                disabled={busy || Boolean(ref?.job_id)}
                onClick={() => void submit()}
              >
                {editable ? "发布" : "核对回执"}
              </button>
            )}
            <a href="#dynamics">打开动态工作台</a>
          </div>
          {data.error && <p className="muted">{calmChatMessage(data.error)}</p>}
          {data.status === "published" && receiptId != null && /^\d+$/.test(String(receiptId)) && (
            <ExternalLink text="查看已发布动态" href={`https://t.bilibili.com/${receiptId}`} />
          )}
        </>
      ) : null}
    </>
  );
}

interface DynamicEditData {
  request_id: string;
  url: string;
  original: string;
  caption: string;
  status: string;
  error?: string | null;
}

function DynamicEditCard({ initial }: { initial: DynamicEditData }) {
  const [override, setOverride] = useState<DynamicEditData | null>(null);
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState("");

  useEffect(() => {
    setOverride(null);
  }, [initial]);

  const value = override || initial;
  const noteText = errMsg || calmChatMessage(value.error) || (value.status === "applied" ? "已核对原动态更新。" : "");

  async function applyEdit() {
    if (busy) return;
    setBusy(true);
    setErrMsg("");
    try {
      const updated = await apiPost<"/api/v1/operations/dynamics/edits/{request_id}/apply", DynamicEditData>(
        "/api/v1/operations/dynamics/edits/{request_id}/apply",
        { pathParams: { request_id: value.request_id } }
      );
      setOverride(updated);
    } catch (error) {
      setErrMsg(calmChatMessage(errorMessage(error)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <ExternalLink text="打开原动态" href={value.url} />
      <details>
        <summary>修改前</summary>
        <p className="quoted-text">{value.original}</p>
      </details>
      <p className="quoted-text">{value.caption}</p>
      <button
        type="button"
        disabled={busy || value.status === "applied"}
        onClick={() => void applyEdit()}
      >
        {value.status === "pending_verification" ? "核对编辑结果" : "确认修改原动态"}
      </button>
      <p className="hint" role="status">
        {noteText}
      </p>
    </>
  );
}

interface MaterialSlice {
  state?: string;
  covered_seconds?: number | null;
  samples?: number[];
  gap_seconds?: number;
  track_complete?: boolean;
  windows?: Array<{ start: number; end: number; state: string; reason?: string }>;
  gaps?: Array<{ start: number; end: number; state: string; reason?: string }>;
  reason?: string;
}

interface CoverageRange {
  start: number;
  end: number;
  subtitle?: MaterialSlice;
  audio?: MaterialSlice;
  visual?: MaterialSlice;
  highlights?: Array<{ start: number; kind: string; text: string; source_url?: string }>;
}

interface CoveragePart {
  part: number;
  title?: string;
  duration?: number | null;
  ranges: CoverageRange[];
}

interface VideoBrowsingData {
  id?: string;
  status?: string;
  phase?: string;
  yielded?: boolean;
  full_video?: boolean;
  can_resume?: boolean;
  error?: string | null;
  selection?: {
    bvid?: string;
    frame?: unknown;
    ranges?: Array<{ part: number; start: number; end: number }>;
  };
  coverage?: {
    title?: string;
    viewing?: string;
    parts?: CoveragePart[];
  };
  inspection?: {
    part: number;
    requested_time: number;
    actual_time: number;
    kind?: string;
    source_url?: string;
    text: string;
    fallback_reason?: string;
  };
  excerpt?: {
    error?: string;
    part?: number;
    start?: number;
    source_url?: string;
    text?: string;
    original_text?: string;
  };
  comments?: {
    at_end?: boolean;
    pages_read?: number;
    thread_pages_read?: number;
  };
}

function materialSummary(value: MaterialSlice | undefined): string {
  if (!value) return "未处理";
  const captions: Record<string, string> = {
    available: "已读取",
    sampled: "抽样",
    partial: "部分覆盖",
    gap: "无有效材料",
    unavailable: "不可用",
    retry: "待补读",
  };
  const state = (value.state && captions[value.state]) || value.state || "";
  const count = value.covered_seconds != null ? `${value.covered_seconds} 秒` : "";
  const samples = value.samples?.length ? `抽样时码 ${value.samples.join("、")} 秒` : "";
  const gaps = value.gap_seconds && value.gap_seconds > 0 ? `${value.gap_seconds} 秒无有效文本` : "";
  const track = value.track_complete === true ? "原轨已取全" : "";
  const windows = value.windows?.length
    ? `覆盖 ${value.windows.filter((w) => w.state === "sampled").length}/${value.windows.length} 段`
    : "";
  return [state, count, track, gaps, windows, samples, calmChatMessage(value.reason)]
    .filter(Boolean)
    .join(" · ");
}

function VideoBrowsingCard({
  turn,
  initial,
}: {
  turn: ChatTurn;
  initial: VideoBrowsingData;
}) {
  const [override, setOverride] = useState<VideoBrowsingData | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionNote, setActionNote] = useState("");
  const [showInspection, setShowInspection] = useState(false);
  const [showCoverage, setShowCoverage] = useState(false);

  useEffect(() => {
    setOverride(null);
  }, [initial]);

  const data = override || initial;
  const parts = data.coverage?.parts || [];
  const ranges = data.selection?.ranges || [];

  const titleText = data.selection?.frame
    ? data.coverage?.title || "按时码查看画面"
    : data.full_video
      ? data.coverage?.title || `指定视频取材${data.selection?.bvid ? ` · ${data.selection.bvid}` : ""}`
      : "只读浏览";

  const phases: Record<string, string> = {
    watching: "正在看视频",
    comments: "正在逛评论区",
    complete: "已完成",
  };
  const viewingMap: Record<string, string> = {
    watched: "看完",
    subtitles_only: "字幕已读完 · 画面覆盖有缺口",
    visual_only: "画面已全程扫描 · 无可用字幕",
    partial: "视频材料覆盖有缺口",
  };
  const taskState =
    data.status === "running" || data.status === "completed"
      ? (data.phase && phases[data.phase]) || statusLabel(data.status)
      : data.yielded
        ? "已保存进度 · 等待下一次调度"
        : statusLabel(data.status);
  const stateText = [taskState, data.coverage?.viewing ? viewingMap[data.coverage.viewing] : undefined]
    .filter(Boolean)
    .join(" · ");

  const selectionText = ranges.length
    ? ranges
        .slice(0, 3)
        .map((r) => `P${r.part} · ${r.start}–${r.end} 秒`)
        .join("；") + (ranges.length > 3 ? ` 等 ${ranges.length} 段` : "")
    : parts.length > 1
      ? `全部 ${parts.length} 个分 P`
      : parts.length
        ? `P${parts[0].part} · 0–${parts[0].duration ?? "?"} 秒`
        : "";

  const materialPicks = (() => {
    if (data.inspection || data.excerpt || !parts.length) return [];
    const flat = parts
      .flatMap((part) =>
        part.ranges.flatMap((range) =>
          (range.highlights || []).map((item) => ({ ...item, part: part.part }))
        )
      )
      .filter((item) => item.text && isPlatformLink(item.source_url));
    const unique = [...new Map(flat.map((item) => [item.text.trim(), item])).values()];
    return unique.length <= 3
      ? unique
      : [unique[0], unique[Math.floor(unique.length / 2)], unique[unique.length - 1]];
  })();

  const isRunning = ACTIVE_VIDEO_STATUSES.has(data.status || "");

  async function handleAction(action: "cancel" | "resume") {
    if (busy) return;
    setBusy(true);
    setActionNote(action === "cancel" ? "正在取消取材…" : "正在继续取材…");
    try {
      const updated =
        action === "cancel"
          ? await apiPost<"/api/v1/agent/chat/turns/{request_id}/cancel-video", { card: { data: VideoBrowsingData } }>(
              "/api/v1/agent/chat/turns/{request_id}/cancel-video",
              { pathParams: { request_id: turn.request_id } }
            )
          : await apiPost<"/api/v1/agent/chat/turns/{request_id}/resume-video", { card: { data: VideoBrowsingData } }>(
              "/api/v1/agent/chat/turns/{request_id}/resume-video",
              { pathParams: { request_id: turn.request_id } }
            );
      setOverride(updated.card.data);
      setActionNote("");
    } catch (error) {
      setActionNote(calmChatMessage(errorMessage(error)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="chat-card-header">
        <h2>{titleText}</h2>
        <StatusPill label={stateText} tone={statusTone(data.status)} role="status" />
      </div>
      {selectionText && <p className="hint">{selectionText}</p>}

      {data.inspection && (
        <div>
          <p>
            P{data.inspection.part} · 目标 {formatTimecode(data.inspection.requested_time)} ·{" "}
            {data.inspection.kind === "stream" ? "视频流" : "故事板"}{" "}
            {formatTimecode(data.inspection.actual_time)}
          </p>
          <details onToggle={(event) => setShowInspection((event.currentTarget as HTMLDetailsElement).open)}>
            <summary>查看取到的画面</summary>
            {showInspection && (
              <img
                src={studioPath(`/api/v1/agent/chat/turns/${encodeURIComponent(turn.request_id)}/inspection-image`)}
                alt={`P${data.inspection.part} ${formatTimecode(data.inspection.actual_time)} 的${
                  data.inspection.kind === "stream" ? "视频流" : "故事板"
                }画面`}
                style={{ maxWidth: "100%" }}
              />
            )}
          </details>
          {isPlatformLink(data.inspection.source_url) && (
            <ExternalLink text="打开原视频" href={data.inspection.source_url} />
          )}
          <blockquote className="quoted-text">{data.inspection.text}</blockquote>
          {data.inspection.fallback_reason && (
            <p className="hint">{calmChatMessage(data.inspection.fallback_reason)}</p>
          )}
        </div>
      )}

      {data.excerpt?.error && <p className="hint">{calmChatMessage(data.excerpt.error)}</p>}
      {isPlatformLink(data.excerpt?.source_url) && data.excerpt && (
        <div>
          <ExternalLink
            text={`P${data.excerpt.part ?? 1} · ${Math.floor(data.excerpt.start ?? 0)} 秒`}
            href={data.excerpt.source_url}
          />
          {data.excerpt.text && <blockquote className="quoted-text">{data.excerpt.text}</blockquote>}
          {data.excerpt.original_text && (
            <small className="hint">B 站原字幕「{data.excerpt.original_text}」，已校正</small>
          )}
        </div>
      )}

      {materialPicks.length > 0 && (
        <ul className="operator-chat-video-findings">
          {materialPicks.map((item, idx) => {
            const cleanText = item.text.replace(/\s+/g, " ").trim();
            const clipped = cleanText.length > 140 ? `${cleanText.slice(0, 140)}…` : cleanText;
            return (
              <li key={`${item.part}-${item.start}-${idx}`}>
                <ExternalLink
                  text={`P${item.part} · ${formatTimecode(item.start)}`}
                  href={item.source_url!}
                />{" "}
                · {item.kind} · {clipped}
              </li>
            );
          })}
        </ul>
      )}

      {data.comments && (
        <div>
          <p className="hint">
            {data.comments.at_end
              ? "已读到平台可见末页"
              : `已读 ${data.comments.pages_read || 0} 页根评论${
                  data.comments.thread_pages_read ? `、${data.comments.thread_pages_read} 页楼内评论` : ""
                }`}
          </p>
          {data.id && (
            <a href={`#automation/comments/${encodeURIComponent(data.id)}`}>查看评论记录</a>
          )}
        </div>
      )}

      {parts.length > 0 && (
        <details onToggle={(event) => setShowCoverage((event.currentTarget as HTMLDetailsElement).open)}>
          <summary>分 P 与材料覆盖</summary>
          {showCoverage && (
            <div>
              {parts.map((part) => (
                <div key={part.part} className="operator-chat-memory">
                  <strong>
                    P{part.part}
                    {part.title ? ` · ${part.title}` : ""}
                    {part.duration == null ? "" : ` · ${part.duration} 秒`}
                  </strong>
                  <ul>
                    {(part.ranges || []).map((range, rIdx) => {
                      const title = `${range.start}–${range.end} 秒`;
                      const href = videoHref(data.selection?.bvid, `?p=${part.part}&t=${range.start}`);
                      const missing =
                        range.visual?.gaps ||
                        (range.visual?.windows || []).filter((w) => w.state !== "sampled");
                      return (
                        <li key={`${part.part}-${rIdx}`}>
                          {href ? <ExternalLink text={title} href={href} /> : <strong>{title}</strong>}
                          <p className="hint">字幕 · {materialSummary(range.subtitle)}</p>
                          <p className="hint">音频 · {materialSummary(range.audio)}</p>
                          <p className="hint">画面 · {materialSummary(range.visual)}</p>
                          {missing.length > 0 && (
                            <details>
                              <summary>画面缺口 {missing.length} 段</summary>
                              <ul>
                                {missing.map((w, wIdx) => {
                                  const reason =
                                    { gap: "无可用预览图", retry: "待重试", pending: "未处理" }[w.state] ||
                                    "画面间隔过大";
                                  return (
                                    <li key={`w-${wIdx}`}>
                                      {w.start}–{w.end} 秒 · {reason}
                                      {w.reason ? ` · ${calmChatMessage(w.reason)}` : ""}
                                    </li>
                                  );
                                })}
                              </ul>
                            </details>
                          )}
                          {(range.highlights || []).map((hl, hIdx) => {
                            const loc = videoHref(data.selection?.bvid, `?p=${part.part}&t=${Math.floor(hl.start)}`);
                            if (!loc || !hl.text) return null;
                            return (
                              <p key={`hl-${hIdx}`}>
                                <ExternalLink text={`${Math.floor(hl.start)} 秒`} href={loc} /> · {hl.kind} ·{" "}
                                {hl.text}
                              </p>
                            );
                          })}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </details>
      )}

      <div className="actions">
        {data.id && (
          <a href={`#automation/round/${encodeURIComponent(data.id)}`}>查看轮次</a>
        )}
        <button
          type="button"
          hidden={!data.full_video || !isRunning}
          disabled={busy}
          onClick={() => void handleAction("cancel")}
        >
          取消取材
        </button>
        <button
          type="button"
          hidden={!data.full_video || !data.can_resume || isRunning}
          disabled={busy}
          onClick={() => void handleAction("resume")}
        >
          继续取材
        </button>
      </div>
      {(actionNote || data.error) && (
        <p className="hint">{actionNote || calmChatMessage(data.error)}</p>
      )}
    </>
  );
}

interface LiveCardData {
  enabled?: boolean;
  state?: string;
  muted?: boolean;
}

function LiveCard({ requestId, type, initial }: { requestId: string; type: string; initial: LiveCardData }) {
  const [override, setOverride] = useState<LiveCardData | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [showRefresh, setShowRefresh] = useState(false);

  useEffect(() => {
    setOverride(null);
  }, [initial]);

  const value = override || initial;

  async function confirmLive() {
    if (busy) return;
    setBusy(true);
    setNote("正在检查并切换资源…");
    try {
      const result = await apiPost<
        "/api/v1/agent/chat/turns/{request_id}/confirm/{action}",
        { card: { data: LiveCardData } },
        Record<string, never>
      >("/api/v1/agent/chat/turns/{request_id}/confirm/{action}", {
        pathParams: { request_id: requestId, action: "live" },
        body: {},
      });
      setOverride(result.card.data);
      setNote("直播会话已开启。");
    } catch (error) {
      setNote(calmChatMessage(errorMessage(error)));
      if (error instanceof ApiError && error.status === 409) {
        setShowRefresh(true);
      }
    } finally {
      setBusy(false);
    }
  }

  async function refreshLive() {
    if (busy) return;
    setBusy(true);
    try {
      const result = await apiPost<
        "/api/v1/agent/chat/turns/{request_id}/refresh-confirmation",
        { card: { data: LiveCardData } }
      >("/api/v1/agent/chat/turns/{request_id}/refresh-confirmation", {
        pathParams: { request_id: requestId },
      });
      setOverride(result.card.data);
      setShowRefresh(false);
      setNote("已更新会话状态，请核对后确认。");
    } catch (error) {
      setNote(calmChatMessage(errorMessage(error)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="chat-card-header">
        <h2>直播状态</h2>
        <StatusPill
          label={
            value.enabled
              ? `${statusLabel(value.state)}${value.muted ? " · 静音" : ""}`
              : "暂未启用直播"
          }
          tone={value.enabled ? statusTone(value.state) : "neutral"}
        />
      </div>
      <a href="#live">打开直播台</a>
      {type === "live_confirmation" && (
        <>
          <div className="actions">
            <button
              type="button"
              className="primary"
              disabled={busy || !value.enabled || value.state === "live"}
              onClick={() => void confirmLive()}
            >
              确认开启直播会话
            </button>
            <button
              type="button"
              hidden={!showRefresh}
              disabled={busy}
              onClick={() => void refreshLive()}
            >
              重新核对会话
            </button>
          </div>
          {note && (
            <p className="hint" role="status">
              {note}
            </p>
          )}
        </>
      )}
    </>
  );
}

export interface OperatorChatCardsProps {
  turn: ChatTurn;
  sharedAutomation: AutomationControlStatus | null;
  controlsPending: boolean;
  onControlsPendingChange: (pending: boolean) => void;
  onReflectAutomation: (status: AutomationControlStatus) => void;
  onOpenTask: (taskId: string) => void;
  onOpenArtifact: (ref: OpenedArtifactRef) => void;
  onChatChanged: () => void;
}

export function OperatorChatCards({
  turn,
  sharedAutomation,
  controlsPending,
  onControlsPendingChange,
  onReflectAutomation,
  onOpenTask,
  onOpenArtifact,
  onChatChanged,
}: OperatorChatCardsProps) {
  const card = turn.card;
  if (!card) return null;
  const { type, data } = card;

  if (type === "history_artifact") {
    return (
      <HistoryArtifactCard
        turn={turn}
        data={data as { task_revision: number; edition: number }}
        onOpenArtifact={onOpenArtifact}
      />
    );
  }

  if (type === "agent_task") {
    const taskId = String(data.id || "");
    const status = String(data.status || "queued");
    const statusMap: Record<string, string> = {
      awaiting_approval: "等待确认",
      needs_input: "等待你的答复",
      waiting: "等待工具结果",
      cancelled: "已停止",
      scheduled: "已预约",
      failed: "暂未完成",
    };
    const statusText = statusMap[status] || statusLabel(status);
    const delivered = Boolean(data.delivered_in_chat);
    const rawMessage = delivered ? "" : String(data.message || "");
    const message = rawMessage ? calmChatMessage(rawMessage) : "";
    const steps = Array.isArray(data.steps) ? (data.steps as Array<{ result?: unknown }>) : [];
    const showReceiptHint =
      !delivered && (status === "completed" || status === "failed") && steps.some((s) => s.result);

    return (
      <>
        <div className="chat-card-header">
          <StatusPill label={statusText} tone={statusTone(status)} role="status" />
          {showReceiptHint && <span className="hint">已整理核对记录</span>}
        </div>
        {message && (
          <div className="chat-task-result">
            <AssistantAnswer turn={{ reply_format: "markdown", reply_text: message }} />
          </div>
        )}
        <div className="actions">
          <button type="button" onClick={() => onOpenTask(taskId)}>
            查看任务
          </button>
        </div>
      </>
    );
  }

  if (type === "task_delivery") {
    const taskId = String(data.id || "");
    const approval = data.approval as
      | (DynamicPublicationApprovalData & {
          target?: string;
          text?: string;
          payload?: { mentions?: PublicMention[] };
        })
      | undefined;
    const canControl = Boolean(data.can_control);
    const items = Array.isArray(data.items)
      ? (data.items as Array<{
          post_id: number;
          text: string;
          status: string;
          duplicate_of?: unknown;
          href?: string;
          number?: string | number;
          reason?: string;
        }>)
      : null;

    return (
      <>
        <div>
          {typeof data.title === "string" && data.title && <p className="hint">{data.title}</p>}
          {approval?.kind === "dynamic_publication" && canControl ? (
            <DynamicPublicationApproval
              taskId={taskId}
              stepId={String(data.step_id || "")}
              revision={Number(data.revision || 1)}
              approval={approval}
              onUpdated={() => onChatChanged()}
            />
          ) : approval && approval.kind !== "dynamic_publication" ? (
            <>
              <p>{approval.target || "等你确认"}</p>
              {approval.text && <p>{approval.text}</p>}
              {approval.payload?.mentions?.length ? (
                <MentionLinks mentions={approval.payload.mentions} />
              ) : null}
            </>
          ) : null}
          <ActionInteractionBlock
            interaction={data.interaction as { kind?: string; href?: string; target?: string } | null}
          />
          {typeof data.artifact_download === "string" && data.artifact_download && (
            <a href={studioPath(data.artifact_download)} download="">
              下载成果
            </a>
          )}
          {items && (
            <ul>
              {items.map((item, idx) => {
                const statusMap: Record<string, string> = {
                  published: "已发",
                  not_sent: "没发",
                  unconfirmed: "待核对",
                };
                const st = item.duplicate_of ? "已有发布记录" : statusMap[item.status] || item.status;
                const caption = item.text.replace(/\s+/g, " ").slice(0, 72);
                return (
                  <li key={`${item.post_id}-${idx}`}>
                    {st} · {caption || `草稿 #${item.post_id}`}
                    {item.status === "published" && isPlatformLink(item.href) && (
                      <>
                        {" "}
                        · <ExternalLink text="查看动态" href={item.href} />
                      </>
                    )}
                    {item.number != null && ` · 编号 ${item.number}`}
                    {item.reason && ` · ${calmChatMessage(item.reason)}`}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="actions">
          <button type="button" onClick={() => onOpenTask(taskId)}>
            查看任务
          </button>
        </div>
      </>
    );
  }

  if (type === "action_interaction") {
    return (
      <ActionInteractionBlock
        interaction={data as { kind?: string; href?: string; target?: string }}
      />
    );
  }

  if (type === "owner_profile" && typeof data.url === "string") {
    return <ExternalLink text="打开已绑定的 B 站主页" href={data.url} />;
  }

  if (type === "dynamic_draft") {
    return <DynamicDraftCard initial={data as DynamicDraftCardData} />;
  }

  if (type === "publication_queue") {
    const pages: Record<string, string> = {
      dynamics: "#dynamics",
      messages: "#messages",
      comments: "#comments",
    };
    const groups = (data.groups || []) as Array<{
      channel: string;
      label: string;
      items: Array<{ status: string; preview: string; draft?: { content?: string } }>;
    }>;
    return (
      <>
        {groups.map((group) => (
          <section key={group.channel}>
            <h2>{group.label}</h2>
            {group.items.map((item, idx) => {
              const caption = `${statusLabel(item.status)} · ${item.preview}`;
              return item.draft?.content ? (
                <details key={`q-${idx}`}>
                  <summary>{caption}</summary>
                  <blockquote className="quoted-text">{item.draft.content}</blockquote>
                </details>
              ) : (
                <p key={`q-${idx}`} className="quoted-text">
                  {caption}
                </p>
              );
            })}
            <a href={pages[group.channel] || "#automation"}>打开{group.label}工作台</a>
          </section>
        ))}
      </>
    );
  }

  if (type === "dynamic_edit") {
    return <DynamicEditCard initial={data as unknown as DynamicEditData} />;
  }

  if (type === "video_browsing") {
    return <VideoBrowsingCard turn={turn} initial={data as unknown as VideoBrowsingData} />;
  }

  if (type === "operation_control") {
    return (
      <OperationControlCard
        requestId={turn.request_id}
        initialStatus={data as AutomationControlStatus}
        sharedStatus={sharedAutomation}
        controlsPending={controlsPending}
        onControlsPendingChange={onControlsPendingChange}
        onReflectAutomation={onReflectAutomation}
      />
    );
  }

  if (type === "video_activity") {
    const available = Boolean(data.available);
    const records = (data.records || []) as Array<{
      bvid: string;
      round_id?: string;
      status: string;
      coverage?: string;
    }>;
    const coverageMap: Record<string, string> = {
      all_available_cc: "字幕完整",
      sampled_visual: "抽样画面",
      sampled_audio: "抽样音频",
      title_only: "只看标题",
      comments_only: "只看评论",
    };
    return (
      <>
        <h2>今日观看</h2>
        <p className="hint">
          {String(data.day || "")} · {String(data.timezone || "")}
        </p>
        {!available ? (
          <p>当前账号的观看记录不可用。</p>
        ) : (
          <>
            <p>
              已开始 {String(data.started ?? 0)} 次 · 已完成 {String(data.completed ?? 0)} 次 · 完整阅读{" "}
              {String(data.fully_read ?? 0)} 次
            </p>
            {records.length > 0 && (
              <details>
                <summary>查看观看记录</summary>
                <ul>
                  {records.map((rec, idx) => {
                    const href = rec.round_id
                      ? `#automation/round/${encodeURIComponent(rec.round_id)}`
                      : videoHref(rec.bvid);
                    return (
                      <li key={`${rec.bvid}-${idx}`}>
                        {href ? <ExternalLink text={rec.bvid} href={href} /> : <strong>{rec.bvid}</strong>} ·{" "}
                        {statusLabel(rec.status)}
                        {rec.coverage ? ` · ${coverageMap[rec.coverage] || "已记录材料范围"}` : ""}
                      </li>
                    );
                  })}
                </ul>
              </details>
            )}
            <a href="#automation/video">查看运营活动</a>
          </>
        )}
      </>
    );
  }

  if (type === "memory_change") {
    const text = String(data.text || "").trim();
    if (!text) return null;
    if (turn.memory_updates?.length) {
      return null;
    }
    return (
      <MemoryUpdatedPill
        updates={[
          {
            action: "updated",
            scope: "owner",
            snippet: text,
          },
        ]}
      />
    );
  }

  if (type === "game_handoff") {
    return (
      <>
        <h3>{String(data.target || "")}</h3>
        <p>{String(data.text || "")}</p>
        <p className="hint">{String(data.state || "")}</p>
      </>
    );
  }

  if (type === "live_history") {
    const items = (data.items || []) as Array<{ text: string }>;
    return (
      <>
        <h3>已播台词</h3>
        <ul>
          {items.map((item, idx) => (
            <li key={`lh-${idx}`}>{item.text}</li>
          ))}
        </ul>
      </>
    );
  }

  if (type === "live_handoff") {
    const current = String(data.status || "");
    const statusMap: Record<string, string> = {
      queued: "等待播报",
      generating: "正在生成台词",
      synthesizing: "正在合成语音",
      ready_to_play: "等待播放器",
      playing: "正在播报",
      failed: "暂未播报",
      cancelled: "任务已取消",
      unavailable: "任务不可用",
      expired: "任务已过期",
    };
    const sources = (data.sources || []) as Array<{ part: number; seconds: number; url: string }>;
    return (
      <>
        <h3>本场直播</h3>
        <p>{String(data.text || "")}</p>
        <p className="hint">
          {typeof data.spoken === "string" && data.spoken
            ? `已播报：${data.spoken}`
            : statusMap[current] || current}
        </p>
        {sources.map((src, idx) => (
          <ExternalLink
            key={`lsrc-${idx}`}
            text={`P${src.part} · ${formatTimecode(src.seconds)}`}
            href={src.url}
          />
        ))}
      </>
    );
  }

  if (type === "public_experiences") {
    const items = (data.items || []) as Array<{
      headline: string;
      anecdote: string;
      links: string[];
    }>;
    return (
      <>
        {items.length > 0 ? (
          <ul>
            {items.map((entry, idx) => (
              <li key={`exp-${idx}`} className="operator-chat-memory">
                <strong>{entry.headline}</strong>
                <p>{entry.anecdote}</p>
                {entry.links.map((href, lIdx) =>
                  isPlatformLink(href) ? <ExternalLink key={`l-${lIdx}`} text="原始来源" href={href} /> : null
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">暂无已保存的公开经历。</p>
        )}
      </>
    );
  }

  if (type === "memory_summary") {
    const groups = (data.groups || []) as Array<{
      kind: string;
      notes: Array<{ text: string; visibility?: string }>;
    }>;
    return (
      <>
        {groups.map((group, idx) => (
          <div key={`${group.kind}-${idx}`}>
            <strong>{MEMORY_GROUP_NAMES[group.kind] || group.kind}</strong>
            {group.notes.map((note, nIdx) => (
              <p key={`n-${nIdx}`}>
                {note.text}{" "}
                {note.visibility && MEMORY_VISIBILITY_LABELS[note.visibility] ? (
                  <small className="hint">{MEMORY_VISIBILITY_LABELS[note.visibility]}</small>
                ) : null}
              </p>
            ))}
          </div>
        ))}
        {!groups.length && <p className="hint">暂无可用的长期记忆。</p>}
        <a href="#personalization">管理记忆</a>
      </>
    );
  }

  if (type === "operation_events") {
    const items = (data.items || []) as OperationEventItem[];
    return (
      <ol>
        {items.map((event, idx) => (
          <li key={`oe-${idx}`}>
            <EventContent item={event} heading="h3" />
          </li>
        ))}
      </ol>
    );
  }

  if (type === "operation_event") {
    return <EventContent item={data.event as OperationEventItem} heading="h2" />;
  }

  if (type === "live_studio_hud" || type === "live_confirmation") {
    return <LiveCard requestId={turn.request_id} type={type} initial={data as LiveCardData} />;
  }

  return null;
}
