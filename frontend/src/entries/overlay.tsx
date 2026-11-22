import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { OverlayPlayerApp } from "@/features/live";

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <StrictMode>
      <OverlayPlayerApp />
    </StrictMode>
  );
}
