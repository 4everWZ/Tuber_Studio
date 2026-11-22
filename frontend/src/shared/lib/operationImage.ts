import { studioPath } from "../api/client";

export interface IllustrationRef {
  asset_id?: string | null;
  artifact_id?: string | null;
  status?: string | null;
  error?: string | null;
  job_id?: string | null;
}

export function illustrationUrl(
  channel: string,
  draftId: string | number,
  ref: IllustrationRef | null | undefined
): string {
  if (ref?.asset_id) {
    return studioPath(`/api/v1/persona/memes/${encodeURIComponent(ref.asset_id)}/preview`);
  }
  if (ref?.artifact_id) {
    return studioPath(
      `/api/v1/operations/illustrations/${encodeURIComponent(channel)}/${encodeURIComponent(String(draftId))}/${encodeURIComponent(ref.artifact_id)}`
    );
  }
  return "";
}

export function illustrationStatus(ref: IllustrationRef | null | undefined): string {
  if (ref?.status && ["failed", "awaiting_choice"].includes(ref.status)) {
    return ref.error || "配图待决定";
  }
  return ref?.job_id ? "正在决定或生成配图" : "配图已绑定";
}
