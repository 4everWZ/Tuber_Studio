import { node, safeMessage } from "./core.js?v=20261105.32";

export function mountFollowup(goal, content, api, setEditing) {
  if (!goal || goal.closed) return;
  const trigger = node("button", "周期跟进设置");
  trigger.type = "button";
  content.append(trigger);
  const path = `/api/agent/tasks/${goal.id}/goal/followup`;
  trigger.onclick = async () => {
    setEditing(true);
    trigger.disabled = true;
    const form = node("form", null, "chat-task-steer");
    const notice = node("p", "正在读取跟进设置…", "hint");
    notice.setAttribute("role", "status");
    const back = node("button", "返回目标");
    back.type = "button";
    back.onclick = () => {
      form.replaceWith(trigger);
      trigger.disabled = false;
      setEditing(false);
      trigger.focus();
    };
    trigger.replaceWith(form);
    form.append(notice, back);
    try {
      const current = await api(path);
      if (!form.isConnected) return;
      const config = current.schedule || {};
      notice.textContent = current.enabled
        ? `下次跟进：${new Date(current.next_due * 1000).toLocaleString()}（设备时间）`
        : "当前未启用。仅重读所选来源，结果留在此目标内。";
      function field(label, element) {
        const wrapper = node("label", label);
        wrapper.append(element);
        form.insertBefore(wrapper, back);
        return wrapper;
      }
      function select(label, choices, value) {
        const element = node("select");
        for (const [key, title] of choices) {
          const option = node("option", title);
          option.value = key;
          element.append(option);
        }
        element.value = value;
        field(label, element);
        return element;
      }
      const cadence = select("频率", [["daily", "每天"], ["weekly", "每周"]], config.cadence || "daily");
      const weekday = select("星期", Array.from("一二三四五六日", (label, index) => [index, label]), config.weekday ?? 0);
      cadence.onchange = () => { weekday.parentElement.hidden = cadence.value !== "weekly"; };
      cadence.onchange();
      const time = node("input"), zone = node("input"), pages = node("textarea");
      time.type = "time";
      time.required = zone.required = true;
      time.value = config.local_time || "";
      zone.value = config.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
      field("本地时间", time);
      field("时区", zone);
      const checks = [];
      for (const [key, title] of Object.entries({
        live_status: "直播状态", game_status: "模拟游戏状态", operation_status: "运营状态", today_activity: "当天公开运营活动",
      })) {
        const check = node("input");
        check.type = "checkbox";
        check.value = key;
        check.checked = config.states?.includes(key) || false;
        field(title, check);
        checks.push(check);
      }
      pages.value = (config.pages || []).join("\n");
      field("公开 HTTPS 来源（每行一个，最多四个）", pages);
      const save = node("button", "授权并保存"), stop = node("button", "停用跟进");
      save.type = "submit";
      stop.type = "button";
      stop.hidden = !current.enabled;
      async function update(method, body) {
        save.disabled = stop.disabled = true;
        try {
          const updated = await api(path, { method, body });
          notice.textContent = updated.enabled
            ? `已启用，下次跟进：${new Date(updated.next_due * 1000).toLocaleString()}（设备时间）` : "周期跟进已停用。";
          stop.hidden = !updated.enabled;
        } catch (error) {
          notice.textContent = safeMessage(error.message);
        } finally {
          save.disabled = stop.disabled = false;
        }
      }
      form.onsubmit = event => {
        event.preventDefault();
        const states = checks.filter(item => item.checked).map(item => item.value);
        const sources = pages.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        if (!states.length && !sources.length) {
          notice.textContent = "请先选择或填写一个只读来源。";
          return;
        }
        update("PUT", { epoch: current.epoch, cadence: cadence.value, weekday: cadence.value === "weekly" ? Number(weekday.value) : null,
          local_time: time.value, timezone: zone.value.trim(), states, pages: sources });
      };
      stop.onclick = () => update("DELETE");
      form.insertBefore(save, back);
      form.insertBefore(stop, back);
      cadence.focus();
    } catch (error) {
      notice.textContent = safeMessage(error.message);
    }
  };
}
