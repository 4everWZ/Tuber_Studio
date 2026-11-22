import type { SystemSettings, SystemStatus } from "@/shared/hooks/useAppSession";
import { inferenceDisplay } from "./InferenceStatusBadge";

export interface ServiceStatusCapsuleProps {
  status: SystemStatus | null | undefined;
  settings: SystemSettings | null | undefined;
  className?: string;
  dotClassName?: string;
  dotGoodClassName?: string;
  dotBadClassName?: string;
}

export function ServiceStatusCapsule({
  status,
  settings,
  className = "",
  dotClassName = "status-dot",
  dotGoodClassName = "good",
  dotBadClassName = "bad",
}: ServiceStatusCapsuleProps) {
  const state = inferenceDisplay(status, settings);
  const online = state.connected;
  const capsuleStateClass = online === false ? "offline" : online ? "online" : "";
  const dotStateClass = online === false ? dotBadClassName : online ? dotGoodClassName : "";

  return (
    <a
      href="#settings"
      id="service-status-capsule"
      className={["service-status-capsule", capsuleStateClass, className].filter(Boolean).join(" ")}
      title="查看 Omni 服务能力"
      data-chat-hide=""
    >
      <span className={[dotClassName, dotStateClass].filter(Boolean).join(" ")} aria-hidden="true" />
      <span className="status-text">{state.connection}</span>
    </a>
  );
}
