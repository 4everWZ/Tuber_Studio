import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPut } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";

export interface UserMemoryTarget {
  platform: "bilibili";
  userId: string;
  accountId: string;
  characterId: string;
  characterName?: string;
  name?: string | null;
  onChange?: () => Promise<void> | void;
}

export interface UserMemoryProfile {
  personalization_enabled?: boolean;
  nickname?: string;
  preferences?: string[];
  important_events?: string[];
  affinity?: number;
  stage?: string;
  last_interaction_at?: number | null;
}

export interface UserMemoryModalProps {
  target: UserMemoryTarget | null;
  onClose: () => void;
}

export function UserMemoryModal({ target, onClose }: UserMemoryModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const requestSeqRef = useRef(0);

  const [personalizationEnabled, setPersonalizationEnabled] = useState(false);
  const [nickname, setNickname] = useState("");
  const [preferences, setPreferences] = useState("");
  const [importantEvents, setImportantEvents] = useState("");
  const [summary, setSummary] = useState("");
  const [feedback, setFeedback] = useState("");
  const [disabled, setDisabled] = useState(false);

  const runRequest = useCallback(
    async (
      method: "GET" | "PUT" | "DELETE",
      activeTarget: UserMemoryTarget,
      body?: {
        personalization_enabled: boolean;
        nickname: string;
        preferences: string[];
        important_events: string[];
      }
    ) => {
      const seq = ++requestSeqRef.current;
      setDisabled(true);
      setFeedback(method === "GET" ? "正在读取档案…" : "正在更新档案…");
      const query = {
        account_id: activeTarget.accountId,
        character_id: activeTarget.characterId,
      };
      const pathParams = {
        platform: activeTarget.platform,
        user_id: activeTarget.userId,
      };

      try {
        const profile =
          method === "GET"
            ? await apiGet<
                "/api/v1/persona/user-memory/{platform}/{user_id}",
                UserMemoryProfile
              >("/api/v1/persona/user-memory/{platform}/{user_id}", { pathParams, query })
            : method === "PUT"
              ? await apiPut<
                  "/api/v1/persona/user-memory/{platform}/{user_id}",
                  UserMemoryProfile,
                  typeof body
                >("/api/v1/persona/user-memory/{platform}/{user_id}", {
                  pathParams,
                  query,
                  body,
                })
              : await apiDelete<
                  "/api/v1/persona/user-memory/{platform}/{user_id}",
                  UserMemoryProfile,
                  never
                >("/api/v1/persona/user-memory/{platform}/{user_id}", { pathParams, query });

        if (seq !== requestSeqRef.current) return;
        setPersonalizationEnabled(Boolean(profile.personalization_enabled));
        setNickname(profile.nickname || "");
        setPreferences((profile.preferences || []).join("\n"));
        setImportantEvents((profile.important_events || []).join("\n"));
        const last = profile.last_interaction_at
          ? new Date(profile.last_interaction_at * 1000).toLocaleString("zh-CN")
          : "尚无记录";
        setSummary(
          `好感度 ${profile.affinity ?? 0} / 100 · ${profile.stage || "—"} · 最近互动 ${last}`
        );
        setFeedback({ GET: "", PUT: "档案已保存。", DELETE: "档案已清空。" }[method]);
        setDisabled(false);

        if (method !== "GET") {
          void activeTarget.onChange?.();
        }
      } catch (err) {
        if (seq === requestSeqRef.current) {
          setFeedback(errorMessage(err));
          setDisabled(method === "GET");
        }
      }
    },
    []
  );

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (target) {
      setPersonalizationEnabled(false);
      setNickname("");
      setPreferences("");
      setImportantEvents("");
      setSummary("");
      setFeedback("");
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      }
      void runRequest("GET", target);
    } else {
      requestSeqRef.current += 1;
      if (dialog.open) {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.removeAttribute("open");
      }
    }
  }, [target, runRequest]);

  const displayName = target?.name || (target ? `UID ${target.userId}` : "");
  const identityText = target
    ? [target.name && `UID ${target.userId}`, target.characterName || target.characterId]
        .filter(Boolean)
        .join(" · ")
    : "";

  function splitLines(text: string): string[] {
    return text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!target || disabled) return;
    void runRequest("PUT", target, {
      personalization_enabled: personalizationEnabled,
      nickname,
      preferences: splitLines(preferences),
      important_events: splitLines(importantEvents),
    });
  }

  function handleClear() {
    if (!target || disabled) return;
    if (
      typeof window !== "undefined" &&
      typeof window.confirm === "function" &&
      !window.confirm(`清空${displayName}的称呼、偏好、重要互动与好感度？`)
    ) {
      return;
    }
    void runRequest("DELETE", target);
  }

  return (
    <dialog
      ref={dialogRef}
      className="account-dialog"
      aria-labelledby="user-memory-heading"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id="user-memory-heading">
        {target ? `${displayName}的档案` : "联系档案"}
      </h2>
      <p data-identity="" className="hint">
        {identityText}
      </p>
      <form onSubmit={handleSubmit}>
        <fieldset disabled={disabled}>
          <label className="check-field">
            <input
              name="personalization_enabled"
              type="checkbox"
              checked={personalizationEnabled}
              onChange={(e) => setPersonalizationEnabled(e.target.checked)}
            />{" "}
            在评论和私信中使用个性化
          </label>
          <label>
            已确认的称呼
            <input
              name="nickname"
              maxLength={80}
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
            />
          </label>
          <label>
            已确认的偏好（每行一项）
            <textarea
              name="preferences"
              rows={3}
              value={preferences}
              onChange={(e) => setPreferences(e.target.value)}
            />
          </label>
          <label>
            重要互动（每行一项）
            <textarea
              name="important_events"
              rows={3}
              value={importantEvents}
              onChange={(e) => setImportantEvents(e.target.value)}
            />
          </label>
          <p data-summary="" className="hint">
            {summary}
          </p>
          <div className="actions">
            <button type="submit" className="primary">
              确认并保存
            </button>
            <button
              data-clear=""
              type="button"
              className="danger"
              onClick={handleClear}
            >
              清空档案…
            </button>
          </div>
        </fieldset>
      </form>
      <p data-feedback="" className="hint" role="status">
        {feedback}
      </p>
      <button data-close="" type="button" onClick={onClose}>
        关闭
      </button>
    </dialog>
  );
}
