import type { ParsedRoute } from "@/shell/navigation";
import { AutomationDashboardPage } from "./AutomationDashboardPage";
import { AutomationRecordsPage } from "./AutomationRecordsPage";
import { AutomationSettingsPage } from "./AutomationSettingsPage";
import { CommentsPage } from "./CommentsPage";
import { DynamicsPage } from "./DynamicsPage";
import { IssuesPage } from "./IssuesPage";
import { MessagesPage } from "./MessagesPage";
import { VideoStudioPage } from "./VideoStudioPage";

export function OperationsFeature({ route }: { route: ParsedRoute }) {
  switch (route.page) {
    case "automation":
      return <AutomationDashboardPage route={route} />;
    case "automation-records":
      return <AutomationRecordsPage route={route} />;
    case "automation-settings":
      return <AutomationSettingsPage />;
    case "comments":
      return <CommentsPage route={route} />;
    case "messages":
      return <MessagesPage route={route} />;
    case "dynamics":
      return <DynamicsPage route={route} />;
    case "videos":
      return <VideoStudioPage route={route} />;
    case "issues":
      return <IssuesPage />;
    default:
      return <AutomationDashboardPage route={route} />;
  }
}
