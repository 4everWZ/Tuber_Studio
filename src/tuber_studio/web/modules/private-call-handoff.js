import { api } from "./api.js";
import { rememberButton } from "./owner-memory.js";

export class FinalUtterances {
  items = new Map();
  committed = new Set();
  rejected = new Set();
  seen = new Set();

  accept(event) {
    if (event.type === "input_audio_buffer.committed") {
      if (typeof event.item_id === "string") this.committed.add(event.item_id);
      return;
    }
    if (["conversation.item.added", "conversation.item.created", "conversation.item.done"].includes(event.type)) {
      const item = event.item;
      if (typeof item?.id === "string" && item.type === "message" && Array.isArray(item.content)) {
        if (item.role !== "user") this.rejected.add(item.id);
        else this.items.set(item.id, item.content);
      }
      return;
    }
    const part = this.items.get(event.item_id)?.[event.content_index];
    if (event.type !== "conversation.item.input_audio_transcription.completed" || this.rejected.has(event.item_id) ||
        !this.committed.has(event.item_id) || !Number.isInteger(event.content_index) ||
        part?.type !== "input_audio" ||
        typeof event.transcript !== "string" || !event.transcript.trim()) return;
    if (part.transcript && part.transcript !== event.transcript) return;
    const key = JSON.stringify([event.item_id, event.content_index]);
    if (this.seen.has(key)) return;
    this.seen.add(key);
    return event.transcript.trim();
  }
}

export function showHandoff(root, text, call = {}) {
  const row = document.createElement("div");
  const quote = document.createElement("blockquote");
  quote.textContent = text;
  row.append(quote);
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "将这句话交办";
  const feedback = document.createElement("span");
  feedback.setAttribute("role", "status");
  if ([...text].length > 2000) {
    feedback.textContent = "超过任务字数上限，请在文字对话中整理后交办。";
  } else {
    const requestId = crypto.randomUUID();
    button.onclick = async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        await api("/api/agent/tasks", {method: "POST", body: {request_id: requestId, goal: text}});
        feedback.textContent = "已交办，可在活动中查看。";
      } catch {
        feedback.textContent = "交办未确认，可以重试。";
        button.disabled = false;
      }
    };
    row.append(button);
  }
  row.append(feedback);
  if ([...text].length <= 150) row.append(rememberButton(() => ({source: "confirmed_voice", text}), "确认并记住原话"));
  const targets = (call.memory_citations || []).filter(item => call.correction_targets?.includes(item.id));
  if ([...text].length <= 150 && call.memory_use && targets.length) {
    const label = document.createElement("label"), select = document.createElement("select");
    label.append("更正本次引用的偏好 ", select);
    select.append(new Option("选择原偏好", ""));
    for (const item of targets) select.append(new Option(`#${item.id} · v${item.revision} · ${item.note}`, String(item.id)));
    row.append(label, rememberButton(() => {
      const item = targets.find(target => String(target.id) === select.value);
      if (!item) throw Error("请选择要更正的原偏好");
      return {source: "confirmed_voice", text, correction: {id: item.id, revision: item.revision, use: call.memory_use}};
    }, "确认用这句话更正"));
  }
  root.append(row);
}
