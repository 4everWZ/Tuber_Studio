import { $, node } from "./core.js";

const pageSize = 10;
const sources = { manual: "手动", interaction: "公开互动", discovery: "自主浏览" };
const statuses = {
  pending_review: "待审核", approved: "已审核 · 待提交", publish_uncertain: "待核对",
  publishing: "提交中", published: "平台已受理", rejected: "已拒绝", copied: "已复制",
  private_handoff: "已转私信", pending: "排队中", queued: "排队中", running: "处理中", completed: "已完成",
  failed: "失败", partial: "部分完成", cancelled: "已取消",
};
export const commentQueueViews = ["review", "verify", "processing", "history", "activity"];
const queueViewForStatus = status => {
  if (["pending_review", "approved"].includes(status)) return "review";
  if (status === "publish_uncertain") return "verify";
  if (["pending", "queued", "running", "publishing"].includes(status)) return "processing";
  return "history";
};
const keyOf = row => row && `${row.channel}/${row.id}`;

function link(label, href) {
  const result = node("a", label);
  result.href = href;
  return result;
}

function details(label, content) {
  const result = node("details", null, "subsection");
  result.append(node("summary", label), content);
  return result;
}

function receiptText(value) {
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch (_) { return value; }
  }
  return JSON.stringify(value, null, 2);
}

export function evidenceList(segments = []) {
  const result = node("div", null, "evidence-list");
  for (const segment of segments) {
    const seconds = segment.start ?? segment.time ?? segment.timestamp;
    const source = [segment.id?.startsWith("frame:") ? "画面观察" : "字幕原文",
      segment.cid && `CID ${segment.cid}`, Number.isFinite(seconds) && `${seconds} 秒`].filter(Boolean);
    if (source.length) result.append(node("p", source.join(" · "), "hint"));
    result.append(node("blockquote", segment.text || segment.observation || segment.content || "材料正文不可用", "quoted-text"));
  }
  return result;
}

