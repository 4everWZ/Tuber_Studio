/**
 * Operations Workbench Component for Tuber Studio
 * Implements a modern Master-Detail split-view (VSCode / Antigravity style),
 * with Inbox Zero candidate review, humanized reports, and responsive pagination.
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

  const { formatRelativeTime, humanizeKind, humanizeStatus, humanizeReason, humanizeRoundReport } = Formatters;

  class OperationsWorkbench {
    constructor(container, api, notify) {
      this.container = container;
      this.api = api;
      this.notify = notify;

      // State
      this.activeTab = "intents"; // "intents" | "inbox" | "tasks" | "rounds"
      this.intentFilter = "pending_review"; // Default to Inbox Zero!
      this.taskFilter = "";

      this.intents = [];
      this.tasks = [];
      this.rounds = [];
      this.inbox = [];

      this.selectedIntentId = null;
      this.selectedTaskId = null;
      this.draftDirty = false;

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
        onPageChange: () => this.renderTaskRows(),
      });

      this.initLayout();
    }

    initLayout() {
      this.container.innerHTML = `
        <div class="op-workbench-container">
          <!-- Segmented Master Navigation Bar -->
          <div class="op-workbench-nav">
            <div class="op-segmented-control" role="tablist">
              <button type="button" class="op-nav-btn active" data-tab="intents">
                <span class="op-nav-icon">📝</span>
                <span class="op-nav-label">待办审核</span>
                <span class="op-badge-counter" id="op-counter-intents" hidden>0</span>
              </button>
              <button type="button" class="op-nav-btn" data-tab="inbox">
                <span class="op-nav-icon">📥</span>
                <span class="op-nav-label">互动收件箱</span>
                <span class="op-badge-counter" id="op-counter-inbox" hidden>0</span>
              </button>
              <button type="button" class="op-nav-btn" data-tab="tasks">
                <span class="op-nav-icon">🧭</span>
                <span class="op-nav-label">浏览历史</span>
              </button>
              <button type="button" class="op-nav-btn" data-tab="rounds">
                <span class="op-nav-icon">📑</span>
                <span class="op-nav-label">巡航简报</span>
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

      this.container.querySelectorAll(".op-nav-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === tab);
      });

      this.renderMasterSubbar();
      this.renderMasterList();
      this.renderDetail();
    }

    updateData({ intents, tasks, rounds, inbox }) {
      if (intents) this.intents = intents;
      if (tasks) this.tasks = tasks;
      if (rounds) this.rounds = rounds;
      if (inbox) this.inbox = inbox;

      // Update counters
      const pendingIntentsCount = this.intents.filter(i => i.status === "pending_review").length;
      const counterIntents = this.container.querySelector("#op-counter-intents");
      if (counterIntents) {
        counterIntents.textContent = pendingIntentsCount;
        counterIntents.hidden = pendingIntentsCount === 0;
      }

      const pendingInboxCount = this.inbox.filter(i => i.status === "pending" || i.status === "received").length;
      const counterInbox = this.container.querySelector("#op-counter-inbox");
      if (counterInbox) {
        counterInbox.textContent = pendingInboxCount;
        counterInbox.hidden = pendingInboxCount === 0;
      }

      // Automatically select first pending intent if nothing selected or current item disappeared
      if (this.activeTab === "intents") {
        const filtered = this.getFilteredIntents();
        if (!this.selectedIntentId || !filtered.some(i => i.id === this.selectedIntentId)) {
          this.selectedIntentId = filtered.length > 0 ? filtered[0].id : null;
        }
      }

      this.renderMasterSubbar();
      this.renderMasterList();
      this.renderDetail();
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
            this.renderMasterSubbar();
            this.renderMasterList();
            this.renderDetail();
          };
          pills.append(pill);
        });

        subbar.append(pills);
      } else if (this.activeTab === "tasks") {
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
          this.taskPaginator.currentPage = 1;
          this.renderMasterList();
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
            <h4>待审核队列已全部清空！</h4>
            <p class="hint">大肥鱼正在自主巡航中，一旦发现优质视频并生成草稿，会实时出现在这里。</p>
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
          <div class="op-card-title" title="${videoTitle}">${videoTitle}</div>
          <div class="op-card-snippet">${intent.text || "（点赞或收藏操作，无需文字）"}</div>
          <div class="op-card-footer">
            <span class="op-card-reason hint">💡 ${humanizeReason(intent.reason)}</span>
          </div>
        `;

        card.onclick = () => {
          if (this.selectedIntentId !== intent.id) {
            this.selectedIntentId = intent.id;
            this.draftDirty = false;
            this.renderIntentCards();
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
        listContainer.innerHTML = `<p class="hint empty-cell">暂无巡航记录，可以点击上方「单轮运营预览」开启巡航。</p>`;
        return;
      }

      items.forEach((round, idx) => {
        const report = humanizeRoundReport(round, this.rounds.length - ((this.roundPaginator.currentPage - 1) * this.roundPaginator.pageSize + idx));
        const card = document.createElement("div");
        card.className = "op-round-card";

        card.innerHTML = `
          <div class="op-round-header">
            <span class="op-round-title">${report.title}</span>
            <span class="op-tag-status ${report.statusInfo.className}">${report.statusInfo.label}</span>
          </div>
          <p class="op-round-narrative">${report.narrative}</p>
          <div class="op-round-metrics-grid">
            <div class="op-metric-chip">
              <span class="op-metric-k">浏览视频</span>
              <strong class="op-metric-v">${report.metrics.reads} 篇</strong>
            </div>
            <div class="op-metric-chip">
              <span class="op-metric-k">模型调用</span>
              <strong class="op-metric-v">${report.metrics.modelCalls} 次</strong>
            </div>
            <div class="op-metric-chip">
              <span class="op-metric-k">沉淀草稿</span>
              <strong class="op-metric-v">${report.metrics.topLevels} 条</strong>
            </div>
            <div class="op-metric-chip">
              <span class="op-metric-k">Token 消耗</span>
              <strong class="op-metric-v">${report.metrics.tokens}</strong>
            </div>
          </div>
          <details class="op-tech-details">
            <summary>⚙️ 技术底层明细 (展开排障)</summary>
            <pre class="code-small">${JSON.stringify(round, null, 2)}</pre>
          </details>
        `;

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
            <th>大肥鱼判断</th>
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
        const statusInfo = humanizeStatus(task.status);
        const reasonText = humanizeReason(task.reason || task.error_kind || task.error || task.payload?.reason);

        tr.innerHTML = `
          <td>
            <button type="button" class="comment-link" style="text-align:left;font-weight:600;">${title}</button>
            <small class="cell-sub">${task.bvid || "—"} · ${formatRelativeTime(task.created_at)}</small>
          </td>
          <td>${reasonText}</td>
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
            <p class="hint">当观众在评论区回复大肥鱼或艾特大肥鱼时，会在此实时收件。</p>
          </div>
        `;
        return;
      }

      this.inbox.forEach(event => {
        const item = document.createElement("div");
        item.className = "op-inbox-item";
        const sender = event.sender_name || event.author_name || "B站观众";
        const timeAgo = formatRelativeTime(event.created_at);
        const text = event.text || event.content || event.source_text || "（互动消息）";

        item.innerHTML = `
          <div class="op-card-header">
            <strong>${sender}</strong>
            <span class="op-card-time">${timeAgo}</span>
          </div>
          <p class="quoted-text" style="margin: 6px 0;">${text}</p>
          <small class="hint">${event.resource_title ? `来源视频：${event.resource_title}` : ""}</small>
        `;
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
      const intent = this.intents.find(i => i.id === this.selectedIntentId);
      if (!intent) {
        detailPane.innerHTML = `
          <div class="op-empty-state">
            <div class="op-empty-icon">👈</div>
            <h3>请选择左侧待审核草稿</h3>
            <p class="hint">点击左侧列表中的草稿卡片，在此处审查大肥鱼的第一人称拟发表发言。</p>
          </div>
        `;
        return;
      }

      let target = {};
      if (typeof intent.target === "string") {
        try { target = JSON.parse(intent.target); } catch (e) {}
      } else if (typeof intent.target === "object") {
        target = intent.target || {};
      }

      const bvid = intent.bvid || target.bvid || intent.source_id || "";
      const relatedTask = this.tasks.find(t => t.bvid === bvid);
      const videoTitle = relatedTask?.title || relatedTask?.metadata?.title || target.title || bvid;
      const bvidUrl = bvid ? `https://www.bilibili.com/video/${bvid}` : "#";

      const kindInfo = humanizeKind(intent.kind);
      const statusInfo = humanizeStatus(intent.status);
      const isPending = intent.status === "pending_review";
      const isApproved = intent.status === "approved";
      const isPublished = intent.status === "published";

      detailPane.innerHTML = `
        <div class="op-inspector-content">
          <!-- Top Action Bar -->
          <div class="op-inspector-header">
            <div class="op-inspector-meta">
              <span class="op-tag-kind">${kindInfo.label}</span>
              <span class="op-tag-status ${statusInfo.className}">${statusInfo.label}</span>
              <span class="op-inspector-time">${formatRelativeTime(intent.created_at)}</span>
            </div>
            <div class="op-inspector-actions">
              <button type="button" class="op-btn-action primary" id="op-btn-approve" ${!isPending ? "disabled" : ""}>
                ✓ 审核通过
              </button>
              <button type="button" class="op-btn-action success" id="op-btn-publish" ${!isApproved ? "disabled" : ""}>
                🚀 立即发布到B站
              </button>
              <button type="button" class="op-btn-action danger" id="op-btn-reject" ${intent.status === "rejected" || isPublished ? "disabled" : ""}>
                ✕ 放弃草稿
              </button>
            </div>
          </div>

          <!-- Target Video Context Card -->
          <div class="op-video-context-card">
            <div class="op-video-info">
              <span class="op-context-label">关联视频</span>
              <h3 class="op-video-title">${videoTitle}</h3>
              <div class="op-video-links">
                ${bvid ? `<a href="${bvidUrl}" target="_blank" rel="noreferrer" class="op-link-external">🎬 在 B 站打开原视频 (${bvid}) ↗</a>` : ""}
                ${target.parent && target.parent !== "0" ? `<span class="op-badge-thread">💬 楼内回复目标评论 #${target.parent}</span>` : ""}
              </div>
            </div>
          </div>

          <!-- AI Motivation & Humanized Evidence -->
          <div class="op-ai-reason-card">
            <h4>💡 大肥鱼为什么想发言？</h4>
            <p class="op-reason-text">${humanizeReason(intent.reason)}</p>
          </div>

          <!-- Draft Editor Section -->
          <div class="op-draft-editor-box">
            <div class="op-editor-header">
              <label for="op-intent-editor"><strong>大肥鱼的发言台词（第一人称口吻）</strong></label>
              <span class="hint" id="op-editor-status">已就绪</span>
            </div>
            <textarea id="op-intent-editor" rows="6" ${isPublished ? "disabled" : ""}>${intent.text || ""}</textarea>
            <div class="op-editor-tip hint">
              提示：您可以直接在上方修改台词文字，确认无误后点击顶部「审核通过」或「立即发布」。
            </div>
          </div>

          <!-- Collapsible Technical Trace / Receipts -->
          <details class="op-tech-details" style="margin-top: 20px;">
            <summary>⚙️ 技术证据引用与平台可见性回执</summary>
            <pre class="code-small">${JSON.stringify({
              target: target,
              evidence: intent.evidence,
              receipt: intent.receipt,
              visibility: intent.visibility
            }, null, 2)}</pre>
          </details>
        </div>
      `;

      const editor = detailPane.querySelector("#op-intent-editor");
      const editorStatus = detailPane.querySelector("#op-editor-status");

      if (editor) {
        editor.oninput = () => {
          this.draftDirty = true;
          if (editorStatus) editorStatus.textContent = "有未保存的台词修改";
        };
      }

      // Button Handlers
      const approveBtn = detailPane.querySelector("#op-btn-approve");
      if (approveBtn) {
        approveBtn.onclick = async () => {
          const newText = editor ? editor.value : intent.text;
          approveBtn.disabled = true;
          try {
            await this.api(`/api/automation/intents/${encodeURIComponent(intent.id)}/review`, {
              method: "POST",
              body: { action: "approve", text: newText },
            });
            this.draftDirty = false;
            if (this.notify) this.notify("✓ 草稿已通过审核！可以点击立即发布。");
            await this.refresh();
          } catch (e) {
            approveBtn.disabled = false;
            if (this.notify) this.notify(e.message, true);
          }
        };
      }

      const publishBtn = detailPane.querySelector("#op-btn-publish");
      if (publishBtn) {
        publishBtn.onclick = async () => {
          publishBtn.disabled = true;
          try {
            await this.api(`/api/automation/intents/${encodeURIComponent(intent.id)}/publish`, {
              method: "POST",
            });
            if (this.notify) this.notify("🚀 已成功提交发布到 B 站！");
            await this.refresh();
          } catch (e) {
            publishBtn.disabled = false;
            if (this.notify) this.notify(e.message, true);
          }
        };
      }

      const rejectBtn = detailPane.querySelector("#op-btn-reject");
      if (rejectBtn) {
        rejectBtn.onclick = async () => {
          if (!confirm("确认放弃并拒绝此条草稿吗？")) return;
          rejectBtn.disabled = true;
          try {
            await this.api(`/api/automation/intents/${encodeURIComponent(intent.id)}/review`, {
              method: "POST",
              body: { action: "reject", text: editor ? editor.value : intent.text },
            });
            this.draftDirty = false;
            if (this.notify) this.notify("已放弃该草稿。");
            await this.refresh();
          } catch (e) {
            rejectBtn.disabled = false;
            if (this.notify) this.notify(e.message, true);
          }
        };
      }
    }

    renderTaskDetail(detailPane) {
      const task = this.tasks.find(t => t.id === this.selectedTaskId);
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
      const statusInfo = humanizeStatus(task.status);
      const bvidUrl = task.bvid ? `https://www.bilibili.com/video/${task.bvid}` : "#";

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
            <h3 class="op-video-title">${title}</h3>
            <p class="hint">${task.bvid || "—"}</p>
          </div>
          <div class="op-ai-reason-card">
            <h4>💡 视频理解与决策</h4>
            <p class="op-reason-text">${humanizeReason(task.reason || task.payload?.reason)}</p>
          </div>
          <div class="actions" style="margin-top: 16px;">
            <button type="button" class="op-btn-action primary" id="op-btn-reread">
              🎬 重新深度分析视频并生成草稿
            </button>
          </div>
          <details class="op-tech-details" style="margin-top: 20px;">
            <summary>⚙️ 技术底层明细</summary>
            <pre class="code-small">${JSON.stringify(task, null, 2)}</pre>
          </details>
        </div>
      `;

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

    async refresh() {
      const [intentsRes, tasksRes, roundsRes, inboxRes] = await Promise.allSettled([
        this.api("/api/automation/intents"),
        this.api("/api/automation/tasks"),
        this.api("/api/automation/rounds"),
        this.api("/api/automation/inbox")
      ]);

      const intents = intentsRes.status === "fulfilled" ? intentsRes.value : [];
      const tasks = tasksRes.status === "fulfilled" ? tasksRes.value : [];
      const rounds = roundsRes.status === "fulfilled" ? roundsRes.value : [];
      const inbox = inboxRes.status === "fulfilled" ? inboxRes.value : [];

      this.updateData({ intents, tasks, rounds, inbox });
    }
  }

  return OperationsWorkbench;
});
