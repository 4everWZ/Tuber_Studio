"use strict";

import { $ } from "./core.js";
import { commentCanGenerate, commentFinished, reconcileCommentSelection } from "./comment-state.js?v=20261107.13";

const sourcePageSize = 10;
const visibleComments = model => (model.comments || []).slice((model.commentSourcePage || 0) * sourcePageSize, ((model.commentSourcePage || 0) + 1) * sourcePageSize);

export function commentQuery() {
  const params = new URLSearchParams();
  [["video_id", "filter-video"], ["thread_id", "filter-thread"], ["category", "filter-category"], ["status", "filter-status"]].forEach(([key, id]) => {
    const el = $(id);
    if (el && el.value.trim() && !(key === "status" && el.value === "all")) params.set(key, el.value.trim());
  });
  return params.toString();
}

export function selectedThread(model) {
  const selected = model.comments?.find(item => item.id === model.commentId);
  let videoId = $("filter-video")?.value.trim(), threadId = $("filter-thread")?.value.trim();
  if ((!videoId || !threadId) && selected) {
    const selectedId = selected.thread_id || selected.comment_id;
    if ((!videoId || videoId === selected.video_id) && (!threadId || threadId === selectedId)) {
      videoId = selected.video_id;
      threadId = selectedId;
    }
  }
  return videoId && threadId ? { videoId, threadId, key: JSON.stringify([videoId, threadId]) } : null;
}

export async function refreshThreadContext(model, api, tokenCount, occupancyText) {
  const target = selectedThread(model);
  const stateEl = $("thread-context-state");
  if (!stateEl) return;
  if (!target) {
    stateEl.textContent = "选择评论或筛选单楼，查看该楼上下文；不同楼不会合并。";
    return;
  }
  if (model.threadContextPending === target.key) return;
  model.threadContextPending = target.key;
  stateEl.textContent = `正在读取 ${target.videoId} / 楼 ${target.threadId} 的上下文…`;
  try {
    const query = new URLSearchParams({ video_id: target.videoId, thread_id: target.threadId });
    const context = await api(`/api/context/status?${query}`);
    if (selectedThread(model)?.key !== target.key) return;
    stateEl.textContent = `所选楼 ${target.videoId} / ${target.threadId}：${tokenCount(context.active_tokens ?? 0)} / ${tokenCount(context.configured_tokens)}（${occupancyText(context)}），累计压缩 ${context.compact_count ?? 0} 轮、最近 ${context.compaction_passes_last ?? 0} 轮。${context.last_compaction_warning || ""}`;
  } catch (error) {
    if (selectedThread(model)?.key === target.key) {
      stateEl.textContent = `所选楼上下文读取失败：${error.message}`;
    }
  } finally {
    if (model.threadContextPending === target.key) model.threadContextPending = null;
  }
}

export function updateCommentSelection(model) {
  reconcileCommentSelection(model);
  if ($("selected-comment-count")) {
    $("selected-comment-count").textContent = `已选 ${model.selectedComments.size} 条`;
  }
  const page = visibleComments(model).filter(commentCanGenerate);
  const selected = page.filter(comment => model.selectedComments.has(comment.id)).length;
  if ($("generate-drafts")?.getAttribute("aria-busy") !== "true") $("generate-drafts").disabled = !model.selectedComments.size;
  if ($("select-comments")) {
    $("select-comments").checked = !!page.length && selected === page.length;
    $("select-comments").indeterminate = selected > 0 && selected < page.length;
  }
}

function renderCommentSource(model, commentContinuation, setBadge, describe) {
  const comment = model.comments?.find(item => item.id === model.commentId);
  const draft = comment?.draft;
  const source = $("draft-source");
  const open = $("open-comment-draft");
  source.textContent = comment ? `${comment.display_name || "匿名观众"}：${comment.content}` : "选择一条评论，查看原文和所在楼。";
  setBadge($("draft-status"), draft?.status || comment?.continuation?.state || "选择评论");
  describe($("draft-meta"), comment ? [
    ["来源评论", `${comment.video_id} / ${comment.comment_id}`],
    ["与当前 OC 的关系", typeof comment.relationship?.affinity === "number" ? `${comment.relationship.affinity} / 100 · ${comment.relationship.stage}` : comment.platform_user_id ? "个性化未启用" : "匿名用户"],
    ["楼 ID", comment.thread_id || comment.comment_id],
    ["父评论 ID", comment.parent_comment_id || "顶层"],
  ] : []);
  $("open-comment-memory").hidden = !(comment?.platform === "bilibili" && comment.platform_user_id && comment.relationship_account_id);
  open.hidden = !draft;
  if (draft) {
    open.href = draft.status === "private_handoff" ? "#messages" : `#comments/comments/${draft.id}`;
    open.textContent = draft.status === "private_handoff" ? "到私信工作台处理" : "到待办处理草稿";
  }
  commentContinuation.render(draft ? null : comment?.continuation, { channel: "comments", localId: comment?.id });
}

