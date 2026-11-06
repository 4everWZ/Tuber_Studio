"use strict";

import { $ } from "./core.js";

export function commentQuery() {
  const params = new URLSearchParams();
  [["video_id", "filter-video"], ["thread_id", "filter-thread"], ["category", "filter-category"], ["status", "filter-status"]].forEach(([key, id]) => {
    const el = $(id);
    if (el && el.value.trim()) params.set(key, el.value.trim());
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
  if ($("selected-comment-count")) {
    $("selected-comment-count").textContent = `已选 ${model.selectedComments.size} 条`;
  }
  const selected = (model.comments || []).filter(comment => model.selectedComments.has(comment.id)).length;
  if ($("select-comments")) {
    $("select-comments").checked = !!model.comments?.length && selected === model.comments.length;
    $("select-comments").indeterminate = selected > 0 && selected < model.comments.length;
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
}) {
  const tbody = $("comments");
  if (!tbody) return;
  if ($("comment-count")) $("comment-count").textContent = `(${model.comments?.length || 0})`;
  if (model.categorySaving.size || document.activeElement?.matches("#comments .category-select")) {
    updateCommentSelection(model);
    renderCommentSource(model, commentContinuation, setBadge, describe);
    return;
  }
  if (!model.comments || !model.comments.length) {
    empty(tbody, 4, "暂无匹配评论。读取视频或导入已有评论后开始。");
  } else {
    tbody.replaceChildren();
    model.comments.forEach(comment => {
      const row = node("tr", null, comment.id === model.commentId ? "selected-row" : "");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = model.selectedComments.has(comment.id);
      check.setAttribute("aria-label", `选择 ${comment.display_name || "观众"} 的评论`);
      check.onchange = () => {
        check.checked ? model.selectedComments.add(comment.id) : model.selectedComments.delete(comment.id);
        updateCommentSelection(model);
      };
      cell(row, check);

      const text = node("button", comment.content, "comment-link");
      text.onclick = () => {
        model.commentId = comment.id;
        if ($("compact-video-id")) $("compact-video-id").value = comment.video_id;
        if ($("compact-thread-id")) $("compact-thread-id").value = comment.thread_id || comment.comment_id;
        renderComments(model, {
          api, cell, node, label, badge, empty, busy, notice,
          commentContinuation, setBadge, describe, refreshThreadContextFn, refreshCommentsFn
        });
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
  const base = policy.base || policy;
  if (!model.commentPolicyDirty) {
    if ($("auto-browse")) $("auto-browse").checked = !!base.auto_browse;
    if ($("auto-reply")) $("auto-reply").checked = !!base.auto_reply;
    if ($("watch-videos")) $("watch-videos").value = (base.watch_videos || []).join("\n");
    if ($("browse-interval")) $("browse-interval").value = base.browse_interval_seconds || 300;
    if ($("replies-hour")) $("replies-hour").value = base.max_replies_per_hour || 5;
    if ($("replies-day")) $("replies-day").value = base.max_replies_per_day || 20;
  }
  if ($("reply-mode")) {
    $("reply-mode").textContent = policy.auto_reply ? "已配置自动回复" : "人工审核";
    $("reply-mode").className = `badge ${policy.auto_reply ? "active" : ""}`;
  }
  if ($("comment-fetch-mode")) {
    $("comment-fetch-mode").textContent = policy.auto_reply ? "公开读取 · 已配置自动回复" : "公开读取 · 人工审核";
  }
  if ($("comment-policy-overlay")) $("comment-policy-overlay").hidden = !policy.automation_override && !policy.emergency;
  if ($("comment-policy-overlay-text")) {
    $("comment-policy-overlay-text").textContent = policy.emergency ?
      "紧急停止已生效，所有自动收发被阻止。这里编辑个人基础设置；解除紧停前不会恢复自动业务。" :
      "自动化覆盖正在生效。这里编辑个人基础设置，被覆盖项目关闭自动化后生效；立即停止请前往自动化工作区。";
  }
  if ($("comment-policy-base-note")) {
    $("comment-policy-base-note").textContent = model.commentPolicyDirty ?
      "有未保存的基础设置。" :
      policy.automation_override ?
      "保存个人基础设置；被覆盖项目关闭自动化后生效。" :
      "默认人工审核。开启基础自动回复后，新草稿按来源和额度校验处理。";
  }
  if ($("stop-auto-reply")) {
    $("stop-auto-reply").textContent = policy.emergency ? "紧急停止已生效" : policy.automation_override ? "前往自动化紧急停止" : "停止基础自动回复";
    $("stop-auto-reply").disabled = !!policy.emergency;
  }
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
    model.comments = list(comments);
    doRenderComments();
    renderCommentTasks(list(tasks), { node, cell, badge, empty });
    await doRefreshThreadContext();
  };

  const doLoadPolicy = async () => {
    const policy = await api("/api/comments/policy");
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
        const payload = {
          video_id: $("comment-video")?.value.trim() || undefined,
          thread_id: $("comment-thread")?.value.trim() || undefined,
          limit: Number($("comment-limit")?.value) || 20,
        };
        const result = await api("/api/comments/fetch", { method: "POST", body: payload });
        if ($("fetch-state")) $("fetch-state").textContent = `读取成功：获取到 ${result.count || 0} 条公开评论。`;
        await doRefreshComments();
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

  $("generate-drafts")?.addEventListener("click", () => busy($("generate-drafts"), async () => {
    if (!model.selectedComments.size) throw new Error("请先选择评论");
    await api("/api/comments/drafts", { method: "POST", body: { comment_ids: [...model.selectedComments] } });
    notice("生成任务已加入队列，可在下方查看进度。");
    await doRefreshComments();
  }));

  $("select-comments")?.addEventListener("change", () => {
    model.comments?.forEach(comment => {
      $("select-comments").checked ? model.selectedComments.add(comment.id) : model.selectedComments.delete(comment.id);
    });
    doRenderComments();
  });

  $("comment-policy-form")?.addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const payload = {
        auto_browse: $("auto-browse")?.checked,
        auto_reply: $("auto-reply")?.checked,
        watch_videos: $("watch-videos")?.value.split(/[\s,，]+/).filter(Boolean),
        browse_interval_seconds: Number($("browse-interval")?.value),
        max_replies_per_hour: Number($("replies-hour")?.value),
        max_replies_per_day: Number($("replies-day")?.value)
      };
      if (payload.auto_reply && !(model.policy?.base || model.policy)?.auto_reply &&
          !confirm("开启指定视频的自动回复？新回复将按现有账号与额度规则尝试提交。")) return;
      await api("/api/comments/policy", { method: "PUT", body: payload });
      model.commentPolicyDirty = false;
      await doLoadPolicy();
      notice(model.policy.automation_override ? "个人基础设置已保存；被覆盖项目关闭自动化后生效。" : "回复基础设置已保存，请以当前生效状态为准。");
    });
  });

  $("stop-auto-reply")?.addEventListener("click", () => busy($("stop-auto-reply"), async () => {
    if (model.policy?.automation_override) {
      location.hash = "automation";
      notice("自动化正在覆盖此开关，请在工作区停止本轮、关闭总开关或紧急停止所有自动收发。");
      return;
    }
    await api("/api/comments/policy", { method: "PUT", body: { auto_reply: false } });
    await doLoadPolicy();
    notice("基础自动回复已停止。已提交给平台的请求以回执为准。");
  }));

  $("comment-policy-form")?.addEventListener("input", () => {
    model.commentPolicyDirty = true;
    if ($("comment-policy-base-note")) {
      $("comment-policy-base-note").textContent = "有未保存的基础设置；自动化覆盖项目关闭总开关后生效。";
    }
  });

  $("refresh-comments")?.addEventListener("click", () => busy($("refresh-comments"), doRefreshComments));
  ["filter-video", "filter-thread", "filter-category", "filter-status"].forEach(id => {
    $(id)?.addEventListener("change", () => doRefreshComments());
  });

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
    refreshComments: doRefreshComments,
    renderComments: doRenderComments,
    loadPolicy: doLoadPolicy,
    refreshThreadContext: doRefreshThreadContext,
  };
}
