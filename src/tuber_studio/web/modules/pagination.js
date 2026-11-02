/**
 * Reusable Pagination Controller for Tuber Studio Web
 */

(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.TuberPaginator = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {

  class Paginator {
    constructor(options = {}) {
      this.pageSize = options.pageSize || 8;
      this.currentPage = 1;
      this.items = [];
      this.onPageChange = options.onPageChange || (() => {});
    }

    setItems(items, resetPage = false) {
      this.items = Array.isArray(items) ? items : [];
      const totalPages = this.getTotalPages();
      if (resetPage || this.currentPage > totalPages) {
        this.currentPage = 1;
      }
    }

    getTotalItems() {
      return this.items.length;
    }

    getTotalPages() {
      return Math.max(1, Math.ceil(this.items.length / this.pageSize));
    }

    getPageItems() {
      const start = (this.currentPage - 1) * this.pageSize;
      return this.items.slice(start, start + this.pageSize);
    }

    goToPage(page) {
      const target = Math.max(1, Math.min(this.getTotalPages(), page));
      if (target !== this.currentPage) {
        this.currentPage = target;
        this.onPageChange(this.currentPage, this.getPageItems());
      }
    }

    prev() {
      if (this.currentPage > 1) {
        this.goToPage(this.currentPage - 1);
      }
    }

    next() {
      if (this.currentPage < this.getTotalPages()) {
        this.goToPage(this.currentPage + 1);
      }
    }

    renderControls(container) {
      if (!container) return;
      container.replaceChildren();

      const totalItems = this.getTotalItems();
      const totalPages = this.getTotalPages();

      if (totalItems === 0) {
        container.style.display = "none";
        return;
      }
      container.style.display = "flex";

      const nav = document.createElement("div");
      nav.className = "tuber-pagination-bar";

      const info = document.createElement("span");
      info.className = "tuber-pagination-info";
      info.textContent = `第 ${this.currentPage} / ${totalPages} 页 · 共 ${totalItems} 条`;

      const btnGroup = document.createElement("div");
      btnGroup.className = "tuber-pagination-buttons";

      const prevBtn = document.createElement("button");
      prevBtn.type = "button";
      prevBtn.className = "tuber-page-btn";
      prevBtn.textContent = "‹ 上一页";
      prevBtn.disabled = this.currentPage <= 1;
      prevBtn.onclick = (e) => {
        e.preventDefault();
        this.prev();
      };

      const nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "tuber-page-btn";
      nextBtn.textContent = "下一页 ›";
      nextBtn.disabled = this.currentPage >= totalPages;
      nextBtn.onclick = (e) => {
        e.preventDefault();
        this.next();
      };

      btnGroup.append(prevBtn, nextBtn);
      nav.append(info, btnGroup);
      container.append(nav);
    }
  }

  return Paginator;
});
