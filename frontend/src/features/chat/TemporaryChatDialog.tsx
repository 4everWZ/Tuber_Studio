import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiPost } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./ChatPage.module.css";
import { createRequestId } from "./types";

interface TempMessage {
  role: "user" | "assistant";
  content: string;
}

export interface TemporaryChatDialogProps {
  open: boolean;
  onClose: () => void;
}

export function TemporaryChatDialog({ open, onClose }: TemporaryChatDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const [messages, setMessages] = useState<TempMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState("");

  const resetState = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setInputValue("");
    setPending(false);
    setStatus("");
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      inputRef.current?.focus();
    } else {
      if (dialog.open) {
        if (typeof dialog.close === "function") {
          dialog.close();
        } else {
          dialog.removeAttribute("open");
        }
      }
      resetState();
    }
  }, [open, resetState]);

  useEffect(() => {
    if (!open || typeof window === "undefined") return;
    const leave = () => onClose();
    window.addEventListener("pagehide", leave);
    window.addEventListener("hashchange", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      window.removeEventListener("hashchange", leave);
    };
  }, [open, onClose]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const message = inputValue.trim();
    if (!message || pending) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setPending(true);
    setStatus("正在回复…");

    try {
      const result = await apiPost<
        "/api/v1/agent/chat",
        { reply_text?: string },
        {
          request_id: string;
          message: string;
          temporary: true;
          window: TempMessage[];
        }
      >("/api/v1/agent/chat", {
        signal: controller.signal,
        body: {
          request_id: createRequestId(),
          message,
          temporary: true,
          window: messages,
        },
      });
      if (abortRef.current !== controller || !open) return;
      const reply = result.reply_text || "";
      const turn: TempMessage[] = [
        { role: "user", content: message },
        { role: "assistant", content: reply },
      ];
      setMessages((prev) =>
        [
          ...prev,
          ...turn.map((item) => ({ ...item, content: item.content.slice(0, 8000) })),
        ].slice(-8)
      );
      setInputValue((prev) => (prev.trim() === message ? "" : prev));
      setStatus("");
      inputRef.current?.focus();
    } catch (error) {
      if (!controller.signal.aborted) {
        setStatus(errorMessage(error));
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setPending(false);
      }
    }
  }

  return (
    <dialog
      ref={dialogRef}
      id="temporary-chat"
      className={`personalization-dialog ${styles.tempDialog}`}
      aria-labelledby="temporary-chat-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={onClose}
    >
      <header className={styles.tempDialogHeader}>
        <h2 id="temporary-chat-title">临时对话</h2>
        <button type="button" aria-label="结束临时对话" onClick={onClose}>
          关闭
        </button>
      </header>
      <div className={`personalization-dialog-body ${styles.tempDialogBody}`}>
        <p>仅使用本窗口最近的对话。关闭后清除，不读取或保存长期记忆。</p>
        <div data-turns="" role="log" className={styles.tempTurns}>
          {messages.map((item, idx) => (
            <p key={`${item.role}-${idx}`}>{item.content}</p>
          ))}
        </div>
        <form data-send="" onSubmit={(event) => void handleSubmit(event)}>
          <label>
            临时消息
            <textarea
              ref={inputRef}
              required
              maxLength={8000}
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
            />
          </label>
          <button type="submit" disabled={pending}>
            发送
          </button>
        </form>
        <p data-status="" role="status">
          {status}
        </p>
      </div>
    </dialog>
  );
}
