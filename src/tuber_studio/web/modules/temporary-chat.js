import { api } from "./api.js";

const dialog = document.getElementById("temporary-chat");
const log = dialog.querySelector("[data-turns]"), form = dialog.querySelector("[data-send]");
const input = form.querySelector("textarea"), button = form.querySelector("button");
const status = dialog.querySelector("[data-status]");
let messages = [], pending;
document.getElementById("temporary-chat-open").onclick = () => {
  document.getElementById("operator-chat-more-toggle").click();
  dialog.showModal();
  input.focus();
};
dialog.onclose = () => {
  pending?.abort();
  pending = null;
  messages = [];
  log.replaceChildren();
  form.reset();
  button.disabled = false;
  status.textContent = "";
};
function leave() { if (dialog.open) dialog.close(); }
window.addEventListener("pagehide", leave);
window.addEventListener("hashchange", leave);
new MutationObserver(() => { if (document.getElementById("workspace").hidden) leave(); })
  .observe(document.getElementById("workspace"), {attributes: true, attributeFilter: ["hidden"]});
form.onsubmit = async event => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || pending) return;
  const controller = new AbortController();
  pending = controller;
  button.disabled = true;
  status.textContent = "正在回复…";
  try {
    const result = await api("/api/chat/intelligent", {method: "POST", signal: controller.signal,
      body: {request_id: crypto.randomUUID(), message, temporary: true, window: messages}});
    if (pending !== controller || !dialog.open) return;
    const turn = [{role: "user", content: message}, {role: "assistant", content: result.reply_text}];
    messages = [...messages, ...turn.map(item => ({...item, content: item.content.slice(0, 8000)}))].slice(-8);
    for (const item of turn) {
      const line = document.createElement("p");
      line.textContent = item.content;
      log.append(line);
    }
    if (input.value.trim() === message) input.value = "";
    status.textContent = "";
    input.focus();
  } catch (error) {
    if (!controller.signal.aborted) status.textContent = error.message;
  } finally {
    if (pending === controller) { pending = null; button.disabled = false; }
  }
};
