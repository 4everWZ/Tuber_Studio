import { $, node } from "./core.js";

const pageSize = 30;
const sources = { manual: "手动", interaction: "公开互动", discovery: "自主浏览" };
const statuses = {
  pending_review: "待审核", approved: "已审核 · 待提交", publish_uncertain: "待核对",
  publishing: "提交中", published: "平台已受理", rejected: "已拒绝", copied: "已复制",
  private_handoff: "已转私信", queued: "排队中", running: "处理中", completed: "已完成",
  failed: "失败", partial: "部分完成", cancelled: "已取消",
};
export const commentQueueViews = ["review", "verify", "processing", "history"];
const queueViewForStatus = status => {
  if (["pending_review", "approved"].includes(status)) return "review";
  if (status === "publish_uncertain") return "verify";
  if (["queued", "running", "publishing"].includes(status)) return "processing";
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
  const filter = $("queue-view"), sourcesButton = $("open-manual-comments");
  const draftEdits = new Map();
  let view = "review", offset = 0, routeKey = null, inferRouteView = false, selected = null, version = 0;
  let rows = [], listSignature = "", detailSignature = "", active = false, busy = false, loaded = false, focusedKey = null;

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
          node("strong", row.excerpt || statuses[row.status] || "评论记录"),
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
    const canPublish = status === "approved" && row.can_publish;
    let publish, copy;

    function syncActions() {
      const dirty = draftEdits.has(key);
      if (publish) publish.disabled = dirty || busy;
      if (copy) copy.disabled = dirty || busy;
      continuation.render(row.continuation, { channel, localId: row.id, dirty, entityStatus: status });
    }
    editor.addEventListener("input", () => {
      if (editor.value === (row.text || "")) draftEdits.delete(key);
      else draftEdits.set(key, editor.value);
      syncActions();
    });
    const action = (text, handler, className) => {
      const button = node("button", text, className);
      button.type = "button";
      button.onclick = async () => {
        if (busy) return;
        busy = true;
        ++version;
        for (const control of actions.querySelectorAll("button")) control.disabled = true;
        editor.readOnly = true;
        try {
          await handler();
          draftEdits.delete(key);
          focusedKey = null;
          busy = false;
          await refresh(true);
        } catch (error) {
          busy = false;
          await refresh(true);
          feedback.textContent = error.message;
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
      action("保存并审核通过", () => post("review", { action: "approve", text: editor.value }), "primary");
      action("拒绝", () => post("review", { action: "reject" }));
      if (canPublish) publish = action("发布已审核回复", () => post("publish"));
      if (["approved", "copied"].includes(status)) {
        copy = action("复制回复", async () => {
          await navigator.clipboard.writeText(row.text);
          if (channel === "comments" && status === "approved") await post("copy");
          message.textContent = "已复制回复";
        });
      }
      if (status === "approved" && !row.can_publish) {
        message.textContent ||= row.unavailable_materials ? "先重新取材并核对来源，再提交。" : "配置 B 站写入凭证后可发布。";
      }
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
    if (!active || view === "sources") return;
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
      const data = await api(`/api/comments/workbench?view=${view}&limit=${pageSize}&offset=${offset}`);
      if (request !== version || !active) return;
      loaded = true;
      offset = data.offset;
      const previous = selected, previousRow = rows.find(row => keyOf(row) === previous);
      rows = data.items;
      const current = rows.find(row => keyOf(row) === previous);
      selected = routeKey || keyOf(current || rows[0]);
      renderRows(data);
      feedback.replaceChildren();
      if (!data.total) {
        const discovery = discoveryStatus();
        const idle = view === "review" && discovery?.state === "off" && !discovery?.overlay_enabled;
        feedback.append("此视图暂无记录。", link(idle ? " 前往开始自动刷视频" : " 查看自动刷视频状态", "#automation"));
      }
      if (!selected) emptyDetail("选择一条记录查看来源和下一步操作。");
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
    if (view !== nextView) offset = 0;
    view = nextView;
    if (view !== "sources") filter.value = view;
  }

  function open(nextView, nextKey = null) {
    active = true;
    setView(nextView || (view === "sources" ? filter.value : view));
    routeKey = nextKey;
    inferRouteView = Boolean(routeKey && !nextView);
    $("comment-sources").hidden = view !== "sources";
    $("comment-queue").hidden = view === "sources";
    sourcesButton.textContent = view === "sources" ? "返回草稿待办" : "评论来源与手动取材";
    if (view === "sources") return refreshSources();
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
    offset = 0;
    clearLinkedRecord(filter.value);
    return open(filter.value);
  };
  sourcesButton.onclick = () => { location.hash = view === "sources" ? "comments" : "comment-acquisition"; };
  $("queue-prev").onclick = () => { clearLinkedRecord(view); offset = Math.max(0, offset - pageSize); return refresh(); };
  $("queue-next").onclick = () => { clearLinkedRecord(view); offset += pageSize; return refresh(); };
  emptyDetail("选择一条记录查看来源和下一步操作。");
  return {
    open, refresh: () => !busy && refresh(), isSources: () => active && view === "sources",
    leave() { active = false; ++version; },
    hasUnsaved: () => draftEdits.size > 0,
  };
}
