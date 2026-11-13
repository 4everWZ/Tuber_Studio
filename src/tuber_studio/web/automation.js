"use strict";

/**
 * AutomationPanel (Refactored Coordinator)
 * Delegates UI rendering to the modular OperationsWorkbench (VSCode / Antigravity style),
 * while preserving API polling, recovery policy, and state synchronization.
 */

class AutomationPanel {
  constructor(root, api, notify) {
    this.root = root;
    this.api = api;
    this.notify = notify;

    this.tasks = [];
    this.rounds = [];
    this.intents = [];
    this.inbox = [];
    this.refreshing = false;
    this.roundId = undefined;
    this.refreshedAt = 0;
    this.activityRoot = document.getElementById("automation-round-activity");
    this.activityPage = 1;
    this.activityRound = undefined;
    this.activityRequest = 0;

    // Build Host Container
    this.root.innerHTML = '<div id="op-workbench-mount"></div>';

    // Instantiate Modern Workbench
    const mount = this.root.querySelector("#op-workbench-mount");
    if (typeof OperationsWorkbench !== "undefined") {
      this.workbench = new OperationsWorkbench(mount, api, notify, () => this.refresh());
    } else {
      console.warn("OperationsWorkbench class not found, check script inclusion order.");
    }

    // Attach Recovery Policy to technical container
    const recoveryContainer = document.getElementById("operations-recovery-settings");
    if (typeof OperationRecoveryPolicyView !== "undefined" && recoveryContainer) {
      this.recoveryPolicy = new OperationRecoveryPolicyView(recoveryContainer, api, notify);
    }
  }

  array(value) {
    return Array.isArray(value) ? value : value?.items || [];
  }

  async loadRecovery() {
    if (this.recoveryPolicy && !this.recoveryPolicyStarted) {
      await this.recoveryPolicy.load();
      this.recoveryPolicyStarted = true;
    }
  }

  async openRound(roundId, taskId = null) {
    const previous = this.roundId;
    this.roundId = roundId;
    if (previous !== roundId) {
      this.workbench?.switchTab(roundId ? "tasks" : "rounds");
      this.workbench?.setRoundFilter(roundId);
    }
    if (taskId && this.workbench) {
      const diagnostics = this.root.closest("details");
      if (diagnostics) diagnostics.open = true;
      this.workbench.switchTab("tasks");
      this.workbench.selectedTaskId = taskId;
    }
    return this.refresh();
  }

  async refresh({ background = false } = {}) {
    if (this.refreshing || this.root.closest("[hidden]")) return;
    if (background && Date.now() - this.refreshedAt < 8000) return;
    this.refreshing = true;
    this.refreshedAt = Date.now();
    const round = this.roundId;
    try {
      const results = await Promise.allSettled([
        this.api(round ? `/api/automation/tasks?round_id=${encodeURIComponent(round)}` : "/api/automation/tasks"),
        this.api("/api/automation/rounds"),
        this.api("/api/automation/intents"),
        this.api("/api/automation/inbox")
      ]);

      if (round !== this.roundId || this.root.closest("[hidden]")) return;
      if (results[0].status === "fulfilled") this.tasks = this.array(results[0].value);
      if (results[1].status === "fulfilled") this.rounds = this.array(results[1].value);
      if (results[2].status === "fulfilled") {
        this.intents = this.array(results[2].value).filter(row => ["video_like", "video_fav"].includes(row.kind));
      }
      if (results[3].status === "fulfilled") {
        const inboxData = results[3].value;
        this.inbox = Array.isArray(inboxData) ? inboxData : (inboxData?.items || []);
      }

      // Update Modern Workbench View
      if (this.workbench) {
        this.workbench.updateData({
          intents: this.intents,
          tasks: this.tasks,
          rounds: this.rounds,
          inbox: this.inbox,
        });
      }
      const current = round || this.rounds.find(item => item.status === "running")?.id || this.rounds[0]?.id;
      await this.refreshActivity(current);

    } catch (err) {
      console.error("AutomationPanel refresh failed:", err);
    } finally {
      this.refreshing = false;
      if (round !== this.roundId && !this.root.closest("[hidden]")) void this.refresh();
    }
  }

