export function inferenceModeChanging(inference) {
  return inference?.operation?.mode != null && inference.operation.mode !== inference.mode;
}

export function liveActionDisabled(action, status = {}, { enabled = false, transitioning = false } = {}) {
  const state = status.state || "stopped";
  if (!enabled && !(action === "stop" && state !== "stopped")) return true;
  if (transitioning && ["start", "resume"].includes(action)) return true;
  return action === "start" ? state !== "stopped" :
    action === "pause" ? !["live", "degraded"].includes(state) :
    action === "resume" ? state !== "paused" :
    action === "stop" ? state === "stopped" :
    action === "mute" ? !!status.muted :
    action === "unmute" ? !status.muted :
    action === "skip" ? !status.current_job :
    action === "clear" ? !status.queue_length : false;
}
