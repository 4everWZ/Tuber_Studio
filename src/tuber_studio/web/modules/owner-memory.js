import { api } from "./api.js";
import { node } from "./core.js";

export function citations(items) {
  const details = node("details");
  details.className = "owner-memory-citations";
  details.append(node("summary", "为何提到它"));
  for (const item of items) {
    details.append(node("strong", item.note), node("blockquote", item.quote),
      node("small", `来源 ${item.source_key} · v${item.revision}`));
  }
  return details;
}

export function rememberButton(proposal, label = "记住这句话") {
  const group = node("span"), button = node("button", label), feedback = node("span");
  button.type = "button";
  feedback.role = "status";
  button.onclick = async () => {
    button.disabled = true;
    try {
      const body = proposal();
      const preview = await api("/api/agent/memory.propose", {method: "POST", body});
      if (!button.isConnected || !window.confirm(`保存为号主私有记忆？\n\n${preview.text}`)) return;
      await api("/api/agent/memory/confirm", {method: "POST", body: {...body, stamp: preview.stamp}});
      feedback.textContent = "已保存";
      document.dispatchEvent(new Event("owner-memory-changed"));
    } catch (error) {
      feedback.textContent = error.message;
    } finally { button.disabled = false; }
  };
  group.append(button, feedback);
  return group;
}
