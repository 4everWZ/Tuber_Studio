import { node, safeMessage } from "./core.js?v=20261105.32";
import { operationControlCard } from "./operator-chat-control.js?v=20261113.03";

const statuses = {
  queued: "排队中", waiting: "等待资源", running: "运行中", completed: "已完成", paused: "已暂停",
  partial: "部分完成", failed: "失败", cancelled: "已停止", pending_review: "待审核", approved: "已审核",
  rejected: "已拒绝", published: "已发布", publish_uncertain: "待核对", publishing: "提交中", invalidated: "来源已变化",
  unavailable: "草稿已不可用", stopped: "已停止", live: "直播中", mock: "模拟中", idle: "待机",
  selected: "已读材料", viewed_without_comment: "已看过", reading: "正在读取",
};
const label = value => statuses[value] || value || "未知";
const activeVideo = new Set(["queued", "waiting", "running", "pending", "starting"]);

function button(text) {
  const element = node("button", text);
  element.type = "button";
  return element;
}

function link(text, href) {
  const element = node("a", text);
  element.href = href;
  if (href.startsWith("https://")) {
    element.target = "_blank";
    element.rel = "noopener noreferrer";
  }
  return element;
}

function videoHref(bvid, suffix = "") {
  return /^BV[0-9A-Za-z]{10}$/.test(bvid) ? `https://www.bilibili.com/video/${bvid}${suffix}` : "";
}

function dynamicCard(root, initial, api) {
  let data, signature, dirty = false, busy = false, revision = 0;
  function update(next) {
    if (dirty || busy || signature === JSON.stringify(next)) return;
    data = next;
    signature = JSON.stringify(next);
    const outcome = node("p", label(data.status), "muted");
    outcome.setAttribute("role", "status");
    root.replaceChildren(node("h2", "动态草稿"), outcome);
    if (!data.id) return;
    const editable = ["pending_review", "approved"].includes(data.status);
    const body = editable ? node("textarea") : node("p", data.content);
    if (editable) {
      body.value = data.content;
      body.rows = 4;
      body.setAttribute("aria-label", "动态正文");
      body.oninput = () => { dirty = body.value !== data.content; revision++; };
    }
    root.append(body);
    const actions = node("div", null, "actions");
    if (editable || data.status === "publish_uncertain") {
      const submit = button(editable ? "发布" : "核对回执");
      submit.className = "primary";
      actions.append(submit);
      submit.onclick = async () => {
        if (busy) return;
        revision++;
        busy = submit.disabled = body.readOnly = true;
        outcome.textContent = "正在处理…";
        try {
          const updated = await api(`/api/dynamics/posts/${data.id}/publish`, {
            method: "POST", ...(editable ? { body: { text: body.value.trim() } } : {}),
          });
          dirty = busy = false;
          signature = null;
          update(updated);
        } catch (error) {
          outcome.textContent = safeMessage(error.message);
        } finally {
          busy = submit.disabled = body.readOnly = false;
        }
      };
    }
    actions.append(link("打开动态工作台", "#dynamics"));
    root.append(actions);
    if (data.error) root.append(node("p", safeMessage(data.error), "muted"));
    if (data.status === "published" && data.receipt) {
      const id = data.receipt.id_str || data.receipt.dynamic_id_str || data.receipt.dynamic_id || data.receipt.id;
      if (/^\d+$/.test(String(id))) root.append(link("查看已发布动态", `https://t.bilibili.com/${id}`));
    }
  }
  update(initial);
  return { update, revision: () => revision, needsRefresh: () => !dirty && !busy && !["published", "rejected", "invalidated"].includes(data.status) };
}

function materialSummary(value) {
  if (!value) return "未处理";
  const captions = { available: "已读取", sampled: "抽样", gap: "无有效材料", unavailable: "不可用", retry: "待补读" };
  const state = captions[value.state] || value.state;
  const count = value.covered_seconds != null ? `${value.covered_seconds} 秒` : "";
  const samples = value.samples?.length ? `抽样时码 ${value.samples.join("、")} 秒` : "";
  const gaps = value.gap_seconds > 0 ? `${value.gap_seconds} 秒无有效文本` : "";
  const track = value.track_complete === true ? "原轨已取全" : "";
  return [state, count, track, gaps, samples, safeMessage(value.reason)].filter(Boolean).join(" · ");
}

function coverageDetails(data, container) {
  container.replaceChildren();
  for (const part of data.coverage?.parts || []) {
    const detail = node("div", null, "operator-chat-memory");
    detail.append(node("strong", `P${part.part}${part.title ? ` · ${part.title}` : ""}${part.duration == null ? "" : ` · ${part.duration} 秒`}`));
    const ranges = node("ul");
    for (const range of part.ranges || []) {
      const item = node("li");
      const title = `${range.start}–${range.end} 秒`;
      const href = videoHref(data.selection?.bvid, `?p=${part.part}&t=${range.start}`);
      item.append(href ? link(title, href) : node("strong", title));
      for (const [caption, value] of [["字幕", range.subtitle], ["音频", range.audio], ["画面", range.visual]]) {
        item.append(node("p", `${caption} · ${materialSummary(value)}`, "hint"));
      }
      ranges.append(item);
    }
    detail.append(ranges);
    container.append(detail);
  }
}

