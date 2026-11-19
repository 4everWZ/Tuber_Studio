import { illustrationUrl } from "./operation-image.js";
import { $, node, safeMessage } from "./core.js?v=20261105.32";
import { renderArtifact, renderAssistantAnswer } from "./operator-chat-answer.js";
import { mountFollowup } from "./goal-followup.js";
import { mountGoalEvents } from "./goal-events.js";
import { browserTaskForm, mountTaskBrowser } from "./task-browser.js";
import { renderCapabilityStrip } from "./operation-capabilities.js";

const base = "/api/agent/tasks";
const states = {
  queued: "等待执行", scheduled: "已预约", running: "进行中", waiting: "等待工具结果",
  awaiting_approval: "等待确认", needs_input: "等待你的答复", completed: "已完成", cancelled: "已停止", failed: "未完成",
};
const tools = {
  web_search: "搜索网页", web_page: "打开来源", bilibili_owner: "查号主主页", operation_status: "查看运营",
  today_activity: "查看当日活动", inspect_video: "读取视频", live_status: "查看直播", game_status: "查看游戏",
  live_handoff: "交接直播素材", game_action: "执行模拟游戏动作",
  inspect_dynamic_draft: "核对动态草稿", publish_dynamic_draft: "发布指定动态", delegate_text: "委派文本工作",
  owner_attachment: "号主提供的私有附件",
  owner_code_grant: "已允许本次隔离计算", private_code: "处理私有文件",
};
const stoppable = new Set(["queued", "scheduled", "running", "waiting", "needs_input", "awaiting_approval"]);
const needsAttention = new Set(["needs_input", "awaiting_approval"]);
const timeLabel = value => new Date(value * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });

function observedStatus(tool, result) {
  if (!result || result.error) return "";
  if (tool === "operation_status") {
    const labels = {
      off: "未启动", starting: "正在启动", running: "运行中", draining: "正在收尾",
      stopping: "正在停止", blocked: "已阻塞", emergency: "紧急停止",
    };
    return Object.hasOwn(labels, result.state) ? labels[result.state] : "";
  }
  if (tool === "live_status") {
    const platform = { stopped: "已停止", live: "直播中", paused: "已暂停" };
    const native = { idle: "空闲", starting: "正在启动", publishing: "正在发布", stopping: "正在停止", unavailable: "暂不可查" };
    const program = result.native_program?.state;
    const status = [];
    if (Object.hasOwn(platform, result.state)) status.push(`平台直播${platform[result.state]}`);
    if (Object.hasOwn(native, program)) status.push(`Omni 节目${native[program]}`);
    return status.join("，");
  }
  return "";
}

function button(label) {
  const element = node("button", label);
  element.type = "button";
  return element;
}

