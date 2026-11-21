import { MemePicker } from "../memes.js";
import { illustrationUrl, illustrationStatus } from "./operation-image.js";
import { $, node, safeMessage } from "./core.js";
import { commentFinished } from "./comment-state.js?v=20261107.13";
import { replyHandoff } from "./reply-handoff.js";
import { mentionEditor } from "./public-mentions.js";

const pageSize = 10;
const sources = { manual: "手动", interaction: "公开互动", discovery: "自主浏览" };
const statuses = {
  pending_review: "待审核", approved: "已审核 · 待提交", publish_uncertain: "待核对",
  publishing: "提交中", published: "平台已受理", rejected: "已拒绝", copied: "已复制", operator_replied: "账号已在原楼回复",
  private_handoff: "已转私信", pending: "排队中", queued: "排队中", running: "处理中", completed: "已完成",
  failed: "失败", partial: "部分完成", cancelled: "已取消",
  send_queued: "已授权待发", send_waiting: "等额度或恢复", send_blocked: "发布被拒／待处理",
  rejected_by_platform: "平台已拒绝", source_changed: "原楼已失效或变化", unverified: "原楼暂时无法核验", ready: "可排队",
};
export const commentQueueViews = ["review", "authorized", "blocked", "verify", "processing", "history", "activity"];
const queueViewForStatus = status => {
  if (["pending_review", "approved"].includes(status)) return "review";
  if (status === "publish_uncertain") return "verify";
  if (["send_queued", "send_waiting"].includes(status)) return "authorized";
  if (["send_blocked", "rejected_by_platform"].includes(status)) return "blocked";
  if (["pending", "queued", "running", "publishing"].includes(status)) return "processing";
  return "history";
};
const keyOf = row => row && `${row.channel}/${row.id}`;
const workbench = "/api/comments/workbench";
const canBatchPublish = row => row.can_publish === true && ["comments", "browsing"].includes(row.channel);

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

