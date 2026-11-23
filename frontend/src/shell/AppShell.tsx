import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, apiGet, apiPost, setUnauthorizedHandler } from "@/shared/api/client";
import {
  AppSessionProvider,
  type AppSessionValue,
  type SystemSettings,
  type SystemStatus,
} from "@/shared/hooks/useAppSession";
import { characterDisplayStore } from "@/shared/hooks/useCharacterDisplay";
import { hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { emitNotice, errorMessage, subscribeWorkspaceIssues } from "@/shared/lib/errors";
import { operationsStore } from "@/features/operations/operationsStore";
import styles from "@/shared/styles/workbench.module.css";
import { ToastNotice } from "@/shared/ui/ToastNotice";
import { LoginGate } from "./LoginGate";
import {
  PAGE_TITLES,
  parseHashRoute,
  type PageId,
  type ParsedRoute,
} from "./navigation";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { Sidebar } from "./Sidebar";
import { StatusFooter } from "./StatusFooter";
import { SubNav } from "./SubNav";
import {
  TopStatusBar,
  type AutomationSummary,
  type BilibiliAccountSummary,
} from "./TopStatusBar";
import { AdminWsClient } from "./ws/adminClient";

export interface AppShellProps {
  renderRoute: (route: ParsedRoute) => ReactNode;
  queryClient?: QueryClient;
}

function createDefaultQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        refetchOnWindowFocus: false,
      },
    },
  });
}

