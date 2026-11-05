"use strict";

class InferenceNodePanel {
  constructor(root, api, notify) {
    this.root = root; this.api = api; this.notify = notify; this.busy = false; this.dirty = false;
    root.innerHTML = `<div class="panel-heading"><h2>Jetson 推理</h2><span data-state class="badge">待读取</span></div>
      <p class="hint">通过局域网连接 Jetson。账号、对话和任务保存在业务电脑。</p>
      <dl data-summary class="description-list"></dl>
      <form data-pair autocomplete="off"><div class="form-grid"><label>推理节点地址<input data-url type="url" placeholder="http://jetson.local:9012" required></label><label>配对凭据<input data-token type="password" autocomplete="new-password" spellcheck="false" placeholder="同一节点留空保留；更换节点须填写"></label></div>
      <div class="actions"><button type="submit">保存并连接</button><button data-check type="button">检查连接</button></div></form>
      <p data-feedback class="hint" role="status">配对后重启与代码更新会保留连接设置。</p>
      <details><summary>高级诊断与维护</summary>
      <dl data-diagnostics class="description-list"></dl>
      <div class="actions"><button data-probe type="button">验证视觉能力</button><button data-idle type="button">释放推理资源</button></div>
      <div data-storage></div></details>`;
    this.$ = selector => root.querySelector(selector);
    this.$('[data-url]').oninput = () => { this.dirty = true; };
    this.$('[data-pair]').onsubmit = event => { event.preventDefault(); this.action(async () => {
      const token = this.$('[data-token]').value.trim();
      const body = {base_url: this.$('[data-url]').value.trim(), ...(token ? {token} : {})};
      const value = await api('/api/inference/node', {method:'PUT', body});
      this.$('[data-token]').value = ''; this.dirty = false; this.render(value);
      return '已连接，运营配置已保存。';
    }); };
    this.$('[data-check]').onclick = () => this.action(async () => { this.render(await api('/api/inference/node/check', {method:'POST'})); return '节点能力已刷新。'; });
    this.$('[data-probe]').onclick = () => this.action(async () => {
      this.feedback('正在用角色图片验证视觉接口并测量节点内存；可以继续浏览其他页面。');
      const result = await api('/api/inference/node/vision-probe', {method:'POST'});
      return result.capabilities.admitted ? '视觉接口与资源验证通过。' : '图像接口已运行，资源条件未通过；可查看节点状态后重试。';
    });
    this.$('[data-idle]').onclick = () => this.action(async () => { const result = await api('/api/inference/node/idle', {method:'POST'}); return `模型已卸载，KV 已释放；清理 ${result.cache_result.removed_files} 个推理缓存文件。下一次生成会重新加载。`; });
  }
  feedback(message, error = false) { const item = this.$('[data-feedback]'); item.textContent = message; item.classList.toggle('error-text', error); }
  async action(work) {
    if (this.busy) return; this.busy = true; this.root.querySelectorAll('button').forEach(b => b.disabled = true);
    try { const message = await work(); if (message) this.feedback(message); }
    catch (error) { let message = error.message; const token = this.$('[data-token]').value; if (token) message = message.split(token).join('[凭据已隐藏]'); this.feedback(message, true); }
    finally { this.busy = false; this.root.querySelectorAll('button').forEach(b => b.disabled = false); }
  }
  async refresh() {
    if (this.busy || this.root.closest('[hidden]')) return;
    try { this.render(await this.api('/api/inference/node')); } catch (error) { this.feedback(error.message, true); }
  }
  render(value) {
    if (!this.dirty) this.$('[data-url]').value = value.base_url || '';
    const caps = value.capabilities, resource = caps?.resource;
    this.$('[data-state]').textContent = value.error ? '连接异常' : caps ? '已连接' : '待检查';
    const mode = value.mode === 'live' ? '直播' : value.mode === 'text' ? '运营' : '—';
    const render = (selector, items) => {
      const list = this.$(selector);
      list.replaceChildren();
      for (const [name, text] of items) {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = name;
        dd.textContent = text || '—';
        list.append(dt, dd);
      }
    };
    render('[data-summary]', [['模式', mode], ['当前模型', caps?.model || value.model]]);
    render('[data-diagnostics]', [
      ['业务数据目录', value.business_data],
      ['节点可用内存', this.bytes(resource?.available_bytes)],
      ['节点进程内存', this.bytes(resource?.process_tree_rss_bytes)],
      ['最近检查', caps?.checked_at ? new Date(caps.checked_at * 1000).toLocaleString('zh-CN') : '尚未检查'],
      ['异常', value.error],
    ]);
    const storage = this.$('[data-storage]'); storage.replaceChildren();
    for (const entry of caps?.storage || []) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = `${entry.path} · ${this.bytes(entry.used_bytes)}${entry.protected ? ' · 受保护' : ''}`; storage.append(p); }
  }
  bytes(value) { if (typeof value !== 'number') return '未测量'; const n = Math.max(0, Math.min(3, Math.floor(Math.log2(Math.max(1, value)) / 10))); return `${(value / 1024 ** n).toFixed(n ? 2 : 0)} ${['B','KiB','MiB','GiB'][n]}`; }
}
