"use strict";

// Shared by public drafts and the private-message editor; each host receives
// only its own entity DTO. No cross-channel fetch or client-side resend loop.
class OperationContinuationView {
  constructor(root, api, notify, refresh) {
    this.root = root;
    this.api = api;
    this.notify = notify;
    this.refresh = refresh;
    this.pending = false;
    this.root.hidden = true;
    root.classList.add("operation-continuation", "subsection");
    root.innerHTML = `<div class="panel-heading"><h3>处理进度与修订</h3><span data-continuation-state class="badge"></span></div>
      <p data-continuation-summary class="hint"></p><p data-continuation-feedback class="operation-feedback"></p>
      <p data-continuation-reconciliation class="hint" role="status"></p>
      <p data-continuation-budget class="hint"></p><p data-continuation-note class="hint" role="status"></p>
      <button type="button" data-continuation-resume>继续处理</button><button type="button" data-continuation-reconcile hidden>核对发送结果</button>
      <p data-continuation-private hidden><a href="#messages">到私信工作台查看生成进度</a></p>
      <details data-continuation-history><summary>查看原稿与修订记录</summary><div data-continuation-versions></div></details>`;
    this.$ = selector => root.querySelector(selector);
    this.$("[data-continuation-resume]").onclick = () => this.resume();
    this.$("[data-continuation-reconcile]").onclick = () => this.resume(true);
  }
  element(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  label(state) {
    return {pending:"等待处理", deciding:"Agent 处理中", awaiting_review:"等待审核", waiting:"等待下一步", needs_material:"等待补充材料", checkpoint:"进度已保存", needs_reconciliation:"发送结果待核对", completed:"处理完成", abandoned:"已暂缓", cancelled:"已取消"}[state] || state || "状态未返回";
  }
  body(value) {
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object") return "";
    for (const key of ["text", "spoken_text", "content", "body"]) if (typeof value[key] === "string") return value[key];
    return JSON.stringify(value, null, 2);
  }
  feedback(value) {
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object") return "";
    const kind = {platform_error:"平台反馈", style_diagnostic:"表达建议", resource_limit:"额度反馈", model_error:"模型反馈", platform_uncertain:"平台结果待核对"}[value.kind] || value.kind;
    return [kind, value.code == null ? null : `错误码 ${value.code}`, value.message].filter(item => item != null && item !== "").map(String).join(" · ");
  }
  render(value, {channel, localId, dirty = false, entityStatus, privateContext = false, displayChannel = channel} = {}) {
    const privateOutput = value?.private_handoff === true || ["private", "unresolved"].includes(value?.output_visibility);
    const generation = value?.phase === "generation" && value.state !== "needs_reconciliation" && entityStatus !== "publish_uncertain";
    const target = generation ? value.resume_target : null;
    const targetChannelAllowed = target && (target.channel === channel || privateContext && displayChannel === "messages" && privateOutput && ["comments", "browsing"].includes(target.channel));
    const validTarget = targetChannelAllowed && typeof target.local_id === "string" && target.local_id.startsWith("generation:") && target.local_id.length > "generation:".length;
    if (generation) {localId = validTarget ? target.local_id : null; if (validTarget) channel = target.channel;}
    this.value = value;
    this.binding = {channel, localId, dirty, entityStatus, privateContext, displayChannel};
    this.key = value ? `${channel}:${localId}:${value.id}` : null;
    this.root.hidden = !value;
    if (!value) {
      for (const selector of ["[data-continuation-state]", "[data-continuation-summary]", "[data-continuation-feedback]", "[data-continuation-reconciliation]", "[data-continuation-budget]", "[data-continuation-note]"]) this.$(selector).textContent = "";
      this.$("[data-continuation-versions]").replaceChildren();
      this.signature = null;
      this.$("[data-continuation-history]").open = false;
      return;
    }
    this.$("[data-continuation-state]").textContent = this.label(value.state);
    const publicHandoff = privateOutput && displayChannel !== "messages";
    this.$("[data-continuation-private]").hidden = !publicHandoff;
    this.$("[data-continuation-history]").hidden = publicHandoff;
    if (publicHandoff) {
      this.$("[data-continuation-state]").textContent = "生成原稿在私信工作台";
      for (const selector of ["[data-continuation-summary]", "[data-continuation-feedback]", "[data-continuation-reconciliation]", "[data-continuation-budget]", "[data-continuation-note]"]) this.$(selector).textContent = "";
      this.$("[data-continuation-versions]").replaceChildren();
      this.$("[data-continuation-resume]").hidden = true;
      this.$("[data-continuation-reconcile]").hidden = true;
      this.signature = null;
      return;
    }
    this.$("[data-continuation-summary]").textContent = `本轮恢复 ${value.recoveries ?? "—"} / ${value.max_recoveries ?? "—"} 次 · 累计 ${value.total_recoveries ?? value.recoveries ?? "—"} 次`;
    const feedback = this.feedback(value.feedback), trigger = this.feedback(value.trigger_feedback);
    const feedbackText = feedback === trigger || !trigger ? feedback : `${trigger}\n当前：${feedback || this.label(value.state)}`;
    this.$("[data-continuation-feedback]").textContent = value.state === "completed" && feedbackText ? `此前反馈：${feedbackText}` : feedbackText;
    const check = (Array.isArray(value.revisions) ? value.revisions : []).findLast(row => row.phase === "reconciliation");
    const checkLabels = {visible:"已找到匹配的发送记录", not_visible:"已找到匹配记录，当前不可见", not_found:"本次读取未找到匹配记录", ambiguous:"存在多条候选，仍需核对", check_failed:"本次平台读取未成功", unsupported:"当前渠道尚不支持读回核对"};
    this.$("[data-continuation-reconciliation]").textContent = check ? `平台核对：${checkLabels[check.status] || check.status}` : "";
    const budget = value.budget || {}, limits = value.limits || {};
    const budgetText = [["调用", "model_calls"], ["输入 tokens", "input_tokens"], ["输出 tokens", "output_tokens"]].filter(([,key]) => budget[key] != null).map(([name,key]) => `${name} ${budget[key]}${limits[`max_${key}`] != null ? " / " + limits[`max_${key}`] : ""}`);
    if (Number.isFinite(value.deadline)) budgetText.push(`本轮期限 ${new Date(value.deadline * 1000).toLocaleString("zh-CN")}`);
    this.$("[data-continuation-budget]").textContent = budgetText.join(" · ");
    const reconciling = value.state === "needs_reconciliation" || entityStatus === "publish_uncertain";
    this.$("[data-continuation-note]").textContent = dirty ? reconciling ? "核对将读取平台结果，保留本地编辑。" : "有未保存修改，保存后再继续处理。" : reconciling ? "平台结果尚待确认。" : value.state === "pending" ? "等待 Agent 处理，后续反馈会保留在下方。" : value.state === "deciding" ? "Agent 正在处理，修订与平台反馈会保留在下方。" : value.state === "checkpoint" ? "进度已保存，可在后续授权轮继续。" : "";
    if (this.resumeError?.key === this.key && this.resumeError.state === value.state) this.$("[data-continuation-note]").textContent = this.resumeError.message;
    if (generation && !validTarget) this.$("[data-continuation-note]").textContent = value.can_resume ? "生成恢复入口尚未返回，请刷新后继续。" : "此记录保留供查看。";
    const resume = this.$("[data-continuation-resume]");
    resume.hidden = !value.can_resume || reconciling;
    resume.disabled = this.pending || dirty || !value.can_resume || reconciling || localId == null;
    resume.textContent = this.pending ? "正在继续…" : "继续处理";
    const reconcile = this.$("[data-continuation-reconcile]");
    reconcile.hidden = !reconciling;
    reconcile.disabled = this.pending;
    reconcile.textContent = this.pending ? "正在核对…" : "核对发送结果";
    const signature = JSON.stringify([this.key, value.original_output, value.current_output, value.revisions]);
    if (signature !== this.signature) {
      this.signature = signature;
      const list = this.$("[data-continuation-versions]");
      list.replaceChildren();
      const original = this.body(value.original_output);
      if (original) list.append(this.element("h4", "原稿"), this.element("pre", original, "operation-revision-text"));
      const current = this.body(value.current_output);
      if (current && current !== original) list.append(this.element("h4", "当前修订"), this.element("pre", current, "operation-revision-text"));
      for (const [index, revision] of (Array.isArray(value.revisions) ? value.revisions : []).entries()) {
        const detail = this.element("details", null, "operation-revision");
        const phase = {feedback:"收到反馈", decision:"Agent 判断", apply:"应用修订", checkpoint:"保存进度", resume:"继续处理", review:"审核", publish:"平台提交", reconciliation:"平台结果核对"}[revision.phase] || revision.phase || "修订";
        const action = {revise:"修订正文", defer:"稍后处理", abandon:"暂缓", wait:"等待", resume:"继续", received:"已收到", completed:"已完成", pending:"等待处理", failed:"失败", cancelled:"已取消", ...checkLabels}[revision.action || revision.status] || revision.action || revision.status || "已记录";
        detail.append(this.element("summary", `记录 ${revision.version ?? index + 1} · ${phase} · ${action}`));
        const text = this.body(revision.output), feedback = this.feedback(revision.feedback);
        if (text) detail.append(this.element("pre", text, "operation-revision-text"));
        if (feedback) detail.append(this.element("p", feedback, "operation-feedback"));
        list.append(detail);
      }
      if (!list.children.length) list.append(this.element("p", "暂无正文修订记录。", "hint"));
    }
  }
  async resume(checkOnly = false) {
    const value = this.value, {channel, localId, dirty, entityStatus} = this.binding || {}, key = this.key;
    const uncertain = value?.state === "needs_reconciliation" || entityStatus === "publish_uncertain";
    const publicPrivate = (value?.private_handoff === true || ["private", "unresolved"].includes(value?.output_visibility)) && this.binding?.displayChannel !== "messages";
    if (this.pending || !value?.id || publicPrivate || (checkOnly ? !uncertain : !value.can_resume || dirty || uncertain) || !["comments", "messages", "dynamics", "browsing"].includes(channel) || localId == null) return;
    this.pending = true;
    this.resumeError = null;
    this.render(value, this.binding);
    try {
      const result = await this.api(`/api/operations/continuations/${channel}/${encodeURIComponent(localId)}/resume`, {method:"POST", body:{continuation_id:value.id}});
      if (this.key === key && result.continuation) this.render(result.continuation, this.binding);
      await this.refresh();
    } catch (error) {
      this.notify(error.message);
      if (this.key === key) this.resumeError = {key, state:this.value.state, message:error.message};
    } finally {
      this.pending = false;
      if (this.value) this.render(this.value, this.binding);
    }
  }
}

class OperationRecoveryPolicyView {
  constructor(root, api, notify) {
    this.root = root; this.api = api; this.notify = notify; this.dirty = false; this.pending = false; this.loaded = false;
    root.innerHTML = `<h3>反馈后的继续处理</h3><form data-recovery-policy class="actions"><label>每轮恢复次数<input name="max_recoveries" type="number" min="0" max="64" step="1" required disabled></label><button type="submit" disabled>保存恢复额度</button><button type="button" data-recovery-refresh>重新读取</button></form><p class="hint">评论、私信、动态与浏览共用。0 表示关闭自动恢复，已保存进度仍保留。</p><p data-recovery-state class="hint" role="status"></p>`;
    this.$ = selector => root.querySelector(selector);
    this.$("input").oninput = () => {this.dirty = true; this.$("[data-recovery-state]").textContent = "恢复额度有未保存修改。";};
    this.$("[data-recovery-refresh]").onclick = () => this.load();
    this.$("form").onsubmit = event => {event.preventDefault(); this.save();};
  }
  controls() {
    this.$("input").disabled = this.pending || !this.loaded;
    this.$("[type=submit]").disabled = this.pending || !this.loaded;
    this.$("[data-recovery-refresh]").disabled = this.pending;
  }
  async load() {
    if (this.pending) return;
    this.pending = true; this.controls();
    try {
      const result = await this.api("/api/operations/recovery-policy");
      if (!Number.isInteger(result.max_recoveries) || result.max_recoveries < 0 || result.max_recoveries > 64) throw new Error("恢复额度返回值无效，请重新读取。");
      this.loaded = true;
      if (!this.dirty) this.$("input").value = result.max_recoveries;
      this.$("[data-recovery-state]").textContent = this.dirty ? "已读取当前设置，保留未保存修改。" : `已保存：每轮 ${result.max_recoveries} 次。`;
    } catch (error) {this.$("[data-recovery-state]").textContent = `读取失败：${error.message}`;}
    finally {this.pending = false; this.controls();}
  }
  async save() {
    if (this.pending || !this.loaded || !this.$("form").reportValidity()) return;
    const count = Number(this.$("input").value);
    if (!Number.isInteger(count) || count < 0 || count > 64) return;
    this.pending = true; this.controls();
    try {
      const result = await this.api("/api/operations/recovery-policy", {method:"PUT", body:{max_recoveries:count}});
      if (result.max_recoveries !== count) throw new Error("保存结果未确认，请重新读取设置。");
      this.dirty = false; this.$("[data-recovery-state]").textContent = count ? `已保存：每轮最多恢复 ${count} 次。` : "已关闭自动恢复，现有检查点保留。";
    } catch (error) {this.notify(error.message); this.$("[data-recovery-state]").textContent = error.message;}
    finally {this.pending = false; this.controls();}
  }
}

window.OperationContinuationView = OperationContinuationView;
window.OperationRecoveryPolicyView = OperationRecoveryPolicyView;
