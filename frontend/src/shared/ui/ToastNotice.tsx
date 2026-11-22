import { useEffect, useState } from "react";
import { safeMessage, subscribeNotices } from "../lib/errors";

export interface ToastNoticeProps {
  id?: string;
  message?: string;
  error?: boolean;
  onDismiss?: () => void;
}

export function ToastNotice({ id = "notice", message, error = false, onDismiss }: ToastNoticeProps) {
  const [busNotice, setBusNotice] = useState<{ message: string; error: boolean } | null>(null);

  useEffect(() => {
    if (message !== undefined) return undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const unsubscribe = subscribeNotices((payload) => {
      if (!payload.message) {
        setBusNotice(null);
        return;
      }
      const isErr = Boolean(payload.error);
      setBusNotice({ message: safeMessage(payload.message), error: isErr });
      if (timer !== undefined) clearTimeout(timer);
      timer = setTimeout(() => {
        setBusNotice(null);
      }, isErr ? 10000 : 4000);
    });
    return () => {
      if (timer !== undefined) clearTimeout(timer);
      unsubscribe();
    };
  }, [message]);

  const activeMessage = message !== undefined ? safeMessage(message) : busNotice?.message ?? "";
  const isError = message !== undefined ? error : Boolean(busNotice?.error);

  return (
    <div
      id={id}
      className={`notice win-notice${isError ? " error" : ""}`}
      role="status"
      hidden={!activeMessage}
      onClick={() => {
        if (onDismiss) onDismiss();
        else setBusNotice(null);
      }}
    >
      {activeMessage}
    </div>
  );
}
