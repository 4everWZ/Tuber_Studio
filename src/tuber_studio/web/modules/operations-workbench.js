/**
 * Operations Workbench Component for Tuber Studio
 * Browser tasks, rounds, public notifications, and engagement review.
 */

(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define(['./formatters', './pagination'], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./formatters'), require('./pagination'));
  } else {
    root.OperationsWorkbench = factory(root.TuberFormatters, root.TuberPaginator);
  }
})(typeof self !== 'undefined' ? self : this, function (Formatters, Paginator) {

  const { formatRelativeTime, humanizeKind, humanizeStatus, humanizeReason, humanizeRoundReport, humanizeTaskReason } = Formatters;

  function escapeHtml(value) {
    const chars = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
    return String(value ?? "").replace(/[&<>"]/g, char => chars[char]);
  }

  function taskReason(task) {
    return task.retry_kind || task.payload?.retry_kind || task.reason || task.error_kind || task.error || task.payload?.reason;
  }

  class OperationsWorkbench {
    constructor(container, api, notify, refresh) {
      this.container = container;
      this.api = api;
      this.notify = notify;
      this.refresh = refresh;

      // State
      this.activeTab = "tasks";
      this.intentFilter = "pending_review";
      this.taskFilter = "";
      this.roundFilter = null;

      this.intents = [];
      this.tasks = [];
      this.rounds = [];
      this.inbox = [];

      this.selectedIntentId = null;
      this.selectedTaskId = null;
      this.intentPending = false;
      this.renderedIntent = "";
      this.renderedTaskId = null;
      this.renderedTaskStatus = null;
      this.renderedTab = null;
      this.lastMasterView = "";

      // Paginators
      this.intentPaginator = new Paginator({
        pageSize: 8,
        onPageChange: () => this.renderIntentCards(),
      });
      this.roundPaginator = new Paginator({
        pageSize: 6,
        onPageChange: () => this.renderRoundCards(),
      });
      this.taskPaginator = new Paginator({
        pageSize: 10,
        onPageChange: () => { this.selectedTaskId = null; this.renderTaskRows(); this.renderDetail(); },
      });

      this.initLayout();
    }

    initLayout() {
      this.container.innerHTML = `
        <div class="op-workbench-container">
          <!-- Segmented Master Navigation Bar -->
          <div class="op-workbench-nav">
            <div class="op-segmented-control" role="tablist">
              <button type="button" class="op-nav-btn active" data-tab="tasks">
                <span class="op-nav-label">浏览任务</span>
              </button>
              <button type="button" class="op-nav-btn" data-tab="rounds">
                <span class="op-nav-label">轮次记录</span>
              </button>
              <button type="button" class="op-nav-btn" data-tab="inbox">
                <span class="op-nav-label">互动收件箱</span>
                <span class="op-badge-counter" id="op-counter-inbox" hidden>0</span>
              </button>
              <button type="button" class="op-nav-btn" data-tab="intents">
                <span class="op-nav-label">点赞与收藏</span>
                <span class="op-badge-counter" id="op-counter-intents" hidden>0</span>
              </button>
            </div>
            <div class="op-nav-actions">
              <button type="button" class="op-btn-subtle" id="op-btn-refresh-all" title="刷新工作台数据">
                🔄 刷新
              </button>
            </div>
          </div>

          <!-- Split Workspace (Master on Left, Detail Inspector on Right) -->
          <div class="op-split-workspace">
            <!-- Left Master Column -->
            <div class="op-master-pane">
              <!-- Filter / Action Sub-bar -->
              <div class="op-master-subbar" id="op-master-subbar"></div>

              <!-- Master Content List (Cards or Table) -->
              <div class="op-master-list" id="op-master-list"></div>

              <!-- Pagination Footer -->
              <div class="op-master-pagination" id="op-master-pagination"></div>
            </div>

            <!-- Right Detail Inspector -->
            <div class="op-detail-pane" id="op-detail-pane">
              <div class="op-empty-state">
                <div class="op-empty-icon">👈</div>
                <h3>请选择左侧项目</h3>
                <p class="hint">点击左侧候选草稿、收到的互动或浏览任务，即可在此处查看详情、编辑回复或执行审核发布。</p>
              </div>
            </div>
          </div>
        </div>
      `;

      // Bind Tab Switching
      this.container.querySelectorAll(".op-nav-btn").forEach(btn => {
        btn.onclick = () => {
          const tab = btn.dataset.tab;
          this.switchTab(tab);
        };
      });

      const refreshBtn = this.container.querySelector("#op-btn-refresh-all");
      if (refreshBtn) {
        refreshBtn.onclick = async () => {
          refreshBtn.classList.add("spinning");
          try {
            await this.refresh();
            if (this.notify) this.notify("工作台数据已更新。");
          } finally {
            setTimeout(() => refreshBtn.classList.remove("spinning"), 500);
          }
        };
      }
    }

    switchTab(tab) {
      if (this.activeTab === tab) return;
      this.activeTab = tab;
      this.lastMasterView = "";

      this.container.querySelectorAll(".op-nav-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === tab);
      });

      this.renderMasterSubbar();
      this.renderMasterList();
      this.renderDetail();
    }

    setRoundFilter(roundId) {
      this.roundFilter = roundId;
      this.taskFilter = "";
      this.tasks = [];
      this.selectedTaskId = null;
      this.lastMasterView = "";
      if (this.activeTab === "tasks") {
        this.taskPaginator.currentPage = 1;
        this.renderMasterSubbar();
        this.renderMasterList();
        this.renderDetail();
      }
    }

    masterView() {
      switch (this.activeTab) {
        case "intents": {
          const titles = new Map(this.tasks.map(t => [t.bvid, t.title || t.metadata?.title]));
          const rows = this.getFilteredIntents().map(i => [
            i.id, i.status, i.text, i.reason, i.target?.title, titles.get(i.bvid || i.target?.bvid),
          ]);
          return [this.activeTab, this.intentFilter, this.selectedIntentId, rows];
        }
        case "rounds":
          return [this.activeTab, this.rounds.map(r => [r.id, r.status, r.budget, r.error])];
        case "tasks":
          return [this.activeTab, this.taskFilter, this.selectedTaskId, this.tasks.map(t => [
            t.id, t.status, t.title || t.metadata?.title, taskReason(t),
          ])];
        default:
          return [this.activeTab, this.inbox];
      }
    }

    intentReviewNote(intent) {
      if (intent.status !== "pending_review") return "";
      const round = this.rounds.find(item => item.id === intent.round_id);
      if (round?.preview) return "来自预览轮，需人工审核。";
      return intent.auto_authorized ? "" : "这条草稿生成时未获得自动发布授权，需人工审核。";
    }

    updateData({ intents, tasks, rounds, inbox } = {}) {
      if (intents !== undefined) this.intents = Array.isArray(intents) ? intents : (intents?.items || []);
      if (tasks !== undefined) this.tasks = Array.isArray(tasks) ? tasks : (tasks?.items || []);
      if (rounds !== undefined) this.rounds = Array.isArray(rounds) ? rounds : (rounds?.items || []);
      if (inbox !== undefined) {
        this.inbox = Array.isArray(inbox) ? inbox : (Array.isArray(inbox?.items) ? inbox.items : []);
      }
      if (this.selectedTaskId && !this.tasks.some(task => task.id === this.selectedTaskId && (!this.taskFilter || task.status === this.taskFilter))) this.selectedTaskId = null;

      // Update counters
      const pendingIntentsCount = (Array.isArray(this.intents) ? this.intents : []).filter(i => i.status === "pending_review").length;
      const counterIntents = this.container.querySelector("#op-counter-intents");
      if (counterIntents) {
        counterIntents.textContent = pendingIntentsCount;
        counterIntents.hidden = pendingIntentsCount === 0;
      }

      const inboxList = Array.isArray(this.inbox) ? this.inbox : [];
      const pendingInboxCount = inboxList.filter(i => i.status === "pending" || i.status === "received").length;
      const counterInbox = this.container.querySelector("#op-counter-inbox");
      if (counterInbox) {
        counterInbox.textContent = pendingInboxCount;
        counterInbox.hidden = pendingInboxCount === 0;
      }

      // Automatically select first pending intent if nothing selected or current item disappeared (and not dirty)
      if (this.activeTab === "intents") {
        const filtered = this.getFilteredIntents();
        if (!this.selectedIntentId || !filtered.some(i => i.id === this.selectedIntentId)) {
          this.selectedIntentId = filtered.length > 0 ? filtered[0].id : null;
        }
      }

      const view = JSON.stringify(this.masterView());
      if (view !== this.lastMasterView) {
        this.lastMasterView = view;
        const listContainer = this.container.querySelector("#op-master-list");
        const prevScroll = listContainer ? listContainer.scrollTop : 0;
        this.renderMasterSubbar();
        this.renderMasterList();
        if (listContainer) listContainer.scrollTop = prevScroll;
      }

      if (this.activeTab === "intents") {
        const intent = this.intents.find(item => item.id === this.selectedIntentId);
        if (this.renderedTab !== "intents" || this.renderedIntent !== JSON.stringify(intent)) this.renderDetail();
      } else if (this.activeTab === "tasks") {
        const currentTask = this.tasks.find(t => t.id === this.selectedTaskId);
        if (this.renderedTab !== "tasks" || this.renderedTaskId !== this.selectedTaskId || this.renderedTaskStatus !== JSON.stringify([currentTask?.status, currentTask?.cache_status, currentTask?.updated_at])) {
          this.renderDetail();
        }
      } else {
        if (this.renderedTab !== this.activeTab) {
          this.renderDetail();
        }
      }
    }

    getFilteredIntents() {
      if (!this.intentFilter || this.intentFilter === "all") {
        return this.intents;
      }
      return this.intents.filter(i => i.status === this.intentFilter);
    }

    renderMasterSubbar() {
      const subbar = this.container.querySelector("#op-master-subbar");
      if (!subbar) return;
      subbar.replaceChildren();

      if (this.activeTab === "intents") {
        const pendingCount = this.intents.filter(i => i.status === "pending_review").length;
        const approvedCount = this.intents.filter(i => i.status === "approved").length;
        const publishedCount = this.intents.filter(i => i.status === "published").length;

        const pills = document.createElement("div");
        pills.className = "op-filter-pills";

        const filterOptions = [
          { key: "pending_review", label: `⏳ 待审核 (${pendingCount})`, active: this.intentFilter === "pending_review" },
          { key: "approved", label: `✓ 已通过 (${approvedCount})`, active: this.intentFilter === "approved" },
          { key: "publish_uncertain", label: "待核对", active: this.intentFilter === "publish_uncertain" },
          { key: "published", label: `🚀 已发布 (${publishedCount})`, active: this.intentFilter === "published" },
          { key: "rejected", label: `✕ 已放弃`, active: this.intentFilter === "rejected" },
          { key: "all", label: `全部 (${this.intents.length})`, active: this.intentFilter === "all" },
        ];

        filterOptions.forEach(opt => {
          const pill = document.createElement("button");
          pill.type = "button";
          pill.className = `op-pill ${opt.active ? "active" : ""}`;
          pill.textContent = opt.label;
          pill.onclick = () => {
            if (this.intentFilter === opt.key) return;
            this.intentFilter = opt.key;
            this.intentPaginator.currentPage = 1;
            const filtered = this.getFilteredIntents();
            this.selectedIntentId = filtered.length > 0 ? filtered[0].id : null;
            this.lastMasterView = "";
            this.renderMasterSubbar();
            this.renderMasterList();
            this.renderDetail();
          };
          pills.append(pill);
        });

        subbar.append(pills);
      } else if (this.activeTab === "tasks") {
        if (this.roundFilter) {
          const all = document.createElement("a");
          all.href = "#automation";
          all.textContent = "本轮任务 · 查看全部";
          subbar.append(all);
        }
        const select = document.createElement("select");
        select.className = "op-task-select";
        select.innerHTML = `
          <option value="">全部任务 (${this.tasks.length})</option>
          <option value="selected">✨ 已生成互动</option>
          <option value="viewed_without_comment">👀 已阅未发</option>
          <option value="checkpoint">💾 进度已存</option>
          <option value="no_subtitles">🔇 缺少可用字幕</option>
          <option value="failed">🔴 失败/异常</option>
        `;
        select.value = this.taskFilter;
        select.onchange = () => {
          this.taskFilter = select.value;
          this.selectedTaskId = null;
          this.taskPaginator.currentPage = 1;
          this.renderMasterList();
          this.renderDetail();
        };
        subbar.append(select);
      } else if (this.activeTab === "rounds") {
        const titleSpan = document.createElement("span");
        titleSpan.className = "op-subbar-title";
        titleSpan.textContent = `共 ${this.rounds.length} 轮历史自主巡航`;
        subbar.append(titleSpan);
      } else if (this.activeTab === "inbox") {
        const titleSpan = document.createElement("span");
        titleSpan.className = "op-subbar-title";
        titleSpan.textContent = `收到的互动与 @ 提及 (${this.inbox.length})`;
        subbar.append(titleSpan);
      }
    }

    renderMasterList() {
      const listContainer = this.container.querySelector("#op-master-list");
      const paginationContainer = this.container.querySelector("#op-master-pagination");
      if (!listContainer) return;

      if (this.activeTab === "intents") {
        const filtered = this.getFilteredIntents();
        this.intentPaginator.setItems(filtered);
        this.renderIntentCards();
        this.intentPaginator.renderControls(paginationContainer);
      } else if (this.activeTab === "rounds") {
        this.roundPaginator.setItems(this.rounds);
        this.renderRoundCards();
        this.roundPaginator.renderControls(paginationContainer);
      } else if (this.activeTab === "tasks") {
        const filtered = this.taskFilter ? this.tasks.filter(t => t.status === this.taskFilter) : this.tasks;
        this.taskPaginator.setItems(filtered);
        this.renderTaskRows();
        this.taskPaginator.renderControls(paginationContainer);
      } else if (this.activeTab === "inbox") {
        this.renderInboxList();
        paginationContainer.replaceChildren();
        paginationContainer.style.display = "none";
      }
    }

    renderIntentCards() {
      const listContainer = this.container.querySelector("#op-master-list");
      listContainer.replaceChildren();

      const items = this.intentPaginator.getPageItems();

      if (items.length === 0) {
        const empty = document.createElement("div");
        empty.className = "op-inbox-zero";
        if (this.intentFilter === "pending_review") {
          empty.innerHTML = `
            <div class="op-zero-icon">🎉</div>
            <h4>暂无待审核的点赞或收藏</h4>
            <p class="hint">公开评论草稿在「评论与草稿」处理。</p>
          `;
        } else {
          empty.innerHTML = `
            <div class="op-zero-icon">📭</div>
            <h4>暂无相关候选草稿</h4>
            <p class="hint">可以切换至其他过滤标签查看历史记录。</p>
          `;
        }
        listContainer.append(empty);
        return;
      }

      items.forEach(intent => {
        const card = document.createElement("div");
        const isSelected = intent.id === this.selectedIntentId;
        card.className = `op-candidate-card ${isSelected ? "selected" : ""}`;

        const kindInfo = humanizeKind(intent.kind);
        const statusInfo = humanizeStatus(intent.status);
        const timeAgo = formatRelativeTime(intent.created_at);

        let target = {};
        if (typeof intent.target === "string") {
          try { target = JSON.parse(intent.target); } catch (e) {}
        } else if (typeof intent.target === "object") {
          target = intent.target || {};
        }

        const bvid = intent.bvid || target.bvid || intent.source_id || "B站视频";
        const relatedTask = this.tasks.find(t => t.bvid === bvid);
        const videoTitle = relatedTask?.title || relatedTask?.metadata?.title || target.title || bvid;

        card.innerHTML = `
          <div class="op-card-header">
            <div class="op-card-badges">
              <span class="op-tag-kind">${kindInfo.label}</span>
              <span class="op-tag-status ${statusInfo.className}">${statusInfo.label}</span>
            </div>
            <span class="op-card-time">${timeAgo}</span>
          </div>
          <div class="op-card-title" title="${escapeHtml(videoTitle)}">${escapeHtml(videoTitle)}</div>
          <div class="op-card-snippet">${escapeHtml(intent.text || "（点赞或收藏操作，无需文字）")}</div>
          <div class="op-card-footer">
            <span class="op-card-reason hint">💡 ${escapeHtml(humanizeReason(intent.reason))}</span>
          </div>
        `;

        card.dataset.id = intent.id;
        card.tabIndex = 0;
        card.setAttribute("role", "button");
        card.onkeydown = event => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            card.click();
          }
        };
        card.onclick = () => {
          if (this.selectedIntentId !== intent.id) {
            this.selectedIntentId = intent.id;
            this.lastMasterView = "";
            const allCards = listContainer.querySelectorAll(".op-candidate-card");
            allCards.forEach(c => c.classList.toggle("selected", c.dataset.id === intent.id));
            this.renderDetail();
          }
        };

        listContainer.append(card);
      });
    }

    renderRoundCards() {
      const listContainer = this.container.querySelector("#op-master-list");
      listContainer.replaceChildren();

      const items = this.roundPaginator.getPageItems();

      if (items.length === 0) {
        listContainer.innerHTML = `<p class="hint empty-cell">暂无轮次记录。</p>`;
        return;
      }

      items.forEach((round, idx) => {
        const report = humanizeRoundReport(round, this.rounds.length - ((this.roundPaginator.currentPage - 1) * this.roundPaginator.pageSize + idx));
        const card = document.createElement("div");
        card.className = "op-round-card";

        card.innerHTML = `
          <div class="op-round-header">
            <span class="op-round-title">${escapeHtml(report.title)}</span>
            <span class="op-tag-status ${report.statusInfo.className}">${report.statusInfo.label}</span>
          </div>
          <p class="op-round-narrative">${escapeHtml(report.narrative)}</p>
          ${report.interactionOnly ? '<button type="button" data-open-inbox>查看互动收件箱</button>' : `<a href="#automation/round/${encodeURIComponent(round.id)}">查看本轮视频</a>`}
          <div class="op-round-metrics-grid">
            <div class="op-metric-chip">
              <span class="op-metric-k">读取请求</span>
              <strong class="op-metric-v">${report.metrics.reads} 次</strong>
            </div>
            <div class="op-metric-chip">
              <span class="op-metric-k">模型调用</span>
              <strong class="op-metric-v">${report.metrics.modelCalls} 次</strong>
            </div>
            <div class="op-metric-chip">
              <span class="op-metric-k">评论草稿</span>
              <strong class="op-metric-v">${report.metrics.drafts ?? "—"}</strong>
            </div>
            <div class="op-metric-chip">
              <span class="op-metric-k">Token 消耗</span>
              <strong class="op-metric-v">${report.metrics.tokens}</strong>
            </div>
          </div>
        `;

        const inboxButton = card.querySelector("[data-open-inbox]");
        if (inboxButton) inboxButton.onclick = () => this.switchTab("inbox");

        listContainer.append(card);
      });
    }

    renderTaskRows() {
      const listContainer = this.container.querySelector("#op-master-list");
      listContainer.replaceChildren();

      const items = this.taskPaginator.getPageItems();

      if (items.length === 0) {
        listContainer.innerHTML = `<p class="hint empty-cell">暂无符合筛选的浏览任务。</p>`;
        return;
      }

      const tableWrapper = document.createElement("div");
      tableWrapper.className = "table-scroll";
      const table = document.createElement("table");
      table.innerHTML = `
        <thead>
          <tr>
            <th>视频 / 来源</th>
            <th>处理进度</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody></tbody>
      `;

      const tbody = table.querySelector("tbody");
      items.forEach(task => {
        const tr = document.createElement("tr");
        if (task.id === this.selectedTaskId) tr.className = "selected-row";

        const title = task.title || task.metadata?.title || task.bvid || `任务 ${task.id?.slice(0, 8)}`;
        const statusInfo = humanizeStatus(task.status, task.material_mode);
        const reasonText = humanizeTaskReason(task);

        tr.innerHTML = `
          <td>
            <button type="button" class="comment-link" style="text-align:left;font-weight:600;">${escapeHtml(title)}</button>
            <small class="cell-sub">${escapeHtml(task.bvid || "—")} · ${formatRelativeTime(task.created_at)}</small>
          </td>
          <td>${escapeHtml(reasonText)}</td>
          <td><span class="op-tag-status ${statusInfo.className}">${statusInfo.label}</span></td>
        `;

        tr.querySelector(".comment-link").onclick = () => {
          this.selectedTaskId = task.id;
          this.renderTaskRows();
          this.renderDetail();
        };

        tbody.append(tr);
      });

      tableWrapper.append(table);
      listContainer.append(tableWrapper);
    }

    renderInboxList() {
      const listContainer = this.container.querySelector("#op-master-list");
      listContainer.replaceChildren();

      if (this.inbox.length === 0) {
        listContainer.innerHTML = `
          <div class="op-inbox-zero">
            <div class="op-zero-icon">📭</div>
            <h4>暂无收到互动或 @ 提及</h4>
            <p class="hint">全渠道运营读取到的公开回复和 @ 提及会出现在这里。</p>
          </div>
        `;
        return;
      }

      this.inbox.forEach(event => {
        const item = document.createElement("div");
        item.className = "op-inbox-item";
        const payload = event.payload || {};
        const sender = event.sender_name || event.author_name || payload.author_name || "B 站观众";
        const timeAgo = formatRelativeTime(event.timestamp || event.created_at);
        const text = event.source_text || payload.source_text || payload.text || "暂无公开原文";

        item.innerHTML = `
          <div class="op-card-header">
            <strong>${escapeHtml(sender)}</strong>
            <span class="op-card-time">${escapeHtml(timeAgo)} · ${escapeHtml(humanizeStatus(event.status).label)}</span>
          </div>
          <p class="quoted-text" style="margin: 6px 0;">${escapeHtml(text)}</p>
          <small class="hint">${event.resource_title ? `来源视频：${escapeHtml(event.resource_title)}` : ""}</small>
        `;
        if (event.continuation) {
          const continuation = document.createElement("section");
          item.append(continuation);
          new OperationContinuationView(continuation, this.api, this.notify, this.refresh)
            .render(event.continuation, { channel: "browsing", localId: `event:${event.id}`, entityStatus: event.status });
        }
        listContainer.append(item);
      });
    }

    renderDetail() {
      const detailPane = this.container.querySelector("#op-detail-pane");
      if (!detailPane) return;

      if (this.activeTab === "intents") {
        this.renderIntentDetail(detailPane);
      } else if (this.activeTab === "tasks") {
        this.renderTaskDetail(detailPane);
      } else {
        this.renderedTab = this.activeTab;
        this.renderedTaskId = null;
        detailPane.innerHTML = `
          <div class="op-empty-state">
            <div class="op-empty-icon">📊</div>
            <h3>运行记录与简报概览</h3>
            <p class="hint">左侧正在展示全量自主巡航与互动记录，您可翻页浏览或展开技术明细。</p>
          </div>
        `;
      }
    }

    renderIntentDetail(detailPane) {
      const intent = this.intents.find(item => item.id === this.selectedIntentId);
      this.renderedTab = "intents";
      this.renderedIntent = JSON.stringify(intent);
      if (!intent) {
        detailPane.innerHTML = '<div class="op-empty-state"><h3>选择一条点赞或收藏记录</h3></div>';
        return;
      }
      const target = intent.target || {};
      const bvid = target.bvid || "";
      const status = humanizeStatus(intent.status);
      const editable = ["pending_review", "approved"].includes(intent.status);
      detailPane.innerHTML = `
        <div class="op-inspector-content">
          <div class="panel-heading"><h3>${escapeHtml(humanizeKind(intent.kind).label)}</h3>
            <span class="op-tag-status ${status.className}">${escapeHtml(status.label)}</span></div>
          <p>${escapeHtml(target.title || bvid)}</p>
          ${bvid ? `<a href="https://www.bilibili.com/video/${encodeURIComponent(bvid)}" target="_blank" rel="noreferrer">在 B 站查看来源</a>` : ""}
          <blockquote class="quoted-text">${escapeHtml(intent.text)}</blockquote>
          <p class="hint">${escapeHtml(this.intentReviewNote(intent))}</p>
          <div class="actions">
            ${intent.status === "pending_review" ? '<button type="button" data-review="approve" class="primary">审核通过</button>' : ""}
            ${editable ? '<button type="button" data-review="reject">拒绝</button>' : ""}
            ${intent.status === "approved" ? '<button type="button" data-publish>提交已审核操作</button>' : ""}
          </div>
          <p class="error-text" data-intent-error role="status">${escapeHtml(intent.error || "")}</p>
          ${intent.receipt ? `<details class="subsection"><summary>平台回执</summary><pre class="operation-revision-text">${escapeHtml(JSON.stringify(intent.receipt, null, 2))}</pre></details>` : ""}
          ${intent.round_id ? `<a href="#automation/round/${encodeURIComponent(intent.round_id)}">查看来源轮次</a>` : ""}
          <section data-intent-continuation hidden></section>
        </div>`;
      const controls = [...detailPane.querySelectorAll("[data-review], [data-publish]")];
      for (const button of controls) button.onclick = async () => {
        if (this.intentPending) return;
        this.intentPending = true;
        for (const control of controls) control.disabled = true;
        try {
          const action = button.dataset.review;
          const path = action ? "review" : "publish";
          await this.api(`/api/automation/intents/${encodeURIComponent(intent.id)}/${path}`,
            { method: "POST", body: action ? { action } : undefined });
        } catch (error) {
          const feedback = detailPane.querySelector("[data-intent-error]");
          if (this.selectedIntentId === intent.id && feedback) feedback.textContent = error.message;
        } finally {
          this.intentPending = false;
          await this.refresh();
          for (const control of controls) control.disabled = false;
        }
      };
      new OperationContinuationView(detailPane.querySelector("[data-intent-continuation]"), this.api, this.notify, this.refresh)
        .render(intent.continuation, { channel: "browsing", localId: intent.id, entityStatus: intent.status });
    }

    renderTaskDetail(detailPane) {
      const task = this.tasks.find(t => t.id === this.selectedTaskId);
      const opened = this.renderedTaskId === task?.id ? new Set([...detailPane.querySelectorAll("details")].filter(item => item.open).map(item => item.querySelector("summary")?.textContent)) : new Set();
      this.renderedTab = "tasks";
      this.renderedTaskId = task?.id || null;
      this.renderedTaskStatus = JSON.stringify([task?.status, task?.cache_status, task?.updated_at]);
      if (!task) {
        detailPane.innerHTML = `
          <div class="op-empty-state">
            <div class="op-empty-icon">👈</div>
            <h3>请选择左侧浏览任务</h3>
            <p class="hint">查看视频内容证据与大肥鱼的思考细节。</p>
          </div>
        `;
        return;
      }

      const title = task.title || task.metadata?.title || task.bvid || `任务 ${task.id}`;
      const statusInfo = humanizeStatus(task.status, task.material_mode);
      const bvidUrl = task.bvid ? `https://www.bilibili.com/video/${encodeURIComponent(task.bvid)}` : "#";
      const tracks = task.subtitle_tracks || [];
      const evidence = task.evidence || [];
      const drafts = [task.intent_id, ...(task.reply_intent_ids || [])].filter(Boolean);
      const coverageLabels = {
        all_available_cc: "已读可用字幕",
        partial: "部分分 P 有材料",
        sampled_visual: "已读取抽样画面",
      };
      const coverage = coverageLabels[task.coverage] || task.coverage;
      const material = { subtitle_visual: "字幕＋画面", subtitle: "仅字幕", visual: "仅画面", audio: "语音转写", unavailable: "取材失败" }[task.material_mode];

      detailPane.innerHTML = `
        <div class="op-inspector-content">
          <div class="op-inspector-header">
            <div class="op-inspector-meta">
              <span class="op-tag-status ${statusInfo.className}">${statusInfo.label}</span>
              <span class="op-inspector-time">${formatRelativeTime(task.created_at)}</span>
            </div>
            <div class="op-inspector-actions">
              ${task.bvid ? `<a href="${bvidUrl}" target="_blank" rel="noreferrer" class="op-link-external">🎬 在 B 站打开 ↗</a>` : ""}
            </div>
          </div>
          <div class="op-video-context-card">
            <h3 class="op-video-title">${escapeHtml(title)}</h3>
            <p class="hint">${escapeHtml(task.bvid || "—")}</p>
          </div>
          <div class="op-ai-reason-card">
            <h4>处理进度与内容结果</h4>
            ${material ? `<p>${escapeHtml(material)}</p>` : ""}
            <p class="op-reason-text">${escapeHtml(task.material_mode === "unavailable" && task.error ? task.error : humanizeTaskReason(task))}</p>
            ${task.visual_error ? `<p class="hint">画面：${escapeHtml(task.visual_error)}</p>` : ""}
          </div>
          ${task.consecutive_failures ? `<p class="hint">连续失败 ${escapeHtml(task.consecutive_failures)} 次${task.consecutive_failures === 3 ? " · 已跳过" : ""}</p>` : ""}
          <details class="subsection"><summary>分 P 覆盖与引用材料</summary>
            <p class="hint">${task.cache_status === "missing" ? "原材料已失效或清理" : escapeHtml(coverage || "暂无覆盖信息")}</p>
            ${tracks.map(track => `<p class="hint">CID ${escapeHtml(track.cid)} · ${escapeHtml(track.language || "—")} · ${track.is_ai == null ? "类型未声明" : track.is_ai ? "平台 AI 字幕" : "人工字幕"} · ${escapeHtml(track.track_id || "无字幕轨")}</p>`).join("")}
            ${task.missing_parts?.length ? `<p class="hint">缺失分 P · ${escapeHtml(JSON.stringify(task.missing_parts))}</p>` : ""}
            ${evidence.map(segment => `<p class="hint">${segment.id?.startsWith("frame:") ? "画面观察" : "字幕原文"} · CID ${escapeHtml(segment.cid || "—")} · ${escapeHtml(segment.start ?? segment.time ?? segment.timestamp ?? "—")} 秒</p><blockquote class="quoted-text">${escapeHtml(segment.text || segment.observation || "材料正文不可用")}</blockquote>`).join("") || '<p class="hint">暂无可展示的引用片段。</p>'}
          </details>
          ${drafts.map((id, index) => `<p><a href="#comments/browsing/${encodeURIComponent(id)}">查看关联回应 ${index + 1}</a></p>`).join("")}
          <section data-task-continuation hidden></section>
          ${task.autonomous ? "" : `<div class="actions" style="margin-top: 16px;">
            <button type="button" class="op-btn-action primary" id="op-btn-reread">
              🎬 重新深度分析视频并生成草稿
            </button>
          </div>`}
        </div>
      `;

      new OperationContinuationView(detailPane.querySelector("[data-task-continuation]"), this.api, this.notify, this.refresh)
        .render(task.autonomous ? null : task.continuation,
          { channel: "browsing", localId: task.id, entityStatus: task.status });
      for (const item of detailPane.querySelectorAll("details")) if (opened.has(item.querySelector("summary")?.textContent)) item.open = true;
      const rereadBtn = detailPane.querySelector("#op-btn-reread");
      if (rereadBtn) {
        rereadBtn.onclick = async () => {
          rereadBtn.disabled = true;
          try {
            await this.api(`/api/automation/tasks/${encodeURIComponent(task.id)}/reread`, { method: "POST" });
            if (this.notify) this.notify("已排队重新分析；生成结果将存为待审核草稿。");
            await this.refresh();
          } catch (e) {
            rereadBtn.disabled = false;
            if (this.notify) this.notify(e.message, true);
          }
        };
      }
    }

  }

  return OperationsWorkbench;
});
