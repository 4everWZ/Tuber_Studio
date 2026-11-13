import { node, safeMessage } from "./core.js?v=20261105.32";

const fields = [
  ["观看视频", "browsing", "discovery_enabled"],
  ["视频主评", "browsing", "top_level_enabled", "top_level_auto_publish"],
  ["公开跟帖", "browsing", "reply_enabled", "reply_auto_publish"],
  ["评论点赞", "browsing", "comment_like_enabled", "comment_like_auto_publish"],
  ["视频点赞", "browsing", "video_like_enabled", "video_like_auto_publish"],
  ["视频收藏", "browsing", "video_fav_enabled", "video_fav_auto_publish"],
  ["私信发送", "messages", "enabled", "auto_send"],
  ["自动读取来信", "messages", "auto_poll"],
  ["接受评论转私聊", "messages", "allow_comment_handoff"],
  ["动态", "dynamics", "enabled", "auto_publish"],
];
const labels = {
  starting: "正在检查启动条件", running: "自主运营中", draining: "正在收尾当前批次",
  stopping: "正在停止自主运营", blocked: "自主运营受阻", off: "自主运营已停止",
};

const readGroup = (status, group) => (group === "browsing" ? status.settings : status.base?.[group]) || {};
const scopeOf = status => [status.account_id || "", status.character?.id, status.character?.version].join("/");
const currentMode = (status, field) => {
  const values = readGroup(status, field.group);
  return !values[field.enabled] ? "0" : field.automatic && values[field.automatic] ? "2" : "1";
};

export function operationControlCard(root, initialStatus, { api, path, turn, reflectAutomation, controls }) {
  let status = initialStatus;
  const choices = [];
  const state = node("p", null, "hint");
  const explanation = node("p", null, "hint");
  const details = node("details");
  details.append(node("summary", "查看与修改运营权限"));
  const policy = node("div", null, "operator-chat-policy");
  details.append(policy);
  const actions = node("div", null, "actions");
  const save = node("button", "保存权限"), start = node("button", "确认并启动", "primary");
  const stop = node("button", "停止自主运营");
  const settings = node("a", "打开运营设置");
  settings.href = "#automation-settings";
  for (const button of [save, start, stop]) button.type = "button";
  actions.append(save, start, stop, settings);
  const note = node("p", "", "hint");
  note.setAttribute("role", "status");
  root.append(node("h2", "自主运营"), state, explanation, details, actions, note);

  for (const [title, group, enabled, automatic] of fields) {
    const label = node("label", title), select = node("select");
    const modes = automatic ? [["0", "关闭"], ["1", "人工审核"], ["2", "自动执行"]] : [["0", "关闭"], ["1", "允许"]];
    for (const [value, text] of modes) {
      const option = node("option", text);
      option.value = value;
      select.append(option);
    }
    const field = { group, enabled, automatic, select, changed: false };
    select.onchange = () => { field.changed = select.value !== currentMode(status, field); sync(); };
    choices.push(field);
    label.append(select);
    policy.append(label);
  }

  function sync() {
    const emergency = status.policy?.emergency === true;
    const enabled = status.policy?.enabled === true;
    const pending = controls.pending || ["starting", "draining", "stopping"].includes(status.state);
    const dirty = choices.some(field => field.changed);
    state.textContent = emergency ? "已紧急停止" : labels[status.state] || (enabled ? labels.running : labels.off);
    const blockers = (status.blockers || []).map(item => typeof item === "string" ? item : item.message).filter(Boolean);
    explanation.textContent = safeMessage(blockers.join("；"));
    explanation.hidden = !blockers.length;
    for (const field of choices) {
      if (!field.changed) field.select.value = currentMode(status, field);
      field.select.disabled = pending;
    }
    save.disabled = pending || !dirty;
    start.hidden = enabled;
    start.disabled = pending || emergency;
    stop.hidden = !enabled;
    stop.disabled = pending || emergency;
  }

  async function apply(action) {
    if (controls.pending) return;
    const scope = scopeOf(status);
    controls.pending = true;
    reflectAutomation(status);
    note.textContent = action === "stop" ? "正在请求收尾…" : "正在确认运营权限…";
    let result;
    try {
      if (action === "stop") {
        result = await api("/api/automation/stop", { method: "POST", body: { emergency: false } });
      } else {
        const fresh = await api("/api/automation");
        if (scopeOf(fresh) !== scope) {
          throw new Error("运营账号或角色已变更，请按当前状态重新确认权限。");
        }
        const body = {};
        for (const group of ["browsing", "messages", "dynamics"]) {
          const changed = choices.filter(field => field.group === group && field.changed);
          if (!changed.length) continue;
          const patch = {};
          for (const { enabled, automatic, select } of changed) {
            patch[enabled] = select.value !== "0";
            if (automatic) patch[automatic] = select.value === "2";
          }
          body[group] = patch;
        }
        const response = await api(`${path}/turns/${turn.request_id}/confirm/${action}`, { method: "POST", body });
        result = response.card.data;
        for (const field of choices) field.changed = false;
      }
      note.textContent = action === "save" ? "权限已保存。" : result.state === "draining" ? "本批收尾后停止。" : labels[result.state] || "控制状态已更新。";
    } catch (error) {
      note.textContent = safeMessage(error.message);
      try { result = await api("/api/automation"); } catch { /* The next status refresh will reconcile the card. */ }
    } finally {
      controls.pending = false;
      reflectAutomation(result || status);
    }
  }

  save.onclick = () => apply("save");
  start.onclick = () => apply("start");
  stop.onclick = () => apply("stop");
  sync();
  return {
    update(value) {
      if (scopeOf(value) !== scopeOf(status)) {
        for (const field of choices) field.changed = false;
      }
      status = value;
      sync();
    },
  };
}
