import { node, safeMessage } from "./core.js?v=20261105.32";
import { memoryGroupNames } from "./personalization.js?v=20261114.1";
import { operationControlCard } from "./operator-chat-control.js?v=20261113.12";

const statuses = {
  queued: "排队中", waiting: "等待资源", running: "运行中", completed: "已完成", paused: "已暂停",
  partial: "部分完成", failed: "失败", cancelled: "已停止", pending_review: "待审核", approved: "已审核",
  rejected: "已拒绝", published: "已发布", publish_uncertain: "待核对", publishing: "提交中", invalidated: "来源已变化",
  unavailable: "草稿已不可用", stopped: "已停止", live: "直播中", mock: "模拟中", idle: "待机",
  selected: "已读材料", viewed_without_comment: "已看过", reading: "正在读取",
};
const label = value => statuses[value] || value || "未知";
const activeVideo = new Set(["queued", "waiting", "running", "pending", "starting"]);
function formatTimecode(seconds) {
  const millis = Math.round(seconds * 1000);
  const minutes = String(Math.floor(millis / 60000)).padStart(2, "0");
  const remainder = (millis % 60000 / 1000).toFixed(3).padStart(6, "0");
  return `${minutes}:${remainder}`;
}

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

function platformLink(value) {
  return typeof value === "string" && /^https:\/\/(?:www\.|t\.)bilibili\.com\//.test(value);
}

function eventContent(item, heading = "h2") {
  const section = node("section", null, "operator-chat-memory");
  const { source, reply } = item;
  section.append(node(heading, item.title));
  for (const entry of [source, item.material].filter(Boolean)) {
    const type = { comment: "原评论", subtitle: "字幕", visual: "画面观察" }[entry.kind] || "来源";
    const description = [type, entry.author, entry.uid ? `UID ${entry.uid}` : "", entry.start != null ? `${Math.floor(entry.start)} 秒` : ""].filter(Boolean).join(" · ");
    section.append(platformLink(entry.href) ? link(description, entry.href) : node("p", description, "hint"));
    section.append(node("blockquote", entry.text, "quoted-text"));
  }
  if (reply?.state === "confirmed" && reply.text) {
    section.append(platformLink(reply.href) ? link("已送达的回复", reply.href) : node("strong", "已送达的回复"));
    section.append(node("blockquote", reply.text, "quoted-text"));
  }
  return section;
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
  const captions = { available: "已读取", sampled: "抽样", partial: "部分覆盖", gap: "无有效材料", unavailable: "不可用", retry: "待补读" };
  const state = captions[value.state] || value.state;
  const count = value.covered_seconds != null ? `${value.covered_seconds} 秒` : "";
  const samples = value.samples?.length ? `抽样时码 ${value.samples.join("、")} 秒` : "";
  const gaps = value.gap_seconds > 0 ? `${value.gap_seconds} 秒无有效文本` : "";
  const track = value.track_complete === true ? "原轨已取全" : "";
  const windows = value.windows?.length ? `覆盖 ${value.windows.filter(window => window.state === "sampled").length}/${value.windows.length} 段` : "";
  return [state, count, track, gaps, windows, samples, safeMessage(value.reason)].filter(Boolean).join(" · ");
}

