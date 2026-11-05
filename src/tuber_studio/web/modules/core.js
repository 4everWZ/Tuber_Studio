/**
 * Core UI Utilities and Helpers for Tuber Studio
 */

export const $ = id => document.getElementById(id);

export function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

export function safeMessage(message) {
  if (!message) return "";
  return String(message).replace(/((?:sessdata|bili_jct|token|api[_-]?key)=)[^&;\s]+/gi, "$1[已隐藏]");
}

let noticeTimer;
export function notice(message, error = false) {
  if (error && !document.dispatchEvent(new CustomEvent("workspace-issue", {
    detail: safeMessage(message), cancelable: true,
  }))) return;
  const element = $("notice");
  element.textContent = safeMessage(message);
  element.hidden = !message;
  element.classList.toggle("error", error);
  element.onclick = () => { element.hidden = true; };
  clearTimeout(noticeTimer);
  if (message) {
    noticeTimer = setTimeout(() => { element.hidden = true; }, error ? 10000 : 4000);
  }
}

export function tokenCount(count) {
  if (count == null || isNaN(count)) return "—";
  if (count < 1000) return `${count} tokens`;
  if (count < 1000000) return `${(count / 1000).toFixed(1)}K tokens`;
  return `${(count / 1000000).toFixed(2)}M tokens`;
}
