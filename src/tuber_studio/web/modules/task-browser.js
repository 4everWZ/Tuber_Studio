import { node, safeMessage } from "./core.js?v=20261105.32";

function button(text, click) {
  const el = node("button", text);
  el.type = "button";
  el.onclick = click;
  return el;
}

function field(title, element) {
  const label = node("label", title);
  label.append(element);
  return label;
}

function sourcesInput() {
  const input = node("textarea");
  input.required = true;
  input.rows = 3;
  input.placeholder = "每行一个完整的 HTTPS 文档地址";
  return input;
}

function sources(input) {
  return input.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

export function browserTaskForm(panel, api, requestId, open, editing) {
  editing(true);
  const form = node("form", null, "chat-task-steer");
  const goal = node("textarea"), urls = sourcesInput(), note = node("p", "", "hint");
  goal.required = true;
  goal.maxLength = 2000;
  const submit = node("button", "交办阅读");
  form.append(field("希望阅读这些来源做什么？", goal), field("允许读取的公开来源", urls),
    submit, button("返回活动", () => open()), note);
  const rid = requestId();
  form.onsubmit = async event => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const task = await api("/api/agent/tasks", { method: "POST",
        body: { request_id: rid, goal: goal.value, browser_sources: sources(urls) } });
      if (form.isConnected) open(task.id);
    } catch (error) {
      note.textContent = safeMessage(error.message);
      submit.disabled = false;
    }
  };
  panel.replaceChildren(form);
  goal.focus();
}

export function mountTaskBrowser(task, panel, api, open, editing) {
  const granted = task.steps.some(s => s.tool === "owner_browser_grant");
  if (!task.can_control || (!granted && ["completed", "failed", "cancelled"].includes(task.status))) return;
  panel.append(button(granted ? "查看阅读浏览器" : "授权阅读浏览器", show));
  async function show() {
    editing(true);
    const path = `/api/agent/tasks/${task.id}/browser`;
    const screen = node("section", null, "chat-task-steer"), heading = node("h3", "阅读浏览器");
    const body = node("div"), notice = node("p", "", "hint");
    notice.setAttribute("role", "status");
    screen.append(heading, body, notice, button("返回任务", () => open(task.id)));
    panel.replaceChildren(screen);
    let state, busy = false;
    const find = node("input");
    find.maxLength = 160;
    async function update(method, payload, suffix = "") {
      if (busy) return;
      busy = true;
      try {
        state = await api(path + suffix, {method, body: payload});
        notice.textContent = "";
        if (screen.isConnected) draw();
      } catch (error) {
        notice.textContent = safeMessage(error.message);
        state = null;
      } finally {
        busy = false;
      }
    }
    function draw() {
      body.replaceChildren();
      if (state.mode === "ended") body.append(node("p", "浏览会话已结束。"));
      if (state.mode === "ungranted" || state.mode === "ended") {
        if (["completed", "failed", "cancelled"].includes(task.status)) return;
        const input = sourcesInput();
        body.append(field("允许读取的公开来源", input), button("授权本次阅读", () =>
          update("PUT", {revision: state.revision, version: state.version || 0, sources: sources(input)})));
        return;
      }
      const version = state.version, owner = state.mode === "owner";
      const act = (kind, index = 0, text = "") => update("POST", {version, kind, index, text}, "/actions");
      const hand = action => update("PATCH", {version, action});
      body.append(node("p", owner ? "由你控制" : "由角色控制"),
        node("p", state.sources[state.position - 1], "hint"),
        button(owner ? "交回角色" : "接管浏览器", () => hand(owner ? "handback" : "takeover")),
        button("终止浏览", () => hand("end")));
      if (state.view.image) {
        const image = node("img");
        image.alt = "当前 Chromium 阅读视口";
        image.src = "data:image/jpeg;base64," + state.view.image;
        image.style.cssText = "display:block;width:100%;height:auto;margin:.7rem 0";
        body.append(image);
        const transcript = node("details");
        transcript.append(node("summary", "视口文字"), node("p", state.view.text));
        body.append(transcript);
      }
      if (!owner) return;
      const list = node("select");
      state.sources.forEach((url, i) => {
        const option = node("option", url);
        option.value = String(i + 1);
        list.append(option);
      });
      list.value = String(state.position);
      list.style.maxWidth = "100%";
      body.append(field("已授权来源", list), button("打开来源", () => act("open", Number(list.value))),
        button("上一屏", () => act("previous")), button("下一屏", () => act("next")),
        field("页内文字", find), button("查找", () => act("find", 0, find.value)));
      for (const c of state.view.controls || []) {
        body.append(button(`${c.kind === "link" ? "打开" : c.kind === "choice" ? "切换选项" : "展开或收起"} · ${c.label || c.index}`,
          () => act("activate", c.index)));
      }
    }
    async function refresh() {
      if (!screen.isConnected) return;
      if (!busy) {
        const before = state;
        try {
          const next = await api(path);
          if (!busy && before === state && screen.isConnected &&
              (!state || state.version !== next.version || state.mode !== next.mode || state.view?.image !== next.view?.image)) {
            state = next;
            draw();
          }
        } catch (error) {
          body.replaceChildren();
          state = null;
          notice.textContent = safeMessage(error.message);
        }
      }
      if (screen.isConnected) setTimeout(refresh, 1300);
    }
    await refresh();
  }
}
