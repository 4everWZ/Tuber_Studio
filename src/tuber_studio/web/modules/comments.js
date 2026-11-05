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

export function renderAnswerCheck(draft) {
  const answerEl = $("draft-answer-check");
  if (!answerEl) return;
  let check = draft?.answer_check;
  if (!check) {
    answerEl.textContent = "暂无记忆核对记录。";
    return;
  }
  if (typeof check === "string") {
    try {
      check = JSON.parse(check);
    } catch {
      answerEl.textContent = "检查记录格式异常，无法展示证据。";
      return;
    }
  }
  if (!check || typeof check !== "object" || Array.isArray(check)) {
    answerEl.textContent = "检查记录格式异常，无法展示证据。";
    return;
  }
  const evidence = {};
  ["sources", "required_slots", "missing", "retried", "warning", "method", "status", "semantic_verdict"].forEach(key => {
    if (Object.hasOwn(check, key)) evidence[key] = check[key];
  });
  answerEl.textContent = Object.keys(evidence).length ? JSON.stringify(evidence, null, 2) : "检查记录未提供来源、缺项、重试或提示字段。";
}

export function renderDraft(model, commentContinuation, setBadge, describe) {
  const comment = (model.comments || []).find(item => item.id === model.commentId);
  if (!comment) {
    if (commentContinuation) commentContinuation.render(null);
    if (model.draftDirty) return;
    renderAnswerCheck(null);
    if ($("draft-source")) $("draft-source").textContent = "选择列表中的评论，查看角色草稿及来源。";
    if ($("open-private-draft")) $("open-private-draft").hidden = true;
    if ($("draft-text")) {
      $("draft-text").value = "";
      $("draft-text").disabled = true;
    }
    if ($("draft-status")) $("draft-status").textContent = "选择评论";
    if ($("draft-meta")) describe($("draft-meta"), []);
    ["approve-draft", "reject-draft", "copy-draft", "publish-draft"].forEach(id => {
      if ($(id)) $(id).disabled = true;
    });
    return;
  }

  const draft = comment.draft;
  renderAnswerCheck(draft);
  if ($("draft-source")) $("draft-source").textContent = `${comment.display_name || "匿名观众"}：${comment.content}`;
  if ($("draft-status")) setBadge($("draft-status"), draft?.status || comment.continuation?.state || "未生成");
  if ($("draft-meta")) {
    describe($("draft-meta"), [
      ["来源评论", `${comment.video_id} / ${comment.comment_id}`],
      ["与当前OC的关系", typeof comment.relationship?.affinity === "number" ? `${comment.relationship.affinity} / 100 · ${comment.relationship.stage}` : "匿名用户"],
      ["楼ID", comment.thread_id || comment.comment_id],
      ["父评论ID", comment.parent_comment_id || "顶层"],
      ["角色版本", draft ? `${draft.character_id} · v${draft.character_version}` : "—"],
      ["模型", draft?.model || "—"],
      ...(draft?.publish_receipt || draft?.receipt ? [["平台回执", draft.publish_receipt || draft.receipt]] : [])
    ]);
  }
  if (!model.draftDirty || model.draftId !== draft?.id) {
    if ($("draft-text")) $("draft-text").value = draft?.text || "";
    model.draftId = draft?.id || null;
    model.draftDirty = false;
  }
  const locked = draft && ["published", "publishing", "publish_uncertain", "private_handoff"].includes(draft.status);
  if (commentContinuation) {
    commentContinuation.render(draft?.status === "private_handoff" ? null : draft?.continuation || comment.continuation, {
      channel: "comments",
      localId: draft?.id || comment.id,
      dirty: model.draftDirty,
      entityStatus: draft?.status
    });
  }
  if ($("draft-text")) $("draft-text").disabled = !draft || locked;
  if ($("approve-draft")) $("approve-draft").disabled = !draft || locked;
  if ($("reject-draft")) $("reject-draft").disabled = !draft || locked;
  if ($("copy-draft")) $("copy-draft").disabled = !draft || draft.status === "private_handoff";
  if ($("open-private-draft")) $("open-private-draft").hidden = draft?.status !== "private_handoff";
  if ($("publish-draft")) {
    $("publish-draft").disabled = !draft || draft.status !== "approved" || !model.policy?.credentials_configured || model.draftDirty;
  }
  if ($("draft-note")) {
    $("draft-note").textContent = draft?.status === "private_handoff" ?
      "在私信工作台审核这份回复并查看发送回执。" :
      draft?.error ? `错误：${draft.error}` :
      model.draftDirty ? "文字已修改，先审核保存后再发布或复制。" :
      model.policy?.auto_reply ? "自动回复已开启；新草稿可能自动发布。当前草稿以此处状态与平台回执为准。" :
      "人工审核模式。审核通过后可复制；账号凭证已配置时可发布。";
  }
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
    renderDraft(model, commentContinuation, setBadge, describe);
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
        if (model.draftDirty && !confirm("回复有未保存修改。放弃修改并查看其他评论？")) return;
        model.commentId = comment.id;
        model.draftDirty = false;
        model.draftId = null;
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
  renderDraft(model, commentContinuation, setBadge, describe);
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
    $("reply-mode").textContent = policy.auto_reply ? "自动回复已开启" : "人工审核";
    $("reply-mode").className = `badge ${policy.auto_reply ? "active" : ""}`;
  }
  if ($("comment-fetch-mode")) {
    $("comment-fetch-mode").textContent = policy.auto_reply ? "公开读取 · 自动回复已开启" : "公开读取 · 人工审核";
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
  renderDraft(model, commentContinuation, setBadge, describe);
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

  const doRefreshComments = async () => {
    const [comments, tasks] = await Promise.all([
      api(`/api/comments?${commentQuery()}`),
      api("/api/comment-tasks")
    ]);
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
    notice(`归类完成：${JSON.stringify(result)}`);
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

  $("draft-text")?.addEventListener("input", () => {
    model.draftDirty = true;
    renderDraft(model, commentContinuation, setBadge, describe);
  });

  async function reviewDraft(action) {
    const comment = model.comments?.find(item => item.id === model.commentId);
    if (!comment?.draft) throw new Error("请先选择已有草稿的评论");
    await api(`/api/comments/drafts/${encodeURIComponent(comment.draft.id)}/review`, {
      method: "POST",
      body: { action, text: $("draft-text")?.value }
    });
    model.draftDirty = false;
    await doRefreshComments();
    notice(action === "approve" ? "草稿已审核保存。" : "草稿已拒绝。");
  }

  $("approve-draft")?.addEventListener("click", () => busy($("approve-draft"), () => reviewDraft("approve")));
  $("reject-draft")?.addEventListener("click", () => busy($("reject-draft"), () => reviewDraft("reject")));

  $("copy-draft")?.addEventListener("click", () => busy($("copy-draft"), async () => {
    const comment = model.comments?.find(item => item.id === model.commentId);
    if (!comment?.draft) return;
    if (model.draftDirty) throw new Error("请先审核保存修改，再复制已保存的回复");
    await navigator.clipboard.writeText($("draft-text")?.value || "");
    await api(`/api/comments/drafts/${encodeURIComponent(comment.draft.id)}/copy`, { method: "POST" });
    notice("回复已复制，未执行平台发布。");
    await doRefreshComments();
  }));

  $("publish-draft")?.addEventListener("click", () => busy($("publish-draft"), async () => {
    const comment = model.comments?.find(item => item.id === model.commentId);
    if (!comment?.draft) return;
    if (model.draftDirty) throw new Error("请先审核保存修改");
    const result = await api(`/api/comments/drafts/${encodeURIComponent(comment.draft.id)}/publish`, { method: "POST" });
    notice(`平台已返回发布回执：${JSON.stringify(result)}`);
    await doRefreshComments();
  }));

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
    renderDraft: () => renderDraft(model, commentContinuation, setBadge, describe),
    loadPolicy: doLoadPolicy,
    refreshThreadContext: doRefreshThreadContext,
  };
}
