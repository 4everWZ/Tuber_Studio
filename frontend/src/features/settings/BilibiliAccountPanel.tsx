import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPost, apiPut, studioPath } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { safeMessage } from "@/shared/lib/errors";
import styles from "./BilibiliAccountPanel.module.css";
import type {
  BilibiliAccountStatus,
  BilibiliLoginPollResponse,
  BilibiliLoginSessionResponse,
} from "./types";

const STATE_LABELS: Record<string, string> = {
  not_configured: "未配置",
  unverified: "已配置 · 未验证",
  logged_in: "已登录",
  expired: "凭证已过期",
  restricted: "读取受限",
  error: "读取失败",
};

const RENEWAL_LABELS: Record<string, string> = {
  manual: "手动凭证 · 可扫码启用",
  available: "已启用",
  checking: "检查中",
  confirmation_pending: "已保存新会话 · 等待确认",
  error: "暂不可用 · 保留会话",
  idle: "未启用",
};

function formatNumber(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)
    : "—";
}

function maskSecrets(raw: string, sensitiveValues: string[]): string {
  let text = safeMessage(raw);
  for (const val of sensitiveValues) {
    if (val) text = text.split(val).join("[凭证已隐藏]");
  }
  return text;
}

function signatureOf(value: BilibiliAccountStatus | null): string {
  return JSON.stringify([value?.state, value?.configured, value?.profile?.uid]);
}

export interface BilibiliAccountPanelProps {
  showSettings?: boolean;
  onChange?: (account: BilibiliAccountStatus) => void;
}

