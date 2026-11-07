import { $, node, safeMessage } from "./core.js";

const pageSize = 10;
const readLabels = { complete: "完整阅读", fully_read: "完整阅读", full: "完整阅读", partial: "部分阅读", sampled_visual: "抽样画面", title_only: "仅看标题", comments_only: "只看评论", not_read: "未阅读", none: "未阅读", new: "尚未观看", started: "已开始观看", reading: "阅读中", viewed: "已看过" };
const statusLabels = { published: "已发送", publish_uncertain: "结果待确认", automatic_skipped: "未发送", failed: "发送失败", pending_review: "待审核", approved: "已保存", private_handoff: "已转私信", received: "收到来信", queued: "排队生成中" };

export function initOperationsActivity(api) {
  const list = $("discovery-activity-list"), date = $("discovery-history-day");
  const feedback = $("discovery-history-feedback"), previous = $("discovery-history-prev"), next = $("discovery-history-next");
  let page = 1, request = 0, signature = "", kind = "all";

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
    const current = JSON.stringify(data.items);
    if (current === signature) return;
    signature = current;
    list.replaceChildren();
    for (const item of data.items || []) {
      const itemKind = item.kind || "video";
      const row = node("article", null, "daily-video-row");
      const heading = node("div", null, "daily-video-heading");
      const names = { comment: item.reply_kind === "top_level" || item.comment_kind === "top_level" ? "视频主评" : "评论回复", message: item.direction === "incoming" ? "收到私信" : "私信回复", dynamic: "动态" };
      const label = itemKind === "video" ? item.title || item.bvid : item.title || [names[itemKind], item.recipient_name].filter(Boolean).join(" · ");
      const title = node(item.href || itemKind === "video" ? "a" : "strong", label);
      if (item.href || itemKind === "video") {
        title.href = item.href || `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}`;
        if (title.href.startsWith("http")) { title.target = "_blank"; title.rel = "noreferrer"; }
      }
      const read = item.fully_read === true ? "完整阅读" :
        item.fully_read === false && ["complete", "full", "fully_read"].includes(item.read_state) ? "已看过" :
          readLabels[item.read_state] || (item.watched ? "已看过" : "未阅读");
      const result = item.status === "received" ? "收到来信" : statusLabels[item.status] || TuberFormatters.humanizeStatus(item.status || "running").label;
      heading.append(title, node("span", itemKind === "video" ? read : result, "badge"));
      row.append(heading);
      const preferences = { like: "喜欢", not_interested: "不感兴趣", neutral: "一般" };
      const preference = preferences[item.video_preference];
      const reasons = [preference ? `偏好：${preference}${item.preference_reason ? ` · ${item.preference_reason}` : ""}` : "",
        item.reason && (!preference || item.reason !== item.preference_reason) ? item.reason : ""].filter(Boolean).join("；");
      if (reasons) row.append(node("p", safeMessage(reasons), "daily-video-reason"));
      const replies = node("div", null, "daily-video-replies");
      if (itemKind === "video") replies.append(node("span", `${item.main_replies > 0 ? `今日已发主评 ${item.main_replies} 条` : item.has_main_reply ? "已有主评" : "未发主评"}${item.comment_replies > 0 ? ` · 今日跟帖 ${item.comment_replies} 条` : ""}`, "hint"));
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
      info.append(node("summary", "处理详情"));
      if (reasons) info.append(node("p", safeMessage(reasons)));
      const progress = TuberFormatters.humanizeStatus(item.status || "running").label;
      info.append(node("p", `${item.bvid || names[itemKind] || "活动"} · ${progress}${item.updated_at ? ` · ${new Date(item.updated_at * 1000).toLocaleString("zh-CN")}` : ""}`, "hint"));
      if (item.error) info.append(node("p", `${safeMessage(item.error)}${item.occurrences > 1 ? `（记录 ${item.occurrences} 次）` : ""}`, "error-text"));
      if (item.round_id) {
        const source = node("a", "查看来源记录");
        source.href = `#automation/round/${item.round_id}`;
        info.append(source);
      }
      row.append(info);
      list.append(row);
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
