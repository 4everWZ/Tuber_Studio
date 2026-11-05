import { $, node, safeMessage } from "./core.js";
import { evidenceList } from "./comment-queue.js?v=20261105.31";

const states = {
  off: "待命", starting: "启动检查", discovering: "正在找视频", reading: "正在取材",
  judging: "正在判断与写稿", publishing: "正在发送", reconciling: "正在查询原回执", finishing: "正在结束本轮",
  waiting: "轮间等待", blocked: "需要处理", stopping: "正在停止", emergency: "已紧急停止",
};
const roundStates = { completed: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已停止" };
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
        row.append(link("查看来源与回执", `#comments/browsing/${item.id}`));
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
  const button = $("discovery-toggle");
  const status = $("discovery-state"), top = $("discovery-top-state");
  const attention = $("discovery-attention"), actions = $("discovery-actions");
  const result = $("discovery-result");
  const form = $("discovery-source-form"), source = $("discovery-source"), save = $("discovery-save-source");
  const keywords = $("discovery-keywords"), videos = $("discovery-videos"), note = $("discovery-source-note");
  const sourceName = mode => source.querySelector(`option[value="${mode}"]`)?.textContent || "";
  let attentionSignature = "";

  function syncControls() {
    const unavailable = !value || status.textContent === "状态不可用";
    button.disabled = unavailable || changing || saving || value.state === "emergency" || value.overlay_enabled ||
      (!value.enabled && dirty);
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
    button.textContent = data.enabled ? "停止自动刷视频" : "开始自动刷视频";
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
      active ? [data.current_video?.title, label].filter(Boolean).join(" · ")
        : round ? `${roundStates[round.status] || "已结束"} · ${time(round.updated_at)}`
          : "还没有运行记录。",
      timing,
    ].filter(Boolean).join(" · ");
    $("discovery-counts").textContent = round ? [
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
      syncControls();
    }
  }

  button.onclick = async () => {
    if (button.disabled || !value || changing) return;
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
  };
  return { refresh, status: () => value };
}

function activityLabel(item) {
  const labels = {
    published: item.kind === "top_level" ? "主评已发送" : "跟帖已发送",
    publish_uncertain: "回执待确认", viewed_without_comment: "看完未发", skipped: "已跳过",
    automatic_skipped: "回应已跳过", retry_wait: "自动重试", cancelled: "已停止",
  };
  return labels[item.status] || item.status;
}

function activityExcerpt(item) {
  if (item.status === "retry_wait") return item.failures === 1 ? "同一轮重试一次" : "安排下一轮重试";
  if (item.status === "publish_uncertain") return "原提交尚未确认";
  if (item.status === "skipped" && item.failures === 3) return "三次失败后跳过";
  if (item.status === "skipped" && !item.reason) return "不符合筛选条件";
  return item.status === "published" ? item.excerpt : decisions[item.reason] || "";
}

export function initDiscoveryActivity(api) {
  const list = $("discovery-activity-list");
  const rows = new Map();
  let request = 0, scopeKey;

  async function loadDetail(row, item) {
    row.detail.replaceChildren(node("p", "正在读取活动详情…", "hint"));
    try {
      const task = item.kind === "task";
      const record = task
        ? (await api(`/api/automation/tasks?task_id=${encodeURIComponent(item.id)}`))[0]
        : await api(`/api/comments/workbench/browsing/${encodeURIComponent(item.id)}`);
      if (row.item !== item) return;
      if (!record) {
        row.detail.textContent = "该条活动详情已不可用。";
        return;
      }
      const content = document.createDocumentFragment();
      const reason = task
        ? [...new Set([record.reason, ...(record.decisions || []).map(decision => decision.reason)]
          .map(code => decisions[code]).filter(Boolean))].join(" · ")
        : safeMessage(record.reason);
      if (reason) content.append(node("p", reason));
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
      list.replaceChildren();
    }
    const keys = new Set(items.map(item => item.id));
    for (const [id, row] of rows) {
      row.retained = !keys.has(id);
      if (row.retained && !row.root.open) {
        row.root.remove();
        rows.delete(id);
      }
    }
    if (!items.length) {
      list.replaceChildren(node("p", "暂无活动记录。", "hint"));
      return;
    }
    list.querySelector(":scope > :not(details)")?.remove();
    items.forEach((item, index) => {
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
        row.title.textContent = item.title || item.bvid;
        row.badge.textContent = activityLabel(item);
        row.when.textContent = time(item.updated_at);
        row.when.dateTime = new Date(item.updated_at * 1000).toISOString();
        row.excerpt.textContent = activityExcerpt(item);
        if (row.root.open) void loadDetail(row, item);
      }
      if (list.children[index] !== row.root) list.insertBefore(row.root, list.children[index] || null);
    });
  }

  return async () => {
    const current = ++request;
    try {
      const { activity, scope } = await api("/api/automation/discovery/records?recent=true");
      if (current === request) render(activity, scope);
    } catch (_) {
      if (current === request && !rows.size) list.replaceChildren(link("读取活动失败，查看告警与错误", "#issues"));
    }
  };
}
