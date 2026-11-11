import { $, node, safeMessage } from "./core.js";
import { contactLabel, contactReason, contactStatus } from "./message-labels.js?v=20261107.20";

const pageSize = 10;
const readLabels = { complete: "完整阅读", fully_read: "完整阅读", full: "完整阅读", partial: "部分阅读", sampled_visual: "抽样画面", sampled_audio: "抽样语音", title_only: "仅看标题", comments_only: "只看评论", not_read: "未阅读", none: "未阅读", new: "尚未观看", started: "已开始观看", reading: "阅读中", viewed: "已看过" };
const statusLabels = { published: "已发送", publish_uncertain: "结果待确认", automatic_skipped: "未发送", failed: "发送失败", pending_review: "待审核", approved: "已保存", copied: "已复制", rejected: "已拒绝", private_handoff: "已转私信", received: "收到来信", queued: "排队生成中" };

export function mainReplySummary(item) {
  if (item.main_replies > 0) return `今日已发主评 ${item.main_replies} 条`;
  if (item.has_main_reply) return "已有主评";
  const draft = item.replies?.find(reply => reply.kind === "top_level");
  const labels = { pending_review: "主评待审核", approved: "主评已审核，待发布", publishing: "主评提交中", publish_uncertain: "主评结果待核对", failed: "主评发送失败", rejected: "主评已拒绝", copied: "主评已复制" };
  if (labels[draft?.status]) return labels[draft.status];
  const action = item.main_decision?.action;
  if (action === "skip" || action === "finish") return "本次不发主评";
  if (action === "wait") return "主评等待继续";
  if (action === "reply_public") return "已决定主评，等待生成";
  if (item.read_state === "comments_only") return "只逛评论，未生成主评";
  return "主评尚未决定";
}

