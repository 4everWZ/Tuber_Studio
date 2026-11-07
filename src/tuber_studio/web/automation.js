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
    this.roundId = null;
    this.refreshedAt = 0;

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
      if (roundId) this.workbench?.switchTab("tasks");
      this.workbench?.setRoundFilter(roundId);
    }
    if (taskId && this.workbench) {
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

    } catch (err) {
      console.error("AutomationPanel refresh failed:", err);
    } finally {
      this.refreshing = false;
      if (round !== this.roundId && !this.root.closest("[hidden]")) void this.refresh();
    }
  }

}

window.AutomationPanel = AutomationPanel;
