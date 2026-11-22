import { useEffect, useRef } from "react";
import styles from "@/shared/styles/workbench.module.css";
import { TOP_NAV_ITEMS, type PageId, type WorkspaceSection } from "./navigation";

export interface SidebarProps {
  currentPage: PageId;
  currentSection: WorkspaceSection | undefined;
  localAccess: boolean;
  menuOpen: boolean;
  onCloseMenu: (restoreFocus?: boolean) => void;
  onLogout: () => void;
}

export function Sidebar({
  currentPage,
  currentSection,
  localAccess,
  menuOpen,
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
        className={`sidebar win-explorer-tree ${styles.sidebar}`}
        id="workspace-sidebar"
      >
        <div className={`explorer-brand-box ${styles.brandBox}`}>
          <a className={`brand ${styles.brandLink}`} href="#chat" onClick={() => onCloseMenu(false)}>
            <span className={`brand-mark ${styles.brandMark}`} aria-hidden="true">
              TS
            </span>
            <div className={`brand-info ${styles.brandInfo}`}>
              <span className={`brand-name ${styles.brandName}`}>Tuber Studio</span>
              <span className={`brand-version ${styles.brandVersion}`}>大肥鱼工坊</span>
            </div>
          </a>
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
