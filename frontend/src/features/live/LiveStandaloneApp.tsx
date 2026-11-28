import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, apiGet, apiPost, setUnauthorizedHandler, studioPath } from "@/shared/api/client";
import type { SystemSettings } from "@/shared/hooks/useAppSession";
import { hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { errorMessage } from "@/shared/lib/errors";
import { LiveDesk } from "./LiveDesk";
import { LiveInteractionPage } from "./LiveInteractionPage";
import { LiveMusicPage } from "./LiveMusicPage";
import { LiveScenePage } from "./LiveScenePage";
import { LiveSettingsPage } from "./LiveSettingsPage";
import styles from "./LiveWorkbench.module.css";
import type { BilibiliAccountSnapshot } from "./types";

export type StandaloneView = "desk" | "interaction" | "scene" | "music" | "settings";

const STANDALONE_VIEWS: readonly { id: StandaloneView; label: string; href: string }[] = [
  { id: "desk", label: "导播台", href: "/live" },
  { id: "interaction", label: "排班与规则", href: "/live?view=interaction" },
  { id: "scene", label: "场景编排", href: "/live?view=scene" },
  { id: "music", label: "曲库与点歌", href: "/live?view=music" },
  { id: "settings", label: "直播设置", href: "/live?view=settings" },
];

function parseStandaloneView(search: string): StandaloneView {
  const focusView = new URLSearchParams(search).get("view");
  return focusView === "interaction" ||
    focusView === "scene" ||
    focusView === "music" ||
    focusView === "settings"
    ? focusView
    : "desk";
}

export function LiveStandaloneApp() {
  const [view, setView] = useState<StandaloneView>(() =>
    typeof window !== "undefined" ? parseStandaloneView(window.location.search) : "desk"
  );
  const [embedded] = useState<boolean>(() =>
    typeof window !== "undefined" ? Boolean(window.frameElement) : false
  );
  const [authenticated, setAuthenticated] = useState(false);
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [account, setAccount] = useState<BilibiliAccountSnapshot | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [loginError, setLoginError] = useState("");
  const [submittingLogin, setSubmittingLogin] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; error: boolean } | null>(null);

  const handleFeedback = useCallback((text: string, error = false) => {
    setFeedback(text ? { text, error } : null);
  }, []);

  const handleUnauthorized = useCallback(() => {
    setAuthenticated(false);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(handleUnauthorized);
    return () => setUnauthorizedHandler(null);
  }, [handleUnauthorized]);

  const boot = useCallback(async () => {
    try {
      const loaded = await apiGet<"/api/v1/system/settings", SystemSettings>(
        "/api/v1/system/settings"
      );
      setSettings(loaded);
      setAuthenticated(true);
      setLoginError("");
      if (!embedded) {
        void apiGet<"/api/v1/operations/account/bilibili", BilibiliAccountSnapshot>(
          "/api/v1/operations/account/bilibili"
        )
          .then((acc) => setAccount(acc))
          .catch(() => {});
      }
    } catch (error) {
      setAuthenticated(false);
      if (!(error instanceof ApiError && error.status === 401)) {
        setLoginError(errorMessage(error));
      }
    }
  }, [embedded]);

  useEffect(() => {
    void boot();
  }, [boot]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onPopState = () => {
      setView(parseStandaloneView(window.location.search));
    };
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (hasAnyUnsavedChanges()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, []);

  const navigateView = useCallback((nextView: StandaloneView) => {
    setView(nextView);
    if (typeof window !== "undefined") {
      const targetUrl = studioPath(nextView === "desk" ? "/live" : `/live?view=${nextView}`);
      window.history.pushState(null, "", targetUrl);
    }
  }, []);

  const handleLogin = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = tokenInput.trim();
    if (!trimmed || submittingLogin) return;
    setSubmittingLogin(true);
    setLoginError("");
    try {
      await apiPost("/api/v1/system/auth/login", { body: { token: trimmed } });
      setTokenInput("");
      await boot();
    } catch (error) {
      setLoginError(errorMessage(error));
    } finally {
      setSubmittingLogin(false);
    }
  };

  if (!authenticated) {
    return (
      <main id="login" className={styles.loginWrap}>
        <form id="login-form" className={styles.loginForm} onSubmit={(e) => void handleLogin(e)}>
          <span className={styles.brandIcon}>🐋</span>
          <h1>进入直播工作台</h1>
          <label>
            管理员令牌
            <input
              id="token"
              type="password"
              autoComplete="current-password"
              required
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              disabled={submittingLogin}
            />
          </label>
          <button type="submit" className="primary" disabled={submittingLogin}>
            登录
          </button>
          <p id="login-error" role="alert" className="error-text">
            {loginError}
          </p>
          <a href={studioPath("/")}>返回评论工作台</a>
        </form>
      </main>
    );
  }

  const profile = account?.profile;

  return (
    <div id="workspace" className={styles.standaloneShell} data-live-embedded={embedded ? "true" : undefined}>
      {!embedded && (
        <>
          <header className={styles.topbar}>
            <a
              className={styles.brand}
              href={studioPath("/live")}
              onClick={(e) => {
                e.preventDefault();
                navigateView("desk");
              }}
            >
              <span className={styles.brandIcon}>🐋</span>
              <span>
                Aqua 直播工坊 <small>直播工作台</small>
              </span>
            </a>
            <a href={studioPath("/#live")}>回到主工作台 ↗</a>
            <div id="global-bilibili-account" className={styles.accountHeader}>
              <a href={studioPath("/#accounts")} className="account-badge" aria-label="B站账号">
                <span className="account-avatar">
                  {profile?.avatar_url ? (
                    <img
                      src={studioPath("/api/v1/operations/account/bilibili/avatar")}
                      alt=""
                      referrerPolicy="no-referrer"
                    />
                  ) : (
                    <span aria-hidden="true">
                      {profile?.name ? Array.from(profile.name)[0] : "B站"}
                    </span>
                  )}
                </span>
                <span className="account-badge-name">
                  {profile?.name || (account?.state === "not_configured" ? "未配置" : "B站账号")}
                </span>
              </a>
            </div>
          </header>

          <nav className={styles.localNav} aria-label="直播工作区">
            {STANDALONE_VIEWS.map((tab) => (
              <a
                key={tab.id}
                data-live-tab={tab.id}
                href={studioPath(tab.href)}
                aria-current={view === tab.id ? "page" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  navigateView(tab.id);
                }}
              >
                {tab.label}
              </a>
            ))}
          </nav>
        </>
      )}

      <div
        id="feedback"
        className={
          feedback?.error ? `${styles.feedback} ${styles.feedbackError}` : styles.feedback
        }
        role="status"
        hidden={!feedback?.text}
      >
        {feedback?.text ?? ""}
      </div>

      {view === "desk" && (
        <LiveDesk
          settings={settings}
          mode="standalone"
          visible
          onNavigateStandaloneView={(target) => navigateView(target)}
        />
      )}
      {view === "interaction" && (
        <LiveInteractionPage visible onFeedback={handleFeedback} />
      )}
      {view === "scene" && (
        <LiveScenePage visible showWorkbenchHeading={false} onFeedback={handleFeedback} />
      )}
      {view === "music" && (
        <LiveMusicPage
          settings={settings}
          visible
          showWorkbenchHeading={false}
          onFeedback={handleFeedback}
        />
      )}
      {view === "settings" && <LiveSettingsPage visible onFeedback={handleFeedback} />}

      {!embedded && (
        <footer className={styles.standaloneFooter}>
          虚构 AI 同人角色 · 非官方{"\u3000/\u3000"}形象：YunYueSama{"\u3000/\u3000"}
          预览不输出声音，正式音频由单一 OBS 播放器输出。
        </footer>
      )}
    </div>
  );
}