function videoCard(root, turn, { api, path }) {
  let data, signature, busy = false, revision = 0;
  const title = node("h2"), state = node("p", null, "hint"), note = node("p", null, "hint");
  const selection = node("p", null, "hint");
  state.setAttribute("role", "status");
  const report = node("details"), coverage = node("div");
  report.append(node("summary", "分 P 与材料覆盖"), coverage);
  report.ontoggle = () => { if (report.open) coverageDetails(data, coverage); else coverage.replaceChildren(); };
  const actions = node("div", null, "actions");
  const record = link("查看轮次", "#automation-records");
  const cancel = button("取消取材"), resume = button("继续取材");
  actions.append(record, cancel, resume);
  root.append(title, state, selection, report, actions, note);

  function update(next) {
    if (busy || signature === JSON.stringify(next)) return;
    signature = JSON.stringify(next);
    data = next;
    title.textContent = data.full_video ? data.coverage?.title || `指定视频取材${data.selection?.bvid ? ` · ${data.selection.bvid}` : ""}` : "只读浏览";
    state.textContent = data.full_video && data.status === "completed" ? "取材完成" : label(data.status);
    selection.textContent = data.selection?.ranges?.length ? data.selection.ranges.map(range => `P${range.part} · ${range.start}–${range.end} 秒`).join("；") : "";
    selection.hidden = !selection.textContent;
    note.textContent = safeMessage(data.error);
    report.hidden = !data.coverage?.parts?.length;
    if (report.open) coverageDetails(data, coverage);
    record.hidden = !data.id;
    if (data.id) record.href = `#automation/round/${encodeURIComponent(data.id)}`;
    cancel.hidden = !data.full_video || !activeVideo.has(data.status);
    resume.hidden = !data.full_video || !data.can_resume || activeVideo.has(data.status);
  }

  async function change(action) {
    if (busy) return;
    revision++;
    busy = cancel.disabled = resume.disabled = true;
    note.textContent = action === "cancel" ? "正在取消取材…" : "正在继续取材…";
    try {
      const updated = await api(`${path}/turns/${turn.request_id}/${action}-video`, { method: "POST" });
      busy = false;
      signature = null;
      update(updated.card.data);
    } catch (error) {
      note.textContent = safeMessage(error.message);
    } finally {
      busy = cancel.disabled = resume.disabled = false;
    }
  }
  cancel.onclick = () => change("cancel");
  resume.onclick = () => change("resume");
  update(turn.card.data);
  return { update, revision: () => revision, needsRefresh: () => !busy && activeVideo.has(data.status) };
}

function activityCard(root, initial) {
  let signature;
  function update(data) {
    if (signature === JSON.stringify(data)) return;
    signature = JSON.stringify(data);
    root.replaceChildren(node("h2", "今日观看"), node("p", `${data.day} · ${data.timezone}`, "hint"));
    if (!data.available) {
      root.append(node("p", "当前账号的观看记录不可用。"));
      return;
    }
    root.append(node("p", `已开始 ${data.started} 次 · 已完成 ${data.completed} 次 · 完整阅读 ${data.fully_read} 次`));
    if (data.records?.length) {
      const detail = node("details"), list = node("ul");
      const coverage = { all_available_cc: "字幕完整", sampled_visual: "抽样画面", sampled_audio: "抽样音频", title_only: "只看标题", comments_only: "只看评论" };
      detail.append(node("summary", "查看观看记录"), list);
      for (const record of data.records) {
        const item = node("li");
        const href = record.round_id ? `#automation/round/${encodeURIComponent(record.round_id)}` : videoHref(record.bvid);
        item.append(href ? link(record.bvid, href) : node("strong", record.bvid), ` · ${label(record.status)}`);
        if (record.coverage) item.append(` · ${coverage[record.coverage] || "已记录材料范围"}`);
        list.append(item);
      }
      root.append(detail);
    }
    root.append(link("查看运营活动", "#automation/video"));
  }
  update(initial);
  return { update, needsRefresh: () => true };
}

