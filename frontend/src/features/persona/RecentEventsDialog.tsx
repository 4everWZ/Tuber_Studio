import { useEffect, useRef } from "react";
import styles from "./Personalization.module.css";
import type { RecentEventItem } from "./types";

const ALLOWED_SOURCE_HREF = /^https:\/\/(?:www\.bilibili\.com\/video\/BV[0-9A-Za-z]+|t\.bilibili\.com\/\d+)(?:[/?#]|$)/;

export function safeBilibiliSourceHref(href: string | null | undefined): string | null {
  if (!href || !ALLOWED_SOURCE_HREF.test(href)) return null;
  return href;
}

export function clipSummaryText(text: string, limit: number): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > limit ? `${line.slice(0, limit)}…` : line;
}

export interface RecentEventsDialogProps {
  open: boolean;
  loading: boolean;
  error: string;
  items: RecentEventItem[];
  onClose: () => void;
  onRetry: () => void;
}

export function RecentEventsDialog({
  open,
  loading,
  error,
  items,
  onClose,
  onRetry,
}: RecentEventsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);

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
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [open]);

  const showLoadState = loading || Boolean(error);

  return (
    <dialog
      ref={dialogRef}
      id="recent-events-dialog"
      className={`personalization-dialog ${styles.dialog}`}
      aria-labelledby="recent-events-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
    >
      <header className={styles.dialogHeader}>
        <h2 id="recent-events-title" className={styles.dialogTitle}>
          近期趣事
        </h2>
        <button
          type="button"
          className={`personalization-close ${styles.closeButton}`}
          aria-label="关闭近期趣事"
          autoFocus
          onClick={onClose}
        >
          ×
        </button>
      </header>
      <div className={`personalization-dialog-body ${styles.dialogBody}`}>
        <div
          id="recent-load-state"
          className={`personalization-load ${styles.loadState}`}
          role="status"
          hidden={!showLoadState}
        >
          {loading ? (
            <p>正在读取近期互动…</p>
          ) : error ? (
            <>
              <p>{error}</p>
              <button
                type="button"
                className={`personalization-button ${styles.pillButton}`}
                onClick={onRetry}
              >
                重试
              </button>
            </>
          ) : null}
        </div>
        <div id="recent-memory-list" className={`memory-detail-list ${styles.detailList}`} hidden={showLoadState}>
          {items.length === 0 ? (
            <p className="hint">近期暂无有来源的运营事件。</p>
          ) : (
            items.map((event, index) => {
              const title = /^BV[0-9A-Za-z]+$/.test(event.title) ? "视频" : event.title;
              const srcHref = safeBilibiliSourceHref(event.source.href);
              const replyHref = safeBilibiliSourceHref(event.reply?.href);
              return (
                <article key={`${title}-${index}`}>
                  <p>
                    <strong>{clipSummaryText(title, 55)}</strong>
                    {` · ${clipSummaryText(event.source.text, 125)}`}
                  </p>
                  {(srcHref || replyHref) && (
                    <p>
                      {srcHref && (
                        <a href={srcHref} target="_blank" rel="noopener noreferrer">
                          {event.source.kind === "comment" ? "原评论" : "原视频"}
                        </a>
                      )}
                      {srcHref && replyHref && " "}
                      {replyHref && (
                        <a href={replyHref} target="_blank" rel="noopener noreferrer">
                          回复
                        </a>
                      )}
                    </p>
                  )}
                </article>
              );
            })
          )}
        </div>
      </div>
    </dialog>
  );
}