export function BilibiliAccountPanel({ showSettings = true, onChange }: BilibiliAccountPanelProps) {
  const { localAccess, onUnauthorized } = useAppSession();

  const [value, setValue] = useState<BilibiliAccountStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [requestError, setRequestError] = useState("");
  const [feedback, setFeedback] = useState<{ text: string; error: boolean }>({ text: "", error: false });

  const [loadedAvatarKey, setLoadedAvatarKey] = useState<string | null>(null);
  const [sessdataInput, setSessdataInput] = useState("");
  const [csrfInput, setCsrfInput] = useState("");
  const [refreshTokenInput, setRefreshTokenInput] = useState("");

  // QR Login state
  const [qrVisible, setQrVisible] = useState(false);
  const [qrImageSrc, setQrImageSrc] = useState<string | null>(null);
  const [qrStatus, setQrStatus] = useState("");

  // Management token reset dialog state
  const [resetDialogOpen, setResetDialogOpen] = useState(false);
  const [resetPending, setResetPending] = useState(false);
  const [tokenPresented, setTokenPresented] = useState(false);
  const [newToken, setNewToken] = useState("");
  const [resetFeedback, setResetFeedback] = useState("");

  const mountedRef = useRef(true);
  const valueRef = useRef<BilibiliAccountStatus | null>(null);
  const loadingRef = useRef(false);
  const pendingRef = useRef(false);
  const sequenceRef = useRef(0);
  const lastRefreshAttemptRef = useRef(0);
  const sensitiveValuesRef = useRef<string[]>([]);
  const loginSessionRef = useRef<{
    id: string;
    expiresAt: number;
    timer: ReturnType<typeof setTimeout> | null;
  } | null>(null);

  const resetDialogRef = useRef<HTMLDialogElement | null>(null);
  const resetCancelRef = useRef<HTMLButtonElement | null>(null);
  const tokenCopyRef = useRef<HTMLButtonElement | null>(null);

  valueRef.current = value;
  loadingRef.current = loading;
  pendingRef.current = pending;

  const safeErrorMessage = useCallback((error: unknown): string => {
    const raw =
      error instanceof Error && error.message
        ? error.message
        : typeof error === "string" && error
          ? error
          : "账号操作失败，请重试。";
    return maskSecrets(raw, sensitiveValuesRef.current);
  }, []);

  const acceptAccount = useCallback(
    (next: BilibiliAccountStatus, changed = false) => {
      const prev = valueRef.current;
      valueRef.current = next;
      setValue(next);
      setRequestError("");
      if (changed || (prev && signatureOf(prev) !== signatureOf(next))) {
        onChange?.(next);
      }
    },
    [onChange]
  );

  const closeScanLogin = useCallback((cancelRemote = false) => {
    const session = loginSessionRef.current;
    if (session?.timer) clearTimeout(session.timer);
    loginSessionRef.current = null;
    setQrVisible(false);
    setQrImageSrc(null);
    if (cancelRemote && session) {
      void apiDelete("/api/v1/operations/account/bilibili/login/{identity}", {
        pathParams: { identity: session.id },
      }).catch(() => {});
    }
  }, []);

  const loadAccount = useCallback(async () => {
    if (!mountedRef.current || loadingRef.current || pendingRef.current) return;
    const seq = ++sequenceRef.current;
    loadingRef.current = true;
    setLoading(true);
    try {
      const next = await apiGet<"/api/v1/operations/account/bilibili", BilibiliAccountStatus>(
        "/api/v1/operations/account/bilibili"
      );
      if (mountedRef.current && seq === sequenceRef.current) {
        acceptAccount(next);
      }
    } catch (error) {
      if (mountedRef.current && seq === sequenceRef.current) {
        setRequestError(safeErrorMessage(error));
      }
    } finally {
      if (seq === sequenceRef.current) {
        loadingRef.current = false;
        if (mountedRef.current) setLoading(false);
      }
    }
  }, [acceptAccount, safeErrorMessage]);

  const runAction = useCallback(
    async (work: () => Promise<void>) => {
      if (!mountedRef.current || pendingRef.current) return;
      pendingRef.current = true;
      setPending(true);
      sequenceRef.current += 1;
      loadingRef.current = false;
      setLoading(false);
      try {
        await work();
      } catch (error) {
        if (mountedRef.current) {
          setFeedback({ text: safeErrorMessage(error), error: true });
        }
      } finally {
        pendingRef.current = false;
        sensitiveValuesRef.current = [];
        if (mountedRef.current) setPending(false);
      }
    },
    [safeErrorMessage]
  );

  const refreshProfile = useCallback(async () => {
    await runAction(async () => {
      lastRefreshAttemptRef.current = Date.now();
      let next: BilibiliAccountStatus;
      try {
        next = await apiPost<"/api/v1/operations/account/bilibili/refresh", BilibiliAccountStatus>(
          "/api/v1/operations/account/bilibili/refresh"
        );
      } catch (error) {
        if (mountedRef.current) {
          setRequestError(safeErrorMessage(error));
        }
        throw error;
      }
      if (!mountedRef.current) return;
      acceptAccount(next, true);
      const labelText = STATE_LABELS[next.state] || "待读取";
      setFeedback({
        text:
          next.state === "logged_in"
            ? "账号资料已核验并更新。"
            : next.error || `核验结果：${labelText}。`,
        error: next.state !== "logged_in",
      });
    });
  }, [acceptAccount, runAction, safeErrorMessage]);

  // Initial load + automatic verify if unverified + 30s background poll
  useEffect(() => {
    mountedRef.current = true;
    void (async () => {
      await loadAccount();
      const loaded = valueRef.current;
      if (
        mountedRef.current &&
        loaded?.configured?.sessdata &&
        loaded.state === "unverified" &&
        Date.now() - lastRefreshAttemptRef.current > 30_000
      ) {
        await refreshProfile();
      }
    })();

    const interval = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void loadAccount();
    }, 30_000);

    return () => {
      mountedRef.current = false;
      clearInterval(interval);
      closeScanLogin(false);
    };
  }, [loadAccount, refreshProfile, closeScanLogin]);

  const profile = value?.profile ?? null;
  const avatarKey = profile?.avatar_url ? `${profile.uid ?? ""}:${profile.avatar_url}` : null;
  const avatarLoaded = Boolean(avatarKey && loadedAvatarKey === avatarKey);

  // Sync token reset dialog native modal
  useEffect(() => {
    const dialog = resetDialogRef.current;
    if (!dialog) return;
    if (resetDialogOpen) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [resetDialogOpen]);

  const pollScanLogin = useCallback(
    async (session: { id: string; expiresAt: number; timer: ReturnType<typeof setTimeout> | null }) => {
      if (!mountedRef.current || loginSessionRef.current !== session) return;
      if (Date.now() >= session.expiresAt) {
        closeScanLogin(false);
        setFeedback({ text: "二维码已过期，请重新扫码。", error: true });
        return;
      }
      try {
        const result = await apiPost<
          "/api/v1/operations/account/bilibili/login/{identity}/poll",
          BilibiliLoginPollResponse
        >("/api/v1/operations/account/bilibili/login/{identity}/poll", {
          pathParams: { identity: session.id },
        });
        if (!mountedRef.current || loginSessionRef.current !== session) return;
        if (result.state === "complete" && result.account) {
          closeScanLogin(false);
          acceptAccount(result.account, true);
          setFeedback({
            text:
              result.account.state === "logged_in"
                ? "B站登录凭证已保存，已启用自动续期。"
                : "B站登录凭证已保存，账号资料将在后台复核。",
            error: false,
          });
          return;
        }
        if (result.state === "expired") {
          closeScanLogin(false);
          setFeedback({ text: "二维码已过期，请重新扫码。", error: true });
          return;
        }
        if (result.state === "scanned") {
          setQrStatus("已扫码，请在手机上确认。");
        }
      } catch {
        if (mountedRef.current && loginSessionRef.current === session) {
          setQrStatus("暂时无法核对扫码，稍后自动重试。");
        }
      }
      if (mountedRef.current && loginSessionRef.current === session) {
        session.timer = setTimeout(() => {
          void pollScanLogin(session);
        }, 3000);
      }
    },
    [acceptAccount, closeScanLogin]
  );

  async function startScanLogin() {
    closeScanLogin(true);
    await runAction(async () => {
      const result = await apiPost<
        "/api/v1/operations/account/bilibili/login",
        BilibiliLoginSessionResponse
      >("/api/v1/operations/account/bilibili/login");
      if (!mountedRef.current) return;
      const session = {
        id: result.id,
        expiresAt: Date.now() + result.expires_in * 1000,
        timer: null as ReturnType<typeof setTimeout> | null,
      };
      loginSessionRef.current = session;
      setQrImageSrc(
        studioPath(`/api/v1/operations/account/bilibili/login/${encodeURIComponent(result.id)}/image`)
      );
      setQrVisible(true);
      setQrStatus("用 B站手机应用扫码并确认登录。");
      session.timer = setTimeout(() => {
        void pollScanLogin(session);
      }, 3000);
    });
  }

  async function handleSaveCredentials(event: FormEvent) {
    event.preventDefault();
    const sessdata = sessdataInput.trim();
    const csrf = csrfInput.trim();
    const refreshToken = refreshTokenInput.trim();
    if (!sessdata && !csrf && !refreshToken) return;
    sensitiveValuesRef.current = [sessdata, csrf, refreshToken];

    await runAction(async () => {
      const next = await apiPut<
        "/api/v1/operations/account/bilibili",
        BilibiliAccountStatus,
        { sessdata?: string; csrf?: string; refresh_token?: string }
      >("/api/v1/operations/account/bilibili", {
        body: {
          ...(sessdata ? { sessdata } : {}),
          ...(csrf ? { csrf } : {}),
          ...(refreshToken ? { refresh_token: refreshToken } : {}),
        },
      });
      if (!mountedRef.current) return;
      setSessdataInput("");
      setCsrfInput("");
      setRefreshTokenInput("");
      acceptAccount(next, true);
      setFeedback({ text: "账号凭证已保存，正在验证登录…", error: false });
      lastRefreshAttemptRef.current = Date.now();
      try {
        const verified = await apiPost<"/api/v1/operations/account/bilibili/refresh", BilibiliAccountStatus>(
          "/api/v1/operations/account/bilibili/refresh"
        );
        if (!mountedRef.current) return;
        acceptAccount(verified, true);
        const verifiedLabel = STATE_LABELS[verified.state] || "待读取";
        setFeedback({
          text:
            verified.state === "logged_in"
              ? "账号凭证已保存，登录与资料已核验。"
              : `凭证已保存。${verified.error || `核验结果：${verifiedLabel}`}`,
          error: verified.state !== "logged_in",
        });
      } catch (error) {
        if (!mountedRef.current) return;
        setFeedback({
          text: `凭证已保存，暂未完成核验：${safeErrorMessage(error)}`,
          error: true,
        });
      }
    });
  }

  async function handleClearCredentials() {
    if (
      !window.confirm("清除已保存的 B站 SESSDATA 与 CSRF 凭证？需要重新配置后才能使用此账号。")
    ) {
      return;
    }
    await runAction(async () => {
      const next = await apiPut<
        "/api/v1/operations/account/bilibili",
        BilibiliAccountStatus,
        { clear: boolean }
      >("/api/v1/operations/account/bilibili", {
        body: { clear: true },
      });
      if (!mountedRef.current) return;
      acceptAccount(next, true);
      closeScanLogin(false);
      setSessdataInput("");
      setCsrfInput("");
      setRefreshTokenInput("");
      setFeedback({ text: "已清除 B站账号凭证。", error: false });
    });
  }

  function openTokenResetDialog() {
    if (resetPending) return;
    setTokenPresented(false);
    setNewToken("");
    setResetFeedback("");
    setResetDialogOpen(true);
    setTimeout(() => resetCancelRef.current?.focus(), 0);
  }

  async function handleResetToken() {
    if (resetPending) return;
    setResetPending(true);
    setResetFeedback("正在重置管理令牌…");
    try {
      const result = await apiPost<"/api/v1/system/auth/reset-token", { token?: string }>(
        "/api/v1/system/auth/reset-token"
      );
      if (typeof result.token !== "string" || !result.token) {
        throw new Error("重置响应未提供新令牌，请查看服务的管理令牌恢复方式。");
      }
      setNewToken(result.token);
      setTokenPresented(true);
      setResetFeedback("尚未复制，请先保存新令牌。");
      setTimeout(() => tokenCopyRef.current?.focus(), 0);
    } catch (error) {
      setResetFeedback(safeErrorMessage(error));
    } finally {
      setResetPending(false);
    }
  }

  async function handleCopyToken() {
    if (!newToken) return;
    try {
      await navigator.clipboard.writeText(newToken);
      setResetFeedback("已复制新令牌。保存后可返回登录。");
    } catch {
      const field = document.querySelector<HTMLTextAreaElement>("[data-new-token]");
      field?.focus();
      field?.select();
      setResetFeedback("自动复制不可用，已选中新令牌，可手动复制。");
    }
  }

  const stateLabel = requestError
    ? "状态读取失败"
    : loading && !value
      ? "读取中"
      : (value?.state && STATE_LABELS[value.state]) || "待读取";

  const stateAttr = requestError ? "error" : value?.state || "unknown";
  const displayName =
    typeof profile?.name === "string" && profile.name
      ? profile.name
      : profile
        ? "用户名未提供"
        : "B站账号";

  const checkedDate = typeof value?.checked_at === "number" ? new Date(value.checked_at * 1000) : null;
  const checkedText =
    checkedDate && !Number.isNaN(checkedDate.getTime())
      ? checkedDate.toLocaleString("zh-CN", { hour12: false })
      : "";
  const showProfileNote = Boolean(profile) && !(value?.state === "logged_in" && !requestError);
  const inlineError = requestError || value?.error || "";

  const busy = pending || loading;
  const configured = Boolean(value?.configured?.sessdata);
  const canClear = Boolean(value?.configured?.sessdata || value?.configured?.csrf);
  const hasCredentialInputs = Boolean(
    sessdataInput.trim() || csrfInput.trim() || refreshTokenInput.trim()
  );

  const refreshBtnLabel = loading
    ? "读取中…"
    : pending
      ? "处理中…"
      : requestError
        ? "重试读取"
        : value?.state === "logged_in"
          ? "刷新账号"
          : "验证登录";

  const avatarSrc = avatarKey
    ? studioPath(`/api/v1/operations/account/bilibili/avatar?v=${encodeURIComponent(avatarKey)}`)
    : undefined;

  const warningsText = Array.isArray(value?.warnings)
    ? value.warnings.filter((item) => typeof item === "string").join("；")
    : "";

  return (
    <>
      <div id="global-bilibili-account" className={`global-bilibili-account ${styles.globalAccount}`}>
        <div className={`bilibili-account ${styles.accountCard}`} aria-label="B站账号">
          <span className={`account-avatar ${styles.avatar}`}>
            <span data-avatar-placeholder="" aria-hidden="true" hidden={avatarLoaded}>
              {profile?.name ? Array.from(profile.name)[0] : "B站"}
            </span>
            <img
              data-avatar=""
              alt=""
              referrerPolicy="no-referrer"
              src={avatarSrc}
              hidden={!avatarKey || !avatarLoaded}
              onLoad={() => {
                if (avatarKey) setLoadedAvatarKey(avatarKey);
              }}
              onError={() => setLoadedAvatarKey(null)}
            />
          </span>

          <div className={styles.identity}>
            <div className={styles.nameLine}>
              <strong data-name="">{displayName}</strong>
              <span data-status="" className="account-state" data-state={stateAttr} role="status">
                {stateLabel}
              </span>
              <small data-profile-note="" hidden={!showProfileNote}>
                {checkedText ? `上次资料 ${checkedText}` : "上次核验资料"}
              </small>
            </div>
            <div className={styles.profileMeta}>
              <span data-uid="">{`UID ${profile?.uid ?? "—"}`}</span>
              <span data-coins="">{`硬币 ${formatNumber(profile?.coins)}`}</span>
              <span data-fans="">{`粉丝 ${formatNumber(profile?.fans)}`}</span>
            </div>
            <span
              data-header-error=""
              className={styles.inlineError}
              hidden={!inlineError}
            >
              {inlineError}
            </span>
          </div>

          <div className={styles.headerActions}>
            <button
              type="button"
              data-refresh=""
              disabled={busy || (!configured && !requestError)}
              aria-busy={busy}
              onClick={() => void (requestError ? loadAccount() : refreshProfile())}
            >
              {refreshBtnLabel}
            </button>
            <a href={showSettings ? "#account-settings" : "#accounts"}>账号设置</a>
          </div>
        </div>
      </div>

      {showSettings && (
        <div id="account-settings" data-workspace-anchor="" className={styles.settingsAnchor}>
          <article
            className={`panel account-settings-panel ${styles.settingsPanelActions}`}
            aria-labelledby="bilibili-account-heading"
          >
            <div className="panel-heading">
              <h2 id="bilibili-account-heading">B站账号</h2>
              <span
                data-settings-status=""
                className="account-state"
                data-state={stateAttr}
                role="status"
              >
                {stateLabel}
              </span>
            </div>
            <p className="hint">
              评论、私信、动态和自主浏览共用此账号。保存凭证后验证登录，资料以平台实际返回为准。
            </p>
            <p className="quoted-text" data-profile-bio="">
              {typeof profile?.bio === "string"
                ? `主页简介：${profile.bio || "未填写"}`
                : "主页简介暂未取得"}
            </p>
            <dl className={styles.details}>
              <dt>SESSDATA</dt>
              <dd data-sessdata-state="">
                {value ? (value.configured?.sessdata ? "已保存（不回显）" : "未保存") : "待读取"}
              </dd>
              <dt>CSRF / bili_jct</dt>
              <dd data-csrf-state="">
                {value ? (value.configured?.csrf ? "已保存（不回显）" : "未保存") : "待读取"}
              </dd>
              <dt>自动续期</dt>
              <dd data-renewal="">
                {(value?.renewal?.state && RENEWAL_LABELS[value.renewal.state]) || "待读取"}
              </dd>
              <dt>最近核验</dt>
              <dd data-checked="">
                {checkedText
                  ? `${checkedText}${value?.verification_stale ? " · 等待复核" : ""}`
                  : "尚未核验"}
              </dd>
            </dl>

            <div className={`actions ${styles.settingsPanelActions}`}>
              <button
                type="button"
                className="primary"
                data-scan-login=""
                disabled={pending}
                onClick={() => void startScanLogin()}
              >
                扫码登录 B站
              </button>
            </div>

            <div className={styles.qrBox} data-login-qr="" hidden={!qrVisible}>
              <img
                data-qr-image=""
                alt="B站登录二维码"
                width={184}
                height={184}
                src={qrImageSrc ?? undefined}
              />
              <p data-qr-status="" role="status">
                {qrStatus}
              </p>
              <button type="button" data-qr-cancel="" onClick={() => closeScanLogin(true)}>
                关闭二维码
              </button>
            </div>

            <form
              data-credentials-form=""
              autoComplete="off"
              onSubmit={(event) => void handleSaveCredentials(event)}
            >
              <fieldset data-credentials-fields="" className={styles.fields} disabled={pending}>
                <div className="form-grid">
                  <label>
                    SESSDATA
                    <input
                      data-sessdata=""
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      placeholder="留空保留已保存值"
                      aria-describedby="account-credential-help"
                      value={sessdataInput}
                      onChange={(event) => setSessdataInput(event.target.value)}
                    />
                  </label>
                  <label>
                    CSRF / bili_jct
                    <input
                      data-csrf=""
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      placeholder="留空保留已保存值"
                      aria-describedby="account-credential-help"
                      value={csrfInput}
                      onChange={(event) => setCsrfInput(event.target.value)}
                    />
                  </label>
                  <label>
                    续期令牌 / ac_time_value
                    <input
                      data-refresh-token=""
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      placeholder="可选，使用同一次登录的令牌"
                      aria-describedby="account-credential-help"
                      value={refreshTokenInput}
                      onChange={(event) => setRefreshTokenInput(event.target.value)}
                    />
                  </label>
                </div>
                <p className="hint" id="account-credential-help">
                  扫码会一并保存自动续期所需的凭证。手动填写时请使用同一次登录的值。凭证只写入，不回显。
                </p>
                <div className={`actions ${styles.settingsPanelActions}`}>
                  <button
                    data-save=""
                    type="submit"
                    className="primary"
                    disabled={pending || !hasCredentialInputs}
                  >
                    {pending ? "处理中…" : "保存账号凭证"}
                  </button>
                  <button
                    data-verify=""
                    type="button"
                    disabled={busy || !configured}
                    onClick={() => void refreshProfile()}
                  >
                    验证登录 / 刷新资料
                  </button>
                  <button
                    data-clear=""
                    type="button"
                    className="danger"
                    disabled={pending || !canClear}
                    onClick={() => void handleClearCredentials()}
                  >
                    清除已保存凭证
                  </button>
                </div>
              </fieldset>
            </form>

            <p
              data-account-warnings=""
              className={`hint ${styles.feedback}`}
              role="status"
              hidden={!warningsText}
            >
              {warningsText}
            </p>
            <p
              data-account-feedback=""
              className={`hint ${styles.feedback}${feedback.error ? " error-text" : ""}`}
              role="status"
            >
              {feedback.text}
            </p>
          </article>

          <article
            className={`panel ${styles.remoteManagement}`}
            data-remote-management=""
            hidden={localAccess}
            aria-labelledby="management-login-heading"
          >
            <div className="panel-heading">
              <h2 id="management-login-heading">工作台远程登录</h2>
            </div>
            <p className="hint">
              同一浏览器可保持登录。退出、清除浏览器数据或重置管理令牌后，需要重新登录。
            </p>
            <p className="hint">
              重置令牌会立即退出所有远程管理登录，新令牌仅在重置成功后显示一次。
            </p>
            <button
              data-reset-open=""
              type="button"
              className="danger"
              disabled={resetPending}
              onClick={openTokenResetDialog}
            >
              重置管理令牌…
            </button>
          </article>

          <dialog
            ref={resetDialogRef}
            className={`account-dialog ${styles.dialog}`}
            aria-labelledby="management-reset-heading"
            onCancel={(event) => {
              if (resetPending || tokenPresented) {
                event.preventDefault();
                return;
              }
              setResetDialogOpen(false);
            }}
          >
            <h2 id="management-reset-heading">重置管理令牌</h2>
            <div data-reset-confirm="" hidden={tokenPresented}>
              <p>所有工作台管理登录都会立即退出，包括当前浏览器。此操作不会更换 B站账号凭证。</p>
              <p className="hint">新令牌只在本次响应中显示。请保存后再返回登录页。</p>
              <div className="actions">
                <button
                  ref={resetCancelRef}
                  data-reset-cancel=""
                  type="button"
                  disabled={resetPending}
                  onClick={() => setResetDialogOpen(false)}
                >
                  取消
                </button>
                <button
                  data-reset-confirm-button=""
                  type="button"
                  className="danger"
                  disabled={resetPending}
                  onClick={() => void handleResetToken()}
                >
                  重置并退出所有管理登录
                </button>
              </div>
            </div>

            <div data-reset-result="" hidden={!tokenPresented}>
              <p>管理令牌已重置，所有管理登录已退出。请先保存新令牌。</p>
              <label>
                新的管理令牌
                <textarea
                  data-new-token=""
                  rows={3}
                  readOnly
                  autoComplete="off"
                  spellCheck={false}
                  aria-describedby="management-token-help"
                  value={newToken}
                />
              </label>
              <p className="hint" id="management-token-help">
                关闭此窗口后不再回显。不要把管理令牌发到评论区或私信中。
              </p>
              <div className="actions">
                <button
                  ref={tokenCopyRef}
                  data-token-copy=""
                  type="button"
                  className="primary"
                  onClick={() => void handleCopyToken()}
                >
                  复制新管理令牌
                </button>
                <button
                  data-token-done=""
                  type="button"
                  onClick={() => {
                    setNewToken("");
                    setTokenPresented(false);
                    setResetDialogOpen(false);
                    onUnauthorized();
                    setTimeout(() => document.getElementById("token")?.focus(), 0);
                  }}
                >
                  已保存，返回登录
                </button>
              </div>
            </div>

            <p data-reset-feedback="" className={styles.feedback} role="status">
              {resetFeedback}
            </p>
          </dialog>
        </div>
      )}
    </>
  );
}
