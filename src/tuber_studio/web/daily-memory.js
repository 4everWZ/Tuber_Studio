"use strict";

class DailyMemoryUI {
  constructor({ container, api, notify }) {
    this.container = container;
    this.api = api;
    this.notify = notify;
    this.busy = false;
    this.data = [];
    this.activeAnecdotes = [];

    if (this.container) {
      const extractBtn = this.container.querySelector("#extract-daily-memory");
      if (extractBtn) extractBtn.onclick = () => this.extract();
      const refreshBtn = this.container.querySelector("#refresh-daily-memory");
      if (refreshBtn) refreshBtn.onclick = () => this.refresh();
    }
  }

  categoryLabel(cat) {
    return {
      comment_anecdote: "💬 评论趣事",
      live_highlight: "🎮 直播战报",
      community_meme: "✨ 社群新梗",
      public_incident: "真实互动经历",
    }[cat] || "📌 每日高光";
  }

  statusBadge(status, publicExperience) {
    if (status === "active") return `<span class="status-badge success">${publicExperience ? "可公开使用" : "已启用谈资"}</span>`;
    if (status === "pending_review") return `<span class="status-badge warning">${publicExperience ? "待确认公开资格" : "待审核"}</span>`;
    return '<span class="status-badge muted">已归档</span>';
  }

  async refresh() {
    if (!this.container || this.busy) return;
    return this.load();
  }

  async load() {
    try {
      const res = await this.api("/api/daily-memory");
      this.data = res.highlights || [];
      this.activeAnecdotes = res.active_anecdotes || [];
      this.render();
    } catch (e) {
      const notice = this.container.querySelector("#daily-memory-notice");
      if (notice) notice.textContent = `读取失败: ${e.message}`;
    }
  }

  async extract(date) {
    if (this.busy) return;
    this.busy = true;
    const btn = this.container.querySelector("#extract-daily-memory");
    if (btn) btn.disabled = true;
    const notice = this.container.querySelector("#daily-memory-notice");
    if (notice) notice.textContent = "大模型正在审阅今日社群互动与直播素材，提炼人设趣事…";

    try {
      const res = await this.api("/api/daily-memory/extract", {
        method: "POST",
        body: { date: date || undefined, auto_approve: false },
      });
      this.notify(`已提炼 ${res.total || 0} 条今日趣事与高光（待审核）。`);
      await this.load();
    } catch (e) {
      this.notify(`提炼失败: ${e.message}`, true);
      if (notice) notice.textContent = `提炼失败: ${e.message}`;
    } finally {
      this.busy = false;
      if (btn) btn.disabled = false;
    }
  }

  async toggleStatus(id, newStatus) {
    try {
      await this.api(`/api/daily-memory/${encodeURIComponent(id)}`, {
        method: "PUT",
        body: { status: newStatus },
      });
      this.notify(newStatus === "active" ? "已批准该趣事进入实时谈资库。" : "已更新状态。");
      await this.refresh();
    } catch (e) {
      this.notify(`操作失败: ${e.message}`, true);
    }
  }

  async deleteItem(id) {
    if (!confirm("确认删除这条记忆吗？")) return;
    try {
      await this.api(`/api/daily-memory/${encodeURIComponent(id)}`, { method: "DELETE" });
      this.notify("记忆条目已删除。");
      await this.refresh();
    } catch (e) {
      this.notify(`删除失败: ${e.message}`, true);
    }
  }

  render() {
    const listEl = this.container.querySelector("#daily-memory-list");
    const notice = this.container.querySelector("#daily-memory-notice");
    if (!listEl) return;

    if (!this.data.length) {
      listEl.innerHTML = '<p class="hint empty-hint">今日暂无提炼的社群趣事。点击右上角“提炼今日趣事”，或在直播与评论区互动后自动生成。</p>';
      if (notice) notice.textContent = "当前活跃谈资：0 条";
      return;
    }

    if (notice) {
      notice.innerHTML = `当前共 ${this.data.length} 条记忆（其中 <strong>${this.activeAnecdotes.length}</strong> 条正作为活跃谈资随身携带入直播/回帖）。`;
    }

    listEl.innerHTML = "";
    this.data.forEach(item => {
      const confirmed = item.source_ref?.startsWith("operator-public:");
      const runtime = item.source_ref?.startsWith("system:downtime:");
      const publicExperience = confirmed || runtime || item.category === "public_incident";
      const card = document.createElement("div");
      card.className = `daily-memory-item ${item.status}`;
      card.dataset.id = item.id;

      const header = document.createElement("div");
      header.className = "item-header";
      header.innerHTML = `
        <span class="category-chip">${confirmed ? "号主确认的事实" : runtime ? "运行经历" : this.categoryLabel(item.category)}</span>
        <strong class="item-headline">${escapeHtml(item.headline)}</strong>
        <span class="item-date">${escapeHtml(item.highlight_date)}</span>
        ${this.statusBadge(item.status, publicExperience)}
      `;

      const body = document.createElement("div");
      body.className = "item-body";
      body.innerHTML = `
        <div class="item-anecdote">
          <span class="quote-icon">❝</span>
          <p class="anecdote-text">${escapeHtml(item.anecdote)}</p>
        </div>
        <div class="item-quote">
          <small class="hint">${escapeHtml(confirmed ? "号主确认" : runtime ? "运行记录" : item.source_user || "公开来源")} · ${escapeHtml(item.quote)}</small>
        </div>
      `;
      if (item.category === "public_incident") {
        for (const source of item.public_sources || []) {
          const line = document.createElement("p");
          line.className = "hint";
          const link = document.createElement("a");
          link.href = `https://www.bilibili.com/video/${encodeURIComponent(source.bvid)}#reply${encodeURIComponent(source.comment_id)}`;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = `${source.bvid} · 评论 ${source.comment_id}`;
          line.append(link, document.createTextNode(`：${source.quote}`));
          body.append(line);
        }
      }

      const actions = document.createElement("div");
      actions.className = "item-actions";

      if (item.status === "pending_review") {
        const approveBtn = document.createElement("button");
        approveBtn.className = "primary small";
        approveBtn.textContent = publicExperience ? "确认公开" : "✓ 批准为谈资";
        approveBtn.onclick = () => this.toggleStatus(item.id, "active");
        actions.append(approveBtn);
      } else if (item.status === "active") {
        const archiveBtn = document.createElement("button");
        archiveBtn.className = "quiet small";
        archiveBtn.textContent = "归档";
        archiveBtn.onclick = () => this.toggleStatus(item.id, "archived");
        actions.append(archiveBtn);
      } else {
        const reactivateBtn = document.createElement("button");
        reactivateBtn.className = "quiet small";
        reactivateBtn.textContent = "重新激活";
        reactivateBtn.onclick = () => this.toggleStatus(item.id, "active");
        actions.append(reactivateBtn);
      }
      if (publicExperience && item.status !== "pending_review") {
        const revoke = document.createElement("button");
        revoke.className = "quiet small";
        revoke.textContent = "撤销公开";
        revoke.onclick = () => this.toggleStatus(item.id, "pending_review");
        actions.append(revoke);
      }

      const deleteBtn = document.createElement("button");
      deleteBtn.className = "danger quiet small";
      deleteBtn.textContent = "删除";
      deleteBtn.onclick = () => this.deleteItem(item.id);
      actions.append(deleteBtn);

      card.append(header, body, actions);
      listEl.append(card);
    });
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
