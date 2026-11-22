import { useState, type FormEvent } from "react";
import { apiGet, apiPost } from "@/shared/api/client";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import styles from "./operations.module.css";

interface PopularVideoItem {
  bvid: string;
  title: string;
  owner?: string | { name?: string };
}

interface ImportPreviewItem {
  comment_id: string;
  thread_id?: string | null;
  parent_comment_id?: string | null;
  platform_user_id?: string | null;
  display_name?: string | null;
  content: string;
  like_count?: number;
  created_at?: string | number | null;
}

interface ImportPayload {
  video_id: string;
  items?: unknown[];
  csv_text?: string;
  field_mapping?: Record<string, string>;
}

const MAPPING_TARGETS = new Set([
  "comment_id",
  "platform_user_id",
  "display_name",
  "content",
  "like_count",
  "created_at",
  "thread_id",
  "parent_comment_id",
]);

function buildImportPayload(
  videoIdRaw: string,
  commentsRaw: string,
  mappingRaw: string
): ImportPayload {
  const video_id = videoIdRaw.trim();
  const raw = commentsRaw.trim();
  if (!video_id || !raw) {
    throw new Error("请填写视频标识和评论数据");
  }
  const payload: ImportPayload = { video_id };
  if (raw.startsWith("[") || raw.startsWith("{")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      throw new Error(`评论 JSON 格式错误：${errorMessage(err)}`);
    }
    const items = Array.isArray(parsed)
      ? parsed
      : typeof parsed === "object" && parsed !== null
        ? (parsed as { items?: unknown }).items
        : undefined;
    if (!Array.isArray(items)) {
      throw new Error("JSON 必须是评论数组，或含 items 数组的对象");
    }
    payload.items = items;
  } else {
    payload.csv_text = raw;
  }

  const mapping = mappingRaw.trim();
  if (mapping) {
    let parsedMap: unknown;
    try {
      parsedMap = JSON.parse(mapping);
    } catch (err) {
      throw new Error(`字段映射 JSON 格式错误：${errorMessage(err)}`);
    }
    if (!parsedMap || typeof parsedMap !== "object" || Array.isArray(parsedMap)) {
      throw new Error("字段映射必须是 JSON 对象");
    }
    for (const [target, source] of Object.entries(parsedMap as Record<string, unknown>)) {
      if (!MAPPING_TARGETS.has(target)) {
        throw new Error(`不支持的目标字段：${target}`);
      }
      if (typeof source !== "string" || !source.trim()) {
        throw new Error(`${target} 的来源字段名必须是非空字符串`);
      }
    }
    payload.field_mapping = parsedMap as Record<string, string>;
  }
  return payload;
}

export interface CommentsImportFormProps {
  sourcesOpen: boolean;
  onSourcesOpenChange: (open: boolean) => void;
  onScopeChanged: (videoId: string) => Promise<void> | void;
}

