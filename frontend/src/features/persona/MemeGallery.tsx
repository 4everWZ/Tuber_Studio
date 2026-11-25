import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { StatusPill, type StatusPillTone } from "@/shared";
import { apiDelete, apiGet, apiPut, apiRequest, studioPath } from "@/shared/api/client";
import { emitWorkspaceIssue, errorMessage } from "@/shared/lib/errors";
import {
  MemeMetadataDetails,
  MemePreviewBox,
  SOURCE_KIND_LABELS,
  safeExternalUrl,
  usageScopeLabel,
  type MemeAsset,
  type MemeListResponse,
  type MemeUsageScope,
} from "@/shared/ui/MemePickerModal";
import styles from "./MemeGallery.module.css";
import type {
  MemeHarvesterConfig,
  MemeInboxItem,
  MemeInboxResponse,
  MemeSourcesResponse,
  MemeSubscription,
  MemeSubscriptionKind,
  MemeUploadPayload,
} from "./types";

const MEME_EMOTIONS: ReadonlyArray<[string, string]> = [
  ["idle", "平常"],
  ["cheerful", "开心"],
  ["angry", "生气"],
  ["shy", "害羞"],
  ["confused", "困惑"],
  ["excited", "兴奋"],
];

const MEME_EMOTION_MAP = Object.fromEntries(MEME_EMOTIONS);
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

type GalleryTab = "gallery" | "inbox" | "sources";
type UsageScopeFilter = "all" | MemeUsageScope;

interface BatchItem {
  file: File;
  importId: string;
  done: boolean;
}

interface UploadFailure {
  name: string;
  message: string;
}

interface InboxDraft {
  title: string;
  tagsText: string;
  emotions: string[];
  description: string;
  usage_scope: MemeUsageScope;
}

const DEFAULT_HARVESTER_CONFIG: MemeHarvesterConfig = {
  comment_harvest_enabled: true,
  default_sync_interval_hours: 12,
  meme_inbox_limit_mib: 512,
  meme_inbox_max_items: 200,
};

function readImageBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("图片文件读取失败，请重新选择。"));
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result.split(",", 2)[1] : null;
      if (result) resolve(result);
      else reject(new Error("图片文件未能读取为有效内容。"));
    };
    reader.readAsDataURL(file);
  });
}