export function initCommentQueue(api, refreshSources, discoveryStatus, { onDraftUpdated = () => {}, onSelectionCleared = () => {} } = {}) {
  const list = $("queue-list"), detail = $("queue-detail"), feedback = $("queue-feedback");
  const filter = $("queue-view");
  const batchActions = $("queue-batch-actions"), batchToggle = $("queue-batch-toggle"), batchPublish = $("queue-batch-publish");
  const batchSave = $("queue-batch-save"), approval = $("queue-approval");
  const batchProgress = $("queue-batch-progress"), batchResult = $("queue-batch-result");
  const draftEdits = new Map();
  const checkedDrafts = new Map(), rowChecks = new Map();
  const delegate = replyHandoff(api), batchDelegate = $("queue-batch-delegate");
  let view = "review", offset = 0, routeKey = null, inferRouteView = false, selected = null, version = 0;
  let rows = [], listSignature = "", detailSignature = "", active = false, busy = false, loaded = false, focusedKey = null;
  let generation = null;
  let picker, savePickedImage;
  let sourceVersion = 0;
  let selectionCleared = false;
  let batchMode = false, batchScope = null, selectionRequest = 0, selectionLoading = false, allSelected = false;

  function scopeParams(useExisting = false) {
    if (!generation) return "";
    const params = new URLSearchParams();
    if (!useExisting || !generation.existing) params.set("task_id", generation.id);
    if (generation.commentIds?.length) params.set("comment_ids", generation.commentIds.join(","));
    return params.toString();
  }

  function syncBatchControls() {
    batchActions.hidden = view !== "review" || !batchMode;
    batchToggle.disabled = busy || selectionLoading;
    batchToggle.textContent = allSelected ? "全不选" : "全选所有页";
    batchPublish.disabled = busy || selectionLoading || !checkedDrafts.size;
    batchSave.disabled = batchPublish.disabled;
    const selectedRows = [...checkedDrafts.values()];
    batchDelegate.disabled = batchPublish.disabled || selectedRows.length > 100 ||
      selectedRows.some(row => row.channel !== "comments" || !row.comment_id || draftEdits.has(keyOf(row)));
    $("queue-batch-count").textContent = `已选 ${checkedDrafts.size} 条`;
    for (const [key, { check, row }] of rowChecks) {
      check.checked = checkedDrafts.has(key);
      check.disabled = busy || selectionLoading || (!check.checked && !canBatchPublish(row));
    }
  }

  function setBatchProgress(text) {
    batchProgress.textContent = text;
    batchProgress.hidden = !text;
  }

  function syncBatchScope() {
    const scope = view === "review" ? scopeParams(true) : null;
    if (scope !== batchScope) {
      batchScope = scope;
      ++selectionRequest;
      selectionLoading = allSelected = batchMode = false;
      checkedDrafts.clear();
      setBatchProgress("");
    }
    syncBatchControls();
  }

  function showBatchResults(results, submitted, mode) {
    const output = $("queue-batch-results");
    output.replaceChildren();
    for (const result of results) {
      const key = keyOf(result);
      const item = node("li");
      item.append(link(submitted.get(key)?.excerpt?.slice(0, 80) || "查看草稿", `#comments/${key}`),
        ` · ${statuses[result.status] || result.status}`);
      if (result.error) item.append(` · ${safeMessage(result.error)}`);
      output.append(item);
    }
    const done = results.filter(row => row.status === (mode === "save" ? "approved" : "send_queued")).length;
    $("queue-batch-summary").textContent = `${mode === "save" ? "已保存审核" : "已入发送队列"} ${done} 条 · 另有 ${results.length - done} 条待处理`;
    batchResult.hidden = false;
    batchResult.open = true;
  }

  function emptyDetail(text) {
    detailSignature = "";
    detail.replaceChildren(node("h2", "记录详情"), node("p", text, "hint"));
  }

  function clearProcessedSelection() {
    selected = routeKey = null;
    inferRouteView = false;
    selectionCleared = true;
    onSelectionCleared();
    ++sourceVersion;
    if (typeof history !== "undefined") history.replaceState(null, "", generation ? `#comments/review/task/${encodeURIComponent(generation.id)}` : `#comments/${view}`);
    emptyDetail("本条已处理，可在「已执行与历史」中查看。请选择下一条记录。");
  }

  function renderRows(data) {
    const signature = JSON.stringify([view, data.items, selected]);
    if (signature !== listSignature) {
      const focusedElement = document.activeElement;
      const focused = focusedElement?.closest("[data-queue-key]")?.dataset.queueKey;
      list.replaceChildren();
      rowChecks.clear();
      for (const row of data.items) {
        const id = keyOf(row);
        const item = link("", `#comments/${view}/${id}`);
        item.dataset.queueKey = id;
        if (row.illustration) {
          const url = illustrationUrl(row.channel, row.id, row.illustration);
          if (url) {
            const image = node("img");
            image.src = url;
            image.alt = "回复配图";
            image.width = image.height = 52;
            item.append(image);
          } else item.append(node("small", illustrationStatus(row.illustration)));
        }
        item.className = "queue-row";
        if (id === selected) item.setAttribute("aria-current", "true");
        item.append(
          node("span", `${sources[row.source]} · ${statuses[row.status] || row.status}`, "hint"),
          node("strong", (row.excerpt || statuses[row.status] || "评论记录").slice(0, 80) + ((row.excerpt?.length || 0) > 80 ? "…" : "")),
          node("span", row.video_id || (row.channel === "jobs" ? "生成任务" : ""), "hint"),
        );
        if (row.send_error) item.append(node("small", safeMessage(row.send_error), "hint"));
        if (row.status === "send_waiting" && row.send_wake_at) {
          item.append(node("small", `预计 ${new Date(row.send_wake_at * 1000).toLocaleString()} 恢复`, "hint"));
        }
        if (generation) item.onclick = event => {
          if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          routeKey = id;
          return refresh();
        };
        if (view === "review" && row.channel !== "jobs") {
          const wrapper = node("div", null, "queue-check-row");
          wrapper.dataset.queueKey = id;
          const label = node("label"), check = node("input");
          check.type = "checkbox";
          check.dataset.queueKey = id;
          check.setAttribute("aria-label", `选择草稿：${(row.excerpt || row.video_id || "评论草稿").slice(0, 80)}`);
          check.addEventListener("change", () => {
            if (busy || selectionLoading) return;
            batchMode = true;
            allSelected = false;
            if (check.checked) checkedDrafts.set(id, row);
            else checkedDrafts.delete(id);
            syncBatchControls();
          });
          rowChecks.set(id, { check, row });
          label.append(check);
          wrapper.append(label, item);
          list.append(wrapper);
        } else list.append(item);
        if (focused === id) (focusedElement.type === "checkbox" ? rowChecks.get(id)?.check : item)?.focus({ preventScroll: true });
      }
      listSignature = signature;
    }
    syncBatchControls();
    $("queue-prev").disabled = !data.offset;
    $("queue-next").disabled = data.offset + data.items.length >= data.total;
    $("queue-prev").parentElement.hidden = data.total <= pageSize;
    $("queue-count-label").textContent = data.total ? `${data.offset + 1}–${data.offset + data.items.length} / ${data.total}` : "";
  }

  function renderDetail(key, row) {
    const signature = JSON.stringify([key, row]);
    if (signature === detailSignature) return;
    const channel = key.split("/")[0], status = row.status;
    const send = row.send_queue;
    detailSignature = signature;
    detail.replaceChildren(node("h2", statuses[send?.status || status] || "记录详情"));
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
      row.account_id && `账号 ${row.account_id}`, (row.platform_user_id || target.recipient) && `对方 UID ${row.platform_user_id || target.recipient}`,
      (row.platform_comment_id || target.parent) && `父评 ${row.platform_comment_id || target.parent}`, row.character_id].filter(Boolean);
    if (metadata.length) detail.append(node("p", metadata.join(" · "), "hint"));
    if (send?.status === "send_waiting") {
      detail.append(node("p", `预计 ${new Date(send.wake_at * 1000).toLocaleString()} 恢复`, "hint"));
    }
    if (original) detail.append(node("blockquote", original, "quoted-text"));
    let sourceUrl;
    try {
      const url = new URL(row.source_url);
      if (url.protocol === "https:" && ["www.bilibili.com", "t.bilibili.com"].includes(url.host)) sourceUrl = url.href;
    } catch { /* A legacy row may only have the video ID. */ }
    if (!sourceUrl && video && /^BV[0-9A-Za-z]{10}$/.test(video)) sourceUrl = `https://www.bilibili.com/video/${video}`;
    if (sourceUrl) {
      const source = link("在 B 站查看来源", sourceUrl);
      source.target = "_blank";
      source.rel = "noreferrer";
      detail.append(source);
    }
    const editable = ["pending_review", "approved", "copied"].includes(status);
    const editor = node("textarea");
    editor.rows = 5;
    editor.value = draftEdits.get(key) ?? row.text ?? "";
    editor.readOnly = !editable;
    const field = node("label", draftEdits.has(key) && commentFinished(row) ? "本地未保存修改" : status === "published" ? "回复正文" : "草稿正文");
    field.append(editor);
    detail.append(field);
    const actions = node("div", null, "actions");
    const message = node("p", send?.error || row.error || "", send?.error || row.error ? "error-text" : "hint");
    message.setAttribute("role", "status");
    const base = channel === "comments" ? "/api/comments/drafts/" : "/api/automation/intents/";
    const continuation = new OperationContinuationView(node("section"), api, error => { message.textContent = error; }, () => refresh(true));
    const canPublish = Boolean(row.can_publish && !send);
    let publish, copy;

    function syncActions() {
      const dirty = draftEdits.has(key);
      if (publish) publish.disabled = !canPublish || busy;
      if (copy) copy.disabled = busy || Boolean(row.image_ref || target.image_ref);
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
        syncBatchControls();
        ++version;
        for (const control of actions.querySelectorAll("button")) control.disabled = true;
        editor.readOnly = true;
        message.textContent = `${text}中…`;
        feedback.textContent = `${text}中…`;
        try {
          const result = await handler();
          const updated = result?.status ? { ...row, ...result } : row;
          onDraftUpdated(updated, key);
          if (clearEdits) draftEdits.delete(key);
          if (commentFinished(updated) && selected === key) {
            draftEdits.delete(key);
            clearProcessedSelection();
          }
          focusedKey = null;
          busy = false;
          await refresh(true);
          feedback.textContent = success || {
            published: "已发送", publish_uncertain: "发布结果待核对", rejected: "已退回，草稿已保存。",
          }[result?.status] || "";
        } catch (error) {
          busy = false;
          await refresh(true);
          feedback.textContent = error.message;
        } finally {
          busy = false;
          syncBatchControls();
          editor.readOnly = !editable;
          for (const control of actions.querySelectorAll("button")) control.disabled = false;
          syncActions();
        }
      };
      actions.append(button);
      return button;
    };
    const post = (endpoint, body) => api(`${base}${encodeURIComponent(row.id)}/${endpoint}`, { method: "POST", body });
    const imageRef = row.illustration;
    if (imageRef) {
      const url = illustrationUrl(channel, row.id, imageRef);
      if (url) {
        const image = node("img", null, "dynamic-comment-card");
        image.src = url;
        image.alt = "将随这条评论发布的图片";
        detail.append(image);
      }
      detail.append(node("small", illustrationStatus(imageRef)));
    }
    if (row.image_uploads?.length) detail.append(details("图片上传回执", node("pre", receiptText(row.image_uploads))));
    if (editable && (row.can_attach_image || row.image_ref || target.image_ref)) {
      const save = async ref => {
        if (selected !== key) throw Error("当前草稿已变化");
        await post("review", {action: "edit", text: editor.value, image_ref: ref, updated_at: row.updated_at});
        draftEdits.delete(key);
        await refresh(true);
      };
      if (row.can_attach_image) {
        const choose = node("button", "选择配图");
        choose.type = "button";
        choose.onclick = () => {
          picker ||= new MemePicker(api, (_id, asset) => savePickedImage({asset_id: asset.asset_id, sha256: asset.sha256}),
            {title: "选择评论配图", description: "选图后与正文一同送审。", showSources: true});
          savePickedImage = save;
          picker.open(imageRef?.asset_id);
        };
        actions.append(choose);
      }
      if (row.image_ref || target.image_ref) action("移除配图", () => save(null));
    }
    if (editable) {
      action(send ? "保存并撤回排队" : "保存", () => post("review", { action: "approve", text: editor.value, updated_at: row.updated_at, expected_image: imageRef ?? null }), "primary", "已保存，草稿审核通过。");
      if (send) action("取消排队", () => post("review", { action: "approve", updated_at: row.updated_at, expected_image: imageRef ?? null }), "", "已取消排队。", false);
      action("拒绝", () => post("review", { action: "reject" }));
      publish = action("发布", async () => {
        if (status !== "approved" || draftEdits.has(key)) {
          await post("review", { action: "approve", text: editor.value, updated_at: row.updated_at, expected_image: imageRef ?? null });
          draftEdits.delete(key);
        }
        return post("publish");
      });
      copy = action("复制", async () => {
          await navigator.clipboard.writeText(editor.value);
          if (channel === "comments" && status === "approved" && !draftEdits.has(key)) return post("copy");
          message.textContent = "已复制回复";
        }, null, "已复制回复。", false);
      if (!row.can_publish) {
        if (imageRef && !row.can_attach_image) message.textContent ||= "此楼使用文字回复，可移除原配图。";
        else message.textContent ||= row.unavailable_materials ? "先重新取材并核对来源，再提交。" : "配置 B 站写入凭证后可发布。";
      }
    }
    if (!editable && row.text) {
      copy = action("复制", () => navigator.clipboard.writeText(editor.value), null, "已复制回复。", false);
    }
    if (status === "publish_uncertain" && !row.continuation) {
      message.textContent ||= "请打开对应 B 站评论楼核对。";
    }
    detail.append(mentionEditor({
      mentions: row.mentions, pending: row.pending_mentions, editable, node,
      save: async mentions => {
        if (busy) return;
        busy = true;
        try {
          await post("review", { action: "edit", text: editor.value, updated_at: row.updated_at, mentions });
          draftEdits.delete(key);
          await refresh(true);
          feedback.textContent = "提及已保存，草稿待审核。";
        } catch (error) {
          feedback.textContent = error.message;
        } finally {
          busy = false;
          syncActions();
        }
      },
    }));
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
        setView(queueViewForStatus(routedDetail.send_queue?.status || routedDetail.status));
        inferRouteView = false;
      }
      const scope = scopeParams();
      const data = await api(`${workbench}?view=${view}&limit=${pageSize}&offset=${offset}${scope ? `&${scope}` : ""}`);
      if (request !== version || !active) return;
      if (generation && !data.items.length && data.task?.status === "completed" && data.task.existing_drafts?.length) {
        const existing = data.task.existing_drafts.filter(row => queueViewForStatus(row.status) === view);
        data.total = existing.length;
        data.offset = offset < existing.length ? offset : 0;
        data.items = existing.slice(data.offset, data.offset + pageSize);
        generation.commentIds ||= data.task.comment_ids;
        generation.existing = true;
        syncBatchScope();
      }
      loaded = true;
      offset = data.offset;
      const previous = selected, previousRow = rows.find(row => keyOf(row) === previous);
      rows = data.items;
      const current = rows.find(row => keyOf(row) === previous);
      let departedDetail, keepLocalEdit = false;
      if (previous && !current && view !== "history" && (!routeKey || routeKey === previous)) {
        departedDetail = await api(`/api/comments/workbench/${previous}`);
        if (request !== version || !active) return;
        if (commentFinished(departedDetail)) {
          onDraftUpdated(departedDetail, previous);
          keepLocalEdit = draftEdits.has(previous);
          if (!keepLocalEdit) clearProcessedSelection();
        }
      }
      selected = routeKey || (keepLocalEdit ? previous : keyOf(current || (!selectionCleared && rows[0])));
      if (generation && !generation.selectedFirst && !selectionCleared && rows.length) {
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
      if (!selected) emptyDetail(selectionCleared ? "本条已处理，可在「已执行与历史」中查看。请选择下一条记录。" : generation ? ["completed", "failed", "cancelled"].includes(data.task?.status) ? "本次任务已结束，未生成可审核的回复；请查看任务进度中的原因。" : "正在生成回复，完成后会在这里显示并自动选中。" : "选择一条记录查看来源和下一步操作。");
      else if (routedDetail || forceDetail || !detailSignature || previous !== selected || previousRow?.updated_at !== current?.updated_at || JSON.stringify(previousRow?.illustration) !== JSON.stringify(current?.illustration) || (routeKey && !current)) {
        const key = selected;
        const row = routedDetail || (key === previous ? departedDetail : null) || await api(`/api/comments/workbench/${key}`);
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
    $("selected-comment-count").hidden = view !== "sources";
    $("queue-list").hidden = view === "sources";
    $("queue-list-title").textContent = view === "sources" ? "选择要回复的评论" : generation ? "本次生成的回复" : "评论草稿";
    $("queue-show-all").hidden = !generation;
    if (view === "sources") $("queue-prev").parentElement.hidden = true;
    syncBatchScope();
  }

  function open(nextView, nextKey = null) {
    active = true;
    selectionCleared = false;
    if (nextView && nextView !== view) {
      selected = null;
      ++sourceVersion;
      emptyDetail("选择一条记录查看来源和下一步操作。");
      onSelectionCleared();
    }
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
    const hash = generation ? `#comments/review/task/${encodeURIComponent(generation.id)}` : `#comments/${nextView}`;
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

  batchToggle.onclick = async () => {
    if (busy || selectionLoading || view !== "review") return;
    if (allSelected) {
      checkedDrafts.clear();
      allSelected = false;
      syncBatchControls();
      return;
    }
    const request = ++selectionRequest;
    selectionLoading = true;
    syncBatchControls();
    setBatchProgress("正在读取所有页的可发布草稿…");
    try {
      const data = await api(`${workbench}/selection${batchScope ? `?${batchScope}` : ""}`);
      if (request !== selectionRequest) return;
      const before = new Map(checkedDrafts);
      checkedDrafts.clear();
      for (const row of data.items) {
        const key = keyOf(row);
        checkedDrafts.set(key, before.get(key) || row);
      }
      allSelected = checkedDrafts.size > 0;
      setBatchProgress(allSelected ? "" : "当前筛选没有可发布的草稿。");
    } catch (error) {
      if (request === selectionRequest) setBatchProgress(`全选失败：${safeMessage(error.message)}`);
    } finally {
      if (request === selectionRequest) {
        selectionLoading = false;
        syncBatchControls();
      }
    }
  };

  batchDelegate.onclick = async () => {
    if (batchDelegate.disabled) return;
    const items = [...checkedDrafts.values()].map(row => ({
      channel: "comments", draft_id: Number(row.id), source_ids: [row.comment_id],
    }));
    busy = true;
    syncBatchControls();
    try {
      await delegate(items);
      setBatchProgress(`已交办 ${items.length} 条回复，可在对话的活动中逐条审批。`);
      checkedDrafts.clear();
      allSelected = false;
    } catch (error) {
      setBatchProgress(safeMessage(error.message));
    } finally {
      busy = false;
      syncBatchControls();
    }
  };

  async function finishReview(mode) {
    if (busy || selectionLoading || view !== "review" || !checkedDrafts.size) return;
    const submitted = new Map(checkedDrafts);
    const scope = batchScope;
    const items = [...submitted].map(([key, { channel, id, updated_at, illustration }]) => ({
      channel, id, updated_at, expected_image: illustration ?? null, ...(draftEdits.has(key) ? { text: draftEdits.get(key) } : {}),
    }));
    busy = detail.inert = true;
    ++version;
    batchResult.hidden = true;
    batchResult.open = false;
    $("queue-batch-results").replaceChildren();
    syncBatchControls();
    setBatchProgress(mode === "save" ? "正在保存审核结果…" : "正在核对所选草稿的原楼…");
    try {
      let checked = [];
      let send = items;
      if (mode === "queue") {
        checked = (await api(`${workbench}/batch-review/preview`, { method: "POST", body: { items } })).items;
        const ready = new Set(checked.filter(row => row.status === "ready").map(keyOf));
        send = items.filter(row => ready.has(keyOf(row)));
        $("queue-approval-scope").textContent = `B 站账号 UID ${checked.find(row => row.account_id)?.account_id ?? "待核验"} · ${send.length} 条可排队`;
        $("queue-approval-items").replaceChildren(...checked.map(row => {
          const entry = node("li");
          entry.append(node("strong", statuses[row.status] || row.status));
          if (row.source_url) entry.append(" · ", link("查看原楼", row.source_url));
          if (row.updated_at) entry.append(` · ${new Date(row.updated_at * 1000).toLocaleString()}`);
          if (row.text) entry.append(node("p", row.text, "quoted-text"));
          const url = illustrationUrl(row.channel, row.id, row.illustration);
          if (url) entry.append(link("查看配图", url));
          if (row.error) entry.append(node("p", safeMessage(row.error), "hint"));
          return entry;
        }));
        $("queue-approval-submit").disabled = !send.length;
        approval.returnValue = "cancel";
        approval.showModal();
        await new Promise(resolve => approval.addEventListener("close", resolve, { once: true }));
        if (approval.returnValue !== "confirm") {
          setBatchProgress("");
          return;
        }
      }
      const result = await api(`${workbench}/batch-review/${mode}`, { method: "POST", body: { items: send } });
      showBatchResults([...result.items, ...checked.filter(row => row.status !== "ready")], submitted, mode);
      for (const row of result.items) {
        if (!["approved", "send_queued"].includes(row.status)) continue;
        draftEdits.delete(keyOf(row));
        checkedDrafts.delete(keyOf(row));
      }
      allSelected = false;
      setBatchProgress("");
    } catch (error) {
      if (batchScope === scope) setBatchProgress(safeMessage(error.message));
    } finally {
      busy = detail.inert = false;
      syncBatchControls();
      detailSignature = "";
      await refresh(true);
    }
  }
  batchSave.onclick = () => finishReview("save");
  batchPublish.onclick = () => finishReview("queue");

  $("refresh-queue").onclick = async event => {
    if (busy) return;
    const button = event.currentTarget;
    button.disabled = true;
    try { await refresh(true); }
    finally { button.disabled = false; }
  };
  $("queue-show-all").onclick = () => { generation = null; routeKey = null; selected = null; detailSignature = ""; return open("review"); };
  const turnPage = delta => {
    clearLinkedRecord(view);
    selected = null;
    onSelectionCleared();
    offset = Math.max(0, offset + delta * pageSize);
    return refresh();
  };
  $("queue-prev").onclick = () => turnPage(-1);
  $("queue-next").onclick = () => turnPage(1);
  emptyDetail("选择一条记录查看来源和下一步操作。");
  return {
    open(nextView, nextKey) {
      // A normal route opens its whole view; only trackGeneration owns task scope.
      generation = null;
      return open(nextView, nextKey);
    },
    refresh: () => !busy && refresh(),
    leave() { active = false; ++version; },
    hasUnsaved: () => draftEdits.size > 0,
    async trackGeneration(id, commentIds) {
      generation = { id, commentIds, selectedFirst: false };
      routeKey = selected = null;
      onSelectionCleared();
      detailSignature = "";
      offset = 0;
      if (typeof history !== "undefined") history.replaceState(null, "", `#comments/review/task/${encodeURIComponent(id)}`);
      await open("review");
      $("queue-records").scrollIntoView?.({ block: "start", behavior: "smooth" });
    },
    async showComment(comment) {
      const request = ++sourceVersion;
      if (view !== "sources") return;
      if (!comment) {
        selected = null;
        emptyDetail("选择一条评论查看来源和草稿。");
        return;
      }
      selectionCleared = false;
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