export function CommentsImportForm({
  sourcesOpen,
  onSourcesOpenChange,
  onScopeChanged,
}: CommentsImportFormProps) {
  // Fetch form state
  const [commentVideo, setCommentVideo] = useState("");
  const [commentPages, setCommentPages] = useState("1");
  const [fetchState, setFetchState] = useState("");
  const [fetching, setFetching] = useState(false);

  // Popular videos state
  const [popularOpen, setPopularOpen] = useState(false);
  const [popularVideos, setPopularVideos] = useState<PopularVideoItem[]>([]);
  const [popularBusy, setPopularBusy] = useState(false);

  // Import form state
  const [importOpen, setImportOpen] = useState(false);
  const [importVideo, setImportVideo] = useState("");
  const [importComments, setImportComments] = useState("");
  const [importMapping, setImportMapping] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [previewState, setPreviewState] = useState("");
  const [previewItems, setPreviewItems] = useState<ImportPreviewItem[] | null>(null);
  const [previewSignature, setPreviewSignature] = useState<string | null>(null);

  function invalidatePreview() {
    if (previewSignature !== null || previewItems !== null || previewState) {
      setPreviewSignature(null);
      setPreviewItems(null);
      setPreviewState("");
    }
  }

  async function handleFetchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (fetching) return;
    setFetching(true);
    setFetchState("正在读取公开评论…");
    setPopularOpen(false);
    try {
      const payload = {
        video: commentVideo.trim(),
        max_pages: Number(commentPages) || 1,
      };
      const result = await apiPost<
        "/api/v1/operations/comments/fetch",
        { count?: number; imported?: number; video_id?: string },
        typeof payload
      >("/api/v1/operations/comments/fetch", { body: payload });
      setFetchState(
        `已读取 ${result.count ?? result.imported ?? 0} 条评论，勾选后即可生成回复。`
      );
      await onScopeChanged(result.video_id || payload.video);
    } catch (err) {
      const msg = errorMessage(err);
      setFetchState(`读取失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setFetching(false);
    }
  }

  async function handleTogglePopular() {
    if (popularBusy) return;
    if (popularOpen) {
      setPopularOpen(false);
      return;
    }
    setPopularBusy(true);
    try {
      const data = await apiGet<
        "/api/v1/operations/comments/videos",
        PopularVideoItem[] | { items?: PopularVideoItem[] }
      >("/api/v1/operations/comments/videos");
      const list = Array.isArray(data) ? data : data.items || [];
      setPopularVideos(list.slice(0, 12));
      setPopularOpen(true);
    } catch (err) {
      emitNotice(errorMessage(err), true);
    } finally {
      setPopularBusy(false);
    }
  }

  async function handlePreviewImport() {
    if (importBusy) return;
    invalidatePreview();
    setImportBusy(true);
    setPreviewState("正在规范化并预览数据…");
    try {
      const payload = buildImportPayload(importVideo, importComments, importMapping);
      const sig = JSON.stringify(payload);
      const result = await apiPost<
        "/api/v1/operations/comments/import/preview",
        { total?: number; items?: ImportPreviewItem[] },
        ImportPayload
      >("/api/v1/operations/comments/import/preview", { body: payload });

      let stillCurrent = false;
      try {
        stillCurrent =
          JSON.stringify(buildImportPayload(importVideo, importComments, importMapping)) === sig;
      } catch {
        stillCurrent = false;
      }
      if (!stillCurrent) {
        invalidatePreview();
        return;
      }
      const items = Array.isArray(result.items) ? result.items : [];
      setPreviewItems(items);
      setPreviewSignature(sig);
      setPreviewState(
        `共 ${result.total ?? items.length} 条，预览前 ${Math.min(items.length, 10)} 条；尚未写入。`
      );
    } catch (err) {
      const msg = errorMessage(err);
      setPreviewState(`预览失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setImportBusy(false);
    }
  }

  async function handleImportSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (importBusy) return;
    setImportBusy(true);
    try {
      const payload = buildImportPayload(importVideo, importComments, importMapping);
      const result = await apiPost<
        "/api/v1/operations/comments/import",
        { imported?: number; duplicates?: number | null; video_id?: string },
        ImportPayload
      >("/api/v1/operations/comments/import", { body: payload });
      setPreviewSignature(null);
      setPreviewItems(null);
      setPreviewState(
        `已导入 ${result.imported ?? 0} 条${result.duplicates != null ? `，已有 ${result.duplicates} 条` : ""}。勾选评论即可生成回复。`
      );
      emitNotice(`导入 ${result.imported ?? 0} 条评论。`);
      setImportOpen(false);
      await onScopeChanged(result.video_id || payload.video_id);
    } catch (err) {
      const msg = errorMessage(err);
      setPreviewState(`导入失败：${msg}`);
      emitNotice(msg, true);
    } finally {
      setImportBusy(false);
    }
  }

  return (
    <details
      id="comment-sources"
      open={sourcesOpen}
      onToggle={(e) => onSourcesOpenChange(e.currentTarget.open)}
      style={{ marginBottom: 14, borderTop: "1px solid var(--line)", paddingBlock: 10 }}
    >
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>读取或导入评论</summary>
      <div id="comment-acquisition" data-workspace-anchor="">
        <form
          id="fetch-comments-form"
          className={styles.fetchRow}
          style={{ marginTop: 10 }}
          onSubmit={handleFetchSubmit}
        >
          <label>
            视频 BV 号或链接
            <input
              id="comment-video"
              placeholder="BV… 或视频链接"
              required
              value={commentVideo}
              onChange={(e) => setCommentVideo(e.target.value)}
            />
          </label>
          <label>
            读取页数
            <select
              id="comment-pages"
              value={commentPages}
              onChange={(e) => setCommentPages(e.target.value)}
            >
              <option value="1">1 页</option>
              <option value="2">2 页</option>
              <option value="3">3 页</option>
              <option value="5">5 页</option>
            </select>
          </label>
          <button
            id="submit-fetch"
            className="primary"
            type="submit"
            disabled={fetching}
            aria-busy={fetching}
          >
            读取评论
          </button>
        </form>

        <p id="fetch-state" className="hint" role="status">
          {fetchState}
        </p>

        <div className="actions">
          <button
            id="refresh-videos"
            type="button"
            disabled={popularBusy}
            onClick={() => void handleTogglePopular()}
          >
            {popularOpen ? "收起热门视频" : "热门视频"}
          </button>
          <span id="comment-fetch-mode" className="hint">
            公开评论
          </span>
        </div>

        {popularOpen && (
          <div id="popular-videos" className={styles.videoList}>
            {popularVideos.length === 0 ? (
              <p className="muted">暂无可读取的热门视频，可直接输入视频链接。</p>
            ) : (
              popularVideos.map((video) => {
                const ownerName =
                  typeof video.owner === "object"
                    ? video.owner?.name || ""
                    : video.owner || "";
                return (
                  <button
                    key={video.bvid}
                    type="button"
                    onClick={() => {
                      setCommentVideo(video.bvid);
                      setFetchState(
                        `已选择：${video.title}。点击「读取评论」获取内容。`
                      );
                      setPopularOpen(false);
                    }}
                  >
                    {video.title}
                    <small>
                      {ownerName} · {video.bvid}
                    </small>
                  </button>
                );
              })
            )}
          </div>
        )}

        <details
          id="comment-import"
          data-workspace-anchor=""
          open={importOpen}
          onToggle={(e) => setImportOpen(e.currentTarget.open)}
          style={{ marginTop: 10 }}
        >
          <summary style={{ cursor: "pointer", color: "var(--muted)" }}>
            导入 JSON / CSV
          </summary>
          <form id="import-comments-form" onSubmit={handleImportSubmit}>
            <label>
              视频标识
              <input
                id="import-video"
                placeholder="BV… 或 import-开头的本地批次"
                required
                value={importVideo}
                onChange={(e) => {
                  setImportVideo(e.target.value);
                  invalidatePreview();
                }}
              />
            </label>
            <label>
              评论数据
              <textarea
                id="import-comments"
                rows={4}
                required
                placeholder='[{"comment_id":"1","display_name":"观众","content":"今天吃白饭了吗？"}]'
                value={importComments}
                onChange={(e) => {
                  setImportComments(e.target.value);
                  invalidatePreview();
                }}
              />
            </label>
            <label>
              字段映射（可选 JSON）
              <input
                id="import-mapping"
                placeholder='{"content":"text"}'
                value={importMapping}
                onChange={(e) => {
                  setImportMapping(e.target.value);
                  invalidatePreview();
                }}
              />
            </label>
            <div className="actions" style={{ marginTop: 8 }}>
              <button
                id="submit-import"
                className="primary"
                type="submit"
                disabled={importBusy}
              >
                导入评论
              </button>
              <button
                id="preview-import"
                type="button"
                disabled={importBusy}
                onClick={() => void handlePreviewImport()}
              >
                预览
              </button>
              <span id="import-preview-state" className="hint" role="status">
                {previewState}
              </span>
            </div>

            {previewItems !== null && (
              <div id="import-preview" className="table-scroll" style={{ marginTop: 8 }}>
                <table>
                  <thead>
                    <tr>
                      <th>评论 ID</th>
                      <th>楼 ID</th>
                      <th>父评论</th>
                      <th>评论者</th>
                      <th>内容</th>
                      <th>点赞</th>
                      <th>时间</th>
                    </tr>
                  </thead>
                  <tbody id="import-preview-rows">
                    {previewItems.length === 0 ? (
                      <tr>
                        <td colSpan={7}>没有可预览的数据。</td>
                      </tr>
                    ) : (
                      previewItems.slice(0, 10).map((item, idx) => (
                        <tr key={`${item.comment_id}-${idx}`}>
                          <td>{item.comment_id}</td>
                          <td>{item.thread_id || item.comment_id}</td>
                          <td>{item.parent_comment_id || "—"}</td>
                          <td>
                            {item.platform_user_id || "未知 ID"}
                            <small className={styles.cellSub}>
                              {item.display_name || "匿名观众"}
                            </small>
                          </td>
                          <td>{item.content}</td>
                          <td>{item.like_count ?? 0}</td>
                          <td>{item.created_at || "—"}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </form>
        </details>
      </div>
    </details>
  );
}
