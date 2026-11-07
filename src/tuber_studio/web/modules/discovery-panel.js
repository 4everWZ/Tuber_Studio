import { $, node, safeMessage } from "./core.js";
import { evidenceList } from "./comment-queue.js?v=20261107.06";

const states = {
  off: "待命", starting: "启动检查", discovering: "正在找视频", reading: "正在取材",
  judging: "正在判断与写稿", publishing: "正在发送", reconciling: "正在查询原回执", finishing: "正在结束本轮",
  waiting: "轮间等待", blocked: "需要处理", stopping: "正在停止", emergency: "已紧急停止",
};
const roundStates = { completed: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已停止", paused: "已暂停，进度保留", deferred_daily: "今日观看次数已用完" };
const decisions = {
  no_interest: "大肥鱼对这个话题没兴趣", nothing_to_add: "没有想补充的内容",
  insufficient_evidence: "现有材料不足", visual_required: "需要画面材料", unsafe_interaction: "这次不参与讨论",
  no_subtitles: "无可用字幕", visual_unavailable: "无可用画面材料", filtered: "不符合筛选条件",
  empty: "没有可用内容", specific_detail: "有具体的内容值得回应",
};
const time = seconds => new Date(seconds * 1000).toLocaleString("zh-CN");

function link(label, href) {
  const anchor = node("a", label);
  anchor.href = href;
  return anchor;
}

export function initIssueLog(api, page) {
  const entries = new Map();
  let remote = [], request = 0;

  function render() {
    const items = [...entries.values(), ...remote].sort((a, b) => b.time - a.time);
    $("workspace-issues").textContent = items.length ? `告警与错误 · ${items.length}` : "告警与错误";
    if (page() !== "issues") return;
    const root = $("workspace-issue-list");
    root.replaceChildren();
    for (const item of items) {
      const row = node("article", null, "issue-row");
      row.append(node("p", item.text), node("span", time(item.time), "hint"));
      if (item.href && item.href !== "#issues") row.append(link("打开相关位置", item.href));
      root.append(row);
    }
    if (!items.length) root.append(node("p", "暂无告警或错误。", "hint"));
  }

  function record(message, href = location.hash || "#automation") {
    const text = safeMessage(message);
    if (!text) return;
    const key = JSON.stringify([text, href]);
    if (entries.has(key)) return;
    entries.set(key, { text, href, time: Date.now() / 1000 });
    if (entries.size > 50) entries.delete(entries.keys().next().value);
    render();
  }

  async function refresh() {
    const current = ++request;
    try {
      const data = await api("/api/automation/discovery/records");
      if (current !== request) return;
      remote = data.issues.map(item => ({
        text: `${item.status === "retry_wait" ? "告警" : "错误"} · ${item.bvid ? item.bvid + " · " : ""}${item.error}`,
        time: item.updated_at, href: item.href || `#automation/round/${item.round_id}${item.bvid ? "/" + item.id : ""}`,
      }));
      render();
      if (page() !== "automation-records") return;
      const published = $("discovery-published");
      const signature = JSON.stringify(data.sent);
      if (published.dataset.snapshot === signature) return;
      published.dataset.snapshot = signature;
      published.replaceChildren();
      for (const item of data.sent) {
        const row = node("details", null, "issue-row");
        row.append(node("summary", `${item.kind === "top_level" ? "主评" : "跟帖"} · ${item.bvid} · ${time(item.updated_at)}`));
        if (item.text) row.append(node("blockquote", item.text, "quoted-text"));
        if (item.receipt_id) row.append(node("p", `平台回执 ${item.receipt_id}`, "hint"));
        row.append(link("查看来源与回执", `#comments/history/browsing/${item.id}`));
        row.addEventListener("toggle", async () => {
          if (!row.open || row.dataset.evidenceLoaded) return;
          row.dataset.evidenceLoaded = "true";
          try {
            const record = await api(`/api/comments/workbench/browsing/${item.id}`);
            if (record.evidence_segments?.length) row.append(evidenceList(record.evidence_segments));
          } catch (_) {
            delete row.dataset.evidenceLoaded;
          }
        });
        published.append(row);
      }
      if (!data.sent.length) published.append(node("p", "暂无发送记录。", "hint"));
    } catch (failure) {
      record(failure.message, "#issues");
    }
  }

  return { record, refresh, render };
}

export function initDiscoveryPanel(api) {
  let value, request = 0, changing = false, saving = false, dirty = false, actionError = false;
  const buttons = [$("discovery-toggle"), $("discovery-toggle-settings")].filter(Boolean);
  const status = $("discovery-state"), top = $("discovery-top-state");
  const settingsStatus = $("discovery-settings-state");
  const attention = $("discovery-attention"), actions = $("discovery-actions");
  const result = $("discovery-result");
  const form = $("discovery-source-form"), source = $("discovery-source"), save = $("discovery-save-source");
  const keywords = $("discovery-keywords"), videos = $("discovery-videos"), note = $("discovery-source-note");
  const sourceName = mode => source.querySelector(`option[value="${mode}"]`)?.textContent || "";
  let attentionSignature = "";

  function syncControls() {
    const unavailable = !value || status.textContent === "状态不可用";
    const toggleDisabled = unavailable || changing || saving || value.state === "emergency" ||
      (!value.enabled && dirty);
    for (const button of buttons) button.disabled = toggleDisabled;
    source.disabled = keywords.disabled = videos.disabled = unavailable || changing || saving;
    save.disabled = unavailable || changing || saving || !dirty;
    $("discovery-keywords-field").hidden = !(keywords.required = source.value === "search");
    $("discovery-videos-field").hidden = !(videos.required = source.value === "specified");
  }

  form.addEventListener("input", () => {
    dirty = true;
    note.textContent = value?.enabled ? "来源有未保存的修改。" : "保存来源后即可开始。";
    syncControls();
  });
  form.onsubmit = async event => {
    event.preventDefault();
    if (save.disabled) return;
    saving = true;
    ++request;
    syncControls();
    const selection = {
      mode: source.value,
      keywords: keywords.required ? keywords.value.split(/[,，\n]/).map(word => word.trim()).filter(Boolean) : [],
      watch_videos: videos.required ? videos.value.split(/[,，\s]+/).filter(Boolean) : [],
    };
    try {
      const data = await api("/api/automation/settings", { method: "PUT", body: { discovery_source: selection } });
      dirty = false;
      render(data);
      note.textContent = data.enabled ? "来源已保存，从下一轮起使用。" : "来源已保存。";
    } catch (_) {
      note.replaceChildren("来源保存失败。", link("查看原因", "#issues"));
    } finally {
      saving = false;
      syncControls();
    }
  };

  function render(data) {
    value = data;
    const label = states[data.state] || "状态未知";
    $("workspace-character").textContent = data.character?.name || data.character?.id || "角色待确认";
    top.textContent = `自动刷视频 · ${label}`;
    status.textContent = label;
    if (settingsStatus) settingsStatus.textContent = label;
    for (const button of buttons) button.textContent = data.enabled ? "停止自动刷视频" : "开始自动刷视频";
    if (!dirty) {
      source.value = data.source.mode;
      keywords.value = data.source.keywords.join("，");
      videos.value = data.source.watch_videos.join("，");
    }
    syncControls();
    const items = data.attention || [];
    attention.hidden = !(items.length || actionError);
    const signature = JSON.stringify([items, actionError]);
    if (signature !== attentionSignature) {
      attentionSignature = signature;
      actions.replaceChildren();
      for (const item of items) {
        const entry = node("li", item.message);
        entry.append(" ", link(item.label, item.href));
        actions.append(entry);
      }
      if (actionError) {
        const entry = node("li");
        entry.append(link("查看启动或停止失败原因", "#issues"));
        actions.append(entry);
      }
    }
    const active = data.active_round, round = active || data.last_round;
    const roundLink = $("discovery-round-link");
    $("discovery-round-heading").textContent = !active && round ? "最近一轮" : "当前一轮";
    roundLink.hidden = !round;
    if (round) roundLink.href = `#automation/round/${round.id}`;
    const timing = data.next_at ? `下次运行 ${time(data.next_at)}` : "";
    result.textContent = [
      data.daily_watch?.limit > 0 && data.daily_watch.remaining === 0 ? "今日观看次数已用完，继续处理评论" : "",
      active ? [data.current_video?.title, label].filter(Boolean).join(" · ")
        : round ? `${roundStates[round.status] || "已结束"} · ${time(round.updated_at)}`
          : "还没有运行记录。",
      timing,
    ].filter(Boolean).join(" · ");
    $("discovery-counts").textContent = round ? [
      data.daily_watch ? `今日观看 ${data.daily_watch.started}${data.daily_watch.limit ? ` / ${data.daily_watch.limit}` : "（不限）"}` : "",
      sourceName(data.round_source), `已处理 ${data.summary.processed} 个视频`,
      `已发送 ${data.sent} 条`, `已跳过 ${data.summary.skipped} 个视频`,
    ].filter(Boolean).join(" · ") : "";
  }

  async function refresh() {
    if (changing || saving) return;
    const current = ++request;
    try {
      const data = await api("/api/automation/discovery");
      if (current === request) render(data);
    } catch (_) {
      if (current !== request) return;
      top.textContent = "自动刷视频 · 状态不可用";
      status.textContent = "状态不可用";
      if (settingsStatus) settingsStatus.textContent = status.textContent;
      syncControls();
    }
  }

  async function toggle(event) {
    if (event.currentTarget.disabled || !value || changing) return;
    changing = true;
    actionError = false;
    ++request;
    syncControls();
    try {
      value = await api("/api/automation/discovery", { method: "PUT", body: { enabled: !value.enabled } });
    } catch (_) {
      actionError = true;
    } finally {
      changing = false;
      render(value);
    }
  }
  for (const button of buttons) button.onclick = toggle;
  return { refresh, status: () => value };
}

function activityLabel(item) {
  const labels = {
    published: "已发送", failed: "发送失败",
    publish_uncertain: "回执待确认", viewed_without_comment: "看过未发", skipped: "已跳过",
    automatic_skipped: "回应已跳过", retry_wait: "已记录失败", cancelled: "已停止",
    recovered: "已恢复", paused: "已暂停", deferred_daily: "次日继续", comments_checked: "已检查评论",
  };
  return labels[item.status] || item.status;
}

function activityExcerpt(item) {
  if (item.status === "retry_wait") return item.failures === 1 ? "同一轮重试一次" : "安排下一轮重试";
  if (item.status === "publish_uncertain") return item.excerpt || "原提交尚未确认";
  if (item.status === "skipped" && item.failures === 3) return "三次失败后跳过";
  if (item.status === "skipped" && !item.reason) return "不符合筛选条件";
  return item.kind !== "task" ? item.excerpt || decisions[item.reason] || "" : decisions[item.reason] || "";
}

export function initDiscoveryActivity(api) {
  const list = $("discovery-activity-list");
  const replies = node("div", null, "activity-replies"), tasks = node("div");
  const empty = node("p", "暂无回复结果。", "hint");
  const scheduling = node("details", null, "activity-scheduling");
  const schedulingSummary = node("summary", "调度详情");
  scheduling.append(schedulingSummary, tasks, link("完整运行记录", "#automation-records"));
  list.replaceChildren(empty, replies, scheduling);
  const rows = new Map();
  let request = 0, scopeKey;

  async function loadDetail(row, item) {
    row.detail.replaceChildren(node("p", "正在读取活动详情…", "hint"));
    try {
      const task = ["task", "failure"].includes(item.kind);
      const record = item.kind === "failure" ? item : task
        ? (await api(`/api/automation/tasks?task_id=${encodeURIComponent(item.id)}`))[0]
        : await api(`/api/comments/workbench/browsing/${encodeURIComponent(item.id)}`);
      if (row.item !== item) return;
      if (!record) {
        row.detail.textContent = "该条活动详情已不可用。";
        return;
      }
      const content = document.createDocumentFragment();
      const reason = task ? TuberFormatters.humanizeTaskReason(record) : safeMessage(record.explanation || record.reason);
      if (reason) content.append(node("p", reason));
      if (item.occurrences > 1) content.append(node("p", `同一问题共记录 ${item.occurrences} 次${item.status === "recovered" ? "，当前已恢复。" : "。"}`, "hint"));
      if (!task && record.text) content.append(node("blockquote", record.text, "quoted-text"));
      if (!task && record.target?.source_text) {
        content.append(node("p", record.target.recipient_name || "回复的原评论", "hint"),
          node("blockquote", record.target.source_text, "quoted-text"));
      }
      const evidence = task ? record.evidence : record.evidence_segments;
      if (evidence?.length) content.append(evidenceList(evidence));
      if (record.cache_status === "missing" || record.unavailable_materials) {
        content.append(node("p", "引用材料已不可用。", "hint"));
      }
      if (record.receipt) {
        content.append(node("p", "平台回执", "hint"),
          node("pre", JSON.stringify(record.receipt, null, 2), "operation-revision-text"));
      }
      if (!task && item.status === "published") {
        content.append(link("在评论历史中查看", `#comments/history/browsing/${encodeURIComponent(item.id)}`), " · ");
      }
      if (record.error && (item.failures || item.status === "publish_uncertain")) {
        content.append(link("查看告警与错误", "#issues"), " · ");
      }
      content.append(link("查看来源轮次", `#automation/round/${item.round_id}${task ? "/" + item.id : ""}`));
      row.detail.replaceChildren(content);
      row.loaded = true;
    } catch (_) {
      if (row.item === item) row.detail.replaceChildren(link("读取详情失败，查看告警与错误", "#issues"));
    }
  }

  function render(items, scope) {
    const key = JSON.stringify(scope);
    if (key !== scopeKey) {
      scopeKey = key;
      rows.clear();
      replies.replaceChildren();
      tasks.replaceChildren();
    }
    const keys = new Set(items.map(item => item.id));
    for (const [id, row] of rows) {
      row.retained = !keys.has(id);
      if (row.retained && !row.root.open) {
        row.root.remove();
        rows.delete(id);
      }
    }
    empty.textContent = "暂无回复结果。";
    empty.hidden = items.some(item => !["task", "failure"].includes(item.kind));
    const taskCount = items.filter(item => ["task", "failure"].includes(item.kind)).length;
    schedulingSummary.textContent = `调度详情${taskCount ? `（${taskCount}）` : ""}`;
    let replyIndex = 0, taskIndex = 0;
    items.forEach(item => {
      const task = ["task", "failure"].includes(item.kind);
      const container = task ? tasks : replies;
      const index = task ? taskIndex++ : replyIndex++;
      let row = rows.get(item.id);
      if (!row) {
        const root = node("details", null, "discovery-activity-row");
        const summary = node("summary"), detail = node("div", null, "discovery-activity-detail");
        const title = node("strong"), badge = node("span", null, "badge"), when = node("time", null, "hint");
        const excerpt = node("span", null, "discovery-excerpt hint");
        summary.append(badge, title, when, excerpt);
        root.append(summary, detail);
        row = { root, title, badge, when, excerpt, detail };
        root.addEventListener("toggle", () => {
          if (root.open && !row.loaded) void loadDetail(row, row.item);
          if (!root.open && row.retained) {
            root.remove();
            rows.delete(row.item.id);
          }
        });
        rows.set(item.id, row);
      }
      const signature = JSON.stringify(item);
      if (signature !== row.signature) {
        row.signature = signature;
        row.item = item;
        row.loaded = false;
        row.title.textContent = task ? item.title || item.bvid :
          `回复 ${item.recipient_name || item.target?.recipient_name || (item.kind === "top_level" ? "视频评论区" : "原评论作者")}`;
        row.title.title = item.title || item.bvid || "";
        row.badge.textContent = `${activityLabel(item)}${item.occurrences > 1 ? ` ×${item.occurrences}` : ""}`;
        row.when.textContent = time(item.updated_at);
        row.when.dateTime = new Date(item.updated_at * 1000).toISOString();
        row.excerpt.textContent = task ? TuberFormatters.humanizeTaskReason(item) : activityExcerpt(item);
        if (row.root.open) void loadDetail(row, item);
      }
      if (container.children[index] !== row.root) container.insertBefore(row.root, container.children[index] || null);
    });
  }

  return async () => {
    const current = ++request;
    try {
      const { activity, scope } = await api("/api/automation/discovery/records?recent=true");
      if (current === request) render(activity, scope);
    } catch (_) {
      if (current === request && !rows.size) {
        empty.hidden = false;
        empty.replaceChildren(link("读取活动失败，查看告警与错误", "#issues"));
      }
    }
  };
}
