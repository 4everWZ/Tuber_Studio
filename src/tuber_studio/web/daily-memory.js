"use strict";

class DailyMemoryUI {
  constructor({ container, api, notify }) {
    this.container = container;
    this.api = api;
    this.notify = notify;
    this.busy = false;
    this.data = [];
    this.loadVersion = 0;
    this.organizedSignature = null;
    this.recentSignature = null;

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
    return Promise.all([this.loadRecent(), this.load()]);
  }

  async loadRecent() {
    const list = document.getElementById("recent-memory-list");
    if (!list) return;
    const day = document.getElementById("recent-memory-day");
    list.setAttribute("aria-busy", "true");
    try {
      const result = await this.api("/api/daily-memory/recent-events");
      day.textContent = result.day;
      const signature = JSON.stringify(result.items);
      if (signature === this.recentSignature) return;
      this.recentSignature = signature;
      list.replaceChildren();
      for (const event of result.items) {
        const article = document.createElement("article");
        article.className = "issue-row";
        const title = document.createElement("strong");
        title.textContent = event.title;
        const at = document.createElement("time");
        const date = new Date(event.at * 1000);
        at.dateTime = date.toISOString();
        at.textContent = date.toLocaleString("zh-CN", { timeZone: result.timezone, hour12: false });
        at.className = "hint";
        const excerpt = document.createElement("p");
        excerpt.textContent = event.source.text.slice(0, 360);
        const state = document.createElement("span");
        state.className = "hint";
        state.textContent = ({
          confirmed: "回复已送达", uncertain: "送达待核对", previously_replied: "此前已回复",
          owner_replied: "号主已代回", same_account_replied: "同账号已回复 · 归属未知",
        })[event.reply?.state] || (event.thread ? "已读评论楼" : "有观看记录");
        article.append(title, " · ", at, excerpt, state);
        for (const [label, href] of [[event.thread ? "打开原楼" : "打开来源", event.source.href],
          ["查看回复", event.reply?.href]]) {
          if (!/^https:\/\/(?:www\.bilibili\.com\/video\/BV[0-9A-Za-z]+|t\.bilibili\.com\/\d+)(?:[/?#]|$)/.test(href || "")) continue;
          const link = document.createElement("a");
          link.href = href;
          link.textContent = label;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          article.append(" · ", link);
        }
        list.append(article);
      }
      if (!result.items.length) list.textContent = "近期暂无有来源的运营事件。";
    } catch (error) {
      this.recentSignature = null;
      list.textContent = `读取近期趣事失败：${error.message}`;
    } finally {
      list.setAttribute("aria-busy", "false");
    }
  }

  async load() {
    const version = ++this.loadVersion;
    try {
      const res = await this.api("/api/daily-memory");
      if (version !== this.loadVersion) return;
      this.data = res.highlights || [];
      this.render();
      this.renderOrganized(res.organized || [], res.organization || {});
    } catch (e) {
      if (version !== this.loadVersion) return;
      const notice = this.container.querySelector("#daily-memory-notice");
      if (notice) notice.textContent = `读取失败: ${e.message}`;
    }
  }

  async extract() {
    if (this.busy) return;
    this.busy = true;
    const btn = this.container.querySelector("#extract-daily-memory");
    if (btn) btn.disabled = true;
    const notice = this.container.querySelector("#daily-memory-notice");
    if (notice) notice.textContent = "已安排后台整理。";

    try {
      await this.api("/api/daily-memory/organize", { method: "POST" });
      this.notify("已安排后台整理。");
    } catch (e) {
      this.notify(`提炼失败: ${e.message}`, true);
      if (notice) notice.textContent = `提炼失败: ${e.message}`;
    } finally {
      this.busy = false;
      if (btn) btn.disabled = false;
    }
  }

  renderOrganized(items, organization) {
    const notice = this.container.querySelector("#daily-memory-notice");
    const labels = { operator: "号主私聊", live: "直播", comment: "回帖", dynamic: "动态" };
    const state = ({
      running: "正在后台整理", waiting: "等待模型空闲", retry: organization.error || "整理失败，稍后重试",
    })[organization.status] || "";
    notice.textContent = [state, organization.processed ? `已处理 ${organization.processed} 条来源` : "",
      organization.invalid ? `${organization.invalid} 条整理结果无效` : ""].filter(Boolean).join(" · ");
    const signature = JSON.stringify(items);
    if (signature === this.organizedSignature) return;
    this.organizedSignature = signature;
    for (const pending of [true, false]) {
      const root = this.container.querySelector(pending ? "#pending-memory-list" : "#organized-memory-list");
      const rows = items.filter(item => (item.status === "pending_review") === pending);
      root.replaceChildren();
      if (!rows.length) root.textContent = pending ? "暂无待确认经历。" : "暂无已保存记忆。";
      for (const item of rows) {
        const card = document.createElement("article");
        card.className = "issue-row";
        const heading = document.createElement("strong");
        heading.textContent = item.headline;
        const text = document.createElement("p");
        text.textContent = item.anecdote;
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "来源与使用权限";
        const meta = document.createElement("p");
        meta.className = "hint";
        meta.textContent = `${new Date(item.updated_at * 1000).toLocaleString("zh-CN")} · ${item.status === "archived" ?
          "已归档" : item.channels.map(channel => labels[channel]).join("、") || "待确认"}`;
        const quote = document.createElement("blockquote");
        quote.textContent = item.quote;
        details.append(summary, meta, quote);
        for (const ref of item.source.references || []) {
          if (!/^https:\/\/(?:www\.bilibili\.com\/video\/BV[0-9A-Za-z]+|t\.bilibili\.com\/\d+)(?:[/?#]|$)/.test(ref.href || "")) continue;
          const link = document.createElement("a");
          link.href = ref.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = ref.text;
          const line = document.createElement("p");
          line.append(link);
          details.append(line);
        }
        const sourceKey = item.source.key || "";
        if (item.memory_kind === "private" && sourceKey.startsWith("turn:")) {
          const line = document.createElement("p");
          line.className = "hint";
          line.textContent = "来源 · 号主对话 " + new Date(item.source.at * 1000).toLocaleString("zh-CN");
          details.append(line);
        }
        const form = document.createElement("form");
        const headline = document.createElement("input");
        headline.value = item.headline;
        headline.maxLength = 40;
        headline.required = true;
        headline.setAttribute("aria-label", "记忆标题");
        const note = document.createElement("textarea");
        note.value = item.anecdote;
        note.maxLength = 150;
        note.required = true;
        note.setAttribute("aria-label", "记忆内容");
        form.append(headline, note);
        const channels = [];
        if (item.memory_kind === "experience") {
          for (const channel of ["live", "comment", "dynamic"]) {
            const label = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.value = channel;
            box.checked = item.channels.includes(channel);
            channels.push(box);
            label.append(box, labels[channel]);
            form.append(label);
          }
        }
        const save = document.createElement("button");
        save.textContent = pending ? "确认并保存" : "保存";
        form.append(save);
        form.onsubmit = event => {
          event.preventDefault();
          this.changeOrganized(item.id, { headline: headline.value.trim(), note: note.value.trim(), status: "active",
            ...(item.memory_kind === "experience" ? { channels: channels.filter(box => box.checked).map(box => box.value) } : {}) });
        };
        details.append(form);
        const actions = document.createElement("div");
        actions.className = "actions";
        if (item.status === "active") {
          const archive = document.createElement("button");
          archive.textContent = "归档";
          archive.onclick = () => this.changeOrganized(item.id, { status: "archived" });
          actions.append(archive);
        }
        if (item.channels.some(channel => channel !== "operator")) {
          const revoke = document.createElement("button");
          revoke.textContent = "撤销公开";
          revoke.onclick = () => this.changeOrganized(item.id, { channels: [] });
          actions.append(revoke);
        }
        const remove = document.createElement("button");
        remove.textContent = "删除";
        remove.onclick = () => { if (confirm("确认删除这条记忆吗？")) this.changeOrganized(item.id); };
        actions.append(remove);
        card.append(heading, text, details, actions);
        root.append(card);
      }
    }
  }

  async changeOrganized(id, body) {
    try {
      await this.api(`/api/daily-memory/organized/${id}`, body ? { method: "PATCH", body } : { method: "DELETE" });
      await this.load();
    } catch (error) {
      this.notify(error.message, true);
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
    if (!listEl) return;

    if (!this.data.length) {
      listEl.innerHTML = '<p class="hint empty-hint">暂无早期记忆记录。</p>';
      return;
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
