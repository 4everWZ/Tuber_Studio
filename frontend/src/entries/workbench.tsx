import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@/shared/styles/global.css";
import { renderWorkbenchRoute } from "@/app/routes";
import { AppShell } from "@/shell/AppShell";

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <StrictMode>
      <AppShell renderRoute={renderWorkbenchRoute} />
    </StrictMode>
  );
}
