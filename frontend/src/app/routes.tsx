import type { ReactNode } from "react";
import { ChatFeature } from "@/features/chat";
import { LiveFeature } from "@/features/live";
import { OperationsFeature } from "@/features/operations";
import { PersonaFeature } from "@/features/persona";
import { SettingsFeature } from "@/features/settings";
import type { PageId, ParsedRoute } from "@/shell/navigation";

export type FeatureSlotRenderer = (route: ParsedRoute) => ReactNode;

export const WORKBENCH_ROUTE_SLOTS: Record<PageId, FeatureSlotRenderer> = {
  chat: (route) => <ChatFeature route={route} />,
  automation: (route) => <OperationsFeature route={route} />,
  "automation-records": (route) => <OperationsFeature route={route} />,
  "automation-settings": (route) => <OperationsFeature route={route} />,
  comments: (route) => <OperationsFeature route={route} />,
  messages: (route) => <OperationsFeature route={route} />,
  dynamics: (route) => <OperationsFeature route={route} />,
  issues: (route) => <OperationsFeature route={route} />,
  live: (route) => <LiveFeature route={route} />,
  "live-scene": (route) => <LiveFeature route={route} />,
  "live-music": (route) => <LiveFeature route={route} />,
  "live-settings": (route) => <LiveFeature route={route} />,
  "live-interaction": (route) => <LiveFeature route={route} />,
  characters: (route) => <PersonaFeature route={route} />,
  voices: (route) => <PersonaFeature route={route} />,
  assets: (route) => <PersonaFeature route={route} />,
  personalization: (route) => <PersonaFeature route={route} />,
  settings: (route) => <SettingsFeature route={route} />,
  accounts: (route) => <SettingsFeature route={route} />,
  overview: (route) => <SettingsFeature route={route} />,
};

export function renderWorkbenchRoute(route: ParsedRoute): ReactNode {
  const renderSlot = WORKBENCH_ROUTE_SLOTS[route.page] ?? WORKBENCH_ROUTE_SLOTS.automation;
  return renderSlot(route);
}
