import { useCallback } from "react";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { emitNotice } from "@/shared/lib/errors";
import type { ParsedRoute } from "@/shell/navigation";
import { LiveDesk } from "./LiveDesk";
import { LiveInteractionPage } from "./LiveInteractionPage";
import { LiveMusicPage } from "./LiveMusicPage";
import { LiveScenePage } from "./LiveScenePage";
import { LiveSettingsPage } from "./LiveSettingsPage";

export interface LiveFeatureProps {
  route: ParsedRoute;
}

export function LiveFeature({ route }: LiveFeatureProps) {
  const { settings } = useAppSession();

  const handleFeedback = useCallback((message: string, error = false) => {
    emitNotice(message, error, "live");
  }, []);

  return (
    <section data-section={route.page} className="win-page-view">
      {route.page === "live" && <LiveDesk settings={settings} mode="workbench" visible />}
      {route.page === "live-scene" && (
        <LiveScenePage visible showWorkbenchHeading onFeedback={handleFeedback} />
      )}
      {route.page === "live-music" && (
        <LiveMusicPage settings={settings} visible showWorkbenchHeading onFeedback={handleFeedback} />
      )}
      {route.page === "live-settings" && (
        <LiveSettingsPage visible onFeedback={handleFeedback} />
      )}
      {route.page === "live-interaction" && (
        <LiveInteractionPage visible onFeedback={handleFeedback} />
      )}
    </section>
  );
}
