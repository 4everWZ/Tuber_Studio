import { node, safeMessage } from "./core.js?v=20261105.32";

export function mountGoalEvents(goal, content, api, setEditing) {
  if (!goal || goal.closed) return;
  const open = node("button", "来信事件跟踪");
  open.type = "button";
  content.append(open);
  open.onclick = async () => {
    setEditing(true);
    const form = node("form", null, "chat-task-steer");
    const notice = node("p", "正在读取跟踪授权…", "hint");
    notice.setAttribute("role", "status");
    const back = node("button", "返回目标");
    back.type = "button";
    back.onclick = () => {
      form.replaceWith(open);
      setEditing(false);
      open.focus();
    };
    open.replaceWith(form);
    form.append(notice, back);
    const path = `/api/agent/tasks/${goal.id}/goal/events`;
    try {
      let state = await api(path);
      if (!form.isConnected) return;
      const note = node("p", "仅跟踪授权后新入库的 B 站来信（含延迟同步）。只记消息标识，不读取正文，不开启轮询或自动回复。", "hint");
      const field = node("label", "发送者 UID"), uid = node("input");
      uid.inputMode = "numeric";
      uid.pattern = "[1-9][0-9]{0,19}";
      uid.maxLength = 20;
      uid.required = true;
      field.append(uid);
      const save = node("button", "授权跟踪此 UID"), stop = node("button", "停止事件跟踪");
      save.type = "submit";
      stop.type = "button";
      form.replaceChildren(note, notice, field, save, stop, back);
      function render() {
        uid.value = state.sender_uid || "";
        const binding = state.account ? `接收账号 ${state.account} · 角色 ${state.character}` : "尚未绑定接收账号";
        notice.textContent = state.enabled ? `${binding} · 已跟踪 UID ${state.sender_uid}` : `${binding} · 未启用`;
        save.disabled = !state.account || !state.epoch || state.closed;
        stop.hidden = !state.enabled;
        stop.disabled = false;
      }
      async function update(method) {
        save.disabled = stop.disabled = true;
        try {
          state = method === "PUT" ? await api(path, { method, body: { source: "bilibili_inbox",
            sender_uid: uid.value, revision: state.revision, epoch: state.epoch } })
            : await api(`${path}?revision=${state.revision}`, { method });
          render();
        } catch (error) {
          notice.textContent = safeMessage(error.message) + "；可返回目标并重新打开设置。";
          save.disabled = stop.disabled = false;
        }
      }
      form.onsubmit = event => { event.preventDefault(); void update("PUT"); };
      stop.onclick = () => update("DELETE");
      render();
      uid.focus();
    } catch (error) {
      notice.textContent = safeMessage(error.message);
    }
  };
}