export function renderCommentTasks(tasks, { node, cell, badge, empty }) {
  const tbody = $("comment-tasks");
  if (!tbody) return;
  if (!tasks.length) return empty(tbody, 4, "暂无评论任务。选中评论后生成角色草稿。");
  tbody.replaceChildren();
  tasks.forEach(task => {
    const row = node("tr");
    cell(row, task.kind || task.type || "生成回复", task.id);
    cell(row, badge(task.status));
    cell(row, task.total != null ? `${task.done || 0} / ${task.total}${task.errors ? ` · ${task.errors} 失败` : ""}` : task.progress ?? task.result ?? task.completed_count ?? "—");
    cell(row, task.error || task.reason || "—");
    tbody.append(row);
  });
}

export async function saveCommentCategory(comment, select, feedback, model, api, notice) {
  const previous = comment.category || "general", category = select.value;
  model.categorySaving.add(comment.id);
  select.disabled = true;
  feedback.textContent = "保存中…";
  try {
    const result = await api(`/api/comments/${encodeURIComponent(comment.id)}/category`, { method: "PUT", body: { category } });
    const current = (model.comments || []).find(item => item.id === comment.id);
    if (current) {
      current.category = result.category || category;
      current.category_source = "operator";
    }
    comment.category = result.category || category;
    comment.category_source = "operator";
    select.value = comment.category;
    feedback.textContent = "人工分类 · 已保存";
  } catch (error) {
    select.value = previous;
    feedback.textContent = "保存失败，可重试";
    notice(error.message, true);
  } finally {
    model.categorySaving.delete(comment.id);
    select.disabled = false;
  }
}

export function renderComments(model, {
  api,
  cell,
  node,
  label,
  badge,
  empty,
  busy,
  notice,
  commentContinuation,
  setBadge,
  describe,
  refreshThreadContextFn,
  refreshCommentsFn,
  onCommentSelected,
}) {
  const tbody = $("comments");
  if (!tbody) return;
  reconcileCommentSelection(model);
  if ($("comment-count")) $("comment-count").textContent = `(${model.comments?.length || 0})`;
  const total = model.comments?.length || 0;
  model.commentSourcePage = Math.min(model.commentSourcePage || 0, Math.max(0, Math.ceil(total / sourcePageSize) - 1));
  $("source-prev").disabled = !model.commentSourcePage;
  $("source-next").disabled = (model.commentSourcePage + 1) * sourcePageSize >= total;
  $("source-page").textContent = `第 ${model.commentSourcePage + 1} / ${Math.max(1, Math.ceil(total / sourcePageSize))} 页`;
  if (model.categorySaving.size || document.activeElement?.matches("#comments .category-select")) {
    updateCommentSelection(model);
    renderCommentSource(model, commentContinuation, setBadge, describe);
    return;
  }
  if (!model.comments || !model.comments.length) {
    empty(tbody, 4, "暂无匹配评论。读取视频或导入已有评论后开始。");
  } else {
    tbody.replaceChildren();
    visibleComments(model).forEach(comment => {
      const row = node("tr", null, comment.id === model.commentId ? "selected-row" : "");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.disabled = !commentCanGenerate(comment);
      check.checked = model.selectedComments.has(comment.id);
      check.setAttribute("aria-label", `选择 ${comment.display_name || "观众"} 的评论`);
      if (check.disabled) check.title = "已有草稿，点击评论查看；无需重复生成。";
      check.onchange = () => {
        check.checked ? model.selectedComments.add(comment.id) : model.selectedComments.delete(comment.id);
        updateCommentSelection(model);
        model.commentId = comment.id;
        onCommentSelected?.(comment);
      };
      cell(row, check);

      const text = node("button", comment.content.length > 80 ? comment.content.slice(0, 80) + "…" : comment.content, "comment-link");
      text.onclick = () => {
        model.commentId = comment.id;
        if ($("compact-video-id")) $("compact-video-id").value = comment.video_id;
        if ($("compact-thread-id")) $("compact-thread-id").value = comment.thread_id || comment.comment_id;
        renderComments(model, {
          api, cell, node, label, badge, empty, busy, notice,
          commentContinuation, setBadge, describe, refreshThreadContextFn, refreshCommentsFn, onCommentSelected
        });
        onCommentSelected?.(comment);
        if (refreshThreadContextFn) refreshThreadContextFn();
      };

      const contentCell = cell(row, text, `${comment.display_name || "匿名观众"} · ${comment.platform_user_id || "未知 ID"} · ${comment.video_id || ""}${comment.like_count != null ? ` · ${comment.like_count} 赞` : ""}`);
      const threadButton = node("button", `楼 ${comment.thread_id || comment.comment_id}`, "thread-link");
      threadButton.title = "只查看此楼，并将压缩操作限定到该楼";
      threadButton.onclick = () => busy(threadButton, async () => {
        if ($("filter-video")) $("filter-video").value = comment.video_id;
        if ($("filter-thread")) $("filter-thread").value = comment.thread_id || comment.comment_id;
        if ($("compact-video-id")) $("compact-video-id").value = comment.video_id;
        if ($("compact-thread-id")) $("compact-thread-id").value = comment.thread_id || comment.comment_id;
        if (refreshCommentsFn) await refreshCommentsFn();
      });
      contentCell.append(threadButton);
      if (comment.parent_comment_id) contentCell.append(node("small", `父评论 ${comment.parent_comment_id}`, "cell-sub"));

      const categorySelect = node("select", null, "category-select");
      categorySelect.setAttribute("aria-label", `评论 ${comment.comment_id} 的分类`);
      ["question", "suggestion", "interaction", "spam", "general"].forEach(value => {
        const option = node("option", label(value));
        option.value = value;
        categorySelect.append(option);
      });
      categorySelect.value = comment.category || "general";
      const categoryCell = cell(row, categorySelect);
      const categoryFeedback = node("small", comment.category_source === "operator" ? "人工分类" : "", "cell-sub");
      categoryFeedback.setAttribute("aria-live", "polite");
      categoryCell.append(categoryFeedback);
      categorySelect.onchange = () => saveCommentCategory(comment, categorySelect, categoryFeedback, model, api, notice);
      cell(row, comment.draft ? badge(comment.draft.status) : comment.continuation ? badge(comment.continuation.state) : "未生成");
      tbody.append(row);
    });
  }
  updateCommentSelection(model);
  renderCommentSource(model, commentContinuation, setBadge, describe);
}

