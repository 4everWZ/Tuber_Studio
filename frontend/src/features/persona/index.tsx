import type { ParsedRoute } from "@/shell/navigation";
import { AssetsPage } from "./AssetsPage";
import { CharactersPage } from "./CharactersPage";
import { PersonalizationPage } from "./PersonalizationPage";
import { VoicesPage } from "./VoicesPage";

export { AssetsPage } from "./AssetsPage";
export { CharactersPage } from "./CharactersPage";
export { MemeGallery } from "./MemeGallery";
export { MemorySummaryDialog } from "./MemorySummaryDialog";
export { PersonalizationPage } from "./PersonalizationPage";
export { RecentEventsDialog } from "./RecentEventsDialog";
export { VoicesPage } from "./VoicesPage";

export function PersonaFeature({ route }: { route: ParsedRoute }) {
  switch (route.page) {
    case "voices":
      return <VoicesPage />;
    case "assets":
      return <AssetsPage />;
    case "personalization":
      return <PersonalizationPage />;
    case "characters":
    default:
      return <CharactersPage />;
  }
}