function coverageDetails(data, container) {
  const expanded = new Set([...container.querySelectorAll("details[open][data-coverage-range]")].map(item => item.dataset.coverageRange));
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
      const missing = range.visual?.gaps || (range.visual?.windows || []).filter(window => window.state !== "sampled");
      if (missing.length) {
        const gaps = node("details"), windows = node("ul");
        gaps.dataset.coverageRange = `${part.part}/${range.start}/${range.end}`;
        gaps.open = expanded.has(gaps.dataset.coverageRange);
        for (const window of missing) {
          const reason = { gap: "无可用预览图", retry: "待重试", pending: "未处理" }[window.state] || "画面间隔过大";
          windows.append(node("li", `${window.start}–${window.end} 秒 · ${reason}${window.reason ? ` · ${safeMessage(window.reason)}` : ""}`));
        }
        gaps.append(node("summary", `画面缺口 ${missing.length} 段`), windows);
        item.append(gaps);
      }
      for (const highlight of range.highlights || []) {
        const location = videoHref(data.selection?.bvid, `?p=${part.part}&t=${Math.floor(highlight.start)}`);
        if (location && highlight.text) {
          const excerpt = node("p");
          excerpt.append(link(`${Math.floor(highlight.start)} 秒`, location), ` · ${highlight.kind} · ${highlight.text}`);
          item.append(excerpt);
        }
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
  const comments = node("div");
  const findings = node("ul", null, "operator-chat-video-findings");
  const excerpt = node("div");
  actions.append(record, cancel, resume);
  root.append(title, state, selection, excerpt, findings, comments, report, actions, note);

  function update(next) {
    if (busy || signature === JSON.stringify(next)) return;
    signature = JSON.stringify(next);
    data = next;
    title.textContent = data.selection?.frame ? data.coverage?.title || "按时码查看画面" :
      data.full_video ? data.coverage?.title || `指定视频取材${data.selection?.bvid ? ` · ${data.selection.bvid}` : ""}` : "只读浏览";
    const phases = { watching: "正在看视频", comments: "正在逛评论区", complete: "已完成" };
    const viewing = { watched: "看完", subtitles_only: "字幕已读完 · 画面覆盖有缺口",
      visual_only: "画面已全程扫描 · 无可用字幕", partial: "视频材料覆盖有缺口" };
    const taskState = data.status === "running" || data.status === "completed" ? phases[data.phase] || label(data.status) :
      data.yielded ? "已保存进度 · 等待下一次调度" : label(data.status);
    state.textContent = [taskState,
      viewing[data.coverage?.viewing]].filter(Boolean).join(" · ");
    const parts = data.coverage?.parts || [];
    const ranges = data.selection?.ranges || [];
    selection.textContent = ranges.length ? ranges.slice(0, 3).map(range => `P${range.part} · ${range.start}–${range.end} 秒`).join("；") +
      (ranges.length > 3 ? ` 等 ${ranges.length} 段` : "") : parts.length > 1 ? `全部 ${parts.length} 个分 P` :
        parts.length ? `P${parts[0].part} · 0–${parts[0].duration} 秒` : "";
    selection.hidden = !selection.textContent;
    note.textContent = safeMessage(data.error);
    excerpt.replaceChildren();
    if (data.inspection) {
      const frame = data.inspection;
      const source = frame.kind === "stream" ? "视频流" : "故事板";
      excerpt.append(node("p", `P${frame.part} · 目标 ${formatTimecode(frame.requested_time)} · ${source} ${formatTimecode(frame.actual_time)}`));
      const details = node("details");
      details.append(node("summary", "查看取到的画面"));
      details.ontoggle = () => {
        if (!details.open || details.querySelector("img")) return;
        const picture = node("img");
        picture.src = `${path}/turns/${turn.request_id}/inspection-image`;
        picture.alt = `P${frame.part} ${formatTimecode(frame.actual_time)} 的${source}画面`;
        picture.style.maxWidth = "100%";
        details.append(picture);
      };
      excerpt.append(details);
      if (platformLink(frame.source_url)) excerpt.append(link("打开原视频", frame.source_url));
      excerpt.append(node("blockquote", frame.text, "quoted-text"));
      if (frame.fallback_reason) excerpt.append(node("p", safeMessage(frame.fallback_reason), "hint"));
    }
    if (data.excerpt?.error) excerpt.append(node("p", data.excerpt.error, "hint"));
    if (platformLink(data.excerpt?.source_url)) {
      excerpt.append(link(`P${data.excerpt.part} · ${Math.floor(data.excerpt.start)} 秒`, data.excerpt.source_url),
        node("blockquote", data.excerpt.text, "quoted-text"));
    }
    findings.replaceChildren();
    if (!data.inspection && !data.excerpt && parts.length) {
      const material = parts.flatMap(part => part.ranges.flatMap(range => (range.highlights || [])
        .map(item => ({ ...item, part: part.part }))))
        .filter(item => item.text && platformLink(item.source_url));
      const unique = [...new Map(material.map(item => [item.text.trim(), item])).values()];
      const picks = unique.length <= 3 ? unique : [unique[0], unique[Math.floor(unique.length / 2)], unique.at(-1)];
      for (const item of picks) {
        const row = node("li");
        const text = item.text.replace(/\s+/g, " ").trim();
        row.append(link(`P${item.part} · ${formatTimecode(item.start)}`, item.source_url),
          ` · ${item.kind} · ${text.length > 140 ? text.slice(0, 140) + "…" : text}`);
        findings.append(row);
      }
    }
    findings.hidden = !findings.childElementCount;
    report.hidden = !data.coverage?.parts?.length;
    if (report.open) coverageDetails(data, coverage);
    comments.replaceChildren();
    if (data.comments) {
      const text = data.comments.at_end ? "已读到平台可见末页" :
        `已读 ${data.comments.pages_read || 0} 页根评论${data.comments.thread_pages_read ? `、${data.comments.thread_pages_read} 页楼内评论` : ""}`;
      comments.append(node("p", text, "hint"));
      if (data.id) comments.append(link("查看评论记录", `#automation/comments/${encodeURIComponent(data.id)}`));
    }
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

function experienceCard(root, initial) {
  const items = node("ul");
  const empty = node("p", "暂无已保存的公开经历。", "hint");
  let signature;
  root.append(items, empty, link("打开记忆设置", "#personalization"));

  function update(data) {
    const next = JSON.stringify(data.items);
    if (signature === next) return;
    signature = next;
    items.replaceChildren();
    empty.hidden = data.items.length > 0;
    items.hidden = !empty.hidden;
    for (const entry of data.items) {
      const item = node("li", null, "operator-chat-memory");
      item.append(node("strong", entry.headline), node("p", entry.anecdote));
      for (const href of entry.links) {
        if (platformLink(href)) item.append(link("原始来源", href));
      }
      items.append(item);
    }
  }

  update(initial);
  return { update, needsRefresh: () => true };
}

export function createOperatorCard(root, turn, context) {
  const { type, data } = turn.card;
  if (type === "dynamic_draft") return dynamicCard(root, data, context.api);
  if (type === "dynamic_edit") {
    const note = node("p", "", "hint");
    note.setAttribute("role", "status");
    const apply = button("确认修改原动态");
    const before = node("details");
    before.append(node("summary", "修改前"), node("p", data.original, "quoted-text"));
    root.append(link("打开原动态", data.url), before, node("p", data.caption, "quoted-text"), apply, note);
    let busy = false;
    function update(value) {
      apply.disabled = busy || value.status === "applied";
      apply.textContent = value.status === "pending_verification" ? "核对编辑结果" : "确认修改原动态";
      note.textContent = value.error || (value.status === "applied" ? "已核对原动态更新。" : "");
    }
    apply.onclick = async () => {
      busy = apply.disabled = true;
      try {
        const value = await context.api(`/api/dynamics/edits/${data.request_id}/apply`, { method: "POST" });
        busy = false;
        update(value);
      } catch (error) {
        busy = apply.disabled = false;
        note.textContent = safeMessage(error.message);
      }
    };
    update(data);
    return { update };
  }
  if (type === "video_browsing") return videoCard(root, turn, context);
  if (type === "operation_control") return operationControlCard(root, data, { ...context, turn });
  if (type === "video_activity") return activityCard(root, data);
  if (type === "memory_change") root.append(node("h3", data.target), node("p", data.text));
  if (type === "game_handoff") root.append(node("h3", data.target), node("p", data.text), node("p", data.state, "hint"));
  if (type === "live_history") {
    const items = node("ul");
    for (const item of data.items) items.append(node("li", item.text));
    root.append(node("h3", "已播台词"), items);
  }
  if (type === "live_handoff") {
    const status = node("p", null, "hint");
    let current = data.status;
    const update = value => {
      current = value.status;
      status.textContent = value.spoken ? `已播报：${value.spoken}` :
        ({ queued: "等待播报", generating: "正在生成台词", synthesizing: "正在合成语音",
           ready_to_play: "等待播放器", playing: "正在播报", failed: "播报失败", cancelled: "任务已取消",
           unavailable: "任务不可用", expired: "任务已过期" }[current] || current);
    };
    root.append(node("h3", "本场直播"), node("p", data.text), status);
    for (const source of data.sources || []) root.append(link(`P${source.part} · ${formatTimecode(source.seconds)}`, source.url));
    update(data);
    return { update, needsRefresh: () => ["queued", "generating", "synthesizing", "ready_to_play", "playing"].includes(current) };
  }
  if (type === "public_experiences") return experienceCard(root, data);
  if (type === "memory_summary") {
    const update = value => {
      root.replaceChildren();
      for (const group of value.groups) {
        root.append(node("strong", memoryGroupNames[group.kind]), node("p", group.notes.map(note => note.text).join(" ")));
      }
      if (!value.groups.length) root.append(node("p", "暂无可用的长期记忆。", "hint"));
      root.append(link("管理记忆", "#personalization"));
    };
    update(data);
    return { update, needsRefresh: () => true };
  }
  if (type === "operation_events") {
    const items = node("ol");
    for (const event of data.items) {
      const item = node("li");
      item.append(eventContent(event, "h3"));
      items.append(item);
    }
    root.append(items);
  }
  if (type === "operation_event") root.append(eventContent(data.event));
  if (["live_studio_hud", "live_confirmation"].includes(type)) {
    const state = node("p", null, "muted"), note = node("p", null, "hint");
    const confirm = button("确认开启直播会话");
    const refresh = button("重新核对会话");
    refresh.hidden = true;
    let busy = false, revision = 0;
    note.setAttribute("role", "status");
    const update = value => {
      state.textContent = value.enabled ? label(value.state) + (value.muted ? " · 静音" : "") : "当前设备尚未启用直播功能";
      confirm.disabled = busy || !value.enabled || value.state === "live";
    };
    root.append(node("h2", "直播状态"), state, link("打开直播工作台", "#live"));
    if (type === "live_confirmation") root.append(confirm, refresh, note);
    confirm.onclick = async () => {
      if (busy) return;
      revision++;
      busy = confirm.disabled = true;
      note.textContent = "正在检查并切换资源…";
      try {
        const value = await context.api(`${context.path}/turns/${turn.request_id}/confirm/live`, { method: "POST", body: {} });
        busy = false;
        update(value.card.data);
        note.textContent = "直播会话已开启。";
      } catch (error) {
        note.textContent = safeMessage(error.message);
        busy = confirm.disabled = false;
        if (error.status === 409) refresh.hidden = false;
      }
    };
    refresh.onclick = async () => {
      refresh.disabled = true;
      try {
        const value = await context.api(`${context.path}/turns/${turn.request_id}/refresh-confirmation`, { method: "POST" });
        update(value.card.data);
        refresh.hidden = true;
        note.textContent = "已更新会话状态，请核对后确认。";
      } catch (error) {
        note.textContent = safeMessage(error.message);
      } finally {
        refresh.disabled = false;
      }
    };
    update(data);
    return { update, revision: () => revision, needsRefresh: () => !busy };
  }
  return { update() {} };
}
