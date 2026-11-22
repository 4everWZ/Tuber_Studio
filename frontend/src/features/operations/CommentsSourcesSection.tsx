import { useRef, useState } from "react";
import { apiPost, apiPut } from "@/shared/api/client";
import { label as displayLabel } from "@/shared/lib/display";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { commentCanGenerate, reconcileCommentSelection } from "./commentState";
import styles from "./operations.module.css";
import type { CommentRecord } from "./types";

const SOURCE_PAGE_SIZE = 10;

export interface SourceFilters {
  videoId: string;
  threadId: string;
  category: string;
  status: string;
}

export interface CommentsSourcesSectionProps {
  comments: CommentRecord[];
  selectedComments: Set<number>;
  selectedCommentId: number | null;
  sourcePage: number;
  filters: SourceFilters;
  onFiltersChange: (next: SourceFilters) => void;
  onPageChange: (page: number) => void;
  onSelectedCommentsChange: (next: Set<number>) => void;
  onSelectComment: (comment: CommentRecord | null) => void;
  onCommentUpdated: (comment: CommentRecord) => void;
  onRefreshComments: () => Promise<void>;
  onDraftsRequested: (taskId: string, commentIds: number[]) => Promise<void> | void;
  onFocusThread: (videoId: string, threadId: string) => Promise<void> | void;
}

