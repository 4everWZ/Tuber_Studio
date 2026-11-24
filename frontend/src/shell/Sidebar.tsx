import { useEffect, useRef } from "react";
import styles from "@/shared/styles/workbench.module.css";
import { TOP_NAV_ITEMS, type PageId, type TopNavItem, type WorkspaceSection } from "./navigation";

export interface SidebarProps {
  currentPage: PageId;
  currentSection: WorkspaceSection | undefined;
  localAccess: boolean;
  menuOpen: boolean;
  sidebarCollapsed?: boolean;
  onToggleCollapse?: () => void;
  onCloseMenu: (restoreFocus?: boolean) => void;
  onLogout: () => void;
}

export function AquaBrandMark({ className }: { className?: string }) {
  return (
    <span className={`brand-mark ${styles.brandMark}${className ? ` ${className}` : ""}`} aria-hidden="true">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
        <path
          d="M12 3.2C8.8 7.5 6.5 10.7 6.5 14a5.5 5.5 0 0 0 11 0c0-3.3-2.3-6.5-5.5-10.8Z"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinejoin="round"
        />
        <path
          d="M9.5 14.5c.3 1.4 1.3 2.3 2.7 2.5"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}

export function SidebarToggleGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3.5" y="4.5" width="17" height="15" rx="3" stroke="currentColor" strokeWidth="1.7" />
      <path d="M9.5 4.5v15" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function NavItemIcon({ item }: { item: TopNavItem }) {
  const key = item.parentPage ?? item.page;
  if (key === "chat") {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <path
          d="M6.5 17.5 3.5 20V6.5A2.5 2.5 0 0 1 6 4h12a2.5 2.5 0 0 1 2.5 2.5v8.5A2.5 2.5 0 0 1 18 17.5H6.5Z"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  if (key === "operations") {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <path
          d="M4 12h3.5l2.2-5 4.2 10 2.3-5H20"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  if (key === "live") {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.7" />
        <path
          d="M7.5 16.5a6.4 6.4 0 0 1 0-9m9 0a6.4 6.4 0 0 1 0 9"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
      </svg>
    );
  }
  if (key === "characters") {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="8.5" r="3.4" stroke="currentColor" strokeWidth="1.7" />
        <path
          d="M5.5 19.2c1.2-3.1 3.7-4.7 6.5-4.7s5.3 1.6 6.5 4.7"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M12 4.5v2m0 11v2m7.5-7.5h-2m-11 0h-2m12.8-5.3-1.4 1.4m-7.8 7.8-1.4 1.4m0-10.6 1.4 1.4m7.8 7.8 1.4 1.4"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function Sidebar({
  currentPage,
  currentSection,
  localAccess,
  menuOpen,
  sidebarCollapsed = false,
  onToggleCollapse,
  onCloseMenu,
  onLogout,
}: SidebarProps) {
  const sidebarRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const firstLink =
      sidebarRef.current?.querySelector<HTMLAnchorElement>('[aria-current="page"]') ||
      sidebarRef.current?.querySelector<HTMLAnchorElement>("a");
    firstLink?.focus();
  }, [menuOpen]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && menuOpen) {
        onCloseMenu(true);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [menuOpen, onCloseMenu]);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(max-width: 760px)");
    const onChange = () => onCloseMenu(false);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, [onCloseMenu]);

  return (
    <>
      <aside
        ref={sidebarRef}
        className={`sidebar aqua-sidebar ${styles.sidebar}`}
        id="workspace-sidebar"
        aria-hidden={sidebarCollapsed && !menuOpen ? true : undefined}
      >
        <div className={`explorer-brand-box ${styles.brandBox}`}>
          <a className={`brand ${styles.brandLink}`} href="#chat" onClick={() => onCloseMenu(false)}>
            <AquaBrandMark />
            <div className={`brand-info ${styles.brandInfo}`}>
              <span className={`brand-name ${styles.brandName}`}>Aqua Studio</span>
              <span className={`brand-version ${styles.brandVersion}`}>Aqua · Personal Agent</span>
            </div>
          </a>
          {onToggleCollapse && (
            <button
              type="button"
              id="collapse-workspace-sidebar"
              className={styles.collapseSidebarBtn}
              aria-label="收起侧边栏"
              aria-expanded={!sidebarCollapsed}
              aria-controls="workspace-sidebar"
              title="收起侧边栏"
              onClick={onToggleCollapse}
            >
              <SidebarToggleGlyph />
            </button>
          )}
          <button
            type="button"
            id="close-workspace-menu"
            className={`btn-close-pane ${styles.closePaneBtn}`}
            aria-label="关闭侧边栏"
            onClick={() => onCloseMenu(true)}
          >
            ✕
          </button>
        </div>

        <nav
          className={`explorer-nav ${styles.sidebarNav}`}
          aria-label="工作区导航"
          onClick={(event) => {
            if ((event.target as HTMLElement).closest("a")) {
              onCloseMenu(true);
            }
          }}
        >
          {TOP_NAV_ITEMS.map((item) => {
            const active = item.parentPage ? item.parentPage === currentSection : item.page === currentPage;
            return (
              <a
                key={item.page}
                href={item.href}
                data-page={item.page}
                data-parent-page={item.parentPage}
                className={["tree-node", styles.treeNode, active ? `selected ${styles.treeNodeSelected}` : ""]
                  .filter(Boolean)
                  .join(" ")}
                aria-current={active ? "page" : undefined}
              >
                <span className={styles.nodeIcon} aria-hidden="true">
                  <NavItemIcon item={item} />
                </span>
                <span className={`node-text ${styles.nodeText}`}>{item.label}</span>
              </a>
            );
          })}
        </nav>

        <div className={`explorer-tree-footer ${styles.sidebarFooter}`}>
          <button
            id="logout-sidebar"
            type="button"
            className={`btn-win-action ${styles.logoutBtn}`}
            hidden={localAccess}
            onClick={onLogout}
          >
            退出登录
          </button>
        </div>
      </aside>

      <button
        type="button"
        id="workspace-backdrop"
        className={styles.backdrop}
        aria-label="关闭栏目"
        tabIndex={-1}
        onClick={() => onCloseMenu(true)}
      />
    </>
  );
}
