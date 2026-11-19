import { api } from "./api.js";
import { node } from "./core.js";

export function voiceSourceButton(id, sourceKey) {
  const inspect = node("button", "核对语音来源");
  inspect.type = "button";
  inspect.onclick = async () => {
    try {
      const row = await api("/api/agent/memory.get", {method: "POST", body: {id, source_key: sourceKey}});
      const origin = row.source_key === sourceKey && row.voice_origin;
      inspect.replaceWith(node("p", origin ? `机器转写：${origin.machine_text}\n号主确认：${row.quote}` : "这条来源没有录音转写回执。"));
    } catch { inspect.replaceWith(node("p", "当前无法读取这条来源。")); }
  };
  return inspect;
}

export function rememberButton(proposal, label) {
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