export function initOperationsActivity(api) {
  const list = $("discovery-activity-list"), date = $("discovery-history-day");
  const feedback = $("discovery-history-feedback"), previous = $("discovery-history-prev"), next = $("discovery-history-next");
  let page = 1, request = 0, signature = "", kind = "all";
  const expanded = new Map(), renderedRows = new Map();

  function render(data) {
    date.value = data.day;
    page = data.page;
    const counts = data.counts || data.summary || {};
    $("discovery-history-counts").textContent = (kind === "videos" ? [
      `当日视频 ${counts.videos ?? data.total ?? 0}`, `看过 ${counts.watched ?? 0}`, `回复 ${counts.replies ?? 0}`, `跳过 ${counts.skipped ?? 0}`,
    ] : [`当日总计 ${counts.total ?? data.total ?? 0}`, `已发送 ${counts.sent ?? 0}`, `待审核 ${counts.pending ?? 0}`])
      .concat(counts.failed > 0 ? [`异常 ${counts.failed}`] : []).join(" · ");
    previous.disabled = page <= 1;
    next.disabled = page >= (data.pages || Math.ceil(data.total / pageSize) || 1);
    $("discovery-history-page").textContent = `第 ${page} / ${data.pages || Math.ceil(data.total / pageSize) || 1} 页 · 共 ${data.total} 条记录`;
    const current = JSON.stringify([data.day, kind, page, data.items]);
    if (current === signature) return;
    signature = current;
    for (const [key, entry] of renderedRows) expanded.set(key, entry.info.open);
    const focus = document.activeElement;
    const focusedKey = focus?.closest?.("[data-activity-key]")?.dataset.activityKey;
    const focusedControl = focus?.dataset.activityControl;
    renderedRows.clear();
    list.replaceChildren();
    for (const item of data.items || []) {
      const itemKind = item.kind || "video";
      const row = node("article", null, "daily-video-row");
      const key = `${data.day}/${kind}/${item.id || `${itemKind}/${item.bvid || item.local_id}`}`;
      row.dataset.activityKey = key;
      const heading = node("div", null, "daily-video-heading");
      const names = { comment: item.reply_kind === "top_level" || item.comment_kind === "top_level" ? "视频主评" : "评论回复", message: contactLabel(item), dynamic: "动态" };
      const label = itemKind === "video" ? item.title || item.bvid : item.title || [names[itemKind], item.recipient_name].filter(Boolean).join(" · ");
      const title = node(item.href || itemKind === "video" ? "a" : "strong", label);
      title.dataset.activityControl = "title";
      if (item.href || itemKind === "video") {
        const href = item.href || `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}`;
        title.href = href;
        if (/^https?:\/\//i.test(href)) { title.target = "_blank"; title.rel = "noreferrer"; }
      }
      const read = item.fully_read === true ? "完整阅读" :
        item.fully_read === false && ["complete", "full", "fully_read"].includes(item.read_state) ? "已看过" :
          readLabels[item.read_state] || (item.watched ? "已看过" : "未阅读");
      const result = (itemKind === "message" ? contactStatus(item) : null) || statusLabels[item.status] || TuberFormatters.humanizeStatus(item.status || "running").label;
      heading.append(title, node("span", itemKind === "video" ? read : result, "badge"));
      row.append(heading);
      const preferences = { like: "喜欢", not_interested: "不感兴趣", neutral: "一般" };
      const preference = preferences[item.video_preference];
      const reasons = [preference ? `偏好：${preference}${item.preference_reason ? ` · ${item.preference_reason}` : ""}` : "",
        item.reason && (!preference || item.reason !== item.preference_reason) ? (itemKind === "message" ? contactReason(item.reason) : item.reason) : ""].filter(Boolean).join("；");
      if (reasons) row.append(node("p", safeMessage(reasons), "daily-video-reason"));
      const replies = node("div", null, "daily-video-replies");
      if (itemKind === "video") replies.append(node("span", `${mainReplySummary(item)}${item.comment_replies > 0 ? ` · 今日跟帖 ${item.comment_replies} 条` : ""}`, "hint"));
      if (itemKind !== "video") replies.append(node("p", item.text || item.content || (item.status === "private_handoff" ? "在私信工作台查看" : item.status === "pending_review" || item.status === "queued" ? "尚未生成回复正文" : "正文未保留")));
      for (const reply of itemKind === "video" && kind === "videos" ? item.replies || [] : []) {
        const outcome = statusLabels[reply.status] || reply.status;
        const target = reply.recipient_name || (reply.kind === "top_level" ? "视频评论区" : "原评论作者");
        const response = node("div", null, "daily-reply-row");
        response.append(node("span", `回复 ${target} · ${outcome}`, "hint"), node("p", reply.text || "正文未保留"));
        replies.append(response);
      }

      row.append(replies);
      const info = node("details", null, "daily-video-details");
      info.open = expanded.get(key) === true;
      const summary = node("summary", "处理详情");
      summary.dataset.activityControl = "summary";
      info.append(summary);
      if (reasons) info.append(node("p", safeMessage(reasons)));
      const progress = result;
      info.append(node("p", `${item.bvid || names[itemKind] || "活动"} · ${progress}${item.updated_at ? ` · ${new Date(item.updated_at * 1000).toLocaleString("zh-CN")}` : ""}`, "hint"));
      if (item.error) info.append(node("p", `${safeMessage(item.error)}${item.occurrences > 1 ? `（记录 ${item.occurrences} 次）` : ""}`, "error-text"));
      if (item.round_id) {
        const source = node("a", "查看来源记录");
        source.href = `#automation/round/${item.round_id}`;
        source.dataset.activityControl = "source";
        info.append(source);
      }
      row.append(info);
      list.append(row);
      renderedRows.set(key, { row, info });
      if (focusedKey === key && focusedControl) row.querySelector(`[data-activity-control="${focusedControl}"]`)?.focus({ preventScroll: true });
    }
    if (!data.items?.length) list.append(node("p", "这一天还没有匹配的运营活动。", "hint"));
  }

  async function refresh() {
    const current = ++request;
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize), kind });
    if (date.value) query.set("day", date.value);
    try {
      const data = await api(`/api/automation/activity?${query}`);
      if (current !== request) return;
      render(data);
      feedback.textContent = "";
    } catch (error) {
      if (current === request) feedback.textContent = `记录读取失败：${safeMessage(error.message)}`;
    }
  }

  date.addEventListener("change", () => { page = 1; return refresh(); });
  previous.onclick = () => { page = Math.max(1, page - 1); return refresh(); };
  next.onclick = () => { page++; return refresh(); };
  $("discovery-history-today").onclick = () => { date.value = ""; page = 1; return refresh(); };
  $("discovery-history-refresh").onclick = refresh;
  refresh.setKind = nextKind => {
    kind = nextKind;
    page = 1;
    for (const button of $("operation-activity-kinds").querySelectorAll("[data-activity-kind]")) button.setAttribute("aria-pressed", String(button.dataset.activityKind === kind));
  };
  $("operation-activity-kinds").addEventListener("click", event => {
    const button = event.target.closest("[data-activity-kind]");
    if (!button) return;
    refresh.setKind(button.dataset.activityKind);
    void refresh();
  });
  return refresh;
}