  async refreshActivity(roundId) {
    if (!this.activityRoot) return;
    if (roundId !== this.activityRound) {
      this.activityRound = roundId;
      this.activityPage = 1;
    }
    const request = ++this.activityRequest, routeRound = this.roundId;
    const query = new URLSearchParams({ page_size: "20", page: String(this.activityPage) });
    if (roundId) query.set("round_id", roundId);
    const node = (tag, text) => {
      const element = document.createElement(tag);
      if (text) element.textContent = text;
      return element;
    };
    const current = () => request === this.activityRequest && routeRound === this.roundId && !this.root.closest("[hidden]");
    let data;
    try {
      data = roundId ? await this.api(`/api/automation/activity?${query}`) : { items: [], total: 0 };
    } catch {
      if (current()) {
        const error = node("p", "本轮事件读取失败，刷新后重试。");
        error.setAttribute("role", "status");
        this.activityRoot.replaceChildren(error);
        delete this.activityRoot.dataset.snapshot;
      }
      return;
    }
    if (!current()) return;
    this.activityPage = data.page || 1;
    const signature = JSON.stringify([roundId, data.items, data.page, data.total]);
    if (signature === this.activityRoot.dataset.snapshot) return;
    const expanded = new Set([...this.activityRoot.querySelectorAll("details[open]")].map(item => item.dataset.event));
    this.activityRoot.dataset.snapshot = signature;
    this.activityRoot.replaceChildren();
    const outcomes = { new_reply: "本轮新回", previous_reply: "此前已回", same_account: "同账号已回 · 归属未确认",
      pending_verification: "待核对", read: "已读", published: "已发送" };
    for (const item of data.items) {
      const row = node("article");
      row.className = "daily-video-row";
      const result = outcomes[item.comment_outcome || item.status] || TuberFormatters.humanizeStatus(item.status).label;
      row.append(node("strong", `${item.title} · ${result}`));
      const ownership = [item.own_resource === true ? (item.bvid ? "自有投稿" : "自有动态") : "",
        item.own_thread === true ? "号主的楼" : item.own_thread === false ? "观众的楼" : ""].filter(Boolean);
      if (ownership.length) row.append(node("p", ownership.join(" · ")));
      if (item.source_text) row.append(node("blockquote", item.source_text));
      const body = item.account_reply_text || item.confirmed_reply_text || item.text;
      if (body) row.append(node("p", body));
      for (const [name, href] of [[item.source_href ? "打开原评论" : "打开来源", item.source_href || item.href],
        ["打开已确认回复", item.sent_href]]) {
        if (!href) continue;
        const anchor = node("a", name);
        anchor.href = href;
        if (href.startsWith("https://")) { anchor.target = "_blank"; anchor.rel = "noreferrer"; }
        row.append(anchor, " ");
      }
      if (item.reason || item.error) {
        const diagnostics = node("details");
        diagnostics.dataset.event = item.id;
        diagnostics.open = expanded.has(item.id);
        diagnostics.append(node("summary", "处理详情"));
        if (item.reason) diagnostics.append(node("p", item.reason));
        if (item.error) diagnostics.append(node("p", item.error));
        row.append(diagnostics);
      }
      this.activityRoot.append(row);
    }
    if (!data.items.length) this.activityRoot.append(node("p", "本轮暂无可展示的事件。"));
    if (data.pages > 1) {
      const pages = node("div");
      pages.className = "actions";
      const previous = node("button", "上一页"), next = node("button", "下一页");
      previous.type = next.type = "button";
      previous.disabled = this.activityPage <= 1;
      next.disabled = this.activityPage >= data.pages;
      previous.onclick = () => { this.activityPage--; void this.refreshActivity(roundId); };
      next.onclick = () => { this.activityPage++; void this.refreshActivity(roundId); };
      pages.append(previous, node("span", `第 ${this.activityPage} / ${data.pages} 页`), next);
      this.activityRoot.append(pages);
    }
  }

}

window.AutomationPanel = AutomationPanel;
