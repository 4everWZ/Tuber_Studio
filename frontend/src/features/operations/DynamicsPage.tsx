import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  MemePickerModal,
  MentionLinksAndEditor,
  OperationContinuationCard,
  StatusPill,
  useUnsavedGuard,
  type MemeAsset,
  type MentionSaveItem,
} from "@/shared";
import { apiGet, apiPost, apiPut, studioPath } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { illustrationStatus, illustrationUrl } from "@/shared/lib/operationImage";
import { defaultMemePreviewUrl } from "@/shared/ui/MemePickerModal";
import type { ParsedRoute } from "@/shell/navigation";
import { useOperationsControl } from "./operationsStore";
import { DynamicPolicyForm } from "./PolicyForms";
import styles from "./operations.module.css";
import type {
  DynamicCommentCard,
  DynamicDecision,
  DynamicEditResult,
  DynamicMaterials,
  DynamicMemory,
  DynamicPolicy,
  DynamicPost,
  DynamicSourceItem,
} from "./types";

const POST_STATUS_LABELS: Record<string, string> = {
  pending_review: "待审核",
  approved: "已审核 · 待发布",
  publish_uncertain: "待核对",
  publishing: "发布中",
  published: "已发布",
  rejected: "已拒绝",
  invalidated: "已失效",
};

const DECISION_STATUS_LABELS: Record<string, string> = {
  completed: "已完成",
  posted: "已生成",
  skipped: "暂不发动态",
  failed: "失败",
  cancelled: "已取消",
  queued: "排队中",
  running: "处理中",
};

const TERMINAL_POST_STATUSES = new Set(["published", "rejected", "invalidated"]);
const MEME_CACHE_TTL_MS = 30_000;
const BG_REFRESH_THROTTLE_MS = 8_000;
const ACCEPT_ALL_MEMES = () => true;

interface MemeCacheEntry {
  fetchedAt: number;
  assets: Map<string, MemeAsset>;
}

