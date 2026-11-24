import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ChatFeature } from "@/features/chat";
import { LiveFeature } from "@/features/live";
import { OperationsFeature } from "@/features/operations";
import { operationsStore } from "@/features/operations/operationsStore";
import type { AutomationStatus } from "@/features/operations/types";
import { ManageMemoriesModal, PersonaFeature, type OpenManageMemoriesDetail } from "@/features/persona";
import { SettingsFeature } from "@/features/settings";
import type { PageId, ParsedRoute } from "@/shell/navigation";

export type FeatureSlotRenderer = (route: ParsedRoute) => ReactNode;

const MANAGE_MEMORIES_HASH_RE = /^#personalization(?:\/|#)(?:manage-memories)$/;

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

function normalizeScope(scope: string | undefined): "all" | "owner" | "public" {
  if (scope === "owner" || scope === "public") return scope;
  return "all";
}

function hashTriggersMemoryModal(hash: string): boolean {
  return MANAGE_MEMORIES_HASH_RE.test(hash);
}

export function ManageMemoriesHost({ currentHash }: { currentHash: string }) {
  const [open, setOpen] = useState(() => hashTriggersMemoryModal(currentHash));
  const [modalDetail, setModalDetail] = useState<OpenManageMemoriesDetail>({});

  useEffect(() => {
    if (hashTriggersMemoryModal(currentHash)) {
      setModalDetail({});
      setOpen(true);
    }
  }, [currentHash]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onOpenManageMemories = (event: Event) => {
      event.preventDefault();
      const custom = event as CustomEvent<OpenManageMemoriesDetail | undefined>;
      setModalDetail(custom.detail ?? {});
      setOpen(true);
    };
    const onHashChange = () => {
      if (hashTriggersMemoryModal(window.location.hash)) {
        setModalDetail({});
        setOpen(true);
      }
    };
    window.addEventListener("aqua:open-manage-memories", onOpenManageMemories);
    window.addEventListener("hashchange", onHashChange);
    return () => {
      window.removeEventListener("aqua:open-manage-memories", onOpenManageMemories);
      window.removeEventListener("hashchange", onHashChange);
    };
  }, []);

  const handleClose = useCallback(() => {
    setOpen(false);
    setModalDetail({});
    if (typeof window !== "undefined" && hashTriggersMemoryModal(window.location.hash)) {
      window.history.replaceState(null, "", "#personalization");
    }
  }, []);

  return (
    <ManageMemoriesModal
      open={open}
      initialScope={normalizeScope(modalDetail.scope)}
      highlightMemoryId={modalDetail.memoryId ?? null}
      highlightSnippet={modalDetail.snippet ?? null}
      onClose={handleClose}
    />
  );
}

interface AutomationStatusEventDetail {
  automation?: AutomationStatus | {
    state?: string | null;
    enabled?: boolean;
    emergency?: boolean;
    epoch?: number;
    inbox_enabled?: boolean;
    discovery_enabled?: boolean;
    comment_cooldown_until?: number | null;
  } | null;
  refresh?: boolean;
}

export function AutomationStatusBridge() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onAutomationStatus = (event: Event) => {
      const detail = (event as CustomEvent<AutomationStatusEventDetail | undefined>).detail;
      if (!detail) return;
      if (detail.automation) {
        const candidate = detail.automation;
        if ("policy" in candidate || "settings" in candidate || "effective" in candidate) {
          operationsStore.applyAutomationStatus(candidate as AutomationStatus);
        } else {
          operationsStore.applyLiveStatus({
            ...candidate,
            state: candidate.state ?? undefined,
          });
        }
      }
      if (detail.refresh) {
        void operationsStore.refreshStatus();
      }
    };
    window.addEventListener("aqua:automation-status", onAutomationStatus);
    return () => {
      window.removeEventListener("aqua:automation-status", onAutomationStatus);
    };
  }, []);

  return null;
}

export function renderWorkbenchRoute(route: ParsedRoute): ReactNode {
  const effectiveSlot =
    route.hash.startsWith("#personalization#") || route.hash === "#personalization"
      ? WORKBENCH_ROUTE_SLOTS.personalization
      : WORKBENCH_ROUTE_SLOTS[route.page] ?? WORKBENCH_ROUTE_SLOTS.automation;
  const effectiveRoute = route.hash.startsWith("#personalization#")
    ? { ...route, page: "personalization" as const, section: "settings" as const }
    : route;

  return (
    <>
      {effectiveSlot(effectiveRoute)}
      <ManageMemoriesHost currentHash={route.hash} />
      <AutomationStatusBridge />
    </>
  );
}
