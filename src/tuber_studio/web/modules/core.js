/**
 * Core UI Utilities and Helpers for Tuber Studio
 */

export const $ = id => document.getElementById(id);
export const $$ = sel => Array.from(document.querySelectorAll(sel));

export function node(tag, content, props = {}) {
  const el = document.createElement(tag);
  if (content != null) {
    if (typeof content === "string") el.textContent = content;
    else if (content instanceof Node) el.appendChild(content);
    else if (Array.isArray(content)) content.forEach(c => el.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
  }
  Object.entries(props).forEach(([k, v]) => {
    if (k === "className") el.className = v;
    else if (k === "dataset") Object.entries(v).forEach(([dk, dv]) => el.dataset[dk] = dv);
    else if (k.startsWith("on")) el[k] = v;
    else el.setAttribute(k, v);
  });
  return el;
}

export function safeMessage(message) {
  if (typeof message !== "string") return "未知错误";
  return message.replace(/[<>&"']/g, c => ({
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    "\"": "&quot;",
    "'": "&#39;"
  }[c] || c));
}

export function notice(message, isError = false) {
  const el = $("flash-notice");
  if (!el) {
    console.log(isError ? `[ERROR] ${message}` : `[NOTICE] ${message}`);
    return;
  }
  el.textContent = message;
  el.className = isError ? "notice error" : "notice";
  el.hidden = false;
  clearTimeout(notice._timer);
  notice._timer = setTimeout(() => {
    el.hidden = true;
  }, isError ? 8000 : 4000);
}

export async function busy(button, task) {
  if (!button) return await task();
  const prevDisabled = button.disabled;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await task();
  } finally {
    button.disabled = prevDisabled;
    button.removeAttribute("aria-busy");
  }
}

export function tokenCount(count) {
  if (count == null || isNaN(count)) return "—";
  if (count < 1000) return `${count} tokens`;
  if (count < 1000000) return `${(count / 1000).toFixed(1)}K tokens`;
  return `${(count / 1000000).toFixed(2)}M tokens`;
}

export function formatBytes(bytes) {
  if (bytes == null || isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function formatDuration(seconds) {
  if (seconds == null || isNaN(seconds)) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
