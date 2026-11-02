"use strict";

class BilibiliAccountUI {
  constructor({header, settings = null, api, notify, onSessionRevoked = () => {}, onChange = () => {}}) {
    this.header = header; this.settings = settings; this.api = api; this.notify = notify;
    this.onSessionRevoked = onSessionRevoked; this.onChange = onChange;
    this.value = null; this.active = false; this.loading = false; this.pending = false;
    this.sequence = 0; this.requestError = ""; this.avatarKey = null;
    this.resetPending = false; this.tokenPresented = false; this.sensitiveValues = []; this.lastRefreshAttempt = 0;
    header.innerHTML = `<div class="bilibili-account" aria-label="B站账号">
      <span class="account-avatar"><span data-avatar-placeholder aria-hidden="true">B站</span><img data-avatar alt="" referrerpolicy="no-referrer" hidden></span>
      <div class="account-identity"><div class="account-name-line"><strong data-name>账号状态待读取</strong><span data-status class="account-state" role="status">待读取</span><small data-profile-note hidden>上次核验资料</small></div><div class="account-profile"><span data-uid>UID —</span><span data-coins>硬币 —</span><span data-fans>粉丝 —</span></div><span data-header-error class="account-inline-error" hidden></span></div>
      <div class="account-header-actions"><button type="button" data-refresh>刷新账号</button><a href="${settings ? "#settings" : "/#settings"}">账号设置</a></div>
    </div>`;
    this.h = selector => header.querySelector(selector);
    this.h("[data-avatar]").onload = () => { if (this.active && this.avatarKey) { this.h("[data-avatar]").hidden = false; this.h("[data-avatar-placeholder]").hidden = true; } };
    this.h("[data-avatar]").onerror = () => { this.h("[data-avatar]").hidden = true; this.h("[data-avatar-placeholder]").hidden = false; };
    this.h("[data-refresh]").onclick = () => this.requestError ? this.load() : this.refreshProfile();
    if (settings) this.buildSettings();
    this.render();
    setInterval(() => { if (this.active && !document.hidden) this.load(); }, 30000);
  }
  buildSettings() {
    this.settings.innerHTML = `<article class="panel account-settings-panel" aria-labelledby="bilibili-account-heading">
      <div class="panel-heading"><h2 id="bilibili-account-heading">B站账号</h2><span data-settings-status class="account-state" role="status">待读取</span></div>
      <p class="hint">评论、私信、动态和自主浏览共用此账号。保存凭证后验证登录，资料以平台实际返回为准。</p>
      <dl class="account-details"><dt>SESSDATA</dt><dd data-sessdata-state>待读取</dd><dt>CSRF / bili_jct</dt><dd data-csrf-state>待读取</dd><dt>最近核验</dt><dd data-checked>尚未核验</dd></dl>
      <form data-credentials-form autocomplete="off"><fieldset data-credentials-fields class="account-fields">
        <div class="form-grid"><label>SESSDATA<input data-sessdata type="password" autocomplete="new-password" spellcheck="false" placeholder="留空保留已保存值" aria-describedby="account-credential-help"></label><label>CSRF / bili_jct<input data-csrf type="password" autocomplete="new-password" spellcheck="false" placeholder="留空保留已保存值" aria-describedby="account-credential-help"></label></div>
        <p class="hint" id="account-credential-help">凭证只写入、不回显；空白不会清除已有值。bili_jct 是 CSRF 凭证。</p>
        <div class="actions"><button data-save type="submit" class="primary" disabled>保存账号凭证</button><button data-verify type="button">验证登录 / 刷新资料</button><button data-clear type="button" class="danger">清除已保存凭证</button></div>
      </fieldset></form><p data-account-warnings class="hint account-feedback" role="status" hidden></p><p data-account-feedback class="hint account-feedback" role="status"></p>
    </article>
    <article class="panel" aria-labelledby="management-login-heading"><div class="panel-heading"><h2 id="management-login-heading">工作台管理登录</h2></div><p class="hint">同一浏览器可保持登录。退出、清除浏览器数据或重置管理令牌后，需要重新登录。</p><p class="hint">重置令牌会立即退出所有管理登录，新令牌仅在重置成功后显示一次。</p><button data-reset-open type="button" class="danger">重置管理令牌…</button></article>`;
    this.s = selector => this.settings.querySelector(selector);
    this.s("[data-credentials-form]").onsubmit = event => { event.preventDefault(); this.saveCredentials(); };
    this.s("[data-sessdata]").oninput = this.s("[data-csrf]").oninput = () => this.renderControls();
    this.s("[data-verify]").onclick = () => this.refreshProfile();
    this.s("[data-clear]").onclick = () => this.clearCredentials();
    this.s("[data-reset-open]").onclick = () => this.openTokenReset();
    this.dialog = document.createElement("dialog"); this.dialog.className = "account-dialog";
    this.dialog.setAttribute("aria-labelledby", "management-reset-heading");
    this.dialog.innerHTML = `<h2 id="management-reset-heading">重置管理令牌</h2><div data-reset-confirm><p>所有工作台管理登录都会立即退出，包括当前浏览器。此操作不会更换 B站账号凭证。</p><p class="hint">新令牌只在本次响应中显示。请保存后再返回登录页。</p><div class="actions"><button data-reset-cancel type="button">取消</button><button data-reset-confirm-button type="button" class="danger">重置并退出所有管理登录</button></div></div><div data-reset-result hidden><p>管理令牌已重置，所有管理登录已退出。请先保存新令牌。</p><label>新的管理令牌<textarea data-new-token rows="3" readonly autocomplete="off" spellcheck="false" aria-describedby="management-token-help"></textarea></label><p class="hint" id="management-token-help">关闭此窗口后不再回显。不要把管理令牌发到评论区或私信中。</p><div class="actions"><button data-token-copy type="button" class="primary">复制新管理令牌</button><button data-token-done type="button">已保存，返回登录</button></div></div><p data-reset-feedback class="account-feedback" role="status"></p>`;
    document.body.append(this.dialog);
    this.d = selector => this.dialog.querySelector(selector);
    this.d("[data-reset-cancel]").onclick = () => this.dialog.close();
    this.d("[data-reset-confirm-button]").onclick = () => this.resetToken();
    this.d("[data-token-copy]").onclick = () => this.copyToken();
    this.d("[data-token-done]").onclick = () => { this.d("[data-new-token]").value = ""; this.tokenPresented = false; this.dialog.close(); document.getElementById("token")?.focus(); };
    this.dialog.addEventListener("cancel", event => { if (this.resetPending || this.tokenPresented) event.preventDefault(); });
  }
  async activate() { this.active = true; await this.load(); await this.verifyIfNeeded(); }
  async settingsOpened() { await this.load(); await this.verifyIfNeeded(); }
  async verifyIfNeeded() { if (this.value?.configured?.sessdata && this.value.state === "unverified" && Date.now() - this.lastRefreshAttempt > 30000) await this.refreshProfile(); }
  deactivate() {
    this.active = false; this.sequence += 1; this.loading = false; this.value = null; this.requestError = "";
    if (this.settings) { this.s("[data-sessdata]").value = ""; this.s("[data-csrf]").value = ""; }
    this.render();
  }
  signature(value) { return JSON.stringify([value?.state, value?.configured, value?.profile?.uid]); }
  accept(value, changed = false) {
    const previous = this.value; this.value = value; this.requestError = ""; this.render();
    if (changed || (previous && this.signature(previous) !== this.signature(value))) this.onChange(value);
  }
  async load() {
    if (!this.active || this.loading || this.pending) return;
    const sequence = ++this.sequence; this.loading = true; this.renderControls();
    try { const value = await this.api("/api/account/bilibili"); if (this.active && sequence === this.sequence) this.accept(value); }
    catch (error) { if (this.active && sequence === this.sequence) { this.requestError = this.safeError(error); this.render(); } }
    finally { if (sequence === this.sequence) { this.loading = false; this.renderControls(); } }
  }
  safeError(error) {
    let text = typeof error?.message === "string" ? error.message : "账号操作失败，请重试。";
    for (const value of this.sensitiveValues) if (value) text = text.split(value).join("[凭证已隐藏]");
    return text;
  }
  stateLabel() {
    if (this.requestError) return "状态读取失败";
    return ({not_configured: "未配置", unverified: "已配置 · 未验证", logged_in: "已登录", expired: "凭证已过期", restricted: "读取受限", error: "读取失败"})[this.value?.state] || "待读取";
  }
  number(value) { return typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(value) : "—"; }
  render() {
    const value = this.value, profile = this.requestError ? null : value?.profile;
    const name = typeof profile?.name === "string" && profile.name ? profile.name : profile ? "用户名未提供" : "B站账号";
    this.h("[data-name]").textContent = name;
    this.h("[data-profile-note]").hidden = !profile || value?.state === "logged_in";
    this.h("[data-uid]").textContent = `UID ${profile?.uid ?? "—"}`;
    this.h("[data-coins]").textContent = `硬币 ${this.number(profile?.coins)}`;
    this.h("[data-fans]").textContent = `粉丝 ${this.number(profile?.fans)}`;
    for (const badge of [this.h("[data-status]"), this.settings && this.s("[data-settings-status]")].filter(Boolean)) {
      badge.textContent = this.stateLabel(); badge.dataset.state = this.requestError ? "error" : value?.state || "unknown";
    }
    const error = this.requestError || value?.error || "";
    this.h("[data-header-error]").textContent = error; this.h("[data-header-error]").hidden = !error;
    this.h("[data-avatar-placeholder]").textContent = profile?.name ? Array.from(profile.name)[0] : "B站";
    const avatarKey = profile?.avatar_url ? `${profile.uid}:${value.checked_at ?? "unverified"}` : null;
    if (avatarKey !== this.avatarKey) {
      this.avatarKey = avatarKey; this.h("[data-avatar]").hidden = true; this.h("[data-avatar-placeholder]").hidden = false;
      if (avatarKey) this.h("[data-avatar]").src = `/api/account/bilibili/avatar?v=${encodeURIComponent(avatarKey)}`;
      else this.h("[data-avatar]").removeAttribute("src");
    }
    if (this.settings) {
      this.s("[data-sessdata-state]").textContent = value ? value.configured?.sessdata ? "已保存（不回显）" : "未保存" : "待读取";
      this.s("[data-csrf-state]").textContent = value ? value.configured?.csrf ? "已保存（不回显）" : "未保存" : "待读取";
      const checked = typeof value?.checked_at === "number" ? new Date(value.checked_at * 1000) : null;
      this.s("[data-checked]").textContent = checked && !Number.isNaN(checked.getTime()) ? checked.toLocaleString("zh-CN", {hour12: false}) : "尚未核验";
      const warnings = Array.isArray(value?.warnings) ? value.warnings.filter(item => typeof item === "string").join("；") : "";
      this.s("[data-account-warnings]").textContent = warnings; this.s("[data-account-warnings]").hidden = !warnings;
    }
    this.renderControls();
  }
  renderControls() {
    const busy = this.pending || this.loading, configured = !!this.value?.configured?.sessdata;
    const refresh = this.h("[data-refresh]"); refresh.disabled = !this.active || busy || (!configured && !this.requestError);
    refresh.textContent = this.loading ? "读取中…" : this.pending ? "处理中…" : this.requestError ? "重试读取" : this.value?.state === "logged_in" ? "刷新账号" : "验证登录";
    refresh.setAttribute("aria-busy", String(busy));
    if (!this.settings) return;
    this.s("[data-credentials-fields]").disabled = !this.active || this.pending;
    this.s("[data-save]").disabled = !this.active || this.pending || !(this.s("[data-sessdata]").value.trim() || this.s("[data-csrf]").value.trim());
    this.s("[data-save]").textContent = this.pending ? "处理中…" : "保存账号凭证";
    this.s("[data-verify]").disabled = !this.active || busy || !configured;
    this.s("[data-clear]").disabled = !this.active || this.pending || !(this.value?.configured?.sessdata || this.value?.configured?.csrf);
    this.s("[data-reset-open]").disabled = !this.active || this.resetPending;
  }
  feedback(text, error = false) {
    if (this.settings) { const target = this.s("[data-account-feedback]"); target.textContent = text; target.classList.toggle("error-text", error); }
    else this.notify(text, error);
  }
  async action(work) {
    if (!this.active || this.pending) return;
    this.pending = true; this.sequence += 1; this.loading = false; this.renderControls();
    try { await work(); }
    catch (error) { this.feedback(this.safeError(error), true); }
    finally { this.pending = false; this.sensitiveValues = []; this.renderControls(); }
  }
  async saveCredentials() {
    if (!this.settings) return;
    const sessdata = this.s("[data-sessdata]").value.trim(), csrf = this.s("[data-csrf]").value.trim();
    if (!sessdata && !csrf) return;
    this.sensitiveValues = [sessdata, csrf];
    await this.action(async () => {
      const value = await this.api("/api/account/bilibili", {method: "PUT", body: {...(sessdata ? {sessdata} : {}), ...(csrf ? {csrf} : {})}});
      if (!this.active) return;
      this.s("[data-sessdata]").value = ""; this.s("[data-csrf]").value = "";
      this.accept(value, true); this.feedback("账号凭证已保存，正在验证登录…");
      this.lastRefreshAttempt = Date.now();
      try {
        const verified = await this.api("/api/account/bilibili/refresh", {method: "POST"});
        if (!this.active) return;
        this.accept(verified, true); this.feedback(verified.state === "logged_in" ? "账号凭证已保存，登录与资料已核验。" : `凭证已保存。${verified.error || "核验结果：" + this.stateLabel()}`, verified.state !== "logged_in");
      } catch (error) { this.feedback(`凭证已保存，暂未完成核验：${this.safeError(error)}`, true); }
    });
  }
  async clearCredentials() {
    if (!this.settings || !confirm("清除已保存的 B站 SESSDATA 与 CSRF 凭证？需要重新配置后才能使用此账号。")) return;
    await this.action(async () => {
      const value = await this.api("/api/account/bilibili", {method: "PUT", body: {clear: true}});
      if (!this.active) return;
      this.accept(value, true);
      this.s("[data-sessdata]").value = ""; this.s("[data-csrf]").value = ""; this.feedback("已清除 B站账号凭证。");
    });
  }
  async refreshProfile() {
    await this.action(async () => {
      this.lastRefreshAttempt = Date.now();
      const value = await this.api("/api/account/bilibili/refresh", {method: "POST"});
      if (!this.active) return;
      this.accept(value, true);
      this.feedback(value.state === "logged_in" ? "账号资料已核验并更新。" : value.error || `核验结果：${this.stateLabel()}。`, value.state !== "logged_in");
    });
  }
  openTokenReset() {
    if (!this.active || this.resetPending) return;
    this.d("[data-reset-confirm]").hidden = false; this.d("[data-reset-result]").hidden = true;
    this.d("[data-new-token]").value = ""; this.d("[data-reset-feedback]").textContent = "";
    this.dialog.showModal(); this.d("[data-reset-cancel]").focus();
  }
  async resetToken() {
    if (!this.active || this.resetPending) return;
    this.resetPending = true; this.renderControls();
    this.d("[data-reset-confirm-button]").disabled = true; this.d("[data-reset-cancel]").disabled = true;
    this.d("[data-reset-feedback]").textContent = "正在重置管理令牌…";
    try {
      const result = await this.api("/api/auth/reset-token", {method: "POST"});
      if (typeof result.token !== "string" || !result.token) throw new Error("重置响应未提供新令牌，请查看服务的管理令牌恢复方式。");
      this.tokenPresented = true; this.d("[data-new-token]").value = result.token;
      this.d("[data-reset-confirm]").hidden = true; this.d("[data-reset-result]").hidden = false;
      this.d("[data-reset-feedback]").textContent = "尚未复制，请先保存新令牌。";
      this.onSessionRevoked(); this.d("[data-token-copy]").focus();
    } catch (error) { this.d("[data-reset-feedback]").textContent = this.safeError(error); }
    finally { this.resetPending = false; this.d("[data-reset-confirm-button]").disabled = false; this.d("[data-reset-cancel]").disabled = false; this.renderControls(); }
  }
  async copyToken() {
    const field = this.d("[data-new-token]"); if (!field.value) return;
    try { await navigator.clipboard.writeText(field.value); this.d("[data-reset-feedback]").textContent = "已复制新令牌。保存后可返回登录。"; }
    catch { field.focus(); field.select(); this.d("[data-reset-feedback]").textContent = "自动复制不可用，已选中新令牌，可手动复制。"; }
  }
}
