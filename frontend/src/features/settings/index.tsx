import type { ParsedRoute } from "@/shell/navigation";
import { AccountsPage } from "./AccountsPage";
import { OverviewPage } from "./OverviewPage";
import { SettingsPage } from "./SettingsPage";

export { AccountsPage } from "./AccountsPage";
export { ArtworkReferencesPanel, CharacterDisplayPanel } from "./ArtworkReferencesPanel";
export { BilibiliAccountPanel } from "./BilibiliAccountPanel";
export { CachePanel } from "./CachePanel";
export { IllustrationPolicyPanel } from "./IllustrationPolicyPanel";
export { OverviewPage } from "./OverviewPage";
export { ProvidersPanel } from "./ProvidersPanel";
export { RetentionPanel } from "./RetentionPanel";
export { SettingsPage } from "./SettingsPage";

export function SettingsFeature({ route }: { route: ParsedRoute }) {
  switch (route.page) {
    case "accounts":
      return <AccountsPage />;
    case "overview":
      return <OverviewPage />;
    case "settings":
    default:
      return <SettingsPage />;
  }
}
