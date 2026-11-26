import React from "react";
import { AquaOverlay, type AquaOverlayProps } from "./AquaOverlay";

export type AquaBadgeProps = AquaOverlayProps;

export const AquaBadge: React.FC<AquaBadgeProps> = (props) => {
  return <AquaOverlay {...props} />;
};

export { AquaOverlay };
