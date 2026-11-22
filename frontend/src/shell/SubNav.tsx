import styles from "@/shared/styles/workbench.module.css";
import { SUB_NAV_TABS, type PageId, type WorkspaceSection } from "./navigation";

const SECTION_LABELS: Record<WorkspaceSection, string> = {
  operations: "自主运营",
  live: "直播工作台",
  characters: "角色工作室",
  settings: "设置",
};

export interface SubNavProps {
  currentPage: PageId;
  section: WorkspaceSection | undefined;
}

export function SubNav({ currentPage, section }: SubNavProps) {
  if (!section) {
    return null;
  }
  const tabs = SUB_NAV_TABS[section];
  const selectedOperation = currentPage === "automation" ? "overview" : currentPage;

  return (
    <nav
      id={section === "operations" ? "operations-navigation" : undefined}
      data-workspace-navigation={section}
      className={`win-property-tabs ${styles.propertyTabs}`}
      aria-label={SECTION_LABELS[section]}
    >
      {tabs.map((tab) => {
        const active = tab.isOperationsTab ? tab.tab === selectedOperation : tab.tab === currentPage;
        return (
          <a
            key={tab.tab}
            href={tab.href}
            data-operations-tab={tab.isOperationsTab ? tab.tab : undefined}
            data-workspace-tab={tab.isOperationsTab ? undefined : tab.tab}
            className={["prop-tab", styles.propTab, active ? `active ${styles.propTabActive}` : ""]
              .filter(Boolean)
              .join(" ")}
            aria-current={active ? "page" : undefined}
          >
            {tab.label}
          </a>
        );
      })}
    </nav>
  );
}
