export function liveActionDisabled(action, status = {}, { enabled = false } = {}) {
  const state = status.state || "stopped";
  if (!enabled && !(action === "stop" && state !== "stopped")) return true;
  return action === "start" ? state !== "stopped" :
    action === "pause" ? !["live", "degraded"].includes(state) :
    action === "resume" ? !["paused", "degraded"].includes(state) :
    action === "stop" ? state === "stopped" :
    action === "mute" ? !!status.muted :
    action === "unmute" ? !status.muted :
    action === "skip" ? !status.current_job :
    action === "clear" ? !status.queue_length : false;
}
