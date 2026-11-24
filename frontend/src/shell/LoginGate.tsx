import { useState, type FormEvent } from "react";
import { apiPost } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import styles from "@/shared/styles/workbench.module.css";
import { AquaBrandMark } from "./Sidebar";

export interface LoginGateProps {
  initialError?: string;
  onAuthenticated: () => Promise<void>;
}

export function LoginGate({ initialError = "", onAuthenticated }: LoginGateProps) {
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(initialError);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      await apiPost("/api/v1/system/auth/login", { body: { token: trimmed } });
      setToken("");
      if (typeof localStorage !== "undefined") {
        localStorage.removeItem("tuber_token");
      }
      await onAuthenticated();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main id="login" className={styles.loginShell}>
      <form id="login-form" className={styles.loginPanel} onSubmit={(e) => void handleSubmit(e)}>
        <AquaBrandMark />
        <h1>Aqua Studio</h1>
        <p className="muted">Aqua · Personal Agent</p>
        <label>
          管理令牌
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            placeholder="请输入启动令牌..."
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
            disabled={submitting}
          />
        </label>
        <p className="hint">使用 Aqua Studio 的管理令牌登录，这个浏览器会保留登录状态。</p>
        <button className="primary" type="submit" disabled={submitting} aria-busy={submitting}>
          登录工作台
        </button>
        <p id="login-error" role="alert" className="error-text">
          {error}
        </p>
      </form>
    </main>
  );
}
