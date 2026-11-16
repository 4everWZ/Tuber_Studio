import { $, node, safeMessage } from "./core.js?v=20261105.32";
import { renderAssistantAnswer } from "./operator-chat-answer.js";

const base = "/api/agent/tasks";
const states = {
  queued: "等待执行", scheduled: "已预约", running: "进行中", waiting: "等待工具结果",
  awaiting_approval: "等待确认", needs_input: "等待你的答复", completed: "已完成", cancelled: "已停止", failed: "未完成",
};
const tools = {
  web_search: "搜索网页", web_page: "打开来源", bilibili_owner: "查号主主页", operation_status: "查看运营",
  today_activity: "查看当日活动", inspect_video: "读取视频", live_status: "查看直播", game_status: "查看游戏",
  live_handoff: "交接直播素材", game_action: "执行模拟游戏动作",
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

export function initTaskActivity(api, requestId) {
  const panel = $("chat-activity"), toggle = $("chat-activity-toggle"), closeButton = $("chat-activity-close");
  const content = $("chat-activity-content"), notice = $("chat-activity-status");
  let selection = null, version = 0, timer, reading = false, viewKey, editing = false;

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
    if (restore) toggle.focus();
  }

  function open(id = null) {
    clearInterval(timer);
    version++;
    selection = id;
    viewKey = null;
    editing = false;
    content.replaceChildren();
    status("正在读取活动…");
    panel.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    closeButton.focus();
    refresh();
    timer = setInterval(refresh, 2000);
  }

  async function refresh() {
    if (reading || panel.hidden || editing) return;
    const current = version;
    reading = true;
    try {
      const value = await api(selection ? `${base}/${selection}` : base);
      if (current !== version || editing) return;
      status("");
      const key = JSON.stringify(value);
      if (key === viewKey) return;
      viewKey = key;
      if (selection) renderTask(value);
      else renderList(value.tasks);
    } catch (error) {
      if (current === version) status(safeMessage(error.message));
    } finally {
      reading = false;
      if (current !== version && !panel.hidden) refresh();
    }
  }

  function renderList(tasks) {
    const schedule = button("预约任务");
    schedule.onclick = showScheduleForm;
    content.replaceChildren(schedule);
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
    if (task.message) {
      renderAssistantAnswer(result, { reply_format: "markdown", reply_text: task.message });
      if (["completed", "failed"].includes(task.status) && task.steps.some(step => step.result)) {
        result.prepend(node("p", "按工具回执交付", "hint"));
      }
    }
    const steps = node("ol", null, "chat-task-steps");
    for (const step of task.steps) {
      const item = node("li");
      const label = tools[step.tool] || step.tool;
      const observation = observedStatus(step.tool, step.result);
      const summary = observation || (step.result ? (step.result.error ? "有错误" : "已收到结果") : "等待结果");
      const details = node("details");
      details.append(node("summary", `${label} · ${summary}`));
      const result = step.result;
      if (result) {
        const description = result.error || result.spoken || result.message || result.excerpt || result.state?.summary;
        if (typeof description === "string" && description) details.append(node("p", description));
        if (result.url) appendSource(details, result.url, result.title || result.name || "打开来源");
        for (const url of result.links || []) appendSource(details, url);
      }
      const diagnostics = node("details");
      diagnostics.append(node("summary", "工具记录"), node("pre", JSON.stringify(result || step.arguments, null, 2)));
      details.append(diagnostics);
      item.append(details);
      steps.append(item);
    }
    const adjust = button(task.status === "needs_input" ? "补充要求" : "调整任务");
    const stop = button("停止任务");
    stop.hidden = !stoppable.has(task.status);
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
    content.replaceChildren(back, title, state, result, steps, adjust, stop);
    if (task.status === "awaiting_approval") {
      const step = task.steps.at(-1);
      const action = node("p", `${step.approval.target} · ${step.approval.text || step.arguments.action}`);
      const confirm = button(step.tool === "live_handoff" ? "确认交接给本场直播" : "确认执行这个动作");
      confirm.onclick = () => apply(task, { action: "approve", step_id: step.id }, confirm);
      content.append(action, confirm);
    }
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
  closeButton.onclick = () => close();
  panel.onkeydown = event => { if (event.key === "Escape") close(); };
  return { open, close };
}