function downloadText(filename, text, mime = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function appendSource(target, value, name = value) {
  if (typeof value !== "string" || !value.startsWith("https://")) return;
  try {
    const url = new URL(value);
    if (url.username || url.password || url.protocol !== "https:") return;
    const anchor = node("a", name);
    anchor.href = url.href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    target.append(anchor);
  } catch { /* An invalid source remains visible in the diagnostic details. */ }
}

export function initTaskActivity(api, requestId, onArtifact = () => {}) {
  const panel = $("chat-activity"), toggle = $("chat-activity-toggle"), closeButton = $("chat-activity-close");
  const content = $("chat-activity-content"), notice = $("chat-activity-status");
  const tabs = {activity: $("chat-panel-activity"), results: $("chat-panel-results"), permissions: $("chat-panel-permissions")};
  const permissions = $("chat-panel-permission-summary"), strip = permissions.querySelector("[role=list]");
  let section = "activity";
  let selection = null, version = 0, timer, reading = false, viewKey, editing = false;
  let returnFocus;

  function status(message) {
    notice.textContent = message;
    notice.hidden = !message;
  }

  function close(restore = true) {
    clearInterval(timer);
    version++;
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    content.replaceChildren();
    editing = false;
    if (!restore) {
      toggle.textContent = "面板";
      toggle.removeAttribute("aria-label");
    }
    if (restore) (returnFocus?.isConnected ? returnFocus : toggle).focus({ preventScroll: true });
    returnFocus = null;
  }

  function open(id = null, nextSection = "activity") {
    if (panel.hidden) returnFocus = document.activeElement?.closest("button") || toggle;
    clearInterval(timer);
    version++;
    selection = id;
    section = nextSection;
    for (const [key, tab] of Object.entries(tabs)) tab.setAttribute("aria-pressed", String(section === key));
    permissions.hidden = section !== "permissions";
    strip.replaceChildren();
    delete strip.dataset.signature;
    if (id) onArtifact(null);
    viewKey = null;
    editing = false;
    content.replaceChildren();
    status(`正在读取${tabs[section].textContent}…`);
    panel.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    closeButton.focus();
    refresh();
    if (section !== "permissions") timer = setInterval(refresh, 2000);
  }

  async function refresh() {
    if (reading || panel.hidden || editing) return;
    const current = version;
    reading = true;
    try {
      const path = section === "permissions" ? "/api/automation" : selection ? `${base}/${selection}` : base;
      const value = await api(path);
      if (current !== version || editing) return;
      status("");
      const key = JSON.stringify(value);
      if (key === viewKey) return;
      viewKey = key;
      if (section === "permissions") {
        renderCapabilityStrip(strip, value);
      } else if (section === "results") {
        renderResults(value);
      } else if (selection) {
        renderTask(value);
        if (value.attention != null) {
          await api(`${base}/${selection}/attention?updated=${encodeURIComponent(value.attention)}`, { method: "DELETE" });
        }
      } else renderList(value.tasks, value.goals || [], value.attention || []);
    } catch (error) {
      if (current === version) {
        if ([401, 403, 404].includes(error.status)) {
          viewKey = null;
          content.replaceChildren();
        }
        status(safeMessage(error.message));
      }
    } finally {
      reading = false;
      if (current !== version && !panel.hidden) refresh();
    }
  }

  function renderResults(value) {
    content.replaceChildren();
    if (!selection) {
      if (!value.tasks.length) {
        content.append(node("p", "暂无任务成果。", "hint"));
        return;
      }
      const field = node("label", "按任务查看成果"), select = node("select");
      select.append(new Option("选择任务", ""));
      for (const task of value.tasks) select.append(new Option(task.goal, task.id));
      select.onchange = () => { if (select.value) open(select.value, "results"); };
      field.append(select);
      content.append(field);
      return;
    }
    const back = button("选择其他任务"), detail = button("查看任务");
    back.onclick = () => open(null, "results");
    detail.onclick = () => open(value.id);
    content.append(back, node("h3", value.goal));
    if (value.artifact) {
      const body = node("div");
      renderArtifact(body, value.artifact.body);
      content.append(body);
      onArtifact({task_id: value.id, task_revision: value.artifact.task_revision, edition: value.artifact.edition});
    } else content.append(node("p", "这项任务暂无成果。", "hint"));
    content.append(detail);
  }

  function renderList(tasks, goals, notices) {
    const schedule = button("预约任务");
    schedule.onclick = showScheduleForm;
    const upload = button("用文本附件交办");
    upload.onclick = showAttachmentForm;
    const browser = button("用浏览器交办");
    browser.onclick = () => browserTaskForm(content, api, requestId, open, value => { editing = value; });
    content.replaceChildren(schedule, upload, browser);
    if (notices.length) content.append(node("h3", "给号主的更新", "chat-activity-group"));
    for (const item of notices) {
      const link = button(`${states[item.status]} · ${item.goal}`);
      link.onclick = () => open(item.id);
      content.append(link);
    }
    if (goals.length) content.append(node("h3", "长期创作目标", "chat-activity-group"));
    for (const goal of goals) {
      const item = button(goal.title + (goal.closed ? " · 已结束" : ""));
      item.onclick = () => open(goal.id);
      content.append(item);
    }
    for (const [heading, group] of [
      ["需要处理", tasks.filter(task => needsAttention.has(task.status))],
      ["进行中", tasks.filter(task => stoppable.has(task.status) && task.status !== "scheduled" && !needsAttention.has(task.status))],
      ["已预约", tasks.filter(task => task.status === "scheduled").sort((left, right) => left.due - right.due)],
      ["已结束", tasks.filter(task => !stoppable.has(task.status))],
    ]) {
      if (!group.length) continue;
      const list = node("ul", null, "chat-activity-list");
      for (const task of group) {
        const item = node("li");
        const show = button(task.goal);
        show.className = "chat-task-row";
        show.append(node("small", task.status === "scheduled" ? timeLabel(task.due) : states[task.status], "hint"));
        show.onclick = () => open(task.id);
        item.append(show);
        list.append(item);
      }
      content.append(node("h3", heading, "chat-activity-group"), list);
    }
    if (!tasks.length) content.append(node("p", "你交办的工作会出现在这里。", "hint"));
  }

  function showAttachmentForm() {
    editing = true;
    const form = node("form", null, "chat-task-steer");
    const field = node("label", "希望用这些材料做什么？"), goal = node("textarea");
    const filesLabel = node("label", "私有附件（最多三份 UTF-8 文本、数据或字幕，每份最多 8000 字符）");
    const files = node("input"), submit = node("button", "交办"), back = button("返回活动");
    files.type = "file";
    files.accept = ".md,.txt,.csv,.json,.srt,.vtt";
    files.multiple = true;
    files.required = true;
    const codeLabel = node("label"), code = node("input");
    code.type = "checkbox";
    code.onchange = () => { files.required = !code.checked; };
    codeLabel.append(code, document.createTextNode("允许本次在隔离环境中运行 JavaScript 计算并生成文件"));
    goal.required = true;
    goal.maxLength = 2000;
    submit.type = "submit";
    back.onclick = () => open();
    field.append(goal);
    filesLabel.append(files);
    form.append(field, filesLabel, codeLabel, node("p", "材料只供当前角色与后台创作使用。这项任务不能访问网页或平台。", "hint"), submit, back);
    content.replaceChildren(form);
    goal.focus();
    let previous;
    form.onsubmit = async event => {
      event.preventDefault();
      const current = version, selected = Array.from(files.files);
      const objective = goal.value.trim();
      submit.disabled = true;
      try {
        if (!objective || (!selected.length && !code.checked) || selected.length > 3) throw new Error("请填写任务并选择附件或启用隔离计算。");
        const attachments = await Promise.all(selected.map(async file => {
          if (!/\.(md|txt|csv|json|srt|vtt)$/.test(file.name) || file.size > 32000) throw new Error("请选择小型文本、CSV、JSON 或字幕文件。");
          const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
          if (!text.trim() || Array.from(text).length > 8000) throw new Error("文本为空或超过 8000 字符。");
          return { name: file.name, text };
        }));
        if (current !== version) return;
        const payload = { goal: objective, attachments, allow_code: code.checked }, key = JSON.stringify(payload);
        if (previous?.key !== key) previous = { key, id: requestId() };
        const task = await api(base, { method: "POST", body: { ...payload, request_id: previous.id } });
        if (current === version) open(task.id);
      } catch (error) {
        if (current === version) status(safeMessage(error.message));
      } finally {
        submit.disabled = false;
      }
    };
  }

  function showScheduleForm() {
    editing = true;
    const form = node("form", null, "chat-task-steer");
    const goalField = node("label", "任务要求"), goal = node("textarea");
    goal.required = true;
    goal.rows = 3;
    goal.maxLength = 2000;
    const timeField = node("label", "开始时间"), when = node("input");
    when.type = "datetime-local";
    when.required = true;
    const submit = node("button", "保存预约"), back = button("返回活动");
    submit.type = "submit";
    back.onclick = () => open();
    goalField.append(goal);
    timeField.append(when);
    form.append(goalField, timeField, submit, back);
    let previous;
    form.onsubmit = async event => {
      event.preventDefault();
      const runAt = new Date(when.value).getTime() / 1000;
      if (!goal.value.trim() || !Number.isFinite(runAt) || runAt <= Date.now() / 1000) {
        status("请填写任务和未来的开始时间。");
        return;
      }
      const payload = { goal: goal.value.trim(), run_at: runAt };
      const key = JSON.stringify(payload);
      if (previous?.key !== key) previous = { key, id: requestId() };
      submit.disabled = true;
      const current = version;
      try {
        const task = await api(base, { method: "POST", body: { ...payload, request_id: previous.id } });
        if (current === version) open(task.id);
      } catch (error) {
        if (current === version) status(safeMessage(error.message));
      } finally {
        submit.disabled = false;
      }
    };
    content.replaceChildren(form);
    goal.focus();
  }

  function renderTask(task) {
    const back = button("全部活动");
    back.onclick = () => open();
    const title = node("h3", task.goal);
    const state = node("p", task.status === "scheduled" ? `已预约 · ${timeLabel(task.due)}` : states[task.status], "hint");
    state.setAttribute("role", "status");
    const result = node("div", null, "chat-task-result");
    if (task.message && (!task.artifact || task.status !== "completed")) {
      renderAssistantAnswer(result, { reply_format: "markdown", reply_text: task.message });
      if (["completed", "failed"].includes(task.status) && task.steps.some(step => step.result)) {
        result.prepend(node("p", "按工具回执交付", "hint"));
      }
    }
    // Keep unchanged steps mounted during polling so a playing video keeps
    // its decoder, playhead and focus. Changed or revoked results are removed.
    const steps = content.querySelector(".chat-task-steps") || node("ol", null, "chat-task-steps");
    const keys = new Map(task.steps.map(step => [step.id, JSON.stringify(step)]));
    const existing = new Map();
    for (const item of Array.from(steps.children)) {
      const id = item.dataset.stepId;
      if (item.stepKey === keys.get(id)) existing.set(id, item);
      else item.remove();
    }
    for (const [index, step] of task.steps.entries()) {
      const next = steps.children[index] || null;
      const prior = existing.get(step.id);
      if (prior) {
        if (prior !== next) steps.insertBefore(prior, next);
        continue;
      }
      const item = node("li");
      item.dataset.stepId = step.id;
      item.stepKey = keys.get(step.id);
      const label = tools[step.tool] || step.tool;
      const observation = observedStatus(step.tool, step.result);
      const summary = observation || (step.result ? (step.result.error ? "有错误" : "已收到结果") : "等待结果");
      const details = node("details");
      details.append(node("summary", `${label} · ${summary}`));
      const result = step.result;
      if (result) {
        if (step.tool === "inspect_video" && result.clip) {
          const clip = result.clip;
          const preview = node("details");
          preview.append(node("summary", `查看 P${clip.part} · ${clip.start}–${clip.end} 秒画面`));
          const video = node("video");
          video.controls = true;
          video.preload = "none";
          video.playsInline = true;
          video.style.maxWidth = "100%";
          video.src = `${base}/${encodeURIComponent(task.id)}/clips/${encodeURIComponent(step.id)}`;
          preview.ontoggle = () => { if (!preview.open) video.pause(); };
          preview.append(video, node("p", `${clip.bvid} · CID ${clip.cid} · ${clip.frames} 个连续原帧 · 仅画面`, "hint"));
          if (/^BV[0-9A-Za-z]{10}$/.test(clip.bvid) && Number.isInteger(clip.part) &&
              clip.part > 0 && Number.isFinite(clip.start) && clip.start >= 0) {
            appendSource(preview, `https://www.bilibili.com/video/${clip.bvid}/?p=${clip.part}&t=${Math.floor(clip.start)}`, "跳到原片");
          }
          item.append(preview);
        }
        if (step.tool === "private_code" && typeof result.text === "string" && result.name) {
          const download = button(`下载 ${result.name}`);
          download.onclick = () => downloadText(result.name, result.text);
          item.append(download);
        }
        const description = result.error || result.spoken || result.message || result.excerpt || result.state?.summary;
        if (typeof description === "string" && description) details.append(node("p", description));
        if (result.url) appendSource(details, result.url, result.title || result.name || "打开来源");
        for (const url of result.links || []) appendSource(details, url);
      }
      const diagnostics = node("details");
      diagnostics.append(node("summary", "工具记录"), node("pre", JSON.stringify(result || step.arguments, null, 2)));
      details.append(diagnostics);
      item.append(details);
      steps.insertBefore(item, next);
    }
    const adjust = button(task.status === "needs_input" ? "补充要求" : "调整任务");
    const stop = button("停止任务");
    stop.hidden = task.can_control === false || !stoppable.has(task.status);
    adjust.hidden = task.can_control === false;
    stop.onclick = () => apply(task, { action: "cancel" }, stop);
    adjust.onclick = () => {
      editing = true;
      const form = node("form", null, "chat-task-steer");
      const field = node("label", "新的任务要求");
      const input = node("textarea");
      input.required = true;
      input.maxLength = 2000;
      input.rows = 4;
      const submit = node("button", "继续任务");
      submit.type = "submit";
      const dismiss = button("取消修改");
      dismiss.onclick = () => { editing = false; renderTask(task); refresh(); };
      field.append(input);
      form.append(field, submit, dismiss);
      form.onsubmit = event => {
        event.preventDefault();
        if (input.value.trim()) apply(task, { action: "steer", goal: input.value.trim() }, submit);
      };
      adjust.replaceWith(form);
      input.focus();
    };
    if (steps.parentElement === content) {
      for (const child of Array.from(content.children)) if (child !== steps) child.remove();
      steps.before(back, title, state, result);
      steps.after(adjust, stop);
    } else {
      content.replaceChildren(back, title, state, result, steps, adjust, stop);
    }
    if (task.can_control === false && !task.creative_goal?.closed) content.append(node("p", "原执行授权已失效。记录与成果仍可查看；如需继续，请重新交办。", "hint"));
    renderWorkspace(task);
    mountTaskBrowser(task, content, api, open, value => { editing = value; });
    if (task.can_control !== false && task.status === "awaiting_approval") {
      const step = task.steps.at(-1);
      const action = node("p", `${step.approval.target} · ${step.approval.text || step.arguments.action}`);
      if (["publish_dynamic_draft", "submit_selected_reply"].includes(step.tool)) {
        const {payload, channel, draft_id, post_id} = step.approval;
        const ref = {asset_id: channel && payload.meme_id, artifact_id: payload.artifact_id};
        const url = illustrationUrl(channel || "dynamics", draft_id || post_id, ref);
        if (url) {
          const image = node("img", null, "dynamic-comment-card");
          image.src = url;
          image.alt = "本次审批绑定的图片";
          action.append(image);
        }
        if (payload.message_kind === "text_image") action.append(node("small", "先发图片，再发正文，各有一条平台回执。"));
      }
      if (step.tool === "publish_dynamic_draft") {
        const { payload, post_id: postId, sources } = step.approval;
        for (const source of sources) {
          appendSource(action, source.source_url, "查看原始来源");
          for (const origin of source.public_sources || []) appendSource(action, origin.url);
        }
        const previews = payload.meme_id ? [`/api/memes/${encodeURIComponent(payload.meme_id)}/preview`] :
          payload.comment_card_sha256 ? Array.from({length: payload.comment_card_page_sha256?.length || 1},
            (_, index) => `/api/dynamics/posts/${encodeURIComponent(postId)}/comment-card?page=${index + 1}`) : [];
        for (const url of previews) {
          const image = node("img", null, "dynamic-comment-card");
          image.src = url;
          image.alt = "将随正文一起发布的配图";
          action.append(image);
        }
      }
      const confirm = button(step.tool === "publish_dynamic_draft" ? "确认公开发布这条动态" :
        step.tool === "live_handoff" ? "确认交接给本场直播" : "确认执行这个动作");
      confirm.onclick = () => apply(task, { action: "approve", step_id: step.id }, confirm);
      content.append(action, confirm);
    }
  }

  function renderWorkspace(task) {
    const path = `${base}/${task.id}`;
    async function change(url, method, body, control) {
      const current = version;
      control.disabled = true;
      try {
        const updated = await api(url, { method, body });
        if (current === version) open(updated.id);
      } catch (error) {
        if (current === version) status(safeMessage(error.message));
      } finally {
        control.disabled = false;
      }
    }
    function editor(label, initial, limit, save) {
      editing = true;
      const form = node("form", null, "chat-task-steer");
      const field = node("label", label), input = node("textarea");
      input.required = true;
      input.rows = 6;
      input.maxLength = limit;
      input.value = initial;
      const submit = node("button", "保存"), dismiss = button("返回");
      submit.type = "submit";
      dismiss.onclick = () => open(task.id);
      field.append(input);
      form.append(field, submit, dismiss);
      form.onsubmit = event => {
        event.preventDefault();
        if (input.value.trim()) save(input.value.trim(), submit);
      };
      content.replaceChildren(form);
      input.focus();
    }
    const goal = task.creative_goal;
    mountFollowup(goal, content, api, value => { editing = value; });
    mountGoalEvents(goal, content, api, value => { editing = value; });
    if (!goal) {
      const track = button("设为长期创作目标");
      track.onclick = () => change(path + "/goal", "PUT", undefined, track);
      content.append(track);
    } else {
      content.append(node("h3", goal.title), node("p", goal.closed ? "长期目标已结束" : "长期目标 · 执行记录", "hint"));
      for (const run of goal.runs) {
        if (run.id === task.id) continue;
        const item = button(run.goal + " · " + states[run.status]);
        item.onclick = () => open(run.id);
        content.append(item);
      }
      if (!goal.closed) {
        const next = button("交办下一项工作"), finish = button("结束长期目标");
        next.onclick = () => {
          const identity = requestId();
          editor("新任务要求", "", 2000, (text, submit) => change(base, "POST", {
            request_id: identity, goal: text, creative_goal_id: goal.id,
          }, submit));
        };
        finish.onclick = () => {
          const scope = goal.stop_scope || {};
          const runs = goal.runs.filter(run => Object.hasOwn(scope, run.id));
          if (runs.length && !window.confirm([
            `结束「${goal.title}」并停止当前授权的 ${runs.length} 项执行？`,
            ...runs.map(run => `· ${run.goal}`),
            "周期和事件跟进也会停止。已提交的动作只核对原回执，历史和成果保留。",
          ].join("\n"))) return;
          change(`${base}/${goal.id}/goal/close`, "POST", {confirmed_runs: scope}, finish);
        };
        content.append(next, finish);
      }
    }
    const artifact = task.artifact;
    if (!artifact) return;
    const card = node("section", null, "chat-task-result");
    card.append(node("h3", "私有成果"), node("small", `任务 r${artifact.task_revision} · 成果 v${artifact.edition}`, "hint"));
    const body = node("div");
    renderArtifact(body, artifact.body);
    onArtifact({ task_id: task.id, task_revision: artifact.task_revision, edition: artifact.edition });
    const edit = button("编辑成果");
    edit.onclick = () => editor("私有成果正文", artifact.body, 64000, (text, submit) => change(
      path + "/artifact", "PATCH", { task_revision: artifact.task_revision, edition: artifact.edition, body: text }, submit,
    ));
    const download = button("下载 Markdown");
    download.onclick = () => downloadText(`工坊成果-${task.id}-r${artifact.task_revision}-v${artifact.edition}.md`,
      artifact.body, "text/markdown;charset=utf-8");
    card.append(body, edit, download);
    if (artifact.edition > 1 || artifact.task_revision > 1) {
      const history = node("details"), reading = node("div");
      history.append(node("summary", "历史成果"), reading);
      history.ontoggle = async () => {
        if (!history.open || reading.childElementCount) return;
        try {
          const versions = await api(path + "/artifacts");
          for (const entry of versions) {
            if (entry.task_revision === artifact.task_revision && entry.edition === artifact.edition) continue;
            const details = node("details"), text = node("div");
            details.append(node("summary", `任务 r${entry.task_revision} · 成果 v${entry.edition}`), text);
            details.ontoggle = async () => {
              if (!details.open) return;
              const coordinate = { task_id: task.id, task_revision: entry.task_revision, edition: entry.edition };
              if (text.childElementCount) {
                onArtifact(coordinate);
                return;
              }
              try {
                const saved = await api(`${path}/artifacts/${entry.task_revision}/${entry.edition}`);
                if (!details.isConnected || !details.open) return;
                renderArtifact(text, saved.body);
                onArtifact(coordinate);
              } catch (error) { text.textContent = safeMessage(error.message); }
            };
            reading.append(details);
          }
          if (!reading.childElementCount) reading.textContent = "当前为首份成果。";
        } catch (error) { reading.textContent = safeMessage(error.message); }
      };
      card.append(history);
    }
    content.append(card);
  }

  async function apply(task, change, control) {
    control.disabled = true;
    const current = version;
    try {
      const updated = await api(`${base}/${task.id}`, { method: "PATCH", body: { ...change, revision: task.revision } });
      if (current !== version) return;
      editing = false;
      renderTask(updated);
      status("");
    } catch (error) {
      if (current === version) status(safeMessage(error.message));
    } finally {
      control.disabled = false;
    }
  }

  toggle.onclick = () => panel.hidden ? open() : close();
  for (const [key, tab] of Object.entries(tabs)) tab.onclick = () => open(null, key);
  closeButton.onclick = () => close();
  panel.onkeydown = event => { if (event.key === "Escape") close(); };
  let counting = false;
  async function refreshAttention() {
    if (counting) return;
    const current = version;
    counting = true;
    try {
      const { count } = await api("/api/agent/attention");
      if (current === version) {
        toggle.textContent = "面板";
        if (count) toggle.textContent += ` · ${count}`;
        toggle.setAttribute("aria-label", count ? `面板，${count} 项更新` : "面板");
      }
    } finally {
      counting = false;
    }
  }
  return { open, close, refreshAttention };
}
