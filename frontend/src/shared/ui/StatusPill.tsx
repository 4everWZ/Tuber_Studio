import type { ReactNode } from "react";

export type StatusPillTone = "neutral" | "good" | "bad" | "amber" | "blue";

export interface StatusPillProps {
  label: ReactNode;
  tone?: StatusPillTone;
  title?: string;
  className?: string;
  id?: string;
  role?: string;
  "data-state"?: string;
}

export function StatusPill({
  label,
  tone = "neutral",
  title,
  className,
  id,
  role,
  "data-state": dataState,
}: StatusPillProps) {
  const toneClass = tone === "neutral" ? "" : tone;
  const classes = ["badge", toneClass, className].filter(Boolean).join(" ");
  return (
    <span id={id} className={classes} title={title} role={role} data-state={dataState}>
      {label}
    </span>
  );
}