export function initCommentQueue(api, refreshSources, discoveryStatus) {
  const list = $("queue-list"), detail = $("queue-detail"), feedback = $("queue-feedback");
  const filter = $("queue-view");
  const draftEdits = new Map();
  let view = "review", offset = 0, routeKey = null, inferRouteView = false, selected = null, version = 0;
  let rows = [], listSignature = "", detailSignature = "", active = false, busy = false, loaded = false, focusedKey = null;
  let generation = null;
  let sourceVersion = 0;

  function emptyDetail(text) {
    detailSignature = "";
    detail.replaceChildren(node("h2", "记录详情"), node("p", text, "hint"));
  }

  function renderRows(data) {
    const signature = JSON.stringify([view, data.items, selected]);
    if (signature !== listSignature) {
      const focused = document.activeElement?.closest("[data-queue-key]")?.dataset.queueKey;
      list.replaceChildren();
      for (const row of data.items) {
        const id = keyOf(row);
        const item = link("", `#comments/${view}/${id}`);
        item.dataset.queueKey = id;
        item.className = "queue-row";
        if (id === selected) item.setAttribute("aria-current", "true");
        item.append(
          node("span", `${sources[row.source]} · ${statuses[row.status] || row.status}`, "hint"),
          node("strong", (row.excerpt || statuses[row.status] || "评论记录").slice(0, 80) + ((row.excerpt?.length || 0) > 80 ? "…" : "")),
          node("span", row.video_id || (row.channel === "jobs" ? "生成任务" : ""), "hint"),
        );
        list.append(item);
        if (focused === id) item.focus({ preventScroll: true });
      }
      listSignature = signature;
    }
    $("queue-prev").disabled = !data.offset;
    $("queue-next").disabled = data.offset + data.items.length >= data.total;
    $("queue-prev").parentElement.hidden = data.total <= pageSize;
    $("queue-count-label").textContent = data.total ? `${data.offset + 1}–${data.offset + data.items.length} / ${data.total}` : "";
  }

  function renderDetail(key, row) {
    const signature = JSON.stringify([key, row]);
    if (signature === detailSignature) return;
    const channel = key.split("/")[0], status = row.status;
    detailSignature = signature;
    detail.replaceChildren(node("h2", statuses[status] || "记录详情"));
    if (status === "private_handoff") {
      detail.append(link("到私信工作台处理", "#messages"));
      return;
    }
    if (channel === "jobs") {
      detail.append(node("p", `已处理 ${row.done ?? 0} / ${row.total ?? 0} 条 · 失败 ${row.errors ?? 0} 条`));
      if (row.error) detail.append(node("p", row.error, "error-text"));
      return;
    }
    const target = row.target || {}, video = row.video_id || target.bvid;
    const original = row.source_text || target.source_text;
    const metadata = [video, row.author || target.recipient_name, row.thread_id && `楼 ${row.thread_id}`,
      row.character_id && `${row.character_id} · v${row.character_version}`].filter(Boolean);
    if (metadata.length) detail.append(node("p", metadata.join(" · "), "hint"));
    if (original) detail.append(node("blockquote", original, "quoted-text"));
    if (video && /^BV[a-zA-Z0-9]+$/.test(video)) {
      const source = link("在 B 站查看来源", `https://www.bilibili.com/video/${video}`);
      source.target = "_blank";
      source.rel = "noreferrer";
      detail.append(source);
    }
    const editable = ["pending_review", "approved", "copied"].includes(status);
    const editor = node("textarea");
    editor.rows = 5;
    editor.value = draftEdits.get(key) ?? row.text ?? "";
    editor.readOnly = !editable;
    const field = node("label", status === "published" ? "回复正文" : "草稿正文");
    field.append(editor);
    detail.append(field);
    const actions = node("div", null, "actions");
    const message = node("p", row.error || "", row.error ? "error-text" : "hint");
    message.setAttribute("role", "status");
    const base = channel === "comments" ? "/api/comments/drafts/" : "/api/automation/intents/";
    const continuation = new OperationContinuationView(node("section"), api, error => { message.textContent = error; }, () => refresh(true));
    const canPublish = Boolean(row.can_publish);
    let publish, copy;

    function syncActions() {
      const dirty = draftEdits.has(key);
      if (publish) publish.disabled = !canPublish || busy;
      if (copy) copy.disabled = busy;
      continuation.render(row.continuation, { channel, localId: row.id, dirty, entityStatus: status });
    }
    editor.addEventListener("input", () => {
      if (editor.value === (row.text || "")) draftEdits.delete(key);
      else draftEdits.set(key, editor.value);
      syncActions();
    });
    const action = (text, handler, className, success, clearEdits = true) => {
      const button = node("button", text, className);
      button.type = "button";
      button.onclick = async () => {
        if (busy) return;
        busy = true;
        ++version;
        for (const control of actions.querySelectorAll("button")) control.disabled = true;
        editor.readOnly = true;
        message.textContent = `${text}中…`;
        feedback.textContent = `${text}中…`;
        try {
          await handler();
          if (clearEdits) draftEdits.delete(key);
          focusedKey = null;
          busy = false;
          await refresh(true);
          feedback.textContent = success || `${text}完成。`;
        } catch (error) {
          busy = false;
          await refresh(true);
          feedback.textContent = `${text}失败：${error.message}`;
        } finally {
          busy = false;
          editor.readOnly = !editable;
          for (const control of actions.querySelectorAll("button")) control.disabled = false;
          syncActions();
        }
      };
      actions.append(button);
      return button;
    };
    const post = (endpoint, body) => api(`${base}${encodeURIComponent(row.id)}/${endpoint}`, { method: "POST", body });
    if (editable) {
      action("保存", () => post("review", { action: "approve", text: editor.value }), "primary", "已保存，草稿审核通过。");
      action("拒绝", () => post("review", { action: "reject" }));
      publish = action("发布", async () => {
        if (status !== "approved" || draftEdits.has(key)) {
          await post("review", { action: "approve", text: editor.value });
          draftEdits.delete(key);
        }
        await post("publish");
      }, null, "发布请求已完成，结果见记录状态与平台回执。");
      copy = action("复制", async () => {
          await navigator.clipboard.writeText(editor.value);
          if (channel === "comments" && status === "approved" && !draftEdits.has(key)) await post("copy");
          message.textContent = "已复制回复";
        }, null, "已复制回复。", false);
      if (!row.can_publish) {
        message.textContent ||= row.unavailable_materials ? "先重新取材并核对来源，再提交。" : "配置 B 站写入凭证后可发布。";
      }
    }
    if (!editable && row.text) {
      copy = action("复制", () => navigator.clipboard.writeText(row.text), null, "已复制回复。", false);
    }
    if (status === "publish_uncertain" && !row.continuation) {
      message.textContent ||= "等待核对平台结果。此旧记录没有可恢复的核对任务。";
    }
    detail.append(actions, message, continuation.root);
    syncActions();
    if (row.round_id) detail.append(link("查看来源轮次", `#automation/round/${row.round_id}`));
    if (row.evidence_segments?.length) {
      detail.append(details(`引用原文（${row.evidence_segments.length}）`, evidenceList(row.evidence_segments)));
    }
    if (row.unavailable_materials) detail.append(node("p", `${row.unavailable_materials} 项原材料已失效或清理。`, "hint"));
    if (row.answer_check) detail.append(details("来源与完整性检查", node("pre", receiptText(row.answer_check), "operation-revision-text")));
    if (row.receipt) {
      detail.append(details("平台回执", node("pre", receiptText(row.receipt), "operation-revision-text")));
    }
    if (row.visibility && row.status === "published") {
      const visibility = { visible: "已查到公开记录", not_visible: "平台标记不可见", not_found: "暂未查到", check_failed: "可见性检查失败", not_checked: "尚未检查" };
      detail.append(node("p", `平台可见性 · ${visibility[row.visibility] || row.visibility}`, "hint"));
    }
    if (matchMedia("(max-width: 760px)").matches && focusedKey !== key && routeKey === key) {
      focusedKey = key;
      detail.scrollIntoView({ block: "start" });
    }
  }

  async function refresh(forceDetail = false) {
    if (!active) return;
    if (view === "sources") {
      await refreshSources();
      if (forceDetail && selected) {
        const row = await api(`/api/comments/workbench/${selected}`);
        renderDetail(selected, row);
      }
      return;
    }
    const request = ++version;
    if (!loaded) feedback.textContent = "正在读取评论记录…";
    try {
      let routedDetail;
      if (routeKey && inferRouteView) {
        routedDetail = await api(`/api/comments/workbench/${routeKey}`);
        if (request !== version || !active) return;
        setView(queueViewForStatus(routedDetail.status));
        inferRouteView = false;
      }
      const scope = generation ? `&task_id=${encodeURIComponent(generation.id)}${generation.commentIds?.length ? `&comment_ids=${generation.commentIds.join(",")}` : ""}` : "";
      const data = await api(`/api/comments/workbench?view=${view}&limit=${pageSize}&offset=${offset}${scope}`);
      if (request !== version || !active) return;
      if (generation && !data.items.length && data.task?.status === "completed" && data.task.existing_drafts?.length) {
        data.items = data.task.existing_drafts;
        data.total = data.items.length;
        data.offset = 0;
        generation.existing = true;
      }
      loaded = true;
      offset = data.offset;
      const previous = selected, previousRow = rows.find(row => keyOf(row) === previous);
      rows = data.items;
      const current = rows.find(row => keyOf(row) === previous);
      selected = routeKey || keyOf(current || rows[0]);
      if (generation && !generation.selectedFirst && rows.length) {
        selected = keyOf(rows[0]);
        generation.selectedFirst = true;
      }
      renderRows(data);
      feedback.replaceChildren();
      if (generation) {
        const task = data.task;
        feedback.textContent = generation.existing ? "所选评论已有草稿，已打开原草稿。" : task ? `本次生成：${task.done || 0} / ${task.total || 0}${task.errors ? ` · ${task.errors} 条未完成` : ""} · ${statuses[task.status] || task.status}${task.error ? ` · ${task.error}` : ""}` : "正在读取本次生成结果…";
      }
      if (!data.total && !generation) {
        const discovery = discoveryStatus();
        const idle = view === "review" && discovery?.state === "off" && !discovery?.overlay_enabled;
        feedback.append("此视图暂无记录。", link(idle ? " 前往开始自动刷视频" : " 查看自动刷视频状态", "#automation"));
      }
      if (!selected) emptyDetail(generation ? ["completed", "failed", "cancelled"].includes(data.task?.status) ? "本次任务已结束，未生成可审核的回复；请查看任务进度中的原因。" : "正在生成回复，完成后会在这里显示并自动选中。" : "选择一条记录查看来源和下一步操作。");
      else if (routedDetail || forceDetail || !detailSignature || previous !== selected || previousRow?.updated_at !== current?.updated_at || (routeKey && !current)) {
        const key = selected;
        const row = routedDetail || await api(`/api/comments/workbench/${key}`);
        if (request === version && active) renderDetail(key, row);
      }
    } catch (error) {
      if (request !== version || !active) return;
      feedback.textContent = `读取失败 · ${error.message}${loaded ? "（保留上次结果）" : ""}`;
    }
  }

  function setView(nextView) {
    if (nextView === "activity") nextView = "review";
    if (view !== nextView) offset = 0;
    view = nextView;
    filter.value = view;
    for (const option of $("queue-status-options").querySelectorAll('input[name="queue-status"]')) {
      option.checked = option.value === view;
    }
    $("queue-records").hidden = false;
    $("comment-source-records").hidden = view !== "sources";
    $("queue-list").hidden = view === "sources";
    $("queue-list-title").textContent = view === "sources" ? "选择要回复的评论" : generation ? "本次生成的回复" : "评论草稿";
    $("queue-show-all").hidden = !generation;
    if (view === "sources") $("queue-prev").parentElement.hidden = true;
  }

  function open(nextView, nextKey = null) {
    active = true;
    if (nextView === "sources") generation = null;
    setView(nextView || view);
    routeKey = nextKey;
    inferRouteView = Boolean(routeKey && !nextView);
    $("comment-sources").hidden = false;
    $("comment-queue").hidden = false;
    if (view === "sources") {
      $("comment-sources").open = true;
      feedback.textContent = "勾选评论后生成回复，生成结果会直接显示在右侧。";
      return refresh();
    }
    if (routeKey && routeKey !== selected) emptyDetail("正在读取记录…");
    return refresh();
  }

  function clearLinkedRecord(nextView) {
    const hash = `#comments/${nextView}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
    routeKey = null;
    inferRouteView = false;
  }

  filter.onchange = () => {
    generation = null;
    offset = 0;
    clearLinkedRecord(filter.value);
    return open(filter.value);
  };
  $("queue-status-options").addEventListener("change", event => {
    if (event.target.name !== "queue-status") return;
    filter.value = event.target.value;
    void filter.onchange();
  });
  $("refresh-queue").onclick = async event => {
    if (busy) return;
    const button = event.currentTarget;
    button.disabled = true;
    try { await refresh(true); }
    finally { button.disabled = false; }
  };
  $("queue-show-all").onclick = () => { generation = null; routeKey = null; selected = null; detailSignature = ""; return open("review"); };
  $("queue-prev").onclick = () => { clearLinkedRecord(view); offset = Math.max(0, offset - pageSize); return refresh(); };
  $("queue-next").onclick = () => { clearLinkedRecord(view); offset += pageSize; return refresh(); };
  emptyDetail("选择一条记录查看来源和下一步操作。");
  return {
    open, refresh: () => !busy && refresh(),
    leave() { active = false; ++version; },
    hasUnsaved: () => draftEdits.size > 0,
    async trackGeneration(id, commentIds) {
      generation = { id, commentIds, selectedFirst: false };
      routeKey = selected = null;
      detailSignature = "";
      offset = 0;
      if (typeof history !== "undefined") history.replaceState(null, "", `#comments/review/task/${encodeURIComponent(id)}`);
      await open("review");
      $("queue-records").scrollIntoView?.({ block: "start", behavior: "smooth" });
    },
    async showComment(comment) {
      const request = ++sourceVersion;
      if (view !== "sources" || !comment) return;
      if (!comment.draft) {
        selected = null;
        detailSignature = "";
        detail.replaceChildren(node("h2", comment.display_name || "匿名观众"), node("blockquote", comment.content, "quoted-text"), node("p", "勾选这条评论并生成回复，即可在这里直接审核。", "hint"));
        return;
      }
      const key = `comments/${comment.draft.id}`;
      try { const row = await api(`/api/comments/workbench/${key}`); if (view === "sources" && request === sourceVersion) { selected = key; renderDetail(key, row); } }
      catch (error) { feedback.textContent = error.message; }
    },
  };
}
