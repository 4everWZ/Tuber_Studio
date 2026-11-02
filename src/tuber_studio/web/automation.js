"use strict";

class AutomationPanel {
  constructor(root, api, notify) {
    this.root = root;
    this.api = api;
    this.notify = notify;
    this.value = null;
    this.tasks = [];
    this.rounds = [];
    this.intents = [];
    this.inbox = {items: [], private_count: 0};
    this.pending = new Set();
    this.dirty = false;
    this.draftDirty = false;
    this.selectedTask = null;
    this.selectedInbox = null;
    this.selectedIntent = null;
    this.tab = "tasks";
    this.refreshing = false;
    this.confirming = false;
    root.innerHTML = `
      <div class="page-heading"><div><p class="eyebrow">依据内容，自主选择互动</p><h1>自主浏览与互动</h1></div>
        <div class="actions"><button type="button" data-refresh>刷新</button><button type="button" data-preview>只读运行预览</button><button type="button" data-round-stop>停止本轮</button><button type="button" data-emergency class="danger">紧急停止所有自动收发</button></div></div>
      <p data-feedback role="status" class="hint" aria-live="polite"></p><p data-error role="alert" class="error-text"></p>
      <section class="panel automation-control" aria-labelledby="automation-control-title">
        <div class="panel-heading"><h2 id="automation-control-title">自动化控制</h2><span data-state class="badge">正在读取</span></div>
        <dl data-identity class="description-list"></dl><p data-overlay-note class="hint"></p>
        <div class="actions"><button type="button" data-preflight>账号与能力预检</button><button type="button" data-enable class="primary">开启自动化</button><button type="button" data-disable>关闭自动化并恢复基础设置</button><button type="button" data-reset hidden>解除紧急停止并恢复基础开关</button></div>
        <ul data-blockers class="automation-blockers"></ul><p data-vision class="hint"></p>
        <section data-confirm hidden class="automation-confirm" aria-labelledby="automation-confirm-title">
          <h3 id="automation-confirm-title">确认本次自动化范围</h3><p data-confirm-summary></p><ul data-confirm-features></ul>
          <p class="hint">开启后，这个模式中新产生且通过校验的候选可以直接提交到平台。历史草稿不会追发；视觉、动态发文、直播和点歌保持各自设置。普通关闭恢复基础开关，紧急停止阻止全部自动收发。</p>
          <div class="actions"><button type="button" data-confirm-enable class="primary">启用以上自动行为</button><button type="button" data-confirm-cancel>取消</button></div>
        </section>
        <details class="subsection"><summary>基础配置与实际生效配置</summary><div data-config-layers class="automation-layers"></div></details>
      </section>
      <details class="panel automation-settings"><summary>浏览来源、独立功能与资源预算</summary>
        <form data-settings>
          <p data-settings-note class="hint">独立功能默认关闭。自动化总开关通过覆盖层启用必要功能，不覆盖个人基础设置。</p>
          <fieldset class="automation-fieldset"><legend>发现来源</legend><div class="policy-checks">
            <label><input type="checkbox" data-source value="recommend">首页推荐</label><label><input type="checkbox" data-source value="search">关键词搜索</label><label><input type="checkbox" data-source value="popular">热门</label><label><input type="checkbox" data-source value="specified">指定视频</label>
          </div><div class="form-grid"><label>用户关键词（每行一个）<textarea name="keywords" rows="3" data-type="lines"></textarea></label><label>指定视频（每行一个 BV 号）<textarea name="watch_videos" rows="3" data-type="lines"></textarea></label></div>
          <p class="hint">没有有效关键词时使用当前账号首页推荐，不用热门或内置词代替。显式选中热门或指定视频时按所选来源处理。</p></fieldset>
          <fieldset class="automation-fieldset"><legend>个人基础开关</legend><div class="policy-checks automation-checks">
            <label><input name="discovery_enabled" type="checkbox">自动发现</label><label><input name="autonomous_topics" type="checkbox">允许 OC 自主选题</label><label><input name="top_level_enabled" type="checkbox">主动顶层留言</label><label><input name="reply_enabled" type="checkbox">评论回复</label><label><input name="mentions_enabled" type="checkbox">读取 @ 通知</label><label><input name="mention_reply_enabled" type="checkbox">回复 @ 通知</label><label><input name="replies_enabled" type="checkbox">读取收到的回复</label><label><input name="visual_enabled" type="checkbox">实验视觉（需可用 Provider）</label>
          </div><p class="hint">视频自动发言始终需要有效 CC 或获准的视觉证据。独立功能生成待审核草稿。</p>
          <p class="hint">私信读取、回复和评论转私信统一在<a href="#messages">私信工作台</a>设置及查看正文。自动化总开关会覆盖其基础开关；普通关闭总开关后恢复最新个人设置。</p><p data-message-settings-summary class="hint"></p></fieldset>
          <details class="subsection"><summary>廉价筛选与字幕语言</summary><div class="form-grid">
            <label>排除词（每行一个）<textarea name="exclude_words" rows="2" data-type="lines"></textarea></label><label>字幕语言优先顺序（每行一个）<textarea name="languages" rows="2" data-type="lines"></textarea></label>
            <label>允许的作者 UID（空为不限）<textarea name="allowed_author_uids" rows="2" data-type="lines"></textarea></label><label>排除的作者 UID<textarea name="excluded_author_uids" rows="2" data-type="lines"></textarea></label>
            <label>允许的分区 ID（空为不限）<textarea name="allowed_categories" rows="2" data-type="lines"></textarea></label><div class="form-grid"><label>最短时长（秒）<input name="min_duration_seconds" type="number" min="0" step="1"></label><label>最长时长（秒）<input name="max_duration_seconds" type="number" min="1" max="86400" step="1"></label></div>
            <label>发布日期不早于（本机时区，空为不限）<input name="published_after" type="datetime-local"></label><label>发布日期不晚于（本机时区，空为不限）<input name="published_before" type="datetime-local"></label>
          </div></details>
          <details class="subsection"><summary>并发、频率与每轮预算</summary><div class="automation-fields">
            <label>准备 Worker 数<input name="workers" type="number" min="1" max="8" step="1"></label><label>候选上限<input name="candidate_limit" type="number" min="1" max="20" step="1"></label><label>细看上限<input name="inspect_limit" type="number" min="1" max="20" step="1"></label><label>浏览间隔（秒）<input name="interval_seconds" type="number" min="60" max="86400" step="1"></label>
            <label>全渠道每小时上限<input name="max_per_hour" type="number" min="1" max="30" step="1"></label><label>全渠道 24 小时上限<input name="max_per_day" type="number" min="1" max="200" step="1"></label><label>每轮模型调用上限<input name="max_model_calls" type="number" min="1" max="32" step="1"></label><label>累计输入 tokens<input name="max_input_tokens" type="number" min="1024" max="1048576" step="1"></label><label>每次生成输出额度（tokens，含思考）<input name="decision_max_tokens" type="number" min="512" max="65536" step="1"></label><label>累计输出 tokens（含思考）<input name="max_output_tokens" type="number" min="2048" max="65536" step="1"></label><label>本轮期限（秒）<input name="deadline_seconds" type="number" min="10" max="1800" step="1"></label><label>只读步骤重试上限<input name="read_retries" type="number" min="0" max="1" step="1"></label><label>未发送任务接管上限<input name="max_takeovers" type="number" min="0" max="1" step="1"></label>
          </div><p class="hint">只有网络／CPU 准备并发，本地模型继续共用一个生成锁。直播优先；渠道已有更严格限额继续生效。重试不适用于平台写入。</p></details>
          <label class="check-field"><input name="report_enabled" type="checkbox">显示运行报告（关闭不影响额度、防重或审计）</label>
          <div class="actions"><button type="submit" data-save>保存基础设置</button><span data-settings-state class="hint" role="status"></span></div>
        </form>
      </details>
      <section class="panel"><div class="panel-heading"><h2>本轮与资源</h2><span data-round-state class="hint"></span></div><dl data-budget class="description-list"></dl><dl data-metrics class="description-list"></dl></section>
      <div class="automation-tabs" role="tablist" aria-label="浏览与互动记录"><button type="button" role="tab" data-tab="tasks" id="automation-tab-tasks" aria-controls="automation-view-tasks" aria-selected="true">浏览任务</button><button type="button" role="tab" data-tab="inbox" id="automation-tab-inbox" aria-controls="automation-view-inbox" aria-selected="false">互动收件箱</button><button type="button" role="tab" data-tab="intents" id="automation-tab-intents" aria-controls="automation-view-intents" aria-selected="false">候选审核</button><button type="button" role="tab" data-tab="rounds" id="automation-tab-rounds" aria-controls="automation-view-rounds" aria-selected="false">运行报告</button></div>
      <section id="automation-view-tasks" data-view="tasks" role="tabpanel" aria-labelledby="automation-tab-tasks"><div class="automation-layout"><article class="panel"><label>筛选浏览结果<select data-task-filter><option value="">全部结果</option><option value="no_subtitles">没有可用字幕</option><option value="platform_error">平台读取失败</option><option value="viewed_without_comment">已看未发</option><option value="selected">已选择互动</option><option value="checkpoint">进度已保存</option><option value="failed">任务失败</option><option value="cancelled">已取消</option></select></label><div class="table-scroll"><table><thead><tr><th>视频／来源</th><th>内容与结果</th><th>状态</th></tr></thead><tbody data-tasks></tbody></table></div></article><article class="panel automation-inspector"><h2>视频证据与判断</h2><p data-task-summary class="hint">选择任务查看字幕覆盖、引用与原因。</p><dl data-task-meta class="description-list"></dl><div data-evidence></div><details><summary>任务诊断</summary><pre data-task-detail class="code-small"></pre></details><div class="actions"><button type="button" data-reread disabled>有界重读并生成待审核草稿</button></div><p class="hint">手工重读不取得自动发送资格，缺少内容证据仍会跳过。</p></article></div></section>
      <section id="automation-view-inbox" data-view="inbox" role="tabpanel" aria-labelledby="automation-tab-inbox" hidden><article class="panel"><div class="panel-heading"><h2>@ 与收到的回复</h2><a data-private-link href="#messages">私信工作台 →</a></div><p data-private-count class="hint"></p><div data-inbox-list></div></article></section>
      <section id="automation-view-intents" data-view="intents" role="tabpanel" aria-labelledby="automation-tab-intents" hidden><div class="automation-layout"><article class="panel"><label>筛选候选<select data-intent-filter><option value="">全部候选</option><option value="pending_review">待审核</option><option value="approved">已审核</option><option value="private_handoff">已转私信</option><option value="published">已发布</option><option value="publish_uncertain">结果待核对</option><option value="rejected">已拒绝</option></select></label><div data-intents class="object-list"></div></article><article class="panel automation-inspector"><h2>候选正文与平台回执</h2><p data-intent-source class="quoted-text">选择候选查看来源与回复。</p><p data-intent-state class="hint"></p><label>编辑公开候选<textarea data-intent-text rows="6" disabled></textarea></label><div class="actions"><button type="button" data-review="approve" class="primary">保存并审核通过</button><button type="button" data-review="reject">拒绝候选</button><button type="button" data-publish class="primary">提交已审核候选</button><a data-handoff href="#messages" hidden>查看私信草稿 →</a></div><details class="subsection"><summary>来源引用与可见性核验</summary><pre data-intent-evidence class="code-small"></pre><pre data-receipt class="code-small"></pre></details></article></div></section>
      <section id="automation-view-rounds" data-view="rounds" role="tabpanel" aria-labelledby="automation-tab-rounds" hidden><article class="panel"><h2>数据库运行报告</h2><p class="hint">报告展示实际轮次结果；部分完成、取消与失败分别保留，不按报告条数推断发送额度。</p><div data-rounds></div></article></section>`;
    this.$ = selector => root.querySelector(selector);
    const recovery = this.element("section", null, "subsection"); this.$(".automation-control").append(recovery);
    this.recoveryPolicy = new OperationRecoveryPolicyView(recovery, api, notify);
    const continuation = this.element("section"); this.$("[data-intent-state]").after(continuation);
    this.continuation = new OperationContinuationView(continuation, api, notify, () => this.refresh());
    const taskProgress = this.element("section"); this.$("[data-task-detail]").closest("details").before(taskProgress);
    this.taskContinuation = new OperationContinuationView(taskProgress, api, notify, () => this.refresh());
    const inboxProgress = this.element("section"); this.$("[data-inbox-list]").after(inboxProgress);
    this.inboxContinuation = new OperationContinuationView(inboxProgress, api, notify, () => this.refresh());
    this.openTaskButton = this.element("button", "查看关联视频证据");
    this.openTaskButton.type = "button";
    this.$("[data-publish]").closest(".actions").append(this.openTaskButton);
    this.openTaskButton.onclick = () => {
      const task = this.relatedTask();
      if (!task) return;
      this.selectedTask = task.id; this.$("[data-task-filter]").value = "";
      this.selectTab("tasks"); this.renderTasks();
    };
    this.$("[data-refresh]").onclick = () => {this.$("[data-error]").textContent = ""; this.refresh(); this.recoveryPolicy.load();};
    this.$("[data-preview]").onclick = () => this.action("preview", async () => {
      await api("/api/automation/round", {method: "POST", body: {preview: true}});
      this.feedback("只读预览已排队，结果只生成待审核草稿。");
      await this.refresh();
    });
    this.$("[data-round-stop]").onclick = () => this.stop(false);
    this.$("[data-emergency]").onclick = () => this.stop(true);
    this.$("[data-preflight]").onclick = () => this.preflight(false);
    this.$("[data-enable]").onclick = () => this.preflight(true);
    this.$("[data-disable]").onclick = () => this.action("disable", async () => {
      await api("/api/automation/enable", {method: "POST", body: {enabled: false}});
      this.confirming = false;
      this.feedback("自动化已关闭，恢复最新基础设置；已生成草稿保留。");
      await this.refresh();
    });
    this.$("[data-reset]").onclick = () => this.action("reset", async () => {
      await api("/api/automation/reset-stop", {method: "POST"});
      this.feedback("紧急停止已解除，基础独立开关恢复；自动化总开关仍按当前设置。");
      await this.refresh();
    });
    this.$("[data-confirm-enable]").onclick = () => this.action("enable", async () => {
      await api("/api/automation/enable", {method: "POST", body: {enabled: true}});
      this.confirming = false;
      this.feedback("自动化已启用；只处理本模式的新授权候选。");
      await this.refresh();
    });
    this.$("[data-confirm-cancel]").onclick = () => {this.confirming = false; this.renderControl();};
    this.$("[data-settings]").oninput = () => {
      this.dirty = true;
      this.confirming = false;
      this.$("[data-confirm]").hidden = true;
      this.$("[data-settings-state]").textContent = "有未保存修改；保存后再开启自动化。";
      this.renderButtons();
    };
    this.$("[data-settings]").onsubmit = event => {
      event.preventDefault();
      this.action("settings", async () => {
        await api("/api/automation/settings", {method: "PUT", body: this.settings()});
        this.dirty = false;
        this.$("[data-settings-state]").textContent = "基础设置已保存。";
        await this.refresh();
      });
    };
    for (const button of root.querySelectorAll("[data-tab]")) {
      button.onclick = () => this.selectTab(button.dataset.tab);
      button.onkeydown = event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const tabs = [...root.querySelectorAll("[data-tab]:not([hidden])")];
        const current = tabs.indexOf(button);
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        tabs[next].click(); tabs[next].focus();
      };
    }
    this.$("[data-task-filter]").onchange = () => this.renderTasks();
    this.$("[data-intent-filter]").onchange = () => this.renderIntents();
    this.$("[data-reread]").onclick = () => this.action("reread", async () => {
      await api(`/api/automation/tasks/${encodeURIComponent(this.selectedTask)}/reread`, {method: "POST"});
      this.feedback("有界重读已排队；结果保留人工审核。");
      await this.refresh();
    });
    this.$("[data-intent-text]").oninput = () => {this.draftDirty = true; this.renderIntent();};
    for (const button of root.querySelectorAll("[data-review]")) button.onclick = () => this.action("review", async () => {
      await api(`/api/automation/intents/${encodeURIComponent(this.selectedIntent)}/review`, {method: "POST", body: {action: button.dataset.review, text: this.$("[data-intent-text]").value}});
      this.draftDirty = false;
      this.feedback(button.dataset.review === "approve" ? "候选已保存并审核。" : "候选已拒绝，防重记录保留。");
      await this.refresh();
    });
    this.$("[data-publish]").onclick = () => this.action("publish", async () => {
      await api(`/api/automation/intents/${encodeURIComponent(this.selectedIntent)}/publish`, {method: "POST"});
      this.feedback("平台提交已结束，请核对候选状态和真实回执。");
      await this.refresh();
    });
    this.renderButtons();
  }

  element(tag, text, className) {
    const result = document.createElement(tag);
    if (text != null) result.textContent = text;
    if (className) result.className = className;
    return result;
  }
  array(value) {return Array.isArray(value) ? value : value?.items || [];}
  decode(value) {if (typeof value !== "string") return value; try {return JSON.parse(value);} catch {return value;}}
  text(value) {return value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);}
  label(value) {
    const reasons = {specific_detail: "有具体内容可回应", no_interest: "不感兴趣", like: "感兴趣", not_interested: "暂不感兴趣", need_more: "需要更多内容", skip: "跳过", nothing_to_add: "暂时没有需要补充的话", insufficient_evidence: "内容证据不足", visual_required: "需要画面证据", unsafe_interaction: "不宜继续互动", light_interaction: "轻互动", private_details: "适合私下讨论的个人细节", user_requested: "对方希望私下继续", long_followup: "长篇跟进适合私信", cooldown: "同用户仍在冷却", spam: "广告或无效内容", permission: "账号或权限不足", restricted: "平台限制", network: "网络读取失败", invalid: "平台数据无效", historical: "历史事件（不自动追发）", all_available_cc: "已读取可用 CC；不代表看过无字幕的分 P", available: "原始材料在本地，发送前仍需重核来源", missing: "原始材料已清理或失效，需重读"};
    if (typeof value === "string" && Object.hasOwn(reasons, value)) return reasons[value];
    return ({off: "已关闭", starting: "启动中", running: "运行中", paused: "已暂停", stopping: "停止中", blocked: "有阻塞项", queued: "排队中", claimed: "已领取", preparing: "准备中", evidence_ready: "证据就绪", evaluated: "已判断", selected: "已选择互动", completed: "已完成", checkpoint: "进度已保存", partial: "部分完成", skipped: "已跳过", failed: "失败", cancelled: "已取消", viewed_without_comment: "已看未发", no_subtitles: "无可用字幕", empty: "无有效字幕正文", pending_review: "待审核", approved: "已审核", rejected: "已拒绝", auto_authorized: "本模式自动授权", publishing: "提交中", published: "平台已受理", rejected_by_platform: "平台明确拒绝", publish_uncertain: "提交结果待核对", private_handoff: "已转私信", mention: "@ 提及", reply: "收到回复", top_level: "主动顶层留言", recommend: "首页推荐", search: "关键词搜索", popular: "热门", specified: "指定视频", visible: "公开可见", not_visible: "平台标记不可见", not_found: "未查到", check_failed: "可见性读取失败", not_checked: "尚未核验可见性"})[value] || value || "—";
  }
  feedback(text) {this.$("[data-feedback]").textContent = text;}
  error(error) {this.$("[data-error]").textContent = error?.message || String(error);}
  async action(key, work) {
    if (this.pending.has(key) || (this.pending.size && key !== "emergency" && key !== "round-stop")) return;
    this.pending.add(key); this.$("[data-error]").textContent = ""; this.renderButtons();
    try {await work();} catch (error) {this.error(error);} finally {this.pending.delete(key); this.renderButtons();}
  }
  async stop(emergency) {
    await this.action(emergency ? "emergency" : "round-stop", async () => {
      await this.api("/api/automation/stop", {method: "POST", body: {emergency}});
      this.confirming = false;
      this.feedback(emergency ? "已紧急停止所有自动收发；不会因恢复基础开关而自动继续。已提交请求只核对回执。" : "已请求停止本轮；未提交任务取消，草稿保留。已提交请求只核对回执。");
      await this.refresh();
    });
  }
  async preflight(confirm) {
    await this.action("preflight", async () => {
      const value = await this.api("/api/automation/preflight", {method: "POST"});
      if (this.pending.has("emergency") || this.value?.policy?.emergency) {
        this.confirming = false;
        await this.refresh();
        this.feedback("紧急停止仍生效；预检不会开启自动化。");
        return;
      }
      this.value = value;
      this.confirming = confirm && !value.policy?.emergency;
      this.renderControl();
      this.feedback(this.array(this.value.blockers).length ? "预检发现阻塞项，配置已保留。" : "账号与能力预检完成，请核对实际账号、角色和发送范围。");
      if (confirm) this.$("[data-confirm]").scrollIntoView({block: "nearest", behavior: "smooth"});
    });
  }
  async refresh() {
    if (this.refreshing || this.root.closest("[hidden]")) return;
    this.refreshing = true;
    try {
      const results = await Promise.allSettled([this.api("/api/automation"), this.api("/api/automation/tasks"), this.api("/api/automation/rounds"), this.api("/api/automation/intents"), this.api("/api/automation/inbox")]);
      const properties = ["value", "tasks", "rounds", "intents", "inbox"];
      let failure = null;
      results.forEach((result, index) => {
        if (result.status === "fulfilled") this[properties[index]] = [1, 2, 3].includes(index) ? this.array(result.value) : result.value;
        else failure ||= result.reason;
      });
      if (this.value) this.render();
      if (results[0].status === "fulfilled" && !this.recoveryPolicyStarted) {this.recoveryPolicyStarted = true; await this.recoveryPolicy.load();}
      if (failure) this.error(failure);
    } finally {this.refreshing = false;}
  }
  settings() {
    const value = {...this.value.settings};
    if ("sources" in value) value.sources = [...this.root.querySelectorAll("[data-source]:checked")].map(input => input.value);
    for (const input of this.$("[data-settings]").querySelectorAll("[name]")) {
      if (!(input.name in value)) continue;
      if (input.type === "checkbox") value[input.name] = input.checked;
      else if (input.type === "datetime-local") value[input.name] = input.value ? Math.floor(new Date(input.value).getTime() / 1000) : 0;
      else if (input.dataset.type) {
        const lines = input.value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
        value[input.name] = input.dataset.type === "numbers" ? lines.map(Number) : lines;
      } else value[input.name] = Number(input.value);
    }
    return value;
  }
  fillSettings() {
    if (this.dirty) return;
    const settings = this.value.settings || {};
    for (const input of this.$("[data-settings]").querySelectorAll("[name]")) {
      input.disabled = !(input.name in settings);
      input.closest("label").hidden = !(input.name in settings);
      if (input.type === "checkbox") input.checked = Boolean(settings[input.name]);
      else if (input.type === "datetime-local") {
        if (!settings[input.name]) input.value = "";
        else {const date = new Date(settings[input.name] * 1000); date.setMinutes(date.getMinutes() - date.getTimezoneOffset()); input.value = date.toISOString().slice(0, 16);}
      } else input.value = input.dataset.type ? this.array(settings[input.name]).join("\n") : settings[input.name] ?? "";
    }
    for (const input of this.root.querySelectorAll("[data-source]")) input.checked = this.array(settings.sources).includes(input.value);
  }
  description(root, entries) {
    root.replaceChildren();
    for (const [name, value] of entries) root.append(this.element("dt", name), this.element("dd", this.text(value)));
  }
  render() {
    this.fillSettings(); this.renderControl(); this.renderTasks(); this.renderInbox(); this.renderIntents(); this.renderRounds();
    const round = this.decode(this.value.active_round);
    this.$("[data-round-state]").textContent = round ? `#${round.id} · ${this.label(round.status)}` : "当前没有活动轮次";
    const limits = this.decode(round?.config) || this.value.settings || {};
    const capKeys = {reads: "max_reads", model_calls: "max_model_calls", input_tokens: "max_input_tokens", output_tokens: "max_output_tokens", top_levels: "top_level_limit"};
    const budget = round ? Object.entries(this.decode(round.budget) || {}).map(([key, value]) => [this.metricLabel(key), Number.isFinite(limits[capKeys[key]]) ? `${value} / ${limits[capKeys[key]]}（剩余 ${Math.max(0, limits[capKeys[key]] - value)}）` : value]) : [["预算", "运行后显示真实消耗与剩余额度"]];
    if (round?.deadline) budget.push(["本轮截止", new Date(round.deadline * 1000).toLocaleString()]);
    this.description(this.$("[data-budget]"), budget);
    const metrics = {...this.value.metrics};
    if (round) metrics.owned_tasks = this.tasks.filter(task => task.round_id === round.id && ["claimed", "preparing", "evidence_ready"].includes(task.status)).length;
    this.description(this.$("[data-metrics]"), Object.entries(metrics).map(([key, value]) => [this.metricLabel(key), value]));
    this.selectTab(this.tab); this.renderButtons();
  }
  metricLabel(key) {
    return ({workers: "准备 Worker", active_workers: "活动 Worker", owned_tasks: "当前持有任务", requests: "读取请求", reads: "读取次数", read_calls: "读取次数", model_calls: "模型调用", top_levels: "主动留言", input_tokens: "累计输入 tokens", output_tokens: "累计输出 tokens", cache_hits: "缓存命中", merged_reads: "在途合并", merged_requests: "在途合并", duplicates: "重复拦截", takeovers: "接管次数", stale_results: "迟到结果拒绝", lock_wait_ms: "生成锁等待（ms）", queue_wait_ms: "排队等待（ms）", elapsed_ms: "已用时（ms）", peak_rss_bytes: "进程峰值内存（bytes）", remaining_calls: "剩余模型调用", remaining_input: "剩余输入 tokens", remaining_output: "剩余输出 tokens", deadline: "截止时间"})[key] || key;
  }
  renderControl() {
    if (!this.value) return;
    const value = this.value, policy = value.policy || {}, settings = value.settings || {};
    const state = policy.emergency ? "紧急停止已生效" : this.label(value.state || (policy.enabled ? "running" : "off"));
    const automaticReady = value.automatic_sending_ready === true;
    this.$("[data-state]").textContent = state;
    this.$("[data-state]").className = `badge ${policy.emergency || this.array(value.blockers).length ? "bad" : policy.enabled ? "good" : ""}`;
    this.description(this.$("[data-identity]"), [["实际账号", value.account_id || policy.account_id || "尚未预检"], ["当前 OC", value.character ? `${value.character.id} v${value.character.version}` : "尚未读取"], ["发送模式", automaticReady ? "本模式新候选可按授权自动提交" : "人工审核（真实模型语义验收未通过，不自动提交）"], ["写入范围", `每小时最多 ${settings.max_per_hour ?? policy.max_per_hour ?? "—"} 次／滚动 24 小时 ${settings.max_per_day ?? policy.max_per_day ?? "—"} 次；渠道更严格限额继续生效`]]);
    this.$("[data-overlay-note]").textContent = policy.emergency ? "紧急停止阻止总开关及独立功能的自动收发。解除前不会恢复自动业务。" : policy.enabled ? `自动化覆盖正在生效。基础设置可继续修改；被覆盖项目在关闭自动化后生效。${!automaticReady ? "当前模型语义验收未通过，候选仍保留人工审核，不自动提交。" : ""}` : "当前使用个人基础设置。开启总开关前会展示新增的自动行为与授权范围。";
    this.$("[data-settings-note]").textContent = policy.enabled ? "自动化运行中：以下编辑个人基础设置；被覆盖项目关闭自动化后生效。" : "独立功能默认关闭。保存基础设置不会自动开启总开关。";
    const messageSummary = p => `${p.enabled ? "已启用" : "未启用"} · ${p.auto_poll ? "自动读取" : "手动读取"} · ${p.auto_send ? "自动回复开关开启" : "人工审核"} · ${p.allow_comment_handoff ? "允许评论移交" : "不允许评论移交"}`;
    this.$("[data-message-settings-summary]").textContent = `私信个人基础：${messageSummary(value.base?.messages || {})}；实际生效：${messageSummary(value.effective?.messages || {})}。${!automaticReady ? "自动化模式的候选在模型语义验收通过前保留人工审核。" : ""}`;
    this.$("[data-vision]").textContent = `视觉：${value.vision?.available ? "Provider 已声明可用，仍受实验开关与资源准入约束" : "不可用"}${value.vision?.reason ? ` · ${value.vision.reason}` : " · 首期不部署视觉模型"}。CC 优先，无有效内容证据时跳过。`;
    this.$("[data-blockers]").replaceChildren(...this.array(value.blockers).map(item => this.element("li", typeof item === "string" ? item : item.message || item.reason || this.text(item))));
    const layers = this.$("[data-config-layers]");
    const layerSignature = JSON.stringify([value.base, value.effective, policy.emergency]);
    if (layerSignature !== this.layerSignature) {
    this.layerSignature = layerSignature; layers.replaceChildren();
    const names = new Set([...Object.keys(value.base || {}), ...Object.keys(value.effective || {})]);
    for (const name of names) {
      const section = this.element("section"), heading = this.element("h3", ({comments: "评论", messages: "私信", dynamics: "动态", browsing: "浏览"})[name] || name);
      const table = this.element("table"), head = this.element("thead"), row = this.element("tr");
      ["设置", "个人基础值", "实际生效值", "来源"].forEach(text => row.append(this.element("th", text))); head.append(row); table.append(head);
      const body = this.element("tbody"), base = value.base?.[name] || {}, effective = value.effective?.[name] || {};
      for (const key of new Set([...Object.keys(base), ...Object.keys(effective)])) {
        if (/credentials|last_error|last_sync|last_browse|token|sessdata|csrf/i.test(key)) continue;
        if (name === "browsing" && ["message_poll_enabled", "message_reply_enabled", "comment_handoff_enabled"].includes(key)) continue;
        const item = this.element("tr"), different = JSON.stringify(base[key]) !== JSON.stringify(effective[key]);
        [key, this.text(base[key]), this.text(effective[key]), different ? (policy.emergency ? "紧急停止" : "由自动化启用") : "个人基础设置"].forEach(text => item.append(this.element("td", text))); body.append(item);
      }
      table.append(body); section.append(heading, table); layers.append(section);
    }
    }
    this.$("[data-confirm]").hidden = !this.confirming;
    this.$("[data-confirm-summary]").textContent = `账号 ${value.account_id || policy.account_id || "未确认"}；OC ${value.character?.id || "未确认"} v${value.character?.version ?? "—"}；每小时最多 ${settings.max_per_hour ?? "—"} 次，滚动 24 小时 ${settings.max_per_day ?? "—"} 次。${!automaticReady ? "当前保留人工审核，不自动提交。" : ""}`;
    const features = value.enabled_features || ["发现视频并读取元信息和 CC 字幕", "基于当前 OC 判断兴趣及生成主动留言", "读取并评估真实 @、收到的评论回复", "读取新私信，按真实来源回复或移交私信", !automaticReady ? "候选只供人工审核；真实模型语义验收通过前不自动提交" : "新候选通过来源、内容和额度校验后自动提交"];
    this.$("[data-confirm-features]").replaceChildren(...this.array(features).map(item => this.element("li", this.text(item))));
    this.renderButtons();
  }
  renderButtons() {
    const loaded = Boolean(this.value), busy = this.pending.size > 0, policy = this.value?.policy || {};
    for (const input of this.$("[data-settings]").querySelectorAll("input,textarea,select")) input.disabled = busy || !loaded || (input.name && !(input.name in (this.value?.settings || {})));
    for (const button of this.root.querySelectorAll("[data-refresh],[data-preview],[data-preflight],[data-enable],[data-disable],[data-save],[data-confirm-enable],[data-confirm-cancel],[data-reset]")) button.disabled = busy || !loaded;
    this.$("[data-refresh]").disabled = busy;
    this.$("[data-enable]").disabled = !loaded || busy || this.dirty || Boolean(policy.enabled) || Boolean(policy.emergency);
    this.$("[data-disable]").disabled = !loaded || busy || !policy.enabled;
    this.$("[data-preview]").disabled = !loaded || busy || this.dirty || Boolean(policy.emergency);
    this.$("[data-confirm-enable]").disabled = !loaded || busy || this.dirty || Boolean(policy.emergency) || this.array(this.value?.blockers).length > 0 || !(this.value?.account_id || policy.account_id);
    this.$("[data-reset]").hidden = !policy.emergency;
    this.$("[data-emergency]").disabled = this.pending.has("emergency");
    this.$("[data-round-stop]").disabled = !loaded || this.pending.has("round-stop") || !this.value?.active_round;
    this.$("[data-reread]").disabled = !this.selectedTask || busy || Boolean(policy.emergency);
    this.renderIntent();
  }
  selectTab(tab) {
    if (tab === "rounds" && this.value?.settings?.report_enabled === false) tab = "tasks";
    this.tab = tab;
    for (const button of this.root.querySelectorAll("[data-tab]")) {
      const active = button.dataset.tab === tab;
      button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1;
      button.hidden = button.dataset.tab === "rounds" && this.value?.settings?.report_enabled === false;
    }
    for (const view of this.root.querySelectorAll("[data-view]")) view.hidden = view.dataset.view !== tab;
  }
  taskReason(row) {return this.label(row.reason || row.error_kind || row.error || row.result?.reason || row.decision?.reason || this.array(row.decisions).map(item => this.label(item.reason)).join("；") || "");}
  sourceSummary(row) {
    return this.array(row.sources).map(source => typeof source === "string" ? this.label(source) : `${this.label(source.source)}${source.keyword ? " · " + source.keyword : ""}${source.page ? " · 页 " + source.page : ""}`).join("；") || this.label(row.source);
  }
  renderTasks() {
    const filter = this.$("[data-task-filter]").value;
    const signature = JSON.stringify([this.tasks, filter, this.selectedTask]);
    if (signature === this.taskSignature) return;
    this.taskSignature = signature;
    const matches = row => {
      if (!filter) return true;
      const reason = this.text(this.taskReason(row));
      if (filter === "no_subtitles") return /no_subtitles|empty|无.*字幕|字幕.*空/.test(reason);
      if (filter === "platform_error") return /permission|restricted|network|invalid|platform|平台|网络|权限/.test(reason) || row.status === "failed";
      return row.status === filter || row.decision?.action === filter;
    };
    const rows = this.tasks.filter(matches), body = this.$("[data-tasks]"); body.replaceChildren();
    for (const task of rows) {
      const row = this.element("tr"), first = this.element("td"), button = this.element("button", task.title || task.metadata?.title || task.bvid || `任务 ${task.id}`, "comment-link");
      if (task.id === this.selectedTask) row.className = "selected-row";
      button.onclick = () => {this.selectedTask = task.id; this.renderTasks();};
      first.append(button, this.element("small", `${task.bvid || task.video_id || "—"} · ${this.sourceSummary(task)}`, "cell-sub"));
      row.append(first, this.element("td", this.text(this.taskReason(task))), this.element("td", this.label(task.status))); body.append(row);
    }
    if (!rows.length) {const row = this.element("tr"), cell = this.element("td", this.tasks.length ? "没有符合筛选的任务。" : "暂无浏览任务。可以先运行只读预览。", "empty-cell"); cell.colSpan = 3; row.append(cell); body.append(row);}
    const task = this.tasks.find(item => item.id === this.selectedTask);
    this.$("[data-task-summary]").textContent = task ? `${task.title || task.metadata?.title || task.bvid || task.id} · ${this.label(task.status)} · ${this.text(this.taskReason(task))}` : "选择任务查看字幕覆盖、引用与原因。";
    this.taskContinuation.render(task?.continuation, {channel:"browsing", localId:task?.id, entityStatus:task?.status});
    const material = this.decode(task?.material) || {}, metadata = this.decode(task?.metadata) || {};
    const tracks = this.array(task?.subtitle_tracks || material.parts);
    const trackSummary = tracks.map(track => `${track.cid || "CID未返回"} · ${track.track_id || "轨道未返回"} · ${track.language || "语言未返回"} · ${track.is_ai == null ? "视觉／类型未声明" : track.is_ai ? "平台AI CC" : "人工CC"}`).join("；");
    const decisions = this.array(task?.decisions || (task?.decision ? [this.decode(task.decision)] : []));
    const decisionSummary = decisions.map(item => `${this.label(item.interest)} · ${item.worth_commenting ? "值得留言" : "无需留言"} · ${this.label(item.reason)}`).join("；") || "尚未判断";
    this.description(this.$("[data-task-meta]"), task ? [["来源", this.sourceSummary(task)], ["作者", metadata.owner || metadata.owner_uid], ["字幕轨／CID", trackSummary || material.track_id || material.subtitle?.track_id || "未返回"], ["字幕类型", tracks.length ? "见逐P轨道信息" : material.is_ai == null ? "未返回" : material.is_ai ? "平台 AI CC" : "人工 CC"], ["实际覆盖", this.label(material.coverage || task.coverage)], ["缺失的分 P", task.missing_parts || material.missing_parts || []], ["兴趣与发言", decisionSummary], ["缓存", this.label(task.cache || task.cache_status || (task.cache_hit ? "命中" : "材料状态尚未核验"))]] : []);
    const evidence = this.$("[data-evidence]"); evidence.replaceChildren();
    const segments = this.array(material.segments || material.subtitle?.segments || task?.evidence || task?.evidence_refs);
    if (task) {
      const details = this.element("details"), summary = this.element("summary", `本次引用的字幕／证据片段（${segments.length}）`); details.append(summary);
      const list = this.element("div", null, "automation-evidence");
      for (const segment of segments) list.append(this.element("p", `${segment.id || segment.ref || ""} ${segment.start ?? "?"}–${segment.end ?? "?"}s · ${segment.text || segment.quote || this.text(segment)}`));
      if (!segments.length) list.append(this.element("p", "尚无可展示的有效内容证据。", "hint")); details.append(list); evidence.append(details);
      evidence.append(this.element("p", "这些是本轮引用记录；后续发言需重新核验原材料。", "hint"));
    }
    const diagnosticFields = ["id", "round_id", "bvid", "status", "account_id", "character_id", "character_version", "generation", "attempts", "lease_until", "input_version", "reason", "error", "material_key", "cache_status", "coverage", "missing_parts", "decisions", "metrics", "created_at", "updated_at"];
    this.$("[data-task-detail]").textContent = task ? JSON.stringify(Object.fromEntries(diagnosticFields.filter(key => key in task).map(key => [key, task[key]])), null, 2) : "暂无任务。";
    this.renderButtons();
  }
  renderInbox() {
    const signature = JSON.stringify([this.inbox, this.selectedInbox]);
    if (signature === this.inboxSignature) return;
    this.inboxSignature = signature;
    const inbox = this.inbox || {}, list = this.$("[data-inbox-list]"); list.replaceChildren();
    this.$("[data-private-count]").textContent = `私信入口：${Number(inbox.private_count || 0)} 条。私密正文只在私信工作台展示。`;
    // Keep the private link inside the authenticated application's known area.
    this.$("[data-private-link]").href = "#messages";
    for (const record of this.array(inbox.items)) {
      const event = {...this.decode(record.payload), ...record};
      if (["message", "direct_message", "private_message", "dm"].includes(event.kind) || event.private) continue;
      const item = this.element("article", null, "automation-notification");
      item.append(this.element("h3", `${this.label(event.kind)} · ${event.sender_name || event.sender_uid || "来源用户"}`), this.element("p", event.text || event.source?.text || event.source_text || event.content || "原文尚未核验"), this.element("small", `${event.resource_type || "—"} · ${event.bvid || event.oid || "—"} · ${this.label(event.status)} · ${event.reason || event.error || ""}`, "muted"));
      if (event.continuation) {
        const progress = this.element("button", "查看生成进度"); progress.type = "button";
        progress.setAttribute("aria-pressed", String(event.id === this.selectedInbox));
        progress.onclick = () => {this.selectedInbox = event.id; this.renderInbox(); this.inboxContinuation.root.scrollIntoView({block:"nearest"});};
        item.append(progress);
      }
      list.append(item);
    }
    const selected = this.array(inbox.items).find(row => row.id === this.selectedInbox);
    const event = selected ? {...this.decode(selected.payload), ...selected} : null;
    const privateEvent = event && (["message", "direct_message", "private_message", "dm"].includes(event.kind) || event.private);
    this.inboxContinuation.render(privateEvent ? null : event?.continuation, {channel:"browsing", localId:event ? "event:" + event.id : null, entityStatus:event?.status});
    if (!list.children.length) list.append(this.element("p", "暂无公开互动事件。首次启用建立高水位，不自动追发旧通知。", "hint"));
  }
  renderIntents() {
    const filter = this.$("[data-intent-filter]").value, list = this.$("[data-intents]");
    const signature = JSON.stringify([this.intents, filter, this.selectedIntent]);
    if (signature === this.intentSignature) {this.renderIntent(); return;}
    this.intentSignature = signature; list.replaceChildren();
    for (const intent of this.intents.filter(row => !filter || row.status === filter)) {
      const button = this.element("button", `#${String(intent.id).slice(0, 8)} · ${this.label(intent.kind)} · ${this.label(intent.status)}`);
      button.classList.toggle("selected", intent.id === this.selectedIntent);
      button.setAttribute("aria-pressed", String(intent.id === this.selectedIntent));
      button.onclick = () => {
        if (this.draftDirty && this.selectedIntent !== intent.id && !confirm("放弃尚未保存的候选修改？")) return;
        this.selectedIntent = intent.id; this.draftDirty = false; this.renderIntents();
      };
      button.title = String(intent.id);
      button.append(this.element("small", `${intent.bvid || intent.target?.bvid || intent.source_id || "—"} · ${this.label(intent.reason)}`)); list.append(button);
    }
    if (!list.children.length) list.append(this.element("p", "暂无符合筛选的候选。无兴趣或无证据时可以不发言。", "hint"));
    this.renderIntent();
  }
  renderIntent() {
    const row = this.intents.find(item => item.id === this.selectedIntent), text = this.$("[data-intent-text]");
    const privateHandoff = row && (row.status === "private_handoff" || row.kind === "private_handoff" || row.channel === "direct_message");
    const editable = row && !privateHandoff && ["pending_review", "approved", "rejected"].includes(row.status), busy = this.pending.size > 0;
    this.continuation.render(privateHandoff ? null : row?.continuation, {channel: "browsing", localId: row?.id, dirty: this.draftDirty, entityStatus: row?.status});
    if (!this.draftDirty) text.value = row && !privateHandoff ? row.text || "" : "";
    text.disabled = !editable || busy;
    this.$("[data-intent-source]").textContent = row ? `${this.label(row.kind)} · ${row.account_id || "账号未返回"} · ${row.character_id || "OC未返回"} v${row.character_version ?? "—"} · ${row.bvid || row.target?.bvid || row.source_id || "—"} · ${this.label(row.reason)}` : "选择候选查看来源与回复。";
    this.$("[data-intent-state]").textContent = row ? `${this.label(row.status)}${row.error ? " · " + row.error : ""}${this.draftDirty ? " · 有未保存修改" : ""}${row.visibility ? " · " + this.label(this.decode(row.visibility)?.status || row.visibility) : ""}` : "";
    for (const button of this.root.querySelectorAll("[data-review]")) button.disabled = !editable || busy;
    this.$("[data-publish]").disabled = !row || row.status !== "approved" || this.draftDirty || privateHandoff || busy || Boolean(this.value?.policy?.emergency);
    this.$("[data-handoff]").hidden = !privateHandoff;
    this.openTaskButton.disabled = !this.relatedTask();
    this.$("[data-intent-evidence]").textContent = row ? JSON.stringify({evidence: this.decode(row.evidence), target: this.decode(row.target), visibility: this.decode(row.visibility)}, null, 2) : "暂无来源引用。";
    this.$("[data-receipt]").textContent = row && !privateHandoff ? JSON.stringify(this.decode(row.receipt) || null, null, 2) : privateHandoff ? "公开区只记录移交状态；请在私信工作台查看正文与回执。" : "暂无平台回执。";
  }
  relatedTask() {
    const intent = this.intents.find(item => item.id === this.selectedIntent);
    if (!intent) return null;
    const target = this.decode(intent.target) || {};
    return this.tasks.find(task => (intent.bvid || target.bvid) && task.bvid === (intent.bvid || target.bvid)) || null;
  }
  renderRounds() {
    const signature = JSON.stringify(this.rounds);
    if (signature === this.roundSignature) return;
    this.roundSignature = signature;
    const list = this.$("[data-rounds]");
    const openRounds = new Set([...list.querySelectorAll("details[open]")].map(details => details.dataset.round));
    list.replaceChildren();
    for (const round of this.rounds) {
      const details = this.element("details", null, "automation-round");
      details.dataset.round = round.id; details.open = openRounds.has(String(round.id));
      details.append(this.element("summary", `#${round.id} · ${this.label(round.status)}${round.preview ? " · 只读预览" : ""}${round.error ? " · " + round.error : ""}`));
      details.append(this.element("pre", JSON.stringify(round, null, 2), "code-small")); list.append(details);
    }
    if (!this.rounds.length) list.append(this.element("p", "暂无轮次记录。", "hint"));
  }
}
