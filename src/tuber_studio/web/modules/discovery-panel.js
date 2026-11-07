import { $, node, safeMessage } from "./core.js";
import { evidenceList } from "./comment-queue.js?v=20261107.17";

const states = {
  off: "待命", starting: "启动检查", discovering: "正在找视频", reading: "正在取材",
  judging: "正在判断与写稿", publishing: "正在发送", reconciling: "正在查询原回执", finishing: "正在结束本轮",
  waiting: "轮间等待", blocked: "需要处理", stopping: "正在停止", emergency: "已紧急停止",
};
const roundStates = { completed: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已停止", paused: "已暂停，进度保留", deferred_daily: "今日观看次数已用完" };
const time = seconds => new Date(seconds * 1000).toLocaleString("zh-CN");

function link(label, href) {
  const anchor = node("a", label);
  anchor.href = href;
  return anchor;
}

export function initIssueLog(api, page) {
  const entries = new Map();
  let remote = [], request = 0;

  function applyRecords(data) {
    remote = (data.issues || []).map(item => ({
      text: `${item.status === "recovered" ? "已恢复" : item.status === "retry_wait" ? "告警" : "错误"} · ${item.bvid ? item.bvid + " · " : ""}${item.error}${item.occurrences > 1 ? `（记录 ${item.occurrences} 次）` : ""}`,
      time: item.updated_at, href: item.href || `#automation/round/${item.round_id}${item.bvid ? "/" + item.id : ""}`,
    }));
    render();
  }

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
      applyRecords(data);
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

  $("clear-workspace-issues").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    ++request;
    const acknowledged = new Set(entries.keys());
    const feedback = $("workspace-issue-feedback");
    feedback.textContent = "正在清除已读告警…";
    try {
      const data = await api("/api/automation/issues/clear", { method: "POST" });
      ++request;
      for (const key of acknowledged) entries.delete(key);
      applyRecords(data.records || data);
      feedback.textContent = "已清除已读告警。";
    } catch (error) {
      feedback.textContent = `清除失败：${safeMessage(error.message)}`;
    } finally {
      button.disabled = false;
    }
  };
  return { record, refresh, render };
}

export function initDiscoveryPanel(api) {
  let value, request = 0, saving = false, dirty = false;
  const status = $("discovery-state");
  const attention = $("discovery-attention"), actions = $("discovery-actions");
  const result = $("discovery-result");
  const form = $("discovery-source-form"), source = $("discovery-source"), save = $("discovery-save-source");
  const keywords = $("discovery-keywords"), videos = $("discovery-videos"), note = $("discovery-source-note");
  const sourceName = mode => source.querySelector(`option[value="${mode}"]`)?.textContent || "";
  let attentionSignature = "";

  function syncControls() {
    const unavailable = !value || status.textContent === "状态不可用";
    source.disabled = keywords.disabled = videos.disabled = unavailable || saving;
    save.disabled = unavailable || saving || !dirty;
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
    status.textContent = label;
    if (!dirty) {
      source.value = data.source.mode;
      keywords.value = data.source.keywords.join("，");
      videos.value = data.source.watch_videos.join("，");
    }
    syncControls();
    const items = data.attention || [];
    attention.hidden = !items.length;
    const signature = JSON.stringify(items);
    if (signature !== attentionSignature) {
      attentionSignature = signature;
      actions.replaceChildren();
      for (const item of items) {
        const entry = node("li", item.message);
        entry.append(" ", link(item.label, item.href));
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
      data.daily_watch?.limit > 0 ? `每日观看上限用量 ${data.daily_watch.started} / ${data.daily_watch.limit}` : "",
      sourceName(data.round_source), `已处理 ${data.summary.processed} 个视频`,
      `已发送 ${data.sent} 条`, `已跳过 ${data.summary.skipped} 个视频`,
    ].filter(Boolean).join(" · ") : "";
  }

  async function refresh() {
    if (saving) return;
    const current = ++request;
    try {
      const data = await api("/api/automation/discovery");
      if (current === request) render(data);
    } catch (_) {
      if (current !== request) return;
      status.textContent = "状态不可用";
      syncControls();
    }
  }

  return { refresh, status: () => value };
}
