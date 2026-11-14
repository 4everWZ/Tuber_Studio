import { $, node, safeMessage } from "./core.js";

const binaryMode = value => value === true ? "auto" : value === false ? "off" : null;
const modes = new Set(["off", "review", "auto"]);

export function operationCapabilities(status) {
  const permissions = status.permissions || {};
  const emergency = status.policy?.emergency === true;
  const blocked = emergency || ["blocked", "error", "failed"].includes(status.state);
  const running = status.policy?.enabled === true && status.state === "running" && !blocked;
  const reason = emergency ? "已紧急停止，请先解除紧停。" :
    (status.blockers || []).map(safeMessage).join("；") || safeMessage(status.last_error) || "自主运营报告受阻。";
  return [
    ["videos", "视频", binaryMode(permissions.video_enabled)],
    ["top_level", "主评", permissions.top_level],
    ["comments", "评论", permissions.replies],
    ["message_poll", "私信收取", binaryMode(permissions.private_poll)],
    ["message_reply", "私信回复", permissions.private_messages],
    ["dynamics", "动态", permissions.dynamics],
    ["like", "视频点赞", permissions.video_like],
    ["comment-like", "评论点赞", permissions.comment_like],
    ["favorite", "收藏", permissions.video_fav],
  ].map(([id, label, mode]) => {
    if (!modes.has(mode)) return { id, label, color: "gray", state: "待确认", detail: "尚未取得此功能的权限状态。" };
    if (mode === "off") return { id, label, color: "gray", state: "关闭", detail: "此功能未开放。可在运营设置中调整。" };
    if (blocked) return { id, label, color: "red", state: emergency ? "已紧停" : "受阻", detail: reason };
    const configured = mode === "auto" ? "已允许自动运行" : "已允许生成待审内容";
    if (status.state === "starting" || status.state === "draining") {
      const state = status.state === "starting" ? "检查中" : "收尾中";
      return { id, label, color: "gray", state, detail: `${configured}，自主运营${state}。` };
    }
    if (!running) return { id, label, color: "gray", state: status.state === "stopping" ? "停止中" : "待启动", detail: `${configured}；${status.state === "stopping" ? "自主运营正在停止" : "自主运营尚未运行"}。` };
    return mode === "review" ? { id, label, color: "yellow", state: "生成待审", detail: "自主运营已启用此功能，生成的内容需要人工审核。" } :
      { id, label, color: "green", state: "自动运行", detail: "自主运营已启用此功能，按已保存设置自动执行。" };
  });
}

export function renderOperationCapabilities(status) {
  const strip = $("op-permissions-summary");
  const capabilities = operationCapabilities(status);
  const signature = JSON.stringify(capabilities);
  if (strip.dataset.signature !== signature) {
    strip.dataset.signature = signature;
    strip.replaceChildren(...capabilities.map(capability => {
      const item = node("span", `${capability.label} · ${capability.state}`, `capability-light is-${capability.color}`);
      item.dataset.capability = capability.id;
      item.dataset.state = capability.color;
      item.setAttribute("role", "listitem");
      item.setAttribute("aria-label", `${capability.label}，${capability.state}。${capability.detail}`);
      item.title = capability.detail;
      return item;
    }));
  }
  const blocker = $("op-capability-blocker");
  if (blocker) {
    const reasons = (status.blockers || []).map(safeMessage);
    const checking = status.startup_check === "checking";
    const failed = !checking && reasons.length > 0;
    const emergency = status.policy?.emergency === true;
    blocker.classList.toggle("is-error", failed || emergency);
    blocker.hidden = !emergency && !failed && !checking && status.startup_check !== "pending";
    blocker.textContent = emergency ? "已紧急停止。" : checking ? "启动检查中…" :
      failed ? reasons.join("；") : status.startup_check === "pending" ? "启动时核验" : "";
  }
}