function experienceCard(root, initial, { api, path, privateTurns }) {
  const items = node("div"), details = node("details"), form = node("form");
  const sourceLabel = node("label", "作为依据的私聊");
  const choice = node("select"), preview = node("blockquote", "", "quoted-text");
  const factLabel = node("label", "确认可公开的事实"), fact = node("textarea");
  const submit = node("button", "确认加入公开经历", "primary");
  const note = node("p", "", "hint");
  let data = [], signature, busy = false, revision = 0;
  choice.required = fact.required = true;
  fact.rows = 2;
  fact.maxLength = 150;
  submit.type = "submit";
  note.setAttribute("role", "status");
  sourceLabel.append(choice);
  factLabel.append(fact);
  form.append(sourceLabel, preview, factLabel, submit);
  details.append(node("summary", "从私聊确认一条公开经历"), form);
  root.append(node("h2", "公开经历"), items, details, note, link("打开角色记忆", "#daily-memory-card"));

  function showPrivateSource() {
    const turn = privateTurns().find(item => item.request_id === choice.value);
    preview.hidden = !turn;
    preview.textContent = turn ? `你：${turn.message}\n大肥鱼：${turn.reply_text}` : "";
    submit.disabled = !turn || busy;
  }

  details.ontoggle = () => {
    if (!details.open) return;
    const previous = choice.value;
    const available = privateTurns();
    const prompt = node("option", available.length ? "请选择一段已加载的私聊" : "可先加载更早的私聊");
    prompt.value = "";
    choice.replaceChildren(prompt);
    for (const turn of available) {
      const option = node("option", turn.message.slice(0, 60));
      option.value = turn.request_id;
      choice.append(option);
    }
    if (available.some(turn => turn.request_id === previous)) choice.value = previous;
    showPrivateSource();
  };
  choice.onchange = showPrivateSource;

  async function mutate(operation, success) {
    if (busy) return;
    revision++;
    busy = choice.disabled = fact.disabled = submit.disabled = true;
    for (const control of items.querySelectorAll("button")) control.disabled = true;
    note.textContent = "正在保存…";
    try {
      const next = await operation();
      busy = false;
      update({ items: next });
      note.textContent = success;
    } catch (error) {
      note.textContent = safeMessage(error.message);
    } finally {
      busy = choice.disabled = fact.disabled = false;
      showPrivateSource();
      for (const control of items.querySelectorAll("button")) control.disabled = false;
    }
  }

  form.onsubmit = event => {
    event.preventDefault();
    const text = fact.value.trim();
    if (!choice.value || !text) return;
    return mutate(async () => {
      const updated = await api(`${path}/${choice.value}/public-experience`, { method: "POST", body: { fact: text } });
      fact.value = "";
      details.open = false;
      return [updated, ...data.filter(item => item.id !== updated.id)];
    }, "已保存号主确认的公开事实。");
  };

  function update(next) {
    if (busy || signature === JSON.stringify(next.items)) return;
    signature = JSON.stringify(next.items);
    note.textContent = "";
    data = next.items;
    items.replaceChildren();
    if (!data.length) items.append(node("p", "暂无公开经历。", "hint"));
    for (const item of data) {
      const row = node("section", null, "operator-chat-memory");
      const source = { confirmed_turn: "号主确认", public_thread: "公开评论", runtime_event: "运行记录" }[item.source?.kind];
      const memoryStatus = { active: "可公开使用", archived: "已归档", pending_review: "已撤销公开" }[item.status];
      row.append(node("strong", item.headline), node("p", [source, item.highlight_date, memoryStatus].filter(Boolean).join(" · "), "hint"));
      row.append(node("p", item.anecdote || item.quote));
      for (const entry of item.source?.public_sources || []) {
        const href = /^\d+$/.test(entry.comment_id) ? videoHref(entry.bvid, `#reply${entry.comment_id}`) : "";
        if (href) row.append(link(`原评论 · ${entry.bvid}`, href));
        if (entry.quote) row.append(node("blockquote", entry.quote, "quoted-text"));
      }
      const actions = node("div", null, "actions");
      for (const [caption, newStatus] of [["归档", "archived"], ["撤销公开", "pending_review"]]) {
        const control = button(caption);
        control.onclick = () => mutate(async () => {
          await api(`${path}/public-experiences/${item.id}`, { method: "PATCH", body: { status: newStatus } });
          return data.filter(current => current.id !== item.id);
        }, newStatus === "archived" ? "已归档，可在角色记忆查看。" : "已撤销公开资格，可在角色记忆查看。");
        actions.append(control);
      }
      const remove = button("删除");
      remove.onclick = () => {
        if (!window.confirm("删除这条记忆及其公开引用资格？")) return;
        return mutate(async () => {
          await api(`${path}/public-experiences/${item.id}`, { method: "DELETE" });
          return data.filter(current => current.id !== item.id);
        }, "已删除该条记忆。");
      };
      actions.append(remove);
      row.append(actions);
      items.append(row);
    }
  }

  update(initial);
  return { update, revision: () => revision, needsRefresh: () => !busy };
}

export function createOperatorCard(root, turn, context) {
  const { type, data } = turn.card;
  if (type === "dynamic_draft") return dynamicCard(root, data, context.api);
  if (type === "video_browsing") return videoCard(root, turn, context);
  if (type === "operation_control") return operationControlCard(root, data, context);
  if (type === "video_activity") return activityCard(root, data);
  if (type === "public_experiences") return experienceCard(root, data, context);
  if (type === "live_studio_hud") {
    const state = node("p", null, "muted");
    const update = value => { state.textContent = label(value.state) + (value.muted ? " · 静音" : ""); };
    root.append(node("h2", "直播状态"), state, link("打开直播工作台", "#live"));
    update(data);
    return { update, needsRefresh: () => true };
  }
  return { update() {} };
}
