import { api } from "./api.js";
import { node } from "./core.js";

export function citations(items) {
  const details = node("details");
  details.className = "owner-memory-citations";
  details.append(node("summary", "为何提到它"));
  for (const item of items) {
    details.append(node("strong", item.note), node("blockquote", item.quote),
      node("small", `来源 ${item.source_key} · v${item.revision}`));
    if (item.source_key.startsWith("confirmed_voice:")) {
      const inspect = node("button", "核对语音来源");
      inspect.type = "button";
      inspect.onclick = async () => {
        try {
          const row = await api("/api/agent/memory.get", {method:"POST", body:{id:item.id, source_key:item.source_key}});
          const origin = row.source_key === item.source_key && row.voice_origin;
          inspect.replaceWith(node("p", origin ? `机器转写：${origin.machine_text}\n号主确认：${row.quote}` : "此引用没有可核对的录音转写回执。"));
        } catch { inspect.replaceWith(node("p", "当前无法读取这条来源。")); }
      };
      details.append(inspect);
    }
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
      const question = preview.replaces
        ? `更正私有偏好 #${preview.replaces.id}（v${preview.replaces.revision}）？\n\n原内容：${preview.replaces.note}\n新原话：${preview.text}`
        : `保存为号主私有记忆？\n\n${preview.text}`;
      const evidence = preview.machine_text ? `\n\n机器转写：${preview.machine_text}\n号主确认：${preview.text}` : "";
      if (!button.isConnected || !window.confirm(question + evidence)) return;
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