export function AppShell({ renderRoute, queryClient: providedQueryClient }: AppShellProps) {
  const [queryClient] = useState(() => providedQueryClient ?? createDefaultQueryClient());
  const [authenticated, setAuthenticated] = useState(false);
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [automation, setAutomation] = useState<AutomationSummary | null>(null);
  const [account, setAccount] = useState<BilibiliAccountSummary | null>(null);
  const [unreadIssuesCount, setUnreadIssuesCount] = useState(0);
  const [route, setRoute] = useState<ParsedRoute>(() =>
    typeof window !== "undefined" ? parseHashRoute(window.location.hash) : parseHashRoute("#chat")
  );

  const mainShellRef = useRef<HTMLDivElement | null>(null);
  const scrollPositionsRef = useRef<Map<PageId, number>>(new Map());
  const prevPageRef = useRef<PageId>(route.page);
  const wsClientRef = useRef<AdminWsClient | null>(null);
  const hasStatusRef = useRef(false);
  hasStatusRef.current = status !== null;

  const handleUnauthorized = useCallback(() => {
    wsClientRef.current?.disconnect();
    characterDisplayStore.reset();
    setAuthenticated(false);
    setWsConnected(false);
    setBooting(false);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(handleUnauthorized);
    return () => setUnauthorizedHandler(null);
  }, [handleUnauthorized]);

  const refreshSettings = useCallback(async (): Promise<SystemSettings | null> => {
    const next = await apiGet<"/api/v1/system/settings", SystemSettings>("/api/v1/system/settings");
    setSettings(next);
    return next;
  }, []);

  const refreshStatus = useCallback(async (): Promise<SystemStatus | null> => {
    const next = await apiGet<"/api/v1/system/status", SystemStatus>("/api/v1/system/status");
    setStatus(next);
    return next;
  }, []);

  const refreshShellBadges = useCallback(async () => {
    const results = await Promise.allSettled([
      apiGet<"/api/v1/operations/automation", AutomationSummary>("/api/v1/operations/automation"),
      apiGet<"/api/v1/operations/account/bilibili", BilibiliAccountSummary>(
        "/api/v1/operations/account/bilibili"
      ),
      apiGet<"/api/v1/operations/discovery/records", { issue_history?: Array<{ unread_occurrences?: number; occurrences?: number }> }>(
        "/api/v1/operations/discovery/records"
      ),
    ]);
    if (results[0].status === "fulfilled") setAutomation(results[0].value);
    if (results[1].status === "fulfilled") setAccount(results[1].value);
    if (results[2].status === "fulfilled") {
      const issues = results[2].value.issue_history || [];
      setUnreadIssuesCount(issues.filter((item) => (item.unread_occurrences ?? item.occurrences ?? 1) > 0).length);
    }
  }, []);

  const enterWorkspace = useCallback(async () => {
    setBootError("");
    const [loadedSettings, loadedStatus] = await Promise.all([
      refreshSettings(),
      refreshStatus(),
      characterDisplayStore.refresh().catch((err) => {
        emitNotice(errorMessage(err), true);
      }),
    ]);
    setSettings(loadedSettings);
    setStatus(loadedStatus);
    setAuthenticated(true);
    setBooting(false);
    void refreshShellBadges();
  }, [refreshSettings, refreshStatus, refreshShellBadges]);

  useEffect(() => {
    let active = true;
    enterWorkspace().catch((error) => {
      if (!active) return;
      if (!(error instanceof ApiError && error.status === 401)) {
        setBootError(errorMessage(error));
      }
      handleUnauthorized();
    });
    return () => {
      active = false;
    };
  }, [enterWorkspace, handleUnauthorized]);

  // Connect WS /ws/admin when authenticated; suppress duplicate /status polls while OPEN.
  useEffect(() => {
    if (!authenticated) {
      wsClientRef.current?.disconnect();
      return;
    }
    const client = new AdminWsClient({
      onStatus(next) {
        setStatus(next);
        operationsStore.applyLiveStatus(next.automation);
        if (next.automation) {
          setAutomation((prev) => ({
            ...(prev || {}),
            state: next.automation?.state ?? prev?.state,
            policy: {
              ...((prev as { policy?: Record<string, unknown> } | null)?.policy || {}),
              enabled: next.automation?.enabled,
              emergency: next.automation?.emergency,
            },
          }));
        }
      },
      onContextUpdated(context) {
        setStatus((prev) => (prev ? { ...prev, context } : prev));
      },
      onConnectionChange(connected) {
        setWsConnected(connected);
      },
      onUnauthorized: handleUnauthorized,
    });
    wsClientRef.current = client;
    client.connect();
    return () => {
      client.disconnect();
      if (wsClientRef.current === client) wsClientRef.current = null;
    };
  }, [authenticated, handleUnauthorized]);

  // Background poll (skips /api/v1/system/status whenever WS /ws/admin is OPEN)
  useEffect(() => {
    if (!authenticated) return;
    const poll = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      const tasks: Array<Promise<unknown>> = [];
      if (!wsClientRef.current?.isOpen || !hasStatusRef.current) {
        tasks.push(refreshStatus().catch(() => {}));
      }
      if (route.page === "chat" || route.page === "settings") {
        tasks.push(characterDisplayStore.refresh().catch(() => {}));
      }
      if (route.section === "operations" || route.page === "accounts") {
        tasks.push(refreshShellBadges());
      }
      await Promise.allSettled(tasks);
    };

    const interval = setInterval(() => {
      void poll();
    }, 2500);

    const onVisibilityChange = () => {
      if (typeof document !== "undefined" && !document.hidden) {
        void poll();
      }
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }
    return () => {
      clearInterval(interval);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    };
  }, [authenticated, route.page, route.section, refreshStatus, refreshShellBadges]);

  // Hash navigation listener
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onHashChange = () => {
      setRoute(parseHashRoute(window.location.hash));
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // Page title, scroll restoration, anchor target scrolling
  useEffect(() => {
    if (typeof document !== "undefined") {
      document.title = `${PAGE_TITLES[route.page]} · 大肥鱼工坊`;
    }
    if (typeof window === "undefined") return;

    const prevPage = prevPageRef.current;
    if (prevPage !== route.page) {
      scrollPositionsRef.current.set(prevPage, window.scrollY);
      prevPageRef.current = route.page;
      if (!route.anchor) {
        window.scrollTo(0, scrollPositionsRef.current.get(route.page) || 0);
      }
    }

    if (authenticated && route.anchor && typeof document !== "undefined") {
      const target = document.getElementById(route.anchor);
      if (target) {
        for (let el: HTMLElement | null = target; el; el = el.parentElement) {
          if (el instanceof HTMLDetailsElement) el.open = true;
        }
        target.scrollIntoView?.();
      }
    }
  }, [authenticated, route]);

  // Mobile drawer inert sync on main shell
  useEffect(() => {
    if (mainShellRef.current) {
      mainShellRef.current.inert = menuOpen;
    }
  }, [menuOpen]);

  // Dirty-form beforeunload protection
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (hasAnyUnsavedChanges()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  // Track error issue badge count when workspace issues fire locally
  useEffect(() => {
    return subscribeWorkspaceIssues(() => {
      setUnreadIssuesCount((count) => count + 1);
    });
  }, []);

  const navigate = useCallback((hash: string) => {
    if (typeof window === "undefined") return;
    const normalized = hash.startsWith("#") ? hash : `#${hash}`;
    window.location.hash = normalized;
    setRoute(parseHashRoute(normalized));
  }, []);

  const logout = useCallback(async () => {
    await apiPost("/api/v1/system/auth/logout");
    handleUnauthorized();
  }, [handleUnauthorized]);

  const closeMenu = useCallback((restoreFocus = false) => {
    setMenuOpen((wasOpen) => {
      if (wasOpen && restoreFocus && typeof document !== "undefined") {
        setTimeout(() => {
          document.getElementById("workspace-menu")?.focus();
        }, 0);
      }
      return false;
    });
  }, []);

  const localAccess = settings?.local_access === true;

  const sessionValue = useMemo<AppSessionValue>(
    () => ({
      authenticated,
      localAccess,
      wsConnected,
      status,
      settings,
      refreshStatus,
      refreshSettings,
      navigate,
      logout,
      onUnauthorized: handleUnauthorized,
    }),
    [
      authenticated,
      localAccess,
      wsConnected,
      status,
      settings,
      refreshStatus,
      refreshSettings,
      navigate,
      logout,
      handleUnauthorized,
    ]
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AppSessionProvider value={sessionValue}>
        {!authenticated ? (
          booting ? (
            <main id="login" className={styles.loginShell} aria-busy="true">
              <div className={styles.loginPanel}>
                <div className={styles.brandMark} aria-hidden="true">
                  TS
                </div>
                <h1>Tuber Studio</h1>
                <p className="muted">正在连接工作台…</p>
              </div>
            </main>
          ) : (
            <LoginGate initialError={bootError} onAuthenticated={enterWorkspace} />
          )
        ) : (
          <div
            id="workspace"
            className={[
              "workspace win-layout",
              styles.layout,
              menuOpen ? `nav-open ${styles.navOpen}` : "",
              route.page === "chat" ? styles.chatMode : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            <Sidebar
              currentPage={route.page}
              currentSection={route.section}
              localAccess={localAccess}
              menuOpen={menuOpen}
              onCloseMenu={closeMenu}
              onLogout={() => void logout().catch((err) => emitNotice(errorMessage(err), true))}
            />

            <div ref={mainShellRef} className={`main-shell win-main-shell ${styles.mainShell}`}>
              <TopStatusBar
                menuOpen={menuOpen}
                onToggleMenu={() => setMenuOpen((prev) => !prev)}
                status={status}
                settings={settings}
                automation={automation}
                account={account}
                unreadIssuesCount={unreadIssuesCount}
              />

              <ToastNotice />

              <main className={`content win-content ${styles.content}`}>
                <SubNav currentPage={route.page} section={route.section} />
                <RouteErrorBoundary routeKey={route.hash}>
                  {renderRoute(route)}
                </RouteErrorBoundary>
              </main>

              <StatusFooter status={status} settings={settings} />
            </div>
          </div>
        )}
      </AppSessionProvider>
    </QueryClientProvider>
  );
}
