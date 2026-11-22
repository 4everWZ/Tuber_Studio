import type { ReactNode } from "react";

export interface PanelCardProps {
  as?: "section" | "article" | "div";
  id?: string;
  title?: ReactNode;
  titleId?: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  className?: string;
  "aria-labelledby"?: string;
  "data-workspace-anchor"?: boolean;
  children: ReactNode;
}

export function PanelCard({
  as: Tag = "section",
  id,
  title,
  titleId,
  subtitle,
  actions,
  className,
  "aria-labelledby": ariaLabelledBy,
  "data-workspace-anchor": workspaceAnchor,
  children,
}: PanelCardProps) {
  const classes = ["panel", className].filter(Boolean).join(" ");
  return (
    <Tag
      id={id}
      className={classes}
      aria-labelledby={ariaLabelledBy ?? titleId}
      data-workspace-anchor={workspaceAnchor ? "" : undefined}
    >
      {(title != null || actions != null) && (
        <div className="panel-heading">
          <div>
            {title != null && (
              <h2 id={titleId} className="win-group-title">
                {title}
              </h2>
            )}
            {subtitle != null && <p className="hint">{subtitle}</p>}
          </div>
          {actions != null && <div className="actions">{actions}</div>}
        </div>
      )}
      {children}
    </Tag>
  );
}