function safeHttpsUrl(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed.startsWith("https://")) return null;
  try {
    const parsed = new URL(trimmed);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function extractBvid(url: string): string | null {
  const match = /\b(BV[0-9A-Za-z]+)\b/.exec(url);
  return match ? match[1] : null;
}

function formatDateTime(value: number | string | null | undefined): string {
  if (value == null || value === "") return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}

function postStatusTone(status: string): "good" | "amber" | "bad" | "neutral" {
  if (status === "published") return "good";
  if (status === "approved" || status === "publishing") return "neutral";
  if (status === "rejected") return "bad";
  if (status === "publish_uncertain" || status === "invalidated") return "amber";
  return "neutral";
}

function collectDeduplicatedCardLinks(card: DynamicCommentCard | null): {
  links: Array<{ bvid: string; url: string }>;
  linkedBvids: Set<string>;
} {
  const links: Array<{ bvid: string; url: string }> = [];
  const linkedBvids = new Set<string>();
  const seenUrls = new Set<string>();
  if (!card) return { links, linkedBvids };

  for (const link of card.links || []) {
    const safeUrl = safeHttpsUrl(link.url);
    if (!safeUrl) continue;
    const bvid = link.bvid || extractBvid(safeUrl) || "";
    if (bvid && linkedBvids.has(bvid)) continue;
    if (seenUrls.has(safeUrl)) continue;
    if (bvid) linkedBvids.add(bvid);
    seenUrls.add(safeUrl);
    links.push({ bvid, url: safeUrl });
  }
  return { links, linkedBvids };
}

function SourceItemsList({
  sources,
  excludedBvids,
}: {
  sources: DynamicSourceItem[];
  excludedBvids?: Set<string>;
}) {
  if (!sources.length) {
    return <p className="hint">暂无参考来源记录。</p>;
  }
  return (
    <ul className={styles.recordList}>
      {sources.map((src, idx) => {
        const safeUrl = safeHttpsUrl(src.source_url);
        const bvid = safeUrl ? extractBvid(safeUrl) : null;
        const hideUrl = Boolean(bvid && excludedBvids?.has(bvid));
        return (
          <li key={src.id || `${idx}`} className={styles.recordListItem}>
            <div className={styles.recordMeta}>
              {src.type ? <strong>{src.type}</strong> : <strong>来源 #{idx + 1}</strong>}
              {safeUrl && !hideUrl && (
                <a href={safeUrl} target="_blank" rel="noopener noreferrer">
                  {bvid ? `查看来源 · ${bvid}` : "查看原来源"}
                </a>
              )}
            </div>
            {src.text && <p style={{ margin: "4px 0 0" }}>{src.text}</p>}
            {src.participants && src.participants.length > 0 && (
              <p className="hint" style={{ margin: "4px 0 0" }}>
                参与者：
                {src.participants
                  .map((p) => (p.name ? `${p.name}${p.uid != null ? ` (UID:${p.uid})` : ""}` : String(p.uid ?? "")))
                  .filter(Boolean)
                  .join("、")}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function DynamicsPage({ route }: { route: ParsedRoute }) {
  const { status: sysStatus } = useAppSession();
  const control = useOperationsControl();
  const routeEditRequestId =
    route.routeKind === "edits" && route.routeId ? route.routeId : "";

  const [policy, setPolicy] = useState<DynamicPolicy | null>(null);
  const [policyDirty, setPolicyDirty] = useState(false);

  const [posts, setPosts] = useState<DynamicPost[]>([]);
  const [decisions, setDecisions] = useState<DynamicDecision[]>([]);
  const [memories, setMemories] = useState<DynamicMemory[]>([]);
  const [materials, setMaterials] = useState<DynamicMaterials | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [postView, setPostView] = useState<"pending" | "history">("pending");
  const [selectedPostId, setSelectedPostId] = useState<number | null>(null);
  const [detachedPost, setDetachedPost] = useState<DynamicPost | null>(null);
  const [listPlaceholder, setListPlaceholder] = useState("请选择左侧一条动态查看详情。");

  const [draftEdits, setDraftEdits] = useState<Map<number, string>>(() => new Map());
  const [topicInput, setTopicInput] = useState("");
  const [evaluateStatus, setEvaluateStatus] = useState("");
  const [actionStatus, setActionStatus] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);

  const [memePickerOpen, setMemePickerOpen] = useState(false);
  const [memeAssets, setMemeAssets] = useState<Map<string, MemeAsset>>(() => new Map());
  const [failedCardPages, setFailedCardPages] = useState<Set<string>>(() => new Set());
  const [memoryErrors, setMemoryErrors] = useState<Map<number, string>>(() => new Map());

  const [editRequestIdInput, setEditRequestIdInput] = useState(routeEditRequestId);
  const [editResult, setEditResult] = useState<DynamicEditResult | null>(null);
  const [editApplyBusy, setEditApplyBusy] = useState(false);
  const [editApplyError, setEditApplyError] = useState("");

  const refreshSeqRef = useRef(0);
  const lastBgRefreshAtRef = useRef(0);
  const memoriesLoadedRef = useRef(false);
  const prevFingerprintRef = useRef("");
  const memeCacheRef = useRef<Map<string, MemeCacheEntry>>(new Map());

  const hasDirtyDrafts = useMemo(() => {
    for (const [postId, editedText] of draftEdits.entries()) {
      const existing = posts.find((p) => p.id === postId);
      const baseText = existing?.content ?? detachedPost?.content ?? "";
      if (editedText !== baseText) {
        return true;
      }
    }
    return false;
  }, [draftEdits, posts, detachedPost]);

  useUnsavedGuard("dynamics-draft-edits", hasDirtyDrafts || policyDirty);

  useEffect(() => {
    if (routeEditRequestId) {
      setEditRequestIdInput(routeEditRequestId);
      setEditResult((prev) =>
        prev?.request_id === routeEditRequestId ? prev : null
      );
      setEditApplyError("");
    }
  }, [routeEditRequestId]);

  const loadMemeMetadata = useCallback(async (characterId: string | undefined) => {
    const key = characterId || "_default";
    const cached = memeCacheRef.current.get(key);
    if (cached && Date.now() - cached.fetchedAt < MEME_CACHE_TTL_MS) {
      setMemeAssets(cached.assets);
      return;
    }
    try {
      const res = await apiGet<
        "/api/v1/persona/memes",
        { assets?: MemeAsset[] } | MemeAsset[]
      >("/api/v1/persona/memes");
      const list = Array.isArray(res) ? res : res.assets || [];
      const map = new Map<string, MemeAsset>();
      for (const item of list) {
        map.set(item.asset_id, item);
      }
      memeCacheRef.current.set(key, { fetchedAt: Date.now(), assets: map });
      setMemeAssets(map);
    } catch {
      // Meme metadata lookup failure shouldn't block dynamic review
    }
  }, []);

  const refreshDynamics = useCallback(
    async (options?: { background?: boolean; force?: boolean }) => {
      const isBg = Boolean(options?.background);
      const now = Date.now();
      if (isBg && !options?.force && now - lastBgRefreshAtRef.current < BG_REFRESH_THROTTLE_MS) {
        return;
      }
      if (isBg) {
        lastBgRefreshAtRef.current = now;
      }
      const seq = ++refreshSeqRef.current;

      try {
        const [nextPolicy, nextPosts, nextDecisions, nextMaterials] = await Promise.all([
          apiGet<"/api/v1/operations/dynamics/policy", DynamicPolicy>(
            "/api/v1/operations/dynamics/policy"
          ),
          apiGet<"/api/v1/operations/dynamics/posts", DynamicPost[]>(
            "/api/v1/operations/dynamics/posts"
          ),
          apiGet<"/api/v1/operations/dynamics/decisions", DynamicDecision[]>(
            "/api/v1/operations/dynamics/decisions"
          ),
          apiGet<"/api/v1/operations/dynamics/materials", DynamicMaterials>(
            "/api/v1/operations/dynamics/materials"
          ),
        ]);
        if (seq !== refreshSeqRef.current) return;

        const postsArr = Array.isArray(nextPosts) ? nextPosts : [];
        const decisionsArr = Array.isArray(nextDecisions) ? nextDecisions : [];
        const fingerprint = `${postsArr.map((p) => `${p.id}:${p.status}:${p.content || ""}`).join("|")}#${decisionsArr.map((d) => `${d.id}:${d.status}`).join("|")}`;
        const changed = fingerprint !== prevFingerprintRef.current;
        prevFingerprintRef.current = fingerprint;

        let nextMemories: DynamicMemory[] | null = null;
        if (!isBg || !memoriesLoadedRef.current || changed) {
          const mems = await apiGet<"/api/v1/operations/dynamics/memories", DynamicMemory[]>(
            "/api/v1/operations/dynamics/memories",
            { query: { limit: 100 } }
          );
          if (seq !== refreshSeqRef.current) return;
          nextMemories = Array.isArray(mems) ? mems : [];
          memoriesLoadedRef.current = true;
        }

        setPolicy(nextPolicy);
        setPosts(postsArr);
        setDecisions(decisionsArr);
        setMaterials(nextMaterials);
        if (nextMemories !== null) {
          setMemories(nextMemories);
        }
        setLoaded(true);

        // Background reconciliation when selected pending post leaves the active list
        if (selectedPostId != null && postView === "pending") {
          const updated = postsArr.find((p) => p.id === selectedPostId);
          const removedFromPending = !updated || TERMINAL_POST_STATUSES.has(updated.status);
          if (removedFromPending) {
            const editedText = draftEdits.get(selectedPostId);
            const originalText =
              updated?.content ??
              posts.find((p) => p.id === selectedPostId)?.content ??
              detachedPost?.content ??
              "";
            const isDirty = editedText !== undefined && editedText !== originalText;
            if (!isDirty) {
              setSelectedPostId(null);
              setDetachedPost(null);
              setMemePickerOpen(false);
              setDraftEdits((prev) => {
                if (!prev.has(selectedPostId)) return prev;
                const next = new Map(prev);
                next.delete(selectedPostId);
                return next;
              });
              setListPlaceholder("当前草稿已完成或移出待处理列表，请选择其他动态。");
            } else {
              const snapshot =
                updated ??
                posts.find((p) => p.id === selectedPostId) ??
                detachedPost;
              if (snapshot) {
                setDetachedPost(snapshot);
              }
            }
          } else {
            setDetachedPost(null);
          }
        }
      } catch (err) {
        if (seq !== refreshSeqRef.current) return;
        if (!isBg) {
          setActionError(errorMessage(err, "动态工坊加载失败。"));
        }
      }
    },
    [detachedPost, draftEdits, postView, posts, selectedPostId]
  );

  // Initial load & character/account scoped refresh
  const activeScopeKey = `${sysStatus?.character?.id || ""}:${control.status?.account_id || ""}`;
  useEffect(() => {
    void refreshDynamics({ force: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeScopeKey]);

  // Background poll every 10s (throttled at 8s inside refreshDynamics)
  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshDynamics({ background: true });
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [refreshDynamics]);

  const filteredPosts = useMemo(() => {
    return posts.filter((post) =>
      postView === "history"
        ? TERMINAL_POST_STATUSES.has(post.status)
        : !TERMINAL_POST_STATUSES.has(post.status)
    );
  }, [posts, postView]);

  const activePost = useMemo(() => {
    if (selectedPostId == null) return null;
    const inView = filteredPosts.find((p) => p.id === selectedPostId);
    if (inView) return inView;
    if (detachedPost && detachedPost.id === selectedPostId) return detachedPost;
    return null;
  }, [detachedPost, filteredPosts, selectedPostId]);

  const isDetached = useMemo(() => {
    if (!activePost || !detachedPost || activePost.id !== detachedPost.id) return false;
    return !filteredPosts.some((p) => p.id === activePost.id);
  }, [activePost, detachedPost, filteredPosts]);

  // Fetch meme metadata when selected post changes
  useEffect(() => {
    if (activePost?.meme_id) {
      void loadMemeMetadata(activePost.character_id || sysStatus?.character?.id || undefined);
    }
  }, [activePost?.id, activePost?.meme_id, activePost?.character_id, sysStatus?.character?.id, loadMemeMetadata]);

  const activeDraftText = activePost
    ? (draftEdits.get(activePost.id) ?? activePost.content ?? "")
    : "";

  const isSelectedEditable = Boolean(
    activePost &&
      !isDetached &&
      (activePost.status === "pending_review" || activePost.status === "approved")
  );
  const awaitingChoice = activePost?.illustration_choice === "awaiting_choice";

  const canPublish = Boolean(
    activePost && !isDetached && activePost.status === "approved" && !busy
  );

  async function handleEvaluate() {
    if (busy) return;
    setBusy(true);
    setActionError("");
    setEvaluateStatus("正在评估是否生成动态…");
    try {
      const payload = topicInput.trim() ? { topic: topicInput.trim() } : {};
      const res = await apiPost<
        "/api/v1/operations/dynamics/evaluate",
        { action?: string; reason?: string; post_id?: number },
        { topic?: string }
      >("/api/v1/operations/dynamics/evaluate", { body: payload });

      setEvaluateStatus(
        res.post_id
          ? "已生成新的动态草稿，请审核后发布。"
          : res.reason || "本轮评估决定暂不发动态。"
      );
      if (res.post_id) {
        setPostView("pending");
        setDetachedPost(null);
        setSelectedPostId(res.post_id);
        setTopicInput("");
      }
      await refreshDynamics({ force: true });
    } catch (err) {
      const msg = errorMessage(err, "触发动态评估失败。");
      setEvaluateStatus(msg);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusy(false);
    }
  }

  async function submitPostReview(
    post: DynamicPost,
    payload: {
      action: "edit" | "approve" | "reject" | "reselect_image";
      text?: string;
      meme_id?: string | null;
      review_key?: string;
      mentions?: MentionSaveItem[];
    },
    successNotice: string
  ) {
    if (busy) return;
    setBusy(true);
    setActionError("");
    setActionStatus("");
    try {
      await apiPost<
        "/api/v1/operations/dynamics/posts/{post_id}/review",
        DynamicPost,
        typeof payload
      >("/api/v1/operations/dynamics/posts/{post_id}/review", {
        pathParams: { post_id: post.id },
        body: payload,
      });

      if (payload.action === "edit" || payload.action === "approve" || payload.action === "reject") {
        setDraftEdits((prev) => {
          if (!prev.has(post.id)) return prev;
          const next = new Map(prev);
          next.delete(post.id);
          return next;
        });
      }
      if (payload.action === "reject") {
        setSelectedPostId(null);
        setDetachedPost(null);
        setMemePickerOpen(false);
        setListPlaceholder("已拒绝该动态草稿。请选择其他动态。");
      }
      setActionStatus(successNotice);
      emitNotice(successNotice);
      await refreshDynamics({ force: true });
    } catch (err) {
      const msg = errorMessage(err, "动态审核操作失败。");
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusy(false);
    }
  }

  async function handlePublish(post: DynamicPost) {
    if (busy) return;
    setBusy(true);
    setActionError("");
    setActionStatus("");
    try {
      await apiPost<
        "/api/v1/operations/dynamics/posts/{post_id}/publish",
        DynamicPost,
        { text: string; review_key?: string }
      >("/api/v1/operations/dynamics/posts/{post_id}/publish", {
        pathParams: { post_id: post.id },
        body: {
          text: post.content ?? "",
          review_key: post.review_key,
        },
      });
      setDraftEdits((prev) => {
        if (!prev.has(post.id)) return prev;
        const next = new Map(prev);
        next.delete(post.id);
        return next;
      });
      setActionStatus("动态已提交发布，平台回执已保存。");
      emitNotice("动态已发布，平台回执已保存。");
      await refreshDynamics({ force: true });
    } catch (err) {
      await refreshDynamics({ force: true });
      const msg = errorMessage(err, "动态发布结果不确定，请核对 B 站动态与发布回执。");
      setActionError(`${msg} 请核对 B 站动态与发布回执。`);
      emitNotice(msg, true);
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleMemoryPublic(mem: DynamicMemory, nextPublic: boolean) {
    setMemories((prev) =>
      prev.map((m) => (m.id === mem.id ? { ...m, public: nextPublic } : m))
    );
    setMemoryErrors((prev) => {
      if (!prev.has(mem.id)) return prev;
      const next = new Map(prev);
      next.delete(mem.id);
      return next;
    });
    try {
      const updated = await apiPut<
        "/api/v1/operations/dynamics/memories/{memory_id}",
        DynamicMemory,
        { public: boolean }
      >("/api/v1/operations/dynamics/memories/{memory_id}", {
        pathParams: { memory_id: mem.id },
        body: { public: nextPublic },
      });
      setMemories((prev) => prev.map((m) => (m.id === mem.id ? updated : m)));
      emitNotice(nextPublic ? "记忆已设为可公开提及。" : "记忆已取消公开。");
    } catch (err) {
      setMemories((prev) =>
        prev.map((m) => (m.id === mem.id ? { ...m, public: mem.public } : m))
      );
      const msg = errorMessage(err, "更新记忆公开状态失败。");
      setMemoryErrors((prev) => {
        const next = new Map(prev);
        next.set(mem.id, msg);
        return next;
      });
      emitNotice(msg, true);
    }
  }

  async function handleApplyDynamicEdit() {
    const requestId = editRequestIdInput.trim();
    if (!requestId || editApplyBusy) return;
    setEditApplyBusy(true);
    setEditApplyError("");
    try {
      const res = await apiPost<
        "/api/v1/operations/dynamics/edits/{request_id}/apply",
        DynamicEditResult,
        never
      >("/api/v1/operations/dynamics/edits/{request_id}/apply", {
        pathParams: { request_id: requestId },
      });
      setEditResult(res);
      if (res.status === "applied") {
        emitNotice("已核对原动态更新。");
      } else if (res.status === "pending_verification") {
        emitNotice("已提交修改请求，结果待核对。");
      }
      await refreshDynamics({ force: true });
    } catch (err) {
      const msg = errorMessage(err, "应用原动态修改失败。");
      setEditApplyError(msg);
      emitNotice(msg, true);
    } finally {
      setEditApplyBusy(false);
    }
  }

  const cardData = activePost
    ? activePost.comment_card || activePost.source_comment_card || null
    : null;
  const { links: cardLinks, linkedBvids } = useMemo(
    () => collectDeduplicatedCardLinks(cardData),
    [cardData]
  );

  const selectedMemeAsset =
    activePost?.meme_id ? memeAssets.get(activePost.meme_id) ?? null : null;
  const activeIllustrationState = illustrationStatus(activePost?.illustration);
  const activeIllustrationHref = activePost
    ? illustrationUrl("dynamics", activePost.id, activePost.illustration)
    : "";
  const activeMemePreviewHref =
    activeIllustrationHref ||
    (activePost?.meme_id
      ? defaultMemePreviewUrl({ asset_id: activePost.meme_id })
      : "");

  const cardPageCount = useMemo(() => {
    if (!cardData) return 0;
    if (typeof cardData.page_count === "number" && cardData.page_count > 0) {
      return cardData.page_count;
    }
    if (cardData.comments && cardData.comments.length > 0) return 1;
    return 0;
  }, [cardData]);

  return (
    <section data-section="dynamics" className="win-page-view">
      {/* 1. Dynamic Policy & Manual Evaluation */}
      <div className={styles.splitWorkspace} style={{ marginBottom: 16 }}>
        <section className="panel" data-dynamic-policy-panel="">
          <h2>动态自主发布规则</h2>
          <DynamicPolicyForm
            policy={policy}
            character={sysStatus?.character}
            onSaved={(next) => {
              setPolicy(next);
              void refreshDynamics({ force: true });
            }}
            onDirtyChange={setPolicyDirty}
          />
        </section>

        <section className="panel" data-dynamic-evaluate-panel="">
          <h2>评估并起草动态</h2>
          <p className="hint">
            结合当前角色的近期动态决策材料、可用配图与公开记忆，按需起草动态草稿。
          </p>
          <div className="row-actions" style={{ marginTop: 10 }}>
            <label style={{ flex: "1 1 220px" }}>
              话题或灵感（可选）
              <input
                type="text"
                value={topicInput}
                disabled={busy}
                placeholder="留空则由大肥鱼自主决定是否起草"
                onChange={(e) => setTopicInput(e.target.value)}
              />
            </label>
            <button
              type="button"
              data-evaluate-dynamics=""
              disabled={busy}
              onClick={() => void handleEvaluate()}
            >
              {busy ? "评估中…" : "评估是否发动态"}
            </button>
            <button
              type="button"
              data-refresh-dynamics=""
              disabled={busy}
              onClick={() => void refreshDynamics({ force: true })}
            >
              刷新列表
            </button>
          </div>
          {evaluateStatus && (
            <p className="hint" role="status" data-evaluate-status="" style={{ marginTop: 8 }}>
              {evaluateStatus}
            </p>
          )}
        </section>
      </div>

      {/* 2. Inline Dynamic Edit Verification */}
      <section className="panel" data-dynamic-edit-verify="" style={{ marginBottom: 16 }}>
        <h2>已发动态原地编辑核对</h2>
        <div className="row-actions" style={{ alignItems: "flex-end" }}>
          <label style={{ flex: "1 1 240px" }}>
            编辑请求编号（request_id）
            <input
              type="text"
              data-dynamic-edit-request-id=""
              value={editRequestIdInput}
              disabled={editApplyBusy}
              placeholder="输入或从聊天卡片跳转的 request_id"
              onChange={(e) => {
                setEditRequestIdInput(e.target.value);
                setEditApplyError("");
              }}
            />
          </label>
          <button
            type="button"
            data-apply-dynamic-edit=""
            disabled={
              editApplyBusy ||
              !editRequestIdInput.trim() ||
              editResult?.status === "applied"
            }
            onClick={() => void handleApplyDynamicEdit()}
          >
            {editResult?.status === "pending_verification"
              ? "核对编辑结果"
              : "确认修改原动态"}
          </button>
        </div>
        {editResult?.status === "applied" && (
          <p className="hint" role="status" data-dynamic-edit-status="" style={{ marginTop: 8 }}>
            已核对原动态更新。{editResult.platform_id ? `（动态 ID：${editResult.platform_id}）` : ""}
          </p>
        )}
        {editResult?.status === "pending_verification" && (
          <p className="hint" role="status" data-dynamic-edit-status="" style={{ marginTop: 8 }}>
            结果待核对，请点击「核对编辑结果」确认原动态更新。
          </p>
        )}
        {editApplyError && (
          <p className="hint err" role="alert" style={{ marginTop: 8 }}>
            {editApplyError}
          </p>
        )}
      </section>

      {/* 3. Posts Queue & Review Detail */}
      <section className="panel" data-dynamics-posts="" style={{ marginBottom: 16 }}>
        <div className={styles.queueToolbar}>
          <fieldset data-post-views="" className={styles.queueStatusOptions}>
            <legend>动态草稿与发布记录</legend>
            {(
              [
                ["pending", "待处理"],
                ["history", "历史"],
              ] as const
            ).map(([val, label]) => (
              <label key={val}>
                <input
                  type="radio"
                  name="dynamics-post-view"
                  value={val}
                  checked={postView === val}
                  onChange={() => {
                    setPostView(val);
                    setDetachedPost(null);
                    setSelectedPostId(null);
                    setMemePickerOpen(false);
                    setActionStatus("");
                    setActionError("");
                  }}
                />
                <span>{label}</span>
              </label>
            ))}
          </fieldset>
        </div>

        <div className={styles.reviewLayout}>
          <div data-post-list="" className={styles.queueList}>
            {!loaded ? (
              <p className="hint">正在读取动态列表…</p>
            ) : filteredPosts.length === 0 ? (
              <p className="hint">
                {postView === "history" ? "暂无历史动态记录。" : "暂无待处理动态草稿。"}
              </p>
            ) : (
              filteredPosts.map((post) => {
                const isSelected = post.id === selectedPostId;
                const dirtyMark =
                  draftEdits.has(post.id) &&
                  draftEdits.get(post.id) !== (post.content ?? "");
                return (
                  <button
                    key={post.id}
                    type="button"
                    className={styles.queueRow}
                    data-post-id={String(post.id)}
                    aria-current={isSelected ? "true" : undefined}
                    onClick={() => {
                      setDetachedPost(null);
                      setSelectedPostId(post.id);
                      setMemePickerOpen(false);
                      setActionStatus("");
                      setActionError("");
                    }}
                  >
                    <strong>
                      #{post.id} · {POST_STATUS_LABELS[post.status] || post.status}
                      {dirtyMark ? " · 未保存" : ""}
                    </strong>
                    <span className={styles.cellSub}>
                      {(post.content || "").slice(0, 72) || "（空正文）"}
                    </span>
                    <span className={styles.cellSub}>{formatDateTime(post.created_at)}</span>
                  </button>
                );
              })
            )}
          </div>

          <div data-post-detail="" className={styles.draftEditor}>
            {!activePost ? (
              <p className="hint">{listPlaceholder}</p>
            ) : (
              <>
                <div className={styles.recordMeta}>
                  <strong>动态 #{activePost.id}</strong>
                  <StatusPill
                    label={POST_STATUS_LABELS[activePost.status] || activePost.status}
                    tone={postStatusTone(activePost.status)}
                  />
                  {activePost.character_id && (
                    <span className="hint">角色：{activePost.character_id}</span>
                  )}
                  <span className="hint">{formatDateTime(activePost.created_at)}</span>
                </div>

                {activePost.reason && (
                  <p className="hint" data-post-reason="">
                    起草理由：{activePost.reason}
                  </p>
                )}

                {isDetached && (
                  <p className="hint err" role="status" data-detached-notice="">
                    草稿已移出当前列表，本地修改可复制。
                  </p>
                )}

                {activePost.status === "publish_uncertain" && (
                  <p className="hint err" role="alert" data-publish-uncertain-notice="">
                    发送状态待核对，请核对 B 站动态与发布回执。
                    {activePost.error ? `（${activePost.error}）` : ""}
                  </p>
                )}

                {activePost.error && activePost.status !== "publish_uncertain" && (
                  <p className="hint err" role="alert">
                    {activePost.error}
                  </p>
                )}

                <label>
                  动态正文
                  <textarea
                    rows={5}
                    data-dynamic-textarea=""
                    value={activeDraftText}
                    readOnly={isDetached || !isSelectedEditable}
                    onChange={(e) => {
                      const val = e.target.value;
                      setDraftEdits((prev) => {
                        const next = new Map(prev);
                        next.set(activePost.id, val);
                        return next;
                      });
                    }}
                  />
                </label>

                <div className="row-actions" style={{ marginTop: 10 }}>
                  {isSelectedEditable && (
                    <>
                      <button
                        type="button"
                        data-save-dynamic-caption=""
                        disabled={busy}
                        onClick={() =>
                          void submitPostReview(
                            activePost,
                            { action: "edit", text: activeDraftText },
                            "动态正文修改已保存。"
                          )
                        }
                      >
                        保存正文修改
                      </button>
                      <button
                        type="button"
                        data-approve-dynamic=""
                        disabled={busy || awaitingChoice}
                        onClick={() =>
                          void submitPostReview(
                            activePost,
                            {
                              action: "approve",
                              text: activeDraftText,
                              review_key: activePost.review_key,
                            },
                            "动态已审核通过，可点击发布。"
                          )
                        }
                      >
                        审核通过
                      </button>
                      <button
                        type="button"
                        data-reject-dynamic=""
                        disabled={busy}
                        onClick={() =>
                          void submitPostReview(
                            activePost,
                            { action: "reject" },
                            "动态草稿已拒绝。"
                          )
                        }
                      >
                        拒绝
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    data-publish-dynamic=""
                    disabled={!canPublish}
                    onClick={() => void handlePublish(activePost)}
                  >
                    发布动态
                  </button>
                  {isDetached && (
                    <button
                      type="button"
                      onClick={() => {
                        setDraftEdits((prev) => {
                          const next = new Map(prev);
                          next.delete(activePost.id);
                          return next;
                        });
                        setDetachedPost(null);
                        setSelectedPostId(null);
                      }}
                    >
                      关闭并放弃本地草稿
                    </button>
                  )}
                </div>

                {actionStatus && (
                  <p className="hint" role="status" style={{ marginTop: 8 }}>
                    {actionStatus}
                  </p>
                )}
                {actionError && (
                  <p className="hint err" role="alert" style={{ marginTop: 8 }}>
                    {actionError}
                  </p>
                )}

                {/* Mentions */}
                <div style={{ marginTop: 12 }}>
                  <MentionLinksAndEditor
                    mentions={activePost.mentions || []}
                    pending={activePost.pending_mentions || []}
                    editable={isSelectedEditable && !busy}
                    onSave={async (items) => {
                      await submitPostReview(
                        activePost,
                        { action: "edit", mentions: items },
                        "动态 @ 提及已保存。"
                      );
                    }}
                  />
                </div>

                {/* Meme / Illustration Preview */}
                <div className={styles.memeAttachmentPreview} data-dynamic-illustration="">
                  <div className={styles.recordMeta}>
                    <strong>配图 / 表情</strong>
                    {activePost.meme_id && (
                      <span className="hint">
                        ID: {activePost.meme_id}
                        {selectedMemeAsset
                          ? selectedMemeAsset.enabled === false
                            ? "（已禁用）"
                            : "（可用）"
                          : memeAssets.size > 0
                            ? "（图库中未找到该配图）"
                            : ""}
                      </span>
                    )}
                    {activePost.illustration_choice && (
                      <span className="hint" data-dynamic-meme-name="">
                        {activePost.illustration_choice === "awaiting_choice"
                          ? "等待重新选图"
                          : activePost.illustration_choice === "text_only"
                            ? "纯文字"
                            : `选择：${activePost.illustration_choice}`}
                      </span>
                    )}
                    {activeIllustrationState && (
                      <StatusPill
                        label={activeIllustrationState}
                        tone={activePost.illustration?.error ? "amber" : "neutral"}
                      />
                    )}
                  </div>

                  {activeMemePreviewHref && (
                    <img
                      src={activeMemePreviewHref}
                      alt={selectedMemeAsset?.title || activePost.meme_id || "动态配图"}
                    />
                  )}

                  {isSelectedEditable && (
                    <div className="row-actions" style={{ marginTop: 6 }}>
                      <button
                        type="button"
                        data-dynamic-pick-meme=""
                        disabled={busy}
                        onClick={() => setMemePickerOpen(true)}
                      >
                        更换配图
                      </button>
                      <button
                        type="button"
                        data-dynamic-reselect-image=""
                        disabled={busy}
                        onClick={() =>
                          void submitPostReview(
                            activePost,
                            { action: "reselect_image" },
                            "已重选或跳过动态配图。"
                          )
                        }
                      >
                        重选或跳过配图
                      </button>
                      {(activePost.meme_id || activeMemePreviewHref || awaitingChoice) && (
                        <button
                          type="button"
                          data-dynamic-clear-meme=""
                          disabled={busy}
                          onClick={() =>
                            void submitPostReview(
                              activePost,
                              { action: "edit", meme_id: null },
                              "已清除动态配图。"
                            )
                          }
                        >
                          清除配图
                        </button>
                      )}
                    </div>
                  )}
                </div>

                {/* Comment Card Attachment */}
                {cardData && (
                  <div className={styles.memeAttachmentPreview} data-dynamic-comment-card="">
                    <strong>附带评论卡片</strong>
                    {cardData.comments && cardData.comments.length > 0 && (
                      <ol data-comment-card-participants="" style={{ margin: "6px 0" }}>
                        {cardData.comments.map((c, idx) => {
                          const safeCommentUrl = safeHttpsUrl(c.url);
                          return (
                            <li key={`${idx}-${c.author || "u"}`}>
                              <strong>{c.author || "匿名用户"}</strong>
                              {c.uid != null && <span className="hint"> (UID:{c.uid})</span>}
                              {c.reply_to
                                ? ` 回复 ${c.reply_to}`
                                : c.parent_comment_id != null
                                  ? ` 回复 #${c.parent_comment_id}`
                                  : ""}
                              {" · "}
                              <span className="hint">
                                {c.time_label || formatDateTime(c.created_at)}
                              </span>
                              {c.quote && <p style={{ margin: "2px 0" }}>{c.quote}</p>}
                              {safeCommentUrl && (
                                <a
                                  href={safeCommentUrl}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                >
                                  查看原评论
                                </a>
                              )}
                            </li>
                          );
                        })}
                      </ol>
                    )}

                    {typeof cardData.omitted === "number" && cardData.omitted > 0 && (
                      <p className="hint">已略去 {cardData.omitted} 条未展示评论。</p>
                    )}
                    {typeof cardData.missing === "number" && cardData.missing > 0 && (
                      <p className="hint">另有 {cardData.missing} 条关联评论已缺失或不可读。</p>
                    )}

                    {cardLinks.length > 0 && (
                      <div className="row-actions" data-comment-card-links="">
                        {cardLinks.map((lnk) => (
                          <a
                            key={lnk.url}
                            href={lnk.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {lnk.bvid ? `打开原评论 · ${lnk.bvid}` : "打开原评论"}
                          </a>
                        ))}
                      </div>
                    )}

                    {Array.from({ length: cardPageCount }, (_, idx) => idx + 1).map((page) => {
                      const errKey = `${activePost.id}:${page}`;
                      if (failedCardPages.has(errKey)) {
                        return (
                          <p key={page} className="hint err" data-comment-card-error="">
                            评论引用图第 {page} 页加载失败，请点击上方原评论链接查看。
                          </p>
                        );
                      }
                      return (
                        <img
                          key={page}
                          className={styles.commentCardImage}
                          src={studioPath(
                            `/api/v1/operations/dynamics/posts/${activePost.id}/comment-card?page=${page}`
                          )}
                          alt={`动态 #${activePost.id} 评论卡片第 ${page} 页`}
                          onError={() => {
                            setFailedCardPages((prev) => {
                              const next = new Set(prev);
                              next.add(errKey);
                              return next;
                            });
                          }}
                        />
                      );
                    })}
                  </div>
                )}

                {/* Sources & Outgoing Links */}
                {(activePost.sources?.length || activePost.outgoing_links?.length) ? (
                  <div style={{ marginTop: 12 }} data-dynamic-sources="">
                    <h3>引用来源与外链</h3>
                    <SourceItemsList
                      sources={activePost.sources || []}
                      excludedBvids={linkedBvids}
                    />
                    {(activePost.outgoing_links || [])
                      .map((u) => safeHttpsUrl(u))
                      .filter((u): u is string => {
                        if (!u) return false;
                        const bv = extractBvid(u);
                        return !(bv && linkedBvids.has(bv));
                      })
                      .map((u) => (
                        <p key={u} style={{ margin: "4px 0" }}>
                          <a href={u} target="_blank" rel="noopener noreferrer">
                            {u}
                          </a>
                        </p>
                      ))}
                  </div>
                ) : null}

                {activePost.continuation && (
                  <div style={{ marginTop: 12 }}>
                    <OperationContinuationCard
                      value={activePost.continuation}
                      channel="dynamics"
                      localId={activePost.id}
                      entityStatus={activePost.status}
                      onRefresh={() => refreshDynamics({ force: true })}
                    />
                  </div>
                )}

                {activePost.receipt != null && (
                  <pre className="code-small" data-dynamic-receipt="" style={{ marginTop: 10 }}>
                    {typeof activePost.receipt === "string"
                      ? activePost.receipt
                      : JSON.stringify(activePost.receipt, null, 2)}
                  </pre>
                )}
              </>
            )}
          </div>
        </div>
      </section>

      {/* 4. Latest Materials */}
      <section className="panel" data-dynamic-materials="" style={{ marginBottom: 16 }}>
        <h2>近期可取材材料</h2>
        <SourceItemsList sources={materials?.sources || []} />
      </section>

      {/* 5. Decisions History */}
      <section className="panel" data-dynamic-decisions="" style={{ marginBottom: 16 }}>
        <h2>动态评估决策历史</h2>
        {decisions.length === 0 ? (
          <p className="hint">暂无动态评估决策记录。</p>
        ) : (
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>编号</th>
                  <th>动作与状态</th>
                  <th>触发方式</th>
                  <th>原因 / 说明</th>
                  <th>续跑</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((dec) => (
                  <tr key={dec.id} data-decision-id={String(dec.id)}>
                    <td>
                      #{dec.id}
                      <span className={styles.cellSub}>{formatDateTime(dec.created_at)}</span>
                    </td>
                    <td>
                      <StatusPill
                        label={`${dec.action} · ${DECISION_STATUS_LABELS[dec.status] || dec.status}`}
                        tone={
                          dec.status === "failed"
                            ? "bad"
                            : dec.status === "completed" || dec.status === "posted"
                              ? "good"
                              : "neutral"
                        }
                      />
                    </td>
                    <td>{dec.manual ? "手动触发" : "自主巡航"}</td>
                    <td>{dec.error || dec.reason || "—"}</td>
                    <td>
                      {dec.continuation ? (
                        <OperationContinuationCard
                          value={dec.continuation}
                          channel="dynamics"
                          localId={dec.id}
                          entityStatus={dec.status}
                          onRefresh={() => refreshDynamics({ force: true })}
                        />
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* 6. Public Memories */}
      <section className="panel" data-dynamic-memories="">
        <h2>公开动态记忆</h2>
        {memories.length === 0 ? (
          <p className="hint">暂无动态记忆条目。</p>
        ) : (
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>允许公开提及</th>
                  <th>类型</th>
                  <th>记忆内容</th>
                </tr>
              </thead>
              <tbody>
                {memories.map((mem) => (
                  <tr key={mem.id} data-memory-id={String(mem.id)}>
                    <td>
                      <label>
                        <input
                          type="checkbox"
                          checked={Boolean(mem.public)}
                          aria-label={`记忆 #${mem.id} 允许公开提及`}
                          onChange={(e) => void handleToggleMemoryPublic(mem, e.target.checked)}
                        />
                      </label>
                    </td>
                    <td>
                      {mem.kind || "memory"}
                      {mem.fictional ? <span className={styles.cellSub}>设定</span> : null}
                    </td>
                    <td>
                      {mem.text}
                      {memoryErrors.get(mem.id) && (
                        <p className="hint err" role="alert">
                          {memoryErrors.get(mem.id)}
                        </p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <MemePickerModal
        open={memePickerOpen}
        selectedAssetId={activePost?.meme_id}
        title="选择动态配图"
        description="从共享表情/插画图库中选择一张图片附带在当前动态草稿中。"
        accept={ACCEPT_ALL_MEMES}
        onClose={() => setMemePickerOpen(false)}
        onSelect={(assetId) => {
          setMemePickerOpen(false);
          if (!activePost) return;
          void submitPostReview(
            activePost,
            { action: "edit", meme_id: assetId },
            "动态配图已更新。"
          );
        }}
      />
    </section>
  );
}