export function CommentsSourcesSection({
  comments,
  selectedComments,
  selectedCommentId,
  sourcePage,
  filters,
  onFiltersChange,
  onPageChange,
  onSelectedCommentsChange,
  onSelectComment,
  onCommentUpdated,
  onRefreshComments,
  onDraftsRequested,
  onFocusThread,
}: CommentsSourcesSectionProps) {
  const [generating, setGenerating] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [categorySavingIds, setCategorySavingIds] = useState<Set<number>>(() => new Set());
  const [categoryFeedback, setCategoryFeedback] = useState<Record<number, string>>({});
  const selectAllCheckboxRef = useRef<HTMLInputElement | null>(null);

  const reconciledSelected = reconcileCommentSelection(comments, selectedComments);
  const total = comments.length;
  const maxPage = Math.max(0, Math.ceil(total / SOURCE_PAGE_SIZE) - 1);
  const safePage = Math.min(sourcePage, maxPage);
  const visibleComments = comments.slice(
    safePage * SOURCE_PAGE_SIZE,
    (safePage + 1) * SOURCE_PAGE_SIZE
  );

  const generatableOnPage = visibleComments.filter(commentCanGenerate);
  const selectedOnPageCount = generatableOnPage.filter((c) => reconciledSelected.has(c.id)).length;
  const allOnPageSelected =
    generatableOnPage.length > 0 && selectedOnPageCount === generatableOnPage.length;
  const someOnPageSelected =
    selectedOnPageCount > 0 && selectedOnPageCount < generatableOnPage.length;

  if (selectAllCheckboxRef.current) {
    selectAllCheckboxRef.current.indeterminate = someOnPageSelected;
  }

  async function handleSaveCategory(comment: CommentRecord, nextCategory: string) {
    setCategorySavingIds((prev) => new Set(prev).add(comment.id));
    setCategoryFeedback((prev) => ({ ...prev, [comment.id]: "保存中…" }));
    try {
      const result = await apiPut<
        "/api/v1/operations/comments/{comment_id}/category",
        { category?: string },
        { category: string }
      >("/api/v1/operations/comments/{comment_id}/category", {
        pathParams: { comment_id: comment.id },
        body: { category: nextCategory },
      });
      const updatedCategory = result.category || nextCategory;
      onCommentUpdated({
        ...comment,
        category: updatedCategory,
        category_source: "operator",
      });
      setCategoryFeedback((prev) => ({ ...prev, [comment.id]: "人工分类 · 已保存" }));
    } catch (err) {
      setCategoryFeedback((prev) => ({ ...prev, [comment.id]: "保存失败，可重试" }));
      emitNotice(errorMessage(err), true);
    } finally {
      setCategorySavingIds((prev) => {
        const next = new Set(prev);
        next.delete(comment.id);
        return next;
      });
    }
  }

  async function handleAnalyze() {
    if (analyzing) return;
    setAnalyzing(true);
    try {
      const result = await apiPost<
        "/api/v1/operations/comments/analyze",
        { updated?: number | null },
        { video_id?: string }
      >("/api/v1/operations/comments/analyze", {
        body: { video_id: filters.videoId.trim() || undefined },
      });
      emitNotice(
        `归类完成${result.updated == null ? "。" : `，已更新 ${result.updated} 条。`}`
      );
      await onRefreshComments();
    } catch (err) {
      emitNotice(errorMessage(err), true);
    } finally {
      setAnalyzing(false);
    }
  }

  async function handleGenerateDrafts() {
    if (generating) return;
    if (reconciledSelected.size === 0) {
      emitNotice("请先选择评论", true);
      return;
    }
    const ids = [...reconciledSelected];
    setGenerating(true);
    try {
      const result = await apiPost<
        "/api/v1/operations/comments/drafts",
        { task_id: string },
        { comment_ids: number[] }
      >("/api/v1/operations/comments/drafts", {
        body: { comment_ids: ids },
      });
      const next = new Set(reconciledSelected);
      for (const id of ids) next.delete(id);
      onSelectedCommentsChange(next);
      await onDraftsRequested(result.task_id, ids);
    } catch (err) {
      emitNotice(errorMessage(err), true);
    } finally {
      setGenerating(false);
    }
  }

  function handleSelectPageAll(checked: boolean) {
    const next = new Set(reconciledSelected);
    for (const comment of generatableOnPage) {
      if (checked) next.add(comment.id);
      else next.delete(comment.id);
    }
    onSelectedCommentsChange(next);
  }

  function turnSourcePage(delta: number) {
    const nextPage = Math.max(0, Math.min(maxPage, safePage + delta));
    onPageChange(nextPage);
    onSelectComment(null);
  }

  return (
    <div id="comment-source-records">
      <div className="actions" style={{ marginBottom: 10 }}>
        <button
          id="generate-drafts"
          className="primary"
          type="button"
          disabled={reconciledSelected.size === 0 || generating}
          aria-busy={generating}
          onClick={() => void handleGenerateDrafts()}
        >
          生成选中评论的回复
        </button>
        <span id="comment-count" className="hint">
          ({total})
        </span>
      </div>

      <details style={{ marginBlock: 10 }}>
        <summary style={{ cursor: "pointer", color: "var(--muted)" }}>筛选评论</summary>
        <div className="form-grid" style={{ marginTop: 8 }}>
          <label>
            视频
            <input
              id="filter-video"
              placeholder="全部视频"
              value={filters.videoId}
              onChange={(e) => {
                onPageChange(0);
                onFiltersChange({ ...filters, videoId: e.target.value });
              }}
            />
          </label>
          <label>
            楼 ID
            <input
              id="filter-thread"
              placeholder="全部楼"
              value={filters.threadId}
              onChange={(e) => {
                onPageChange(0);
                onFiltersChange({ ...filters, threadId: e.target.value });
              }}
            />
          </label>
          <label>
            类别
            <select
              id="filter-category"
              value={filters.category}
              onChange={(e) => {
                onPageChange(0);
                onFiltersChange({ ...filters, category: e.target.value });
              }}
            >
              <option value="">全部</option>
              <option value="question">问题</option>
              <option value="suggestion">建议</option>
              <option value="interaction">互动</option>
              <option value="spam">垃圾</option>
              <option value="general">一般</option>
            </select>
          </label>
          <label>
            草稿状态
            <select
              id="filter-status"
              value={filters.status}
              onChange={(e) => {
                onPageChange(0);
                onFiltersChange({ ...filters, status: e.target.value });
              }}
            >
              <option value="">待操作</option>
              <option value="all">全部（含历史）</option>
              <option value="pending_review">待审核</option>
              <option value="approved">已审核</option>
              <option value="rejected">已拒绝</option>
              <option value="copied">已复制</option>
              <option value="published">已发布</option>
            </select>
          </label>
        </div>
        <div className="actions" style={{ marginTop: 8 }}>
          <button
            id="filter-comments"
            type="button"
            onClick={() => void onRefreshComments()}
          >
            筛选
          </button>
          <button
            id="analyze-comments"
            type="button"
            disabled={analyzing}
            onClick={() => void handleAnalyze()}
          >
            归类
          </button>
        </div>
      </details>

      <div className="table-scroll">
        <table style={{ tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th style={{ width: 34 }}>
                <input
                  ref={selectAllCheckboxRef}
                  id="select-comments"
                  type="checkbox"
                  aria-label="选择本页评论"
                  checked={allOnPageSelected}
                  onChange={(e) => handleSelectPageAll(e.target.checked)}
                />
              </th>
              <th style={{ width: "50%" }}>评论</th>
              <th style={{ width: 105 }}>类别</th>
              <th>草稿</th>
            </tr>
          </thead>
          <tbody id="comments">
            {visibleComments.length === 0 ? (
              <tr>
                <td colSpan={4} className="hint">
                  暂无匹配评论。读取视频或导入已有评论后开始。
                </td>
              </tr>
            ) : (
              visibleComments.map((comment) => {
                const canGenerate = commentCanGenerate(comment);
                const checked = reconciledSelected.has(comment.id);
                const threadId = comment.thread_id || comment.comment_id;
                const isSavingCat = categorySavingIds.has(comment.id);
                const catNote =
                  categoryFeedback[comment.id] ??
                  (comment.category_source === "operator" ? "人工分类" : "");
                const statusVal =
                  comment.draft?.status || comment.continuation?.state || null;

                return (
                  <tr
                    key={comment.id}
                    className={comment.id === selectedCommentId ? "selected-row" : undefined}
                  >
                    <td>
                      <input
                        type="checkbox"
                        disabled={!canGenerate}
                        checked={checked}
                        aria-label={`选择 ${comment.display_name || "观众"} 的评论`}
                        title={
                          !canGenerate
                            ? "已有草稿，点击评论查看；无需重复生成。"
                            : undefined
                        }
                        onChange={(e) => {
                          const next = new Set(reconciledSelected);
                          if (e.target.checked) next.add(comment.id);
                          else next.delete(comment.id);
                          onSelectedCommentsChange(next);
                          onSelectComment(comment);
                        }}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className={styles.commentLink}
                        onClick={() => onSelectComment(comment)}
                      >
                        {comment.content.length > 80
                          ? `${comment.content.slice(0, 80)}…`
                          : comment.content}
                      </button>
                      <small className={styles.cellSub}>
                        {comment.display_name || "匿名观众"} ·{" "}
                        {comment.platform_user_id || "未知 ID"} · {comment.video_id || ""}
                        {comment.like_count != null ? ` · ${comment.like_count} 赞` : ""}
                      </small>
                      <button
                        type="button"
                        className={styles.commentLink}
                        title="只查看此楼，并将压缩操作限定到该楼"
                        onClick={() => void onFocusThread(comment.video_id, threadId)}
                      >
                        楼 {threadId}
                      </button>
                      {comment.parent_comment_id && (
                        <small className={styles.cellSub}>
                          父评论 {comment.parent_comment_id}
                        </small>
                      )}
                    </td>
                    <td>
                      <select
                        className={styles.categorySelect}
                        aria-label={`评论 ${comment.comment_id} 的分类`}
                        disabled={isSavingCat}
                        value={comment.category || "general"}
                        onChange={(e) => void handleSaveCategory(comment, e.target.value)}
                      >
                        {(["question", "suggestion", "interaction", "spam", "general"] as const).map(
                          (opt) => (
                            <option key={opt} value={opt}>
                              {displayLabel(opt)}
                            </option>
                          )
                        )}
                      </select>
                      {catNote && (
                        <small className={styles.cellSub} aria-live="polite">
                          {catNote}
                        </small>
                      )}
                    </td>
                    <td>
                      {statusVal ? (
                        <span className="badge">{displayLabel(statusVal)}</span>
                      ) : (
                        "未生成"
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="actions" style={{ marginTop: 12 }}>
        <button
          id="source-prev"
          type="button"
          disabled={safePage <= 0}
          onClick={() => turnSourcePage(-1)}
        >
          上一页
        </button>
        <span id="source-page" className="hint">
          第 {safePage + 1} / {Math.max(1, Math.ceil(total / SOURCE_PAGE_SIZE))} 页
        </span>
        <button
          id="source-next"
          type="button"
          disabled={(safePage + 1) * SOURCE_PAGE_SIZE >= total}
          onClick={() => turnSourcePage(1)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
