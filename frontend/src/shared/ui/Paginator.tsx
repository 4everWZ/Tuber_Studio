import { useMemo, useState } from "react";

export interface PaginatorProps {
  currentPage: number;
  totalItems: number;
  pageSize?: number;
  onPageChange: (page: number) => void;
  className?: string;
}

export function getTotalPages(totalItems: number, pageSize = 8): number {
  return Math.max(1, Math.ceil(totalItems / Math.max(1, pageSize)));
}

export function usePaginator<T>(items: readonly T[], pageSize = 8) {
  const [rawPage, setRawPage] = useState(1);
  const totalItems = items.length;
  const totalPages = getTotalPages(totalItems, pageSize);
  const currentPage = Math.min(Math.max(1, rawPage), totalPages);

  const pageItems = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return items.slice(start, start + pageSize);
  }, [currentPage, items, pageSize]);

  const goToPage = (page: number) => {
    const target = Math.max(1, Math.min(totalPages, page));
    setRawPage(target);
  };

  return {
    currentPage,
    totalItems,
    totalPages,
    pageItems,
    goToPage,
    reset: () => setRawPage(1),
  };
}

export function Paginator({
  currentPage,
  totalItems,
  pageSize = 8,
  onPageChange,
  className,
}: PaginatorProps) {
  if (totalItems <= 0) {
    return null;
  }
  const totalPages = getTotalPages(totalItems, pageSize);
  const safePage = Math.min(Math.max(1, currentPage), totalPages);

  return (
    <div className={["tuber-pagination-bar", className].filter(Boolean).join(" ")}>
      <span className="tuber-pagination-info">
        第 {safePage} / {totalPages} 页 · 共 {totalItems} 条
      </span>
      <div className="tuber-pagination-buttons">
        <button
          type="button"
          className="tuber-page-btn"
          disabled={safePage <= 1}
          onClick={() => {
            if (safePage > 1) onPageChange(safePage - 1);
          }}
        >
          ‹ 上一页
        </button>
        <button
          type="button"
          className="tuber-page-btn"
          disabled={safePage >= totalPages}
          onClick={() => {
            if (safePage < totalPages) onPageChange(safePage + 1);
          }}
        >
          下一页 ›
        </button>
      </div>
    </div>
  );
}
