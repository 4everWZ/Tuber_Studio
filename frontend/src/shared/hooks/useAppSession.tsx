import { createContext, useContext, type ReactNode } from "react";
import type { CharacterLike } from "../lib/errors";

export interface ContextStatusSnapshot {
  scope?: string | null;
  usable?: boolean;
  error?: string | null;
  configured_tokens?: number | null;
  server_tokens?: number | null;
  native_tokens?: number | null;
  active_tokens?: number | null;
  compact_count?: number | null;
  compaction_passes_last?: number | null;
  summary_budgets_last?: number[] | null;
  secondary_threshold_percent?: number | null;
  last_compaction_warning?: string | null;
  last_compaction_error?: string | null;
  capacity_source?: string | null;
  profile_error?: string | null;
  verified?: boolean | null;
  [key: string]: unknown;
}

export interface SystemStatus {
  state?: string;
  muted?: boolean;
  character?: CharacterLike | null;
  session_id?: string | null;
  queue_length?: number;
  overlay_connected?: boolean;
  current_job?: Record<string, unknown> | null;
  error?: string | null;
  last_played?: Record<string, unknown> | null;
  providers?: Record<string, unknown> | null;
  resources?: Record<string, unknown> | null;
  context?: ContextStatusSnapshot | null;
  deployment?: Record<string, unknown> | null;
  playback?: Record<string, unknown> | null;
  operations_waiting?: boolean;
  [key: string]: unknown;
}

export interface SystemSettings {
  local_access?: boolean;
  voice_id?: string | null;
  omni?: Record<string, unknown> | null;
  features?: {
    live_enabled?: boolean;
    [key: string]: unknown;
  } | null;
  overlay_url?: string | null;
  config_path?: string | null;
  [key: string]: unknown;
}

export interface AppSessionValue {
  authenticated: boolean;
  localAccess: boolean;
  wsConnected: boolean;
  status: SystemStatus | null;
  settings: SystemSettings | null;
  refreshStatus: () => Promise<SystemStatus | null>;
  refreshSettings: () => Promise<SystemSettings | null>;
  navigate: (hash: string) => void;
  logout: () => Promise<void>;
  onUnauthorized: () => void;
}

const defaultSession: AppSessionValue = {
  authenticated: false,
  localAccess: false,
  wsConnected: false,
  status: null,
  settings: null,
  refreshStatus: async () => null,
  refreshSettings: async () => null,
  navigate: (hash: string) => {
    if (typeof window !== "undefined") {
      window.location.hash = hash.startsWith("#") ? hash : `#${hash}`;
    }
  },
  logout: async () => {},
  onUnauthorized: () => {},
};

const AppSessionContext = createContext<AppSessionValue>(defaultSession);

export function AppSessionProvider({
  value,
  children,
}: {
  value: AppSessionValue;
  children: ReactNode;
}) {
  return <AppSessionContext.Provider value={value}>{children}</AppSessionContext.Provider>;
}

export function useAppSession(): AppSessionValue {
  return useContext(AppSessionContext);
}
