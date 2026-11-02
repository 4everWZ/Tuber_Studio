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

    this.value = null;
    this.tasks = [];
    this.rounds = [];
    this.intents = [];
    this.inbox = [];
    this.pending = new Set();
    this.dirty = false;
    this.refreshing = false;

    // Build Host Container
    this.root.innerHTML = `
      <div id="op-workbench-mount"></div>
      <details class="panel" style="margin-top: 18px;" id="op-legacy-recovery-box">
        <summary style="font-weight:600;font-size:12px;color:var(--muted);cursor:pointer;">🛡️ 异常中断与进程恢复策略 (高级)</summary>
        <div style="padding-top:10px;" class="automation-control"></div>
      </details>
    `;

    // Instantiate Modern Workbench
    const mount = this.root.querySelector("#op-workbench-mount");
    if (typeof OperationsWorkbench !== "undefined") {
      this.workbench = new OperationsWorkbench(mount, api, notify);
    } else {
      console.warn("OperationsWorkbench class not found, check script inclusion order.");
    }

    // Attach Recovery Policy to technical container
    const recoveryContainer = this.root.querySelector(".automation-control");
    if (typeof OperationRecoveryPolicyView !== "undefined" && recoveryContainer) {
      this.recoveryPolicy = new OperationRecoveryPolicyView(recoveryContainer, api, notify);
    }
  }

  get draftDirty() {
    return this.workbench ? this.workbench.draftDirty : false;
  }

  set draftDirty(val) {
    if (this.workbench) this.workbench.draftDirty = Boolean(val);
  }

  array(value) {
    return Array.isArray(value) ? value : value?.items || [];
  }

  decode(value) {
    if (typeof value !== "string") return value;
    try { return JSON.parse(value); } catch { return value; }
  }

  async refresh() {
    if (this.refreshing || this.root.closest("[hidden]")) return;
    this.refreshing = true;
    try {
      const results = await Promise.allSettled([
        this.api("/api/automation"),
        this.api("/api/automation/tasks"),
        this.api("/api/automation/rounds"),
        this.api("/api/automation/intents"),
        this.api("/api/automation/inbox")
      ]);

      if (results[0].status === "fulfilled") this.value = results[0].value;
      if (results[1].status === "fulfilled") this.tasks = this.array(results[1].value);
      if (results[2].status === "fulfilled") this.rounds = this.array(results[2].value);
      if (results[3].status === "fulfilled") this.intents = this.array(results[3].value);
      if (results[4].status === "fulfilled") {
        const inboxData = results[4].value;
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

      if (results[0].status === "fulfilled" && this.recoveryPolicy && !this.recoveryPolicyStarted) {
        this.recoveryPolicyStarted = true;
        await this.recoveryPolicy.load();
      }
    } catch (err) {
      console.error("AutomationPanel refresh failed:", err);
    } finally {
      this.refreshing = false;
    }
  }

  // Legacy stubs for backwards compatibility if referenced
  render() {
    if (this.workbench) {
      this.workbench.updateData({
        intents: this.intents,
        tasks: this.tasks,
        rounds: this.rounds,
        inbox: this.inbox,
      });
    }
  }

  selectTab(tab) {
    if (this.workbench) {
      this.workbench.switchTab(tab);
    }
  }
}

window.AutomationPanel = AutomationPanel;