function parseTags(raw: string): string[] {
  return raw
    .split(/[,，、\n]/u)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function formatBytesText(value?: number): string {
  if (typeof value !== "number" || value < 0) return "大小未知";
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value} B`;
}

function formatSyncTime(ts?: number | null): string {
  if (!ts) return "尚未同步";
  const d = new Date(ts * 1000);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString("zh-CN", { hour12: false });
}

function sourceStatusTone(status?: string): StatusPillTone {
  if (status === "ok" || status === "synced") return "good";
  if (status === "error" || status === "failed") return "bad";
  if (status === "syncing" || status === "running") return "blue";
  return "neutral";
}

function makeInboxDraft(item: MemeInboxItem): InboxDraft {
  return {
    title: item.title || "",
    tagsText: (item.tags || []).join("、"),
    emotions: item.emotions && item.emotions.length > 0 ? [...item.emotions] : ["idle"],
    description: item.description || "",
    usage_scope: item.usage_scope || "all_channels",
  };
}

function MemeCard({
  asset,
  busy,
  onSaveLabels,
  onToggleEnabled,
  onToggleUsageScope,
  onDelete,
}: {
  asset: MemeAsset;
  busy: boolean;
  onSaveLabels: (asset: MemeAsset, tags: string[], description: string) => Promise<void>;
  onToggleEnabled: (asset: MemeAsset) => Promise<void>;
  onToggleUsageScope: (asset: MemeAsset) => Promise<void>;
  onDelete: (asset: MemeAsset) => Promise<void>;
}) {
  const [tagsInput, setTagsInput] = useState(() => (asset.tags || []).join("、"));
  const [descInput, setDescInput] = useState(() => asset.description || "");

  useEffect(() => {
    setTagsInput((asset.tags || []).join("、"));
    setDescInput(asset.description || "");
  }, [asset.asset_id, asset.tags, asset.description]);

  const effectiveScope: MemeUsageScope = asset.usage_scope || "all_channels";
  const statusLine = `${asset.missing ? "文件缺失" : asset.enabled ? "已启用" : "已禁用"} · ${(asset.emotions || [])
    .map((item) => MEME_EMOTION_MAP[item] || item)
    .join(" / ")}`;
  const sizeLine = `${asset.width ?? "—"} × ${asset.height ?? "—"} · ${
    asset.animated ? `${asset.frames ?? 0} 帧动图` : "静态图"
  } · ${formatBytesText(asset.bytes)}`;

  return (
    <article className="meme-card">
      <MemePreviewBox asset={asset} />
      <h3 className={styles.cardTitle}>{asset.title}</h3>
      <div className={styles.badgeRow}>
        <StatusPill
          tone={effectiveScope === "interaction_only" ? "amber" : "blue"}
          label={usageScopeLabel(effectiveScope)}
          data-state={effectiveScope}
        />
        {asset.source?.kind && (
          <StatusPill
            tone="neutral"
            label={SOURCE_KIND_LABELS[asset.source.kind] || asset.source.kind}
          />
        )}
      </div>
      <p className={`hint ${styles.cardText}`}>{statusLine}</p>
      <p className={`hint ${styles.cardText}`}>{sizeLine}</p>
      <MemeMetadataDetails asset={asset} />
      {asset.tags && asset.tags.length > 0 && <p className={styles.cardTags}>{asset.tags.join(" · ")}</p>}
      {asset.description && <p className={styles.cardText}>{asset.description}</p>}
      <details className="meme-metadata">
        <summary>编辑标签与描述</summary>
        <form
          className={styles.metadataForm}
          onSubmit={(event) => {
            event.preventDefault();
            void onSaveLabels(asset, parseTags(tagsInput), descInput.trim());
          }}
        >
          <label>
            题材标签
            <input
              name="tags"
              placeholder="例如 雨天、撑伞、Q版"
              value={tagsInput}
              onChange={(event) => setTagsInput(event.target.value)}
            />
          </label>
          <label>
            画面描述
            <textarea
              name="description"
              maxLength={400}
              rows={2}
              value={descInput}
              onChange={(event) => setDescInput(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            保存标签
          </button>
        </form>
      </details>
      <div className={`actions ${styles.cardActions}`}>
        <button
          type="button"
          aria-pressed={Boolean(asset.enabled)}
          data-unavailable={String(Boolean(asset.missing))}
          disabled={busy || Boolean(asset.missing)}
          onClick={() => void onToggleEnabled(asset)}
        >
          {asset.enabled ? "禁用" : "启用"}
        </button>
        <button
          type="button"
          data-toggle-usage-scope=""
          disabled={busy}
          onClick={() => void onToggleUsageScope(asset)}
        >
          {effectiveScope === "interaction_only" ? "切换为全渠道" : "切换为仅互动"}
        </button>
        <button
          type="button"
          className="danger"
          data-unavailable={String(Boolean(asset.enabled))}
          title={asset.enabled ? "请先禁用再删除" : "草稿正在引用的图片由服务保护，无法删除"}
          disabled={busy || Boolean(asset.enabled)}
          onClick={() => {
            if (window.confirm(`删除「${asset.title}」？正在被草稿引用的图片无法删除。`)) {
              void onDelete(asset);
            }
          }}
        >
          删除
        </button>
      </div>
    </article>
  );
}

function InboxCard({
  item,
  draft,
  selected,
  busy,
  onToggleSelect,
  onChangeDraft,
  onApprove,
  onReject,
}: {
  item: MemeInboxItem;
  draft: InboxDraft;
  selected: boolean;
  busy: boolean;
  onToggleSelect: (itemId: string, checked: boolean) => void;
  onChangeDraft: (itemId: string, patch: Partial<InboxDraft>) => void;
  onApprove: (item: MemeInboxItem, draft: InboxDraft) => Promise<void>;
  onReject: (item: MemeInboxItem) => Promise<void>;
}) {
  const source = item.source || {};
  const sourceKindLabel = (source.kind && SOURCE_KIND_LABELS[source.kind]) || source.kind || "未知来源";
  const sourceUrl = safeExternalUrl(source.url);
  const info = item.image_info || {};
  const width = item.width ?? info.width;
  const height = item.height ?? info.height;
  const animated = item.animated ?? info.animated;
  const frames = item.frames ?? info.frames;
  const previewSrc = studioPath(
    item.preview_url || `/api/v1/persona/memes/inbox/${encodeURIComponent(item.item_id)}/preview`
  );
  const isPendingVision = item.status === "pending_vision" || item.ai_tagged === false || item.ai_tagged === 0;

  return (
    <article className={`meme-card ${styles.inboxCard}`} data-inbox-item={item.item_id}>
      <div className={styles.inboxCardHeader}>
        <label className={styles.inboxCheckboxLabel}>
          <input
            type="checkbox"
            checked={selected}
            disabled={busy}
            aria-label={`选择待审图片 ${draft.title || item.item_id}`}
            onChange={(event) => onToggleSelect(item.item_id, event.target.checked)}
          />
          <span>批量勾选</span>
        </label>
        <StatusPill
          tone={isPendingVision ? "amber" : "good"}
          label={
            item.status === "pending_vision"
              ? "等待视觉打标 (pending_vision)"
              : isPendingVision
                ? "待人工补标"
                : typeof item.confidence === "number"
                  ? `AI 置信度 ${Math.round(item.confidence * 100)}%`
                  : "AI 已打标"
          }
        />
      </div>

      <div className="meme-preview">
        <img loading="lazy" src={previewSrc} alt={draft.title || "待审表情包"} />
      </div>

      <div className={styles.badgeRow}>
        <StatusPill tone="blue" label={sourceKindLabel} />
        <StatusPill
          tone={draft.usage_scope === "interaction_only" ? "amber" : "neutral"}
          label={usageScopeLabel(draft.usage_scope)}
        />
      </div>

      <p className={`hint ${styles.cardText}`}>
        作者：{source.author || "未提供"}
        {source.author_id ? ` (${source.author_id})` : ""}
        {sourceUrl && (
          <>
            {" · "}
            <a href={sourceUrl} target="_blank" rel="noopener noreferrer">
              查看来源
            </a>
          </>
        )}
      </p>
      <p className={`hint ${styles.cardText}`}>
        {width ?? "—"} × {height ?? "—"} · {animated ? `${frames ?? 0} 帧动图` : "静态图"} ·{" "}
        {formatBytesText(item.bytes)}
      </p>

      <div className={styles.inboxFormFields}>
        <label>
          标题
          <input
            type="text"
            name="inbox_title"
            maxLength={120}
            value={draft.title}
            disabled={busy}
            onChange={(event) => onChangeDraft(item.item_id, { title: event.target.value })}
          />
        </label>
        <label>
          题材标签
          <input
            type="text"
            name="inbox_tags"
            placeholder="逗号或顿号分隔"
            value={draft.tagsText}
            disabled={busy}
            onChange={(event) => onChangeDraft(item.item_id, { tagsText: event.target.value })}
          />
        </label>
        <fieldset className={styles.emotions} style={{ margin: "4px 0" }}>
          <legend>情绪</legend>
          {MEME_EMOTIONS.map(([key, labelText]) => (
            <label key={key}>
              <input
                type="checkbox"
                checked={draft.emotions.includes(key)}
                disabled={busy}
                onChange={(event) => {
                  const checked = event.target.checked;
                  const nextEmotions = checked
                    ? [...new Set([...draft.emotions, key])]
                    : draft.emotions.filter((e) => e !== key);
                  onChangeDraft(item.item_id, { emotions: nextEmotions });
                }}
              />
              {labelText}
            </label>
          ))}
        </fieldset>
        <label>
          画面描述
          <textarea
            rows={2}
            maxLength={400}
            name="inbox_description"
            value={draft.description}
            disabled={busy}
            onChange={(event) => onChangeDraft(item.item_id, { description: event.target.value })}
          />
        </label>
        <label>
          适用范围
          <select
            name="inbox_usage_scope"
            value={draft.usage_scope}
            disabled={busy}
            onChange={(event) =>
              onChangeDraft(item.item_id, { usage_scope: event.target.value as MemeUsageScope })
            }
          >
            <option value="all_channels">全渠道（含动态与视频）</option>
            <option value="interaction_only">仅评论与私信互动</option>
          </select>
        </label>
      </div>

      <div className={`actions ${styles.cardActions}`}>
        <button
          type="button"
          className="primary"
          disabled={busy || !draft.title.trim() || draft.emotions.length === 0}
          onClick={() => void onApprove(item, draft)}
        >
          批准入库
        </button>
        <button
          type="button"
          className="danger"
          disabled={busy}
          onClick={() => void onReject(item)}
        >
          拒绝（拉黑）
        </button>
      </div>
    </article>
  );
}

export function MemeGallery({ onChange }: { onChange?: () => void }) {
  const [activeTab, setActiveTab] = useState<GalleryTab>("gallery");

  // Formal gallery state
  const [items, setItems] = useState<MemeAsset[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [stateText, setStateText] = useState("");
  const [feedback, setFeedback] = useState<{ text: string; error: boolean }>({ text: "", error: false });
  const [failures, setFailures] = useState<UploadFailure[]>([]);
  const [filterText, setFilterText] = useState("");
  const [activeTagFilter, setActiveTagFilter] = useState("");
  const [usageScopeFilter, setUsageScopeFilter] = useState<UsageScopeFilter>("all");

  // Upload form state
  const [batch, setBatch] = useState<BatchItem[]>([]);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadTags, setUploadTags] = useState("");
  const [uploadDescription, setUploadDescription] = useState("");
  const [uploadUsageScope, setUploadUsageScope] = useState<MemeUsageScope>("all_channels");
  const [emotions, setEmotions] = useState<string[]>(["idle"]);
  const [author, setAuthor] = useState("");
  const [licenseName, setLicenseName] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [licenseUrl, setLicenseUrl] = useState("");
  const [sourceRevision, setSourceRevision] = useState("");
  const [sourceNote, setSourceNote] = useState("");
  const [licenseNote, setLicenseNote] = useState("");

  // Inbox state
  const [inboxItems, setInboxItems] = useState<MemeInboxItem[]>([]);
  const [inboxCache, setInboxCache] = useState<MemeInboxResponse["cache"] | null>(null);
  const [inboxPendingVisionCount, setInboxPendingVisionCount] = useState(0);
  const [inboxDrafts, setInboxDrafts] = useState<Record<string, InboxDraft>>({});
  const [selectedInboxIds, setSelectedInboxIds] = useState<string[]>([]);
  const [inboxLoading, setInboxLoading] = useState(false);

  // Sources & Harvester config state
  const [sources, setSources] = useState<MemeSubscription[]>([]);
  const [harvesterConfig, setHarvesterConfig] = useState<MemeHarvesterConfig>(DEFAULT_HARVESTER_CONFIG);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [newSourceKind, setNewSourceKind] = useState<MemeSubscriptionKind>("remote_feed");
  const [newSourceTargetKind, setNewSourceTargetKind] = useState("manifest");
  const [newSourceName, setNewSourceName] = useState("");
  const [newSourceUrl, setNewSourceUrl] = useState("");
  const [newSourceInterval, setNewSourceInterval] = useState(12);
  const [newSourceAutoApprove, setNewSourceAutoApprove] = useState(false);
  const [newSourceLicenseName, setNewSourceLicenseName] = useState("");
  const [newSourceLicenseUrl, setNewSourceLicenseUrl] = useState("");
  const [newSourceLicenseNote, setNewSourceLicenseNote] = useState("");

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const sequenceRef = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++sequenceRef.current;
    setLoading(true);
    setStateText("正在读取图库…");
    try {
      const result = await apiGet<"/api/v1/persona/memes", MemeListResponse>("/api/v1/persona/memes");
      if (seq !== sequenceRef.current) return;
      const listItems = Array.isArray(result.items) ? result.items : [];
      setItems(listItems);
      setStateText(
        `${listItems.length} 张图片 · ${listItems.filter((item) => item.enabled && !item.missing).length} 张可用`
      );
    } catch (error) {
      if (seq !== sequenceRef.current) return;
      const msg = errorMessage(error);
      emitWorkspaceIssue(msg, "#assets");
      setStateText(`图库读取失败：${msg}`);
      setItems([]);
    } finally {
      if (seq === sequenceRef.current) {
        setLoading(false);
      }
    }
  }, []);

  const refreshInbox = useCallback(async (silent = false) => {
    if (!silent) setInboxLoading(true);
    try {
      const res = await apiRequest<MemeInboxResponse>("/api/v1/persona/memes/inbox");
      const list = Array.isArray(res?.items) ? res.items : [];
      setInboxItems(list);
      setInboxCache(res?.cache ?? null);
      setInboxPendingVisionCount(typeof res?.pending_vision_count === "number" ? res.pending_vision_count : 0);
      setInboxDrafts((prev) => {
        const next: Record<string, InboxDraft> = {};
        for (const item of list) {
          next[item.item_id] = prev[item.item_id] ?? makeInboxDraft(item);
        }
        return next;
      });
      setSelectedInboxIds((prev) => prev.filter((id) => list.some((item) => item.item_id === id)));
    } catch (error) {
      if (!silent) {
        const msg = errorMessage(error);
        emitWorkspaceIssue(msg, "#assets");
        setFeedback({ text: `待审收件箱读取失败：${msg}`, error: true });
      }
    } finally {
      if (!silent) setInboxLoading(false);
    }
  }, []);

  const refreshSources = useCallback(async () => {
    setSourcesLoading(true);
    try {
      const [sourcesRes, configRes] = await Promise.all([
        apiRequest<MemeSourcesResponse>("/api/v1/persona/memes/sources"),
        apiRequest<Partial<MemeHarvesterConfig>>("/api/v1/persona/memes/harvester/config").catch(() => null),
      ]);
      const list = Array.isArray(sourcesRes?.items)
        ? sourcesRes.items
        : Array.isArray(sourcesRes?.sources)
          ? sourcesRes.sources
          : [];
      setSources(list);
      const mergedConfig = {
        ...DEFAULT_HARVESTER_CONFIG,
        ...(sourcesRes?.config || {}),
        ...(configRes || {}),
      };
      setHarvesterConfig(mergedConfig);
    } catch (error) {
      const msg = errorMessage(error);
      emitWorkspaceIssue(msg, "#assets");
      setFeedback({ text: `订阅源读取失败：${msg}`, error: true });
    } finally {
      setSourcesLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    void refreshInbox(true);
  }, [refresh, refreshInbox]);

  useEffect(() => {
    if (activeTab === "inbox") {
      void refreshInbox(false);
    } else if (activeTab === "sources") {
      void refreshSources();
    }
  }, [activeTab, refreshInbox, refreshSources]);

  async function runAction(work: () => Promise<void>) {
    if (pending) return;
    setPending(true);
    try {
      await work();
      onChange?.();
    } catch (error) {
      const msg = errorMessage(error);
      emitWorkspaceIssue(msg, "#assets");
      setFeedback({ text: msg, error: true });
    } finally {
      setPending(false);
    }
  }

  function resetUploadForm() {
    setBatch([]);
    setUploadTitle("");
    setUploadTags("");
    setUploadDescription("");
    setUploadUsageScope("all_channels");
    setEmotions(["idle"]);
    setAuthor("");
    setLicenseName("");
    setSourceUrl("");
    setLicenseUrl("");
    setSourceRevision("");
    setSourceNote("");
    setLicenseNote("");
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function handleUploadSubmit(event: FormEvent) {
    event.preventDefault();
    if (!batch.some((item) => !item.done)) return;
    if (emotions.length === 0) {
      setFeedback({ text: "请至少选择一种情绪。", error: true });
      return;
    }

    const common = {
      tags: parseTags(uploadTags),
      description: uploadDescription.trim(),
      usage_scope: uploadUsageScope,
      emotions,
      source: {
        kind: "user_upload" as const,
        author: author.trim(),
        url: sourceUrl.trim(),
        revision: sourceRevision.trim(),
        note: sourceNote.trim(),
      },
      license: {
        name: licenseName.trim(),
        url: licenseUrl.trim(),
        note: licenseNote.trim(),
      },
    };

    await runAction(async () => {
      const nextFailures: UploadFailure[] = [];
      setFailures([]);

      for (const [index, item] of batch.entries()) {
        if (item.done) continue;
        const { file } = item;
        setFeedback({ text: `正在导入第 ${index + 1}／${batch.length} 张`, error: false });
        try {
          if (file.size > MAX_UPLOAD_BYTES) throw new Error("图片超过 8 MiB。");
          const title =
            (batch.length === 1 && uploadTitle.trim()) ||
            file.name.replace(/\.[^.]+$/u, "").trim().slice(0, 120) ||
            "未命名图片";
          const content_base64 = await readImageBase64(file);
          await apiRequest<MemeAsset, MemeUploadPayload>("/api/v1/persona/memes", {
            method: "POST",
            body: {
              ...common,
              title,
              import_id: item.importId,
              content_base64,
            },
          });
          item.done = true;
        } catch (error) {
          const msg = errorMessage(error);
          emitWorkspaceIssue(msg, "#assets");
          nextFailures.push({ name: file.name, message: msg });
        }
      }

      if (batch.some((item) => item.done)) {
        await refresh();
      }
      if (nextFailures.length === 0) {
        const count = batch.length;
        resetUploadForm();
        setFeedback({ text: `已导入 ${count} 张图片。`, error: false });
      } else if (batch.length === 1) {
        setFeedback({ text: nextFailures[0].message, error: true });
      } else {
        setFeedback({
          text: `已导入 ${batch.length - nextFailures.length} 张，${nextFailures.length} 张失败。再次上传时只重试失败项。`,
          error: true,
        });
        setFailures(nextFailures);
      }
    });
  }

  async function handleApproveInboxItem(item: MemeInboxItem, draft: InboxDraft) {
    await runAction(async () => {
      await apiRequest(`/api/v1/persona/memes/inbox/${encodeURIComponent(item.item_id)}/approve`, {
        method: "POST",
        body: {
          title: draft.title.trim() || item.title,
          tags: parseTags(draft.tagsText).slice(0, 12),
          emotions: draft.emotions.length > 0 ? draft.emotions : ["idle"],
          description: draft.description.trim(),
          usage_scope: draft.usage_scope,
        },
      });
      await Promise.all([refreshInbox(true), refresh()]);
      setFeedback({ text: `已批准「${draft.title.trim() || item.title}」进入正式图库。`, error: false });
    });
  }

  async function handleRejectInboxItem(item: MemeInboxItem) {
    await runAction(async () => {
      await apiRequest(`/api/v1/persona/memes/inbox/${encodeURIComponent(item.item_id)}/reject`, {
        method: "POST",
        body: {},
      });
      await refreshInbox(true);
      setFeedback({ text: `已拒绝「${item.title || item.item_id}」并加入拉黑记录。`, error: false });
    });
  }

  async function handleBatchInbox(action: "approve" | "reject") {
    if (selectedInboxIds.length === 0) return;
    await runAction(async () => {
      await apiRequest("/api/v1/persona/memes/inbox/batch", {
        method: "POST",
        body: {
          action,
          item_ids: selectedInboxIds,
        },
      });
      const count = selectedInboxIds.length;
      setSelectedInboxIds([]);
      await Promise.all([refreshInbox(true), action === "approve" ? refresh() : Promise.resolve()]);
      setFeedback({
        text: action === "approve" ? `已批量批准 ${count} 条入库。` : `已批量拒绝并拉黑 ${count} 条候选。`,
        error: false,
      });
    });
  }

  async function handleSaveHarvesterConfig(event: FormEvent) {
    event.preventDefault();
    await runAction(async () => {
      const updated = await apiRequest<MemeHarvesterConfig>("/api/v1/persona/memes/harvester/config", {
        method: "PUT",
        body: harvesterConfig,
      });
      setHarvesterConfig((prev) => ({ ...prev, ...updated }));
      setFeedback({ text: "采集与收件箱全局设置已保存。", error: false });
    });
  }

  async function handleSyncSources(sourceId?: string) {
    await runAction(async () => {
      await apiRequest("/api/v1/persona/memes/sources/sync", {
        method: "POST",
        body: sourceId ? { source_id: sourceId } : {},
      });
      await Promise.all([refreshSources(), refreshInbox(true), refresh()]);
      setFeedback({
        text: sourceId ? "已触发该订阅源同步。" : "已触发全部订阅源同步。",
        error: false,
      });
    });
  }

  async function handleAddSource(event: FormEvent) {
    event.preventDefault();
    if (!newSourceName.trim() || !newSourceUrl.trim()) {
      setFeedback({ text: "请填写订阅源名称与地址 / ID。", error: true });
      return;
    }
    await runAction(async () => {
      const body =
        newSourceKind === "remote_feed"
          ? {
              kind: "remote_feed",
              target_kind: newSourceTargetKind,
              name: newSourceName.trim(),
              url: newSourceUrl.trim(),
              sync_interval_hours: newSourceInterval,
              auto_approve: newSourceAutoApprove,
              license_name: newSourceLicenseName.trim(),
              license_url: newSourceLicenseUrl.trim(),
              license_note: newSourceLicenseNote.trim(),
            }
          : {
              kind: "bilibili_creator",
              target_kind: newSourceTargetKind,
              name: newSourceName.trim(),
              url: newSourceUrl.trim(),
              sync_interval_hours: newSourceInterval,
            };
      await apiRequest("/api/v1/persona/memes/sources", {
        method: "POST",
        body,
      });
      setNewSourceName("");
      setNewSourceUrl("");
      setNewSourceAutoApprove(false);
      setNewSourceLicenseName("");
      setNewSourceLicenseUrl("");
      setNewSourceLicenseNote("");
      await refreshSources();
      setFeedback({ text: "已添加新的表情包订阅源。", error: false });
    });
  }

  const allTagCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of items) {
      for (const tag of item.tags || []) {
        if (!tag) continue;
        counts.set(tag, (counts.get(tag) || 0) + 1);
      }
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-CN"))
      .slice(0, 12);
  }, [items]);

  const normalizedQuery = filterText.trim().normalize("NFKC").toLocaleLowerCase();
  const visibleItems = items.filter((asset) => {
    const scope = asset.usage_scope || "all_channels";
    if (usageScopeFilter !== "all" && scope !== usageScopeFilter) return false;
    if (activeTagFilter && !(asset.tags || []).includes(activeTagFilter)) return false;
    if (!normalizedQuery) return true;
    return [asset.title, asset.description, ...(asset.tags || [])].some((value) =>
      value?.normalize("NFKC").toLocaleLowerCase().includes(normalizedQuery)
    );
  });

  const allInboxSelected = inboxItems.length > 0 && selectedInboxIds.length === inboxItems.length;

  return (
    <article className="panel meme-library">
      <div className="panel-heading">
        <h2>角色图库</h2>
        <button
          data-meme-refresh=""
          type="button"
          disabled={pending || loading || inboxLoading || sourcesLoading}
          onClick={() => {
            if (activeTab === "gallery") void refresh();
            else if (activeTab === "inbox") void refreshInbox(false);
            else void refreshSources();
          }}
        >
          {activeTab === "gallery" ? "刷新图库" : activeTab === "inbox" ? "刷新收件箱" : "刷新订阅源"}
        </button>
      </div>

      <div className={styles.tabBar} role="tablist" aria-label="图库管理分页">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "gallery"}
          className={`${styles.tabBtn} ${activeTab === "gallery" ? styles.tabBtnActive : ""}`.trim()}
          onClick={() => setActiveTab("gallery")}
        >
          正式图库
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "inbox"}
          className={`${styles.tabBtn} ${activeTab === "inbox" ? styles.tabBtnActive : ""}`.trim()}
          onClick={() => setActiveTab("inbox")}
        >
          <span>待审收件箱</span>
          <span className="badge amber" data-inbox-badge="">
            {inboxItems.length}
          </span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "sources"}
          className={`${styles.tabBtn} ${activeTab === "sources" ? styles.tabBtnActive : ""}`.trim()}
          onClick={() => setActiveTab("sources")}
        >
          订阅源管理
        </button>
      </div>

      <p data-meme-feedback="" className={feedback.error ? "hint error-text" : "hint"} role="status">
        {feedback.text}
      </p>

      {activeTab === "gallery" && (
        <div role="tabpanel" aria-label="正式图库">
          <details className={`meme-upload ${styles.uploadDetails}`}>
            <summary>导入图片</summary>
            <form data-meme-upload="" onSubmit={(event) => void handleUploadSubmit(event)}>
              <fieldset data-meme-upload-fields="" className={styles.fields} disabled={pending}>
                <div className="form-grid">
                  <label>
                    图片文件
                    <input
                      ref={fileInputRef}
                      name="file"
                      type="file"
                      multiple
                      accept="image/png,image/jpeg,image/gif,image/webp,.png,.jpg,.jpeg,.gif,.webp"
                      required
                      onChange={(event) => {
                        const selected = Array.from(event.target.files || [], (file) => ({
                          file,
                          importId: crypto.randomUUID(),
                          done: false,
                        }));
                        setBatch(selected);
                        setFailures([]);
                        setFeedback({ text: "", error: false });
                      }}
                    />
                  </label>
                  <label>
                    名称（单张选填）
                    <input
                      name="title"
                      maxLength={120}
                      disabled={batch.length !== 1}
                      value={uploadTitle}
                      onChange={(event) => setUploadTitle(event.target.value)}
                    />
                  </label>
                </div>
                <p className="hint">
                  可多选 PNG、JPEG、GIF 或 WebP，每张最多 8 MiB、4096 像素宽高和 300 帧。批量导入采用文件名，以下标签、作者与许可应用于这批图片。
                </p>
                <div className="form-grid">
                  <label>
                    题材标签
                    <input
                      name="tags"
                      placeholder="例如 雨天、撑伞、Q版"
                      value={uploadTags}
                      onChange={(event) => setUploadTags(event.target.value)}
                    />
                  </label>
                  <label>
                    适用范围
                    <select
                      name="usage_scope"
                      value={uploadUsageScope}
                      onChange={(event) => setUploadUsageScope(event.target.value as MemeUsageScope)}
                    >
                      <option value="all_channels">全渠道（含动态与视频）</option>
                      <option value="interaction_only">仅评论与私信互动</option>
                    </select>
                  </label>
                </div>
                <label>
                  画面描述
                  <textarea
                    name="description"
                    maxLength={400}
                    rows={2}
                    value={uploadDescription}
                    onChange={(event) => setUploadDescription(event.target.value)}
                  />
                </label>
                <fieldset className={styles.emotions}>
                  <legend>情绪（至少一项）</legend>
                  {MEME_EMOTIONS.map(([key, labelText]) => {
                    const checked = emotions.includes(key);
                    return (
                      <label key={key}>
                        <input
                          type="checkbox"
                          name="emotion"
                          value={key}
                          checked={checked}
                          onChange={(event) => {
                            const isChecked = event.target.checked;
                            setEmotions((prev) =>
                              isChecked ? [...new Set([...prev, key])] : prev.filter((item) => item !== key)
                            );
                          }}
                        />
                        {labelText}
                      </label>
                    );
                  })}
                </fieldset>
                <div className="form-grid">
                  <label>
                    来源作者
                    <input
                      name="author"
                      maxLength={300}
                      required
                      placeholder="实际作者或权利人"
                      value={author}
                      onChange={(event) => setAuthor(event.target.value)}
                    />
                  </label>
                  <label>
                    许可名称
                    <input
                      name="license_name"
                      maxLength={300}
                      required
                      placeholder="实际许可或授权名称"
                      value={licenseName}
                      onChange={(event) => setLicenseName(event.target.value)}
                    />
                  </label>
                </div>
                <div className="form-grid">
                  <label>
                    来源链接（可选）
                    <input
                      name="source_url"
                      type="url"
                      value={sourceUrl}
                      onChange={(event) => setSourceUrl(event.target.value)}
                    />
                  </label>
                  <label>
                    许可链接（可选）
                    <input
                      name="license_url"
                      type="url"
                      value={licenseUrl}
                      onChange={(event) => setLicenseUrl(event.target.value)}
                    />
                  </label>
                </div>
                <label>
                  来源版本（可选）
                  <input
                    name="source_revision"
                    maxLength={160}
                    placeholder="提交 SHA 或素材包版本"
                    value={sourceRevision}
                    onChange={(event) => setSourceRevision(event.target.value)}
                  />
                </label>
                <label>
                  来源备注（可选）
                  <textarea
                    name="source_note"
                    rows={2}
                    maxLength={2000}
                    value={sourceNote}
                    onChange={(event) => setSourceNote(event.target.value)}
                  />
                </label>
                <label>
                  许可备注（可选）
                  <textarea
                    name="license_note"
                    rows={2}
                    maxLength={2000}
                    value={licenseNote}
                    onChange={(event) => setLicenseNote(event.target.value)}
                  />
                </label>
                <div className="actions" style={{ marginTop: 10 }}>
                  <button type="submit" className="primary">
                    上传到图库
                  </button>
                </div>
              </fieldset>
            </form>
          </details>

          <ul data-meme-failures="" className={styles.failuresList} hidden={failures.length === 0}>
            {failures.map((item, idx) => (
              <li key={`${item.name}-${idx}`}>{`${item.name} · ${item.message}`}</li>
            ))}
          </ul>

          <div className={styles.filterBar}>
            <div className={styles.filterRow}>
              <label style={{ marginTop: 0 }}>
                搜索图库
                <input
                  type="search"
                  data-meme-filter=""
                  className={styles.filterInput}
                  placeholder="名称、标签或画面描述"
                  value={filterText}
                  onChange={(event) => setFilterText(event.target.value)}
                />
              </label>
              <label style={{ marginTop: 0 }}>
                适用范围筛选
                <select
                  aria-label="适用范围筛选"
                  data-usage-scope-filter=""
                  value={usageScopeFilter}
                  onChange={(event) => setUsageScopeFilter(event.target.value as UsageScopeFilter)}
                >
                  <option value="all">全部范围</option>
                  <option value="all_channels">全渠道</option>
                  <option value="interaction_only">仅互动</option>
                </select>
              </label>
            </div>

            {allTagCounts.length > 0 && (
              <div className={styles.tagPills} role="group" aria-label="快捷标签筛选">
                <span className="hint">快捷标签：</span>
                {activeTagFilter && (
                  <button
                    type="button"
                    className={styles.pillBtn}
                    onClick={() => setActiveTagFilter("")}
                  >
                    全部标签
                  </button>
                )}
                {allTagCounts.map(([tag, count]) => {
                  const selected = activeTagFilter === tag;
                  return (
                    <button
                      key={tag}
                      type="button"
                      aria-pressed={selected}
                      className={`${styles.pillBtn} ${selected ? styles.pillBtnActive : ""}`.trim()}
                      onClick={() => setActiveTagFilter(selected ? "" : tag)}
                    >
                      {tag} ({count})
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <p data-meme-state="" className="hint" role="status">
            {stateText}
          </p>

          <div data-meme-grid="" className="meme-grid">
            {visibleItems.length === 0 ? (
              <p className="hint">
                {normalizedQuery || activeTagFilter || usageScopeFilter !== "all"
                  ? "没有匹配的图片。"
                  : "图库还没有图片。"}
              </p>
            ) : (
              visibleItems.map((asset) => (
                <MemeCard
                  key={asset.asset_id}
                  asset={asset}
                  busy={pending || loading}
                  onSaveLabels={async (target, tags, description) => {
                    await runAction(async () => {
                      await apiPut<
                        "/api/v1/persona/memes/{asset_id}/labels",
                        MemeAsset,
                        { tags: string[]; description: string }
                      >("/api/v1/persona/memes/{asset_id}/labels", {
                        pathParams: { asset_id: target.asset_id },
                        body: { tags, description },
                      });
                      await refresh();
                      setFeedback({ text: `已保存「${target.title}」的标签与描述。`, error: false });
                    });
                  }}
                  onToggleEnabled={async (target) => {
                    await runAction(async () => {
                      await apiPut<"/api/v1/persona/memes/{asset_id}", MemeAsset, { enabled: boolean }>(
                        "/api/v1/persona/memes/{asset_id}",
                        {
                          pathParams: { asset_id: target.asset_id },
                          body: { enabled: !target.enabled },
                        }
                      );
                      await refresh();
                      setFeedback({
                        text: target.enabled ? "已禁用此表情包。" : "已启用此表情包。",
                        error: false,
                      });
                    });
                  }}
                  onToggleUsageScope={async (target) => {
                    const nextScope: MemeUsageScope =
                      (target.usage_scope || "all_channels") === "interaction_only"
                        ? "all_channels"
                        : "interaction_only";
                    await runAction(async () => {
                      await apiRequest<MemeAsset>(`/api/v1/persona/memes/${encodeURIComponent(target.asset_id)}`, {
                        method: "PUT",
                        body: { usage_scope: nextScope },
                      });
                      await refresh();
                      setFeedback({
                        text: `已将「${target.title}」设为${usageScopeLabel(nextScope)}。`,
                        error: false,
                      });
                    });
                  }}
                  onDelete={async (target) => {
                    await runAction(async () => {
                      await apiDelete("/api/v1/persona/memes/{asset_id}", {
                        pathParams: { asset_id: target.asset_id },
                      });
                      await refresh();
                      setFeedback({ text: "图片已从图库删除。", error: false });
                    });
                  }}
                />
              ))
            )}
          </div>
        </div>
      )}

      {activeTab === "inbox" && (
        <div role="tabpanel" aria-label="待审收件箱">
          <p className="hint" data-inbox-summary="">
            收件箱占用：{formatBytesText(inboxCache?.bytes ?? 0)} /{" "}
            {inboxCache?.unlimited || inboxCache?.limit_bytes === 0
              ? "不限"
              : typeof inboxCache?.limit_bytes === "number"
                ? formatBytesText(inboxCache.limit_bytes)
                : "默认上限"}{" "}
            · 待审条数：{inboxItems.length} /{" "}
            {inboxCache?.max_items === 0
              ? "不限"
              : typeof inboxCache?.max_items === "number"
                ? `${inboxCache.max_items} 条`
                : "默认上限"}
            {inboxPendingVisionCount > 0 ? ` · 等待视觉打标 ${inboxPendingVisionCount} 条` : ""}
          </p>

          {inboxItems.length > 0 && (
            <div className={styles.batchToolbar} data-inbox-batch-bar="">
              <button
                type="button"
                disabled={pending || inboxLoading}
                onClick={() =>
                  setSelectedInboxIds(allInboxSelected ? [] : inboxItems.map((item) => item.item_id))
                }
              >
                {allInboxSelected ? "取消全选" : "全选"}
              </button>
              <span className="hint">已选 {selectedInboxIds.length} 项</span>
              {selectedInboxIds.length > 0 && (
                <>
                  <button
                    type="button"
                    className="primary"
                    disabled={pending || inboxLoading}
                    onClick={() => void handleBatchInbox("approve")}
                  >
                    批量批准
                  </button>
                  <button
                    type="button"
                    className="danger"
                    disabled={pending || inboxLoading}
                    onClick={() => void handleBatchInbox("reject")}
                  >
                    批量拒绝
                  </button>
                </>
              )}
            </div>
          )}

          {inboxLoading && inboxItems.length === 0 ? (
            <p className="hint">正在读取待审收件箱…</p>
          ) : inboxItems.length === 0 ? (
            <p className="hint">待审收件箱暂无候选图片。</p>
          ) : (
            <div className={styles.inboxGrid} data-inbox-grid="">
              {inboxItems.map((item) => {
                const draft = inboxDrafts[item.item_id] ?? makeInboxDraft(item);
                return (
                  <InboxCard
                    key={item.item_id}
                    item={item}
                    draft={draft}
                    selected={selectedInboxIds.includes(item.item_id)}
                    busy={pending || inboxLoading}
                    onToggleSelect={(id, checked) =>
                      setSelectedInboxIds((prev) =>
                        checked ? [...new Set([...prev, id])] : prev.filter((existing) => existing !== id)
                      )
                    }
                    onChangeDraft={(id, patch) =>
                      setInboxDrafts((prev) => ({
                        ...prev,
                        [id]: { ...(prev[id] ?? makeInboxDraft(item)), ...patch },
                      }))
                    }
                    onApprove={handleApproveInboxItem}
                    onReject={handleRejectInboxItem}
                  />
                );
              })}
            </div>
          )}
        </div>
      )}

      {activeTab === "sources" && (
        <div role="tabpanel" aria-label="订阅源管理" className={styles.sourcesSection}>
          <section className={styles.subSection}>
            <div className="panel-heading">
              <h3>全局采集与收件箱策略</h3>
              <button
                type="button"
                disabled={pending || sourcesLoading}
                onClick={() => void handleSyncSources()}
              >
                立即全部同步
              </button>
            </div>
            <form onSubmit={(event) => void handleSaveHarvesterConfig(event)}>
              <label className="check-field">
                <input
                  type="checkbox"
                  name="comment_harvest_enabled"
                  checked={harvesterConfig.comment_harvest_enabled}
                  disabled={pending}
                  onChange={(event) =>
                    setHarvesterConfig((prev) => ({
                      ...prev,
                      comment_harvest_enabled: event.target.checked,
                    }))
                  }
                />
                <span>B 站评论区顺手捡图（仅复用已读取评论，零额外爬取）</span>
              </label>
              <div className="form-grid" style={{ marginTop: 10 }}>
                <label>
                  默认同步间隔（小时）
                  <input
                    type="number"
                    name="default_sync_interval_hours"
                    min={1}
                    max={168}
                    value={harvesterConfig.default_sync_interval_hours}
                    disabled={pending}
                    onChange={(event) =>
                      setHarvesterConfig((prev) => ({
                        ...prev,
                        default_sync_interval_hours: Math.max(1, Number(event.target.value) || 12),
                      }))
                    }
                  />
                </label>
                <label>
                  收件箱最大缓存（MiB，0 表示不限）
                  <input
                    type="number"
                    name="meme_inbox_limit_mib"
                    min={0}
                    max={8192}
                    value={harvesterConfig.meme_inbox_limit_mib}
                    disabled={pending}
                    onChange={(event) =>
                      setHarvesterConfig((prev) => ({
                        ...prev,
                        meme_inbox_limit_mib: Math.max(0, Number(event.target.value) || 0),
                      }))
                    }
                  />
                </label>
                <label>
                  收件箱最大条数（0 表示不限）
                  <input
                    type="number"
                    name="meme_inbox_max_items"
                    min={0}
                    max={10000}
                    value={harvesterConfig.meme_inbox_max_items}
                    disabled={pending}
                    onChange={(event) =>
                      setHarvesterConfig((prev) => ({
                        ...prev,
                        meme_inbox_max_items: Math.max(0, Number(event.target.value) || 0),
                      }))
                    }
                  />
                </label>
              </div>
              <div className="actions" style={{ marginTop: 10 }}>
                <button type="submit" className="primary" disabled={pending}>
                  保存设置
                </button>
              </div>
            </form>
          </section>

          <section className={styles.subSection}>
            <h3>添加订阅源</h3>
            <form onSubmit={(event) => void handleAddSource(event)} data-add-source-form="">
              <div className="form-grid">
                <label>
                  订阅源类型
                  <select
                    name="source_kind"
                    value={newSourceKind}
                    disabled={pending}
                    onChange={(event) => {
                      const nextKind = event.target.value as MemeSubscriptionKind;
                      setNewSourceKind(nextKind);
                      setNewSourceTargetKind(nextKind === "remote_feed" ? "manifest" : "space_dynamic");
                      if (nextKind !== "remote_feed") {
                        setNewSourceAutoApprove(false);
                      }
                    }}
                  >
                    <option value="remote_feed">远程订阅源（GitHub / 远程清单）</option>
                    <option value="bilibili_creator">B 站二创追踪</option>
                  </select>
                </label>
                <label>
                  目标形态
                  <select
                    name="target_kind"
                    value={newSourceTargetKind}
                    disabled={pending}
                    onChange={(event) => setNewSourceTargetKind(event.target.value)}
                  >
                    {newSourceKind === "remote_feed" ? (
                      <>
                        <option value="manifest">标准清单 (manifest)</option>
                        <option value="github_dir">GitHub 目录 (github_dir)</option>
                      </>
                    ) : (
                      <>
                        <option value="space_dynamic">UP 主空间动态 (space_dynamic)</option>
                        <option value="article">专栏文章 (article)</option>
                        <option value="readlist">文集 (readlist)</option>
                      </>
                    )}
                  </select>
                </label>
              </div>
              <div className="form-grid">
                <label>
                  订阅源名称
                  <input
                    type="text"
                    name="source_name"
                    required
                    placeholder="例如 Aqua 二创图包"
                    value={newSourceName}
                    disabled={pending}
                    onChange={(event) => setNewSourceName(event.target.value)}
                  />
                </label>
                <label>
                  {newSourceKind === "remote_feed" ? "清单或 GitHub 目录 URL" : "UP 主链接 / UID / cv / rl"}
                  <input
                    type="text"
                    name="source_url"
                    required
                    placeholder={
                      newSourceKind === "remote_feed"
                        ? "https://github.com/owner/repo/tree/main/memes"
                        : "https://space.bilibili.com/12345 或 cv12345 / rl12345"
                    }
                    value={newSourceUrl}
                    disabled={pending}
                    onChange={(event) => setNewSourceUrl(event.target.value)}
                  />
                </label>
              </div>
              <div className="form-grid">
                <label>
                  同步间隔（小时）
                  <input
                    type="number"
                    name="sync_interval_hours"
                    min={1}
                    max={168}
                    value={newSourceInterval}
                    disabled={pending}
                    onChange={(event) => setNewSourceInterval(Math.max(1, Number(event.target.value) || 12))}
                  />
                </label>
                {newSourceKind === "remote_feed" && (
                  <label>
                    许可名称（可选）
                    <input
                      type="text"
                      name="source_license_name"
                      placeholder="例如 CC-BY-4.0"
                      value={newSourceLicenseName}
                      disabled={pending}
                      onChange={(event) => setNewSourceLicenseName(event.target.value)}
                    />
                  </label>
                )}
              </div>
              {newSourceKind === "remote_feed" && (
                <>
                  <div className="form-grid">
                    <label>
                      许可链接（可选）
                      <input
                        type="url"
                        name="source_license_url"
                        value={newSourceLicenseUrl}
                        disabled={pending}
                        onChange={(event) => setNewSourceLicenseUrl(event.target.value)}
                      />
                    </label>
                    <label>
                      许可备注（可选）
                      <input
                        type="text"
                        name="source_license_note"
                        value={newSourceLicenseNote}
                        disabled={pending}
                        onChange={(event) => setNewSourceLicenseNote(event.target.value)}
                      />
                    </label>
                  </div>
                  <label className="check-field" style={{ marginTop: 8 }}>
                    <input
                      type="checkbox"
                      name="auto_approve"
                      checked={newSourceAutoApprove}
                      disabled={pending}
                      onChange={(event) => setNewSourceAutoApprove(event.target.checked)}
                    />
                    <span>自动入库（仅清单自带完整标题与情绪时跳过人工审核）</span>
                  </label>
                </>
              )}
              <div className="actions" style={{ marginTop: 10 }}>
                <button type="submit" className="primary" disabled={pending}>
                  添加订阅源
                </button>
              </div>
            </form>
          </section>

          <section className={styles.subSection}>
            <h3>已配置订阅源 ({sources.length})</h3>
            {sourcesLoading && sources.length === 0 ? (
              <p className="hint">正在读取订阅源…</p>
            ) : sources.length === 0 ? (
              <p className="hint">暂未配置远程订阅源或二创追踪。</p>
            ) : (
              <div className={styles.sourceList} data-sources-list="">
                {sources.map((src) => {
                  const safeUrl = safeExternalUrl(src.url);
                  return (
                    <article key={src.source_id} className={styles.sourceCard} data-source-id={src.source_id}>
                      <div className={styles.sourceCardHeader}>
                        <div className={styles.badgeRow} style={{ margin: 0 }}>
                          <strong>{src.name}</strong>
                          <StatusPill
                            tone="blue"
                            label={SOURCE_KIND_LABELS[src.kind] || src.kind}
                          />
                          <StatusPill tone="neutral" label={src.target_kind} />
                          {src.kind === "remote_feed" && src.auto_approve && (
                            <StatusPill tone="good" label="自动入库" />
                          )}
                          <StatusPill
                            tone={sourceStatusTone(src.last_status)}
                            label={src.last_status || "idle"}
                          />
                        </div>
                        <div className="actions">
                          <button
                            type="button"
                            disabled={pending}
                            onClick={() =>
                              void runAction(async () => {
                                await apiRequest(
                                  `/api/v1/persona/memes/sources/${encodeURIComponent(src.source_id)}`,
                                  {
                                    method: "PATCH",
                                    body: { enabled: !src.enabled },
                                  }
                                );
                                await refreshSources();
                                setFeedback({
                                  text: src.enabled ? `已禁用订阅源「${src.name}」。` : `已启用订阅源「${src.name}」。`,
                                  error: false,
                                });
                              })
                            }
                          >
                            {src.enabled ? "禁用" : "启用"}
                          </button>
                          <button
                            type="button"
                            disabled={pending || !src.enabled}
                            onClick={() => void handleSyncSources(src.source_id)}
                          >
                            立即同步
                          </button>
                          <button
                            type="button"
                            className="danger"
                            disabled={pending}
                            onClick={() => {
                              if (window.confirm(`确认删除订阅源「${src.name}」？`)) {
                                void runAction(async () => {
                                  await apiRequest(
                                    `/api/v1/persona/memes/sources/${encodeURIComponent(src.source_id)}`,
                                    { method: "DELETE" }
                                  );
                                  await refreshSources();
                                  setFeedback({ text: `已删除订阅源「${src.name}」。`, error: false });
                                });
                              }
                            }}
                          >
                            删除
                          </button>
                        </div>
                      </div>
                      <p className="hint" style={{ margin: 0 }}>
                        {safeUrl ? (
                          <a href={safeUrl} target="_blank" rel="noopener noreferrer">
                            {src.url}
                          </a>
                        ) : (
                          <span>{src.url}</span>
                        )}
                        {" · "}间隔 {src.sync_interval_hours}h · 上次同步：{formatSyncTime(src.last_synced_at)}
                      </p>
                      {src.last_error && (
                        <p className="hint error-text" style={{ margin: 0 }}>
                          同步错误：{src.last_error}
                        </p>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      )}
    </article>
  );
}
