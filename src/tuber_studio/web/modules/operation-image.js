import { studioPath } from "./api.js";

export function illustrationUrl(channel, draftId, ref) {
  if (ref?.asset_id) return studioPath(`/api/memes/${encodeURIComponent(ref.asset_id)}/preview`);
  if (ref?.artifact_id) return studioPath(`/api/operations/illustrations/${channel}/${encodeURIComponent(draftId)}/${encodeURIComponent(ref.artifact_id)}`);
  return "";
}

export function illustrationStatus(ref) {
  if (ref?.status === "failed") return ref.error || "配图失败，可换图或移除配图。";
  return ref?.job_id ? "正在决定或生成配图" : "配图已绑定";
}
