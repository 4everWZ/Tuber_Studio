"use strict";

import { $ } from "./core.js";

export function localDateTime(value, display) {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? (display ? display(value) : String(value)) : date.toLocaleString("zh-CN", { hour12: false });
}

export function boundedInteger(id, title, minimum, maximum) {
  const el = $(id);
  const value = Number(el?.value);
  if (!el?.value.trim() || !Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${title}须为 ${minimum} 至 ${maximum} 的整数。`);
  }
  return value;
}

export function renderDynamicPolicy(model, describe, display) {
  const policy = model.dynamicPolicy;
  if ($("dynamic-policy-fields")) $("dynamic-policy-fields").disabled = !policy || model.dynamicSaving;
  if ($("save-dynamic-policy")) $("save-dynamic-policy").disabled = !policy || model.dynamicSaving || !model.dynamicPolicyDirty;
  if ($("stop-dynamics")) $("stop-dynamics").disabled = !policy?.enabled || model.dynamicSaving;
  if ($("evaluate-dynamics")) {
    $("evaluate-dynamics").disabled = !policy || model.dynamicEvaluating || model.dynamicSaving;
    $("evaluate-dynamics").setAttribute("aria-busy", String(model.dynamicEvaluating));
  }
  if ($("save-dynamic-policy")) $("save-dynamic-policy").setAttribute("aria-busy", String(model.dynamicSaving));
  if (!policy) return;

  if ($("dynamic-mode")) {
    $("dynamic-mode").textContent = !policy.enabled ? "运营已停止" : policy.auto_publish ? "运营中 · 自动发布" : "运营中 · 人工审核";
    $("dynamic-mode").className = `badge ${policy.enabled ? "active" : ""}`;
  }
  const character = model.status?.character;
  if ($("dynamic-policy-status")) {
    describe($("dynamic-policy-status"), [
      ["当前角色", character ? `${character.name} · v${character.version}` : "未报告"],
      ["共用B站账号", policy.credentials_configured ? "已配置（不回显）" : "未配置，可手动生成草稿"],
      ["下次评估（当地时间）", localDateTime(policy.next_evaluation_at, display)],
      ["退避至（当地时间）", localDateTime(policy.backoff_until, display)],
      ["最近错误", policy.last_error || "无"],
      ["表单", model.dynamicPolicyDirty ? "有未保存修改" : "显示已保存策略"]
    ]);
  }
}

export function dynamicPolicyFromServer(policy, model, describe, display) {
  model.dynamicPolicy = policy;
  if (!model.dynamicPolicyDirty) {
    if ($("dynamic-enabled")) $("dynamic-enabled").checked = !!policy.enabled;
    if ($("dynamic-auto-publish")) $("dynamic-auto-publish").checked = policy.auto_publish !== false;
    if ($("dynamic-use-memes")) $("dynamic-use-memes").checked = !!policy.use_memes;
    if ($("dynamic-evaluate-interval")) $("dynamic-evaluate-interval").value = policy.evaluate_interval_seconds ?? 1800;
    if ($("dynamic-min-post-interval")) $("dynamic-min-post-interval").value = policy.min_post_interval_seconds ?? 21600;
    if ($("dynamic-max-posts")) $("dynamic-max-posts").value = policy.max_posts_per_day ?? 2;
    if ($("dynamic-memory-summary-max")) $("dynamic-memory-summary-max").value = policy.memory_summary_max_tokens ?? 2048;
  }
  renderDynamicPolicy(model, describe, display);
}

export function appendDynamicSources(container, sources, { node, display }) {
  container.replaceChildren();
  if (!Array.isArray(sources) || !sources.length) {
    container.append(node("p", "暂无可展示的来源原文。", "hint"));
    return;
  }
  sources.forEach(source => {
    const entry = node("div", null, "dynamic-source");
    entry.append(node("strong", `${source.fictional ? "虚构角色故事" : "公开来源"} · ${source.type || "来源"} · ${source.id || "—"}`));
    entry.append(node("p", source.text || "未返回来源文字。"));
    if (source.original_comment) entry.append(node("p", `原评论：${display(source.original_comment)}`, "hint"));
    if (source.source_url) {
      try {
        const url = new URL(source.source_url);
        if (url.protocol === "https:") {
          const link = node("a", "打开来源");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          entry.append(link);
        }
      } catch {
        /* Source text remains visible without an unsafe or invalid link. */
      }
    }
    container.append(entry);
  });
}

export function renderDynamicPost(model, {
  setBadge,
  describe,
  display,
  dynamicContinuation,
  memePreview,
  publicSettings,
  node
}) {
  const post = (model.dynamicPosts || []).find(item => item.id === model.dynamicPostId);
  const editable = !!post && ["pending_review", "approved", "rejected"].includes(post.status);
  const imageInfo = post?.meme_id ? model.dynamicMemeCache.get(`${post.character_id}:${post.meme_id}`) : null;
  const unavailable = !!post?.meme_id && !!imageInfo && !imageInfo.error && (!imageInfo.asset || !imageInfo.asset.enabled || imageInfo.asset.missing);

  if ($("dynamic-post-status")) {
    if (post) setBadge($("dynamic-post-status"), post.status);
    else {
      $("dynamic-post-status").className = "badge";
      $("dynamic-post-status").textContent = "选择动态";
    }
  }
  if ($("dynamic-post-text")) {
    $("dynamic-post-text").disabled = !editable || model.dynamicPostPending;
    if (!model.dynamicPostDirty) $("dynamic-post-text").value = post?.content || "";
  }
  if ($("approve-dynamic")) $("approve-dynamic").disabled = !editable || model.dynamicPostPending;
  if ($("reject-dynamic")) $("reject-dynamic").disabled = !editable || model.dynamicPostPending;
  if ($("publish-dynamic")) {
    $("publish-dynamic").disabled = !post || post.status !== "approved" || model.dynamicPostDirty || model.dynamicPostPending || unavailable || !model.dynamicPolicy?.credentials_configured;
    $("publish-dynamic").textContent = post?.meme_id ? "发布已审核图文" : "发布已审核动态";
  }
  if ($("dynamic-pick-meme")) $("dynamic-pick-meme").disabled = !editable || model.dynamicPostPending;
  if ($("dynamic-clear-meme")) $("dynamic-clear-meme").disabled = !editable || model.dynamicPostPending || !post?.meme_id;

  if ($("dynamic-posts")) {
    for (const button of $("dynamic-posts").querySelectorAll("button")) {
      button.disabled = !!model.dynamicPostPending;
    }
  }
  if ($("dynamic-meme-preview")) $("dynamic-meme-preview").hidden = !post?.meme_id;
  if (post?.meme_id) {
    if ($("dynamic-meme-name")) $("dynamic-meme-name").textContent = imageInfo?.asset?.title || post.meme_id;
    if ($("dynamic-meme-availability")) {
      $("dynamic-meme-availability").textContent = unavailable ?
        "当前配图已禁用或缺失，请更换图片或移除配图。" :
        imageInfo?.error ?
        "配图状态暂未取得，可到图库核对；发布前会再次检查。" :
        "配图将在发布前重新检查可用性。";
    }
    const key = `${post.id}:${post.character_id}:${post.meme_id}`;
    if (model.dynamicMemePreviewKey !== key && typeof memePreview === "function") {
      model.dynamicMemePreviewKey = key;
      $("dynamic-meme-image")?.replaceChildren(memePreview({ asset_id: post.meme_id, title: imageInfo?.asset?.title || "动态配图" }, post.character_id));
    }
  } else if (model.dynamicMemePreviewKey) {
    model.dynamicMemePreviewKey = null;
    $("dynamic-meme-image")?.replaceChildren();
  }

  if ($("dynamic-post-meta")) {
    describe($("dynamic-post-meta"), post ? [
      ["动态ID", post.id],
      ["角色", `${post.character_id} · v${post.character_version}`],
      ["模型", post.model],
      ["决策原因", post.reason || "未报告"],
      ["发布时间（当地时间）", localDateTime(post.published_at, display)]
    ] : []);
  }

  if ($("dynamic-post-sources")) {
    appendDynamicSources($("dynamic-post-sources"), post?.sources, { node, display });
  }

  if ($("dynamic-post-receipt")) {
    $("dynamic-post-receipt").textContent = JSON.stringify(publicSettings({ receipt: post?.receipt ?? null, error: post?.error ?? null }), null, 2);
  }
  if ($("dynamic-post-note")) {
    $("dynamic-post-note").textContent = !post ? "从列表选择动态，查看来源、决策与平台回执。" :
      model.dynamicPostDirty ? "有未保存修改；审核通过后保存，再发布。" :
      post.error ? `操作未完成：${post.error}` :
      post.status === "publish_uncertain" ? "发布结果不确定，请核对平台回执；不会自动重发。" :
      post.status === "published" ? "已发布，平台回执见下方。" :
      !model.dynamicPolicy?.credentials_configured ? "未配置B站账号；可生成和审核草稿，发布前请到设置页的B站账号区配置凭证。" :
      "手动评估生成的草稿始终保留人工审核，后台不会补发。";
    $("dynamic-post-note").classList.toggle("error-text", !!post?.error || post?.status === "publish_uncertain");
  }

  if (dynamicContinuation) {
    dynamicContinuation.render(post?.continuation, {
      channel: "dynamics",
      localId: post?.id,
      dirty: model.dynamicPostDirty,
      entityStatus: post?.status
    });
  }
}

export function renderDynamicPosts(model, {
  empty,
  cell,
  node,
  badge,
  setBadge,
  describe,
  display,
  dynamicContinuation,
  memePreview,
  publicSettings,
  refreshDynamicMemeInfoFn,
}) {
  const body = $("dynamic-posts");
  if (!body) return;
  if ($("dynamic-count")) $("dynamic-count").textContent = `${model.dynamicPosts?.length || 0} 条`;
  if (!model.dynamicPosts || !model.dynamicPosts.length) {
    empty(body, 3, "暂无动态。可先评估并生成一份待审核草稿。");
  } else {
    body.replaceChildren();
    model.dynamicPosts.forEach(post => {
      const row = node("tr", null, post.id === model.dynamicPostId ? "selected-row" : "");
      cell(row, localDateTime(post.created_at, display), `${post.character_id} · v${post.character_version}`);
      const button = node("button", post.content || "查看动态", "comment-link");
      button.onclick = () => {
        if (model.dynamicPostDirty && post.id !== model.dynamicPostId && !confirm("动态有未保存修改。放弃修改并选择另一条？")) return;
        if (post.id !== model.dynamicPostId) model.dynamicPostDirty = false;
        model.dynamicPostId = post.id;
        renderDynamicPosts(model, {
          empty, cell, node, badge, setBadge, describe, display,
          dynamicContinuation, memePreview, publicSettings, refreshDynamicMemeInfoFn
        });
        if (refreshDynamicMemeInfoFn) refreshDynamicMemeInfoFn(post);
      };
      cell(row, button);
      cell(row, badge(post.status));
      body.append(row);
    });
  }
  renderDynamicPost(model, { setBadge, describe, display, dynamicContinuation, memePreview, publicSettings, node });
}

export function renderDynamicDecisions(model, {
  dynamicDecisionContinuation,
  empty,
  cell,
  node,
  badge,
  label,
  display,
}) {
  const body = $("dynamic-decisions");
  if (!body) return;
  const selected = (model.dynamicDecisions || []).find(item => item.id === model.dynamicDecisionId);
  if (dynamicDecisionContinuation) {
    dynamicDecisionContinuation.render(selected?.continuation, { channel: "dynamics", localId: selected?.id, entityStatus: selected?.status });
  }
  if (!model.dynamicDecisions || !model.dynamicDecisions.length) {
    return empty(body, 4, "暂无评估决策。手动评估或开启运营后查看。");
  }
  body.replaceChildren();
  model.dynamicDecisions.forEach(decision => {
    const row = node("tr");
    cell(row, localDateTime(decision.created_at, display), `${decision.character_id} · v${decision.character_version}${decision.manual ? " · 手动" : " · 后台"}`);
    cell(row, badge(decision.action), label(decision.status));
    const reason = cell(row, decision.reason || "未报告", decision.error || "");
    if (decision.continuation?.phase === "generation") {
      const progress = node("button", "查看生成进度", "secondary");
      progress.type = "button";
      progress.onclick = () => {
        model.dynamicDecisionId = decision.id;
        renderDynamicDecisions(model, { dynamicDecisionContinuation, empty, cell, node, badge, label, display });
        if (dynamicDecisionContinuation?.root) {
          dynamicDecisionContinuation.root.scrollIntoView({ block: "nearest" });
        }
      };
      reason.append(progress);
    }
    const details = node("details");
    const sourceList = Array.isArray(decision.sources) ? decision.sources : [];
    details.append(node("summary", `来源 ${sourceList.length} 条`));
    const sources = node("div");
    appendDynamicSources(sources, decision.sources, { node, display });
    details.append(sources);
    cell(row, details);
    body.append(row);
  });
}

export function renderDynamicMemories(model, {
  empty,
  cell,
  node,
  label,
  display,
  api,
  safeMessage,
}) {
  if (model.dynamicMemorySaving.size) return;
  const body = $("dynamic-memories");
  if (!body) return;
  if (!model.dynamicMemories || !model.dynamicMemories.length) {
    return empty(body, 3, "当前角色暂无可选择的来源记忆；已发布回复无需手动授权。");
  }
  body.replaceChildren();
  model.dynamicMemories.forEach(memory => {
    const row = node("tr");
    const check = node("input");
    check.type = "checkbox";
    check.checked = !!memory.public;
    check.setAttribute("aria-label", `允许记忆 ${memory.id} 用于公开动态`);
    check.onchange = async () => {
      const before = !!memory.public;
      model.dynamicMemorySaving.add(memory.id);
      check.disabled = true;
      try {
        await api(`/api/dynamics/memories/${encodeURIComponent(memory.id)}`, { method: "PUT", body: { public: check.checked } });
        memory.public = check.checked;
        if ($("dynamic-memory-state")) {
          $("dynamic-memory-state").textContent = check.checked ? "已允许该记忆用于公开动态。" : "已撤销该记忆的后续公开使用；已发布内容保留原回执。";
          $("dynamic-memory-state").classList.remove("error-text");
        }
      } catch (error) {
        check.checked = before;
        if ($("dynamic-memory-state")) {
          $("dynamic-memory-state").textContent = `授权保存失败：${safeMessage(error.message)}`;
          $("dynamic-memory-state").classList.add("error-text");
        }
      } finally {
        model.dynamicMemorySaving.delete(memory.id);
        check.disabled = false;
      }
    };
    cell(row, check);
    cell(row, memory.text, `${memory.character_id} · v${memory.character_version} · ${memory.id}`);
    cell(row, memory.fictional || memory.kind === "role_story" ? "虚构角色故事" : label(memory.kind), localDateTime(memory.created_at, display));
    body.append(row);
  });
}

export function renderDynamicMemorySummary(summary, {
  describe,
  tokenCount,
  display,
  empty,
  node,
  cell,
  list,
}) {
  const state = $("dynamic-summary-valid");
  if (state) {
    state.textContent = summary.valid ? "有效" : "待更新";
    state.className = `badge ${summary.valid ? "good" : ""}`;
  }
  if ($("dynamic-summary-meta")) {
    describe($("dynamic-summary-meta"), [
      ["角色", summary.character_id ? `${summary.character_id} · v${summary.character_version}` : "未报告"],
      ["实际用量 / 摘要上限", `${tokenCount(summary.token_count)} / ${tokenCount(summary.max_tokens)}`],
      ["最近更新（当地时间）", localDateTime(summary.updated_at, display)]
    ]);
  }
  if ($("dynamic-summary-state")) {
    $("dynamic-summary-state").textContent = summary.valid ?
      "当前有效摘要供运营评估使用；原文保留在来源核对中。" :
      "尚无有效摘要或来源／策略已变化；下次评估将更新摘要。";
  }
  const body = $("dynamic-summary-entries");
  if (!body) return;
  const entries = list(summary.entries);
  if (!entries.length) {
    empty(body, 3, "暂无运营记忆摘要摘录。公开来源就绪后评估生成。");
    return;
  }
  body.replaceChildren();
  entries.forEach(entry => {
    const row = node("tr");
    cell(row, entry.source_id || "未返回来源ID");
    cell(row, entry.quote || "未返回摘录");
    cell(row, entry.fictional ? "虚构角色故事" : "公开互动摘录");
    body.append(row);
  });
}

export function initDynamicsModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  list,
  label,
  badge,
  empty,
  describe,
  display,
  setBadge,
  tokenCount,
  safeMessage,
  publicSettings,
  memePreview,
  dynamicContinuation,
  dynamicDecisionContinuation,
  dynamicMemePicker,
}) {
  const doRenderPolicy = () => renderDynamicPolicy(model, describe, display);

  const doRenderPosts = () => renderDynamicPosts(model, {
    empty,
    cell,
    node,
    badge,
    setBadge,
    describe,
    display,
    dynamicContinuation,
    memePreview,
    publicSettings,
    refreshDynamicMemeInfoFn: doRefreshDynamicMemeInfo,
  });

  const doRenderPost = () => renderDynamicPost(model, {
    setBadge,
    describe,
    display,
    dynamicContinuation,
    memePreview,
    publicSettings,
    node,
  });

  const doRenderDecisions = () => renderDynamicDecisions(model, {
    dynamicDecisionContinuation,
    empty,
    cell,
    node,
    badge,
    label,
    display,
  });

  const doRenderMemories = () => renderDynamicMemories(model, {
    empty,
    cell,
    node,
    label,
    display,
    api,
    safeMessage,
  });

  const doRenderSummary = (summary) => renderDynamicMemorySummary(summary, {
    describe,
    tokenCount,
    display,
    empty,
    node,
    cell,
    list,
  });

  async function doRefreshDynamicMemeInfo(post) {
    if (!post?.meme_id) return;
    const key = `${post.character_id}:${post.meme_id}`;
    const cached = model.dynamicMemeCache.get(key);
    if (model.dynamicMemePending.has(key) || (cached && Date.now() - cached.at < 30000)) return;
    model.dynamicMemePending.add(key);
    try {
      const value = await api(`/api/memes?character=${encodeURIComponent(post.character_id)}`);
      model.dynamicMemeCache.set(key, { asset: list(value).find(item => item.asset_id === post.meme_id) || null, at: Date.now() });
    } catch (error) {
      model.dynamicMemeCache.set(key, { error: safeMessage(error.message), at: Date.now() });
    } finally {
      model.dynamicMemePending.delete(key);
      if (model.dynamicPostId === post.id) doRenderPost();
    }
  }

  async function doRefreshDynamics() {
    const [policy, posts, decisions, memories, summary] = await Promise.all([
      api("/api/dynamics/policy"),
      api("/api/dynamics/posts"),
      api("/api/dynamics/decisions"),
      api("/api/dynamics/memories?limit=100"),
      api("/api/dynamics/memory-summary")
    ]);
    const first = !model.dynamicListsLoaded;
    const credentialsChanged = model.dynamicPolicy?.credentials_configured !== policy.credentials_configured;
    dynamicPolicyFromServer(policy, model, describe, display);

    if (first || JSON.stringify(model.dynamicPosts) !== JSON.stringify(list(posts))) {
      model.dynamicPosts = list(posts);
      doRenderPosts();
    } else if (credentialsChanged) {
      doRenderPost();
    }

    if (first || JSON.stringify(model.dynamicDecisions) !== JSON.stringify(list(decisions))) {
      model.dynamicDecisions = list(decisions);
      doRenderDecisions();
    }

    if (!model.dynamicMemorySaving.size && (first || JSON.stringify(model.dynamicMemories) !== JSON.stringify(list(memories)))) {
      model.dynamicMemories = list(memories);
      doRenderMemories();
    }

    if (JSON.stringify(model.dynamicMemorySummary) !== JSON.stringify(summary)) {
      model.dynamicMemorySummary = summary;
      doRenderSummary(summary);
    }
    model.dynamicListsLoaded = true;
    await doRefreshDynamicMemeInfo(model.dynamicPosts.find(item => item.id === model.dynamicPostId));
  }

  async function doSaveDynamicPolicy(stop = false) {
    if (model.dynamicSaving) return;
    const payload = stop ? { enabled: false } : {
      enabled: $("dynamic-enabled")?.checked,
      auto_publish: $("dynamic-auto-publish")?.checked,
      use_memes: $("dynamic-use-memes")?.checked,
      evaluate_interval_seconds: boundedInteger("dynamic-evaluate-interval", "动态评估间隔", 300, 86400),
      min_post_interval_seconds: boundedInteger("dynamic-min-post-interval", "最短发布间隔", 21600, 604800),
      max_posts_per_day: boundedInteger("dynamic-max-posts", "每日动态数", 1, 2),
      memory_summary_max_tokens: boundedInteger("dynamic-memory-summary-max", "运营记忆摘要长度", 256, 4096),
    };
    model.dynamicSaving = true;
    doRenderPolicy();
    try {
      const policy = await api("/api/dynamics/policy", { method: "PUT", body: payload });
      if (stop) {
        if ($("dynamic-enabled")) $("dynamic-enabled").checked = false;
      } else {
        model.dynamicPolicyDirty = false;
      }
      dynamicPolicyFromServer(policy, model, describe, display);
      if ($("dynamic-policy-state")) {
        $("dynamic-policy-state").classList.remove("error-text");
        $("dynamic-policy-state").textContent = !policy.enabled ?
          "动态运营已停止；已发布请求以平台回执为准。" :
          policy.auto_publish ?
          "运营已开启，后台评估可直接发布；手动评估仍只生成草稿。" :
          "运营已开启，所有新动态保留人工审核。";
        notice($("dynamic-policy-state").textContent);
      }
    } catch (error) {
      if ($("dynamic-policy-state")) {
        $("dynamic-policy-state").textContent = `策略保存失败：${safeMessage(error.message)}。输入已保留。`;
        $("dynamic-policy-state").classList.add("error-text");
      }
      throw error;
    } finally {
      model.dynamicSaving = false;
      doRenderPolicy();
    }
  }

  async function doReviewDynamic(action) {
    const post = (model.dynamicPosts || []).find(item => item.id === model.dynamicPostId);
    if (!post || model.dynamicPostPending) return;
    model.dynamicPostPending = true;
    doRenderPost();
    try {
      await api(`/api/dynamics/posts/${encodeURIComponent(post.id)}/review`, {
        method: "POST",
        body: { action, text: $("dynamic-post-text")?.value }
      });
      model.dynamicPostDirty = false;
      await doRefreshDynamics();
      if ($("dynamic-post-note")) {
        $("dynamic-post-note").textContent = action === "approve" ? "审核已保存，尚未手动发布。" : "动态已拒绝。";
      }
    } finally {
      model.dynamicPostPending = false;
      doRenderPost();
    }
  }

  async function doSelectDynamicMeme(meme_id, asset = null, context = null) {
    const post = (model.dynamicPosts || []).find(item => item.id === model.dynamicPostId);
    if (!post || model.dynamicPostPending) throw new Error("动态正在处理中或尚未选择，请稍后重试。");
    if (context && (context.id !== post.id || context.character !== post.character_id || context.version !== post.character_version)) {
      throw new Error("动态或角色已变化，请重新选择配图。");
    }
    model.dynamicPostPending = true;
    doRenderPost();
    try {
      await api(`/api/dynamics/posts/${encodeURIComponent(post.id)}/review`, {
        method: "POST",
        body: { action: "edit", text: $("dynamic-post-text")?.value, meme_id }
      });
      if (asset) model.dynamicMemeCache.set(`${post.character_id}:${asset.asset_id}`, { asset, at: Date.now() });
      model.dynamicPostDirty = false;
      await doRefreshDynamics();
      notice(meme_id ? "已为动态附加1张图片，正文保留；请重新审核。" : "已移除动态配图，正文保留；请重新审核。");
    } finally {
      model.dynamicPostPending = false;
      doRenderPost();
    }
  }

  // Bind Listeners
  $("dynamic-policy-form")?.addEventListener("input", () => {
    model.dynamicPolicyDirty = true;
    doRenderPolicy();
  });
  $("dynamic-policy-form")?.addEventListener("change", () => {
    model.dynamicPolicyDirty = true;
    doRenderPolicy();
  });
  $("dynamic-policy-form")?.addEventListener("submit", event => {
    event.preventDefault();
    busy($("save-dynamic-policy"), () => doSaveDynamicPolicy());
  });
  $("stop-dynamics")?.addEventListener("click", () => busy($("stop-dynamics"), () => doSaveDynamicPolicy(true)));
  $("refresh-dynamics")?.addEventListener("click", () => busy($("refresh-dynamics"), doRefreshDynamics));

  $("evaluate-dynamics")?.addEventListener("click", () => busy($("evaluate-dynamics"), async () => {
    model.dynamicEvaluating = true;
    doRenderPolicy();
    try {
      const result = await api("/api/dynamics/evaluate", { method: "POST" });
      if ($("dynamic-policy-state")) {
        $("dynamic-policy-state").classList.remove("error-text");
        $("dynamic-policy-state").textContent = result.action === "post" ?
          `已评估并生成待审核动态（${result.post_id}）。` :
          `评估完成：${result.reason || "无需发动态"}`;
        notice($("dynamic-policy-state").textContent);
      }
      await doRefreshDynamics();
    } catch (error) {
      if ($("dynamic-policy-state")) {
        $("dynamic-policy-state").textContent = `评估失败：${safeMessage(error.message)}`;
        $("dynamic-policy-state").classList.add("error-text");
      }
      throw error;
    } finally {
      model.dynamicEvaluating = false;
      doRenderPolicy();
    }
  }));

  $("approve-dynamic")?.addEventListener("click", () => busy($("approve-dynamic"), () => doReviewDynamic("approve")));
  $("reject-dynamic")?.addEventListener("click", () => busy($("reject-dynamic"), () => doReviewDynamic("reject")));

  $("publish-dynamic")?.addEventListener("click", () => busy($("publish-dynamic"), async () => {
    const post = (model.dynamicPosts || []).find(item => item.id === model.dynamicPostId);
    if (!post || model.dynamicPostPending) return;
    model.dynamicPostPending = true;
    doRenderPost();
    try {
      const result = await api(`/api/dynamics/posts/${encodeURIComponent(post.id)}/publish`, { method: "POST" });
      notice(`平台已返回发布回执：${JSON.stringify(result)}`);
      await doRefreshDynamics();
    } finally {
      model.dynamicPostPending = false;
      doRenderPost();
    }
  }));

  $("dynamic-pick-meme")?.addEventListener("click", () => {
    const post = (model.dynamicPosts || []).find(item => item.id === model.dynamicPostId);
    if (!post || !dynamicMemePicker) return;
    model.dynamicMemeContext = { id: post.id, character: post.character_id, version: post.character_version };
    dynamicMemePicker.open(post.character_id, post.meme_id);
  });

  $("dynamic-clear-meme")?.addEventListener("click", () => busy($("dynamic-clear-meme"), async () => {
    const post = (model.dynamicPosts || []).find(item => item.id === model.dynamicPostId);
    if (!post) return;
    await doSelectDynamicMeme(null, null, { id: post.id, character: post.character_id, version: post.character_version });
  }));

  return {
    refreshDynamics: doRefreshDynamics,
    renderDynamicPosts: doRenderPosts,
    renderDynamicPost: doRenderPost,
    renderDynamicPolicy: doRenderPolicy,
    selectDynamicMeme: doSelectDynamicMeme,
  };
}