export function renderCommentPolicy(policy, model, commentContinuation, setBadge, describe) {
  model.policy = policy;
  if ($("comment-fetch-mode")) $("comment-fetch-mode").textContent = "公开评论";
  renderCommentSource(model, commentContinuation, setBadge, describe);
}

export function initCommentsModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  list,
  label,
  badge,
  empty,
  tokenCount,
  occupancyText,
  commentContinuation,
  setBadge,
  describe,
  onScopeChanged,
  onDraftsRequested,
  onCommentSelected,
}) {
  const doRefreshThreadContext = () => refreshThreadContext(model, api, tokenCount, occupancyText);

  const doRenderComments = () => renderComments(model, {
    api,
    cell,
    node,
    label,
    badge,
    empty,
    busy,
    notice,
    commentContinuation,
    setBadge,
    describe,
    refreshThreadContextFn: doRefreshThreadContext,
    refreshCommentsFn: doRefreshComments,
    onCommentSelected,
  });

  let commentsRequest = 0;
  const doRefreshComments = async () => {
    const query = commentQuery();
    const request = ++commentsRequest;
    const [comments, tasks] = await Promise.all([
      api(`/api/comments?${query}`),
      api("/api/comment-tasks")
    ]);
    if (request !== commentsRequest || query !== commentQuery()) return;
    model.comments = list(comments).filter(comment => $("filter-status")?.value || !commentFinished(comment));
    const changedSelection = !model.comments.some(item => item.id === model.commentId);
    if (changedSelection) model.commentId = null;
    doRenderComments();
    if (changedSelection) await onCommentSelected?.(null);
    renderCommentTasks(list(tasks), { node, cell, badge, empty });
    await doRefreshThreadContext();
  };

  const doLoadPolicy = async () => {
    const status = await api("/api/automation");
    const policy = status.effective?.comments || {};
    renderCommentPolicy(policy, model, commentContinuation, setBadge, describe);
  };

  // Wire buttons & forms
  $("fetch-comments-form")?.addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      if ($("fetch-state")) $("fetch-state").textContent = "正在读取公开评论…";
      if ($("popular-videos")) $("popular-videos").hidden = true;
      if ($("refresh-videos")) $("refresh-videos").textContent = "逛逛热门视频";
      try {
        const payload = { video: $("comment-video").value.trim(), max_pages: Number($("comment-pages").value) || 1 };
        const result = await api("/api/comments/fetch", { method: "POST", body: payload });
        if ($("fetch-state")) $("fetch-state").textContent = `已读取 ${result.count ?? result.imported ?? 0} 条评论，勾选后即可生成回复。`;
        await onScopeChanged?.(result.video_id || payload.video);
      } catch (err) {
        if ($("fetch-state")) $("fetch-state").textContent = `读取失败：${err.message}`;
        throw err;
      }
    });
  });

  $("analyze-comments")?.addEventListener("click", () => busy($("analyze-comments"), async () => {
    const result = await api("/api/comments/analyze", {
      method: "POST",
      body: { video_id: $("filter-video")?.value.trim() || undefined }
    });
    notice(`归类完成${result.updated == null ? "。" : `，已更新 ${result.updated} 条。`}`);
    await doRefreshComments();
  }));

  $("generate-drafts")?.addEventListener("click", async () => {
    try {
      await busy($("generate-drafts"), async () => {
        updateCommentSelection(model);
        if (!model.selectedComments.size) throw new Error("请先选择评论");
        const ids = [...model.selectedComments];
        const result = await api("/api/comments/drafts", { method: "POST", body: { comment_ids: ids } });
        ids.forEach(id => model.selectedComments.delete(id));
        doRenderComments();
        await onDraftsRequested?.(result.task_id, ids);
      });
    } finally {
      updateCommentSelection(model);
    }
  });

  $("select-comments")?.addEventListener("change", () => {
    visibleComments(model).filter(commentCanGenerate).forEach(comment => {
      $("select-comments").checked ? model.selectedComments.add(comment.id) : model.selectedComments.delete(comment.id);
    });
    doRenderComments();
  });

  ["filter-video", "filter-thread", "filter-category", "filter-status"].forEach(id => {
    $(id)?.addEventListener("change", () => { model.commentSourcePage = 0; return doRefreshComments(); });
  });
  $("filter-comments").onclick = () => busy($("filter-comments"), doRefreshComments);
  $("source-prev").onclick = () => { model.commentSourcePage = Math.max(0, (model.commentSourcePage || 0) - 1); doRenderComments(); };
  $("source-next").onclick = () => { model.commentSourcePage = (model.commentSourcePage || 0) + 1; doRenderComments(); };

  $("refresh-videos")?.addEventListener("click", () => busy($("refresh-videos"), async () => {
    const container = $("popular-videos");
    if (!container) return;
    if (!container.hidden) {
      container.hidden = true;
      $("refresh-videos").textContent = "逛逛热门视频";
      return;
    }
    const data = list(await api("/api/comments/videos"));
    container.hidden = false;
    container.replaceChildren();
    if (!data.length) {
      container.append(node("p", "暂无可读取的热门视频，可直接输入视频链接。", "muted"));
      return;
    }
    data.slice(0, 12).forEach(video => {
      const button = node("button", video.title);
      button.append(node("small", `${typeof video.owner === "object" ? video.owner.name || "" : video.owner || ""} · ${video.bvid}`));
      button.onclick = () => {
        if ($("comment-video")) $("comment-video").value = video.bvid;
        if ($("filter-video")) $("filter-video").value = video.bvid;
        if ($("fetch-state")) $("fetch-state").textContent = `已选择：${video.title}。点击「读取评论」获取内容。`;
        container.hidden = true;
        $("refresh-videos").textContent = "逛逛热门视频";
        if ($("comment-video")) $("comment-video").focus();
      };
      container.append(button);
    });
    $("refresh-videos").textContent = "收起热门视频";
  }));

  return {
    draftUpdated(draft, key) {
      if (!key.startsWith("comments/")) return;
      const comment = model.comments.find(item => String(item.draft?.id) === String(draft.id) || String(item.id) === String(draft.comment_id));
      if (!comment) return;
      comment.draft = { ...comment.draft, ...draft };
      model.selectedComments.delete(comment.id);
      if (commentFinished(comment)) {
        if (!$("filter-status")?.value) model.comments = model.comments.filter(item => item !== comment);
        if (model.commentId === comment.id) model.commentId = null;
      }
      doRenderComments();
    },
    refreshComments: doRefreshComments,
    renderComments: doRenderComments,
    loadPolicy: doLoadPolicy,
    refreshThreadContext: doRefreshThreadContext,
  };
}
