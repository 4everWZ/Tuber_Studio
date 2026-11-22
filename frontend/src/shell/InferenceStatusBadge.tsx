import type { SystemSettings, SystemStatus } from "@/shared/hooks/useAppSession";

export interface InferenceDisplayState {
  connected: boolean | undefined;
  connection: string;
  text: string;
}

export function inferenceDisplay(
  status: SystemStatus | null | undefined,
  settings: SystemSettings | null | undefined
): InferenceDisplayState {
  const context = status?.context || {};
  const configured = context.capacity_source === "model_card" || Boolean(settings?.omni);
  const connected = !configured
    ? undefined
    : context.profile_error
      ? false
      : context.verified === true
        ? true
        : undefined;
  const connection = connected === true ? "已连接" : connected === false ? "连接异常" : "待确认";
  return { connected, connection, text: `Omni API · ${connection}` };
}

export function InferenceStatusBadge({
  status,
  settings,
}: {
  status: SystemStatus | null | undefined;
  settings: SystemSettings | null | undefined;
}) {
  const info = inferenceDisplay(status, settings);
  return (
    <span className="sb-val" id="sb-status-engine">
      {info.connection}
    </span>
  );
}
