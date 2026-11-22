import { useEffect, useState } from "react";
import type { SystemSettings, SystemStatus } from "@/shared/hooks/useAppSession";
import { label } from "@/shared/lib/display";
import { webCharacterName } from "@/shared/lib/errors";
import styles from "@/shared/styles/workbench.module.css";
import { InferenceStatusBadge } from "./InferenceStatusBadge";

function formatClock(): string {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

export function StatusFooter({
  status,
  settings,
}: {
  status: SystemStatus | null;
  settings: SystemSettings | null;
}) {
  const [clockText, setClockText] = useState(formatClock);

  useEffect(() => {
    const timer = setInterval(() => {
      setClockText(formatClock());
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const character = webCharacterName(status?.character);

  return (
    <footer className={`win-statusbar ${styles.statusbar}`} data-chat-hide="">
      <div className={`statusbar-panel panel-mode ${styles.statusbarPanel}`}>
        <span className="sb-label">直播:</span>{" "}
        <span id="sb-status-mode" className={`sb-val ${styles.sbVal}`}>
          {status?.state ? label(status.state) : "待读取"}
        </span>
      </div>
      <div className={`statusbar-panel panel-character ${styles.statusbarPanel}`}>
        <span className="sb-label">角色:</span>{" "}
        <span id="sb-status-char" className={`sb-val ${styles.sbVal}`}>
          {character || "—"}
        </span>
      </div>
      <div className={`statusbar-panel panel-engine ${styles.statusbarPanel} ${styles.statusbarEngine}`}>
        <span className="sb-label">Omni API:</span>{" "}
        <InferenceStatusBadge status={status} settings={settings} />
      </div>
      <div className={`statusbar-panel panel-time ${styles.statusbarPanel}`}>
        <span id="sb-status-time" className={`sb-val ${styles.sbVal}`}>
          {clockText}
        </span>
      </div>
    </footer>
  );
}
