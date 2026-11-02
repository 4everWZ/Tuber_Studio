"use strict";

const memeEmotions = {idle: "平常", cheerful: "开心", angry: "生气", shy: "害羞", confused: "困惑", excited: "兴奋"};
let memePickerCount = 0;
function memeElement(tag, text, className) { const item = document.createElement(tag); if (text != null) item.textContent = text; if (className) item.className = className; return item; }
function memePreviewUrl(asset, character) { return `/api/memes/${encodeURIComponent(asset.asset_id)}/preview?character=${encodeURIComponent(character)}`; }
function memePreview(asset, character) {
  const frame = memeElement("div", null, "meme-preview");
  const fallback = memeElement("span", asset.missing ? "图片文件缺失" : "图片预览不可用", "hint");
  if (asset.missing) { frame.append(fallback); return frame; }
  const image = document.createElement("img"); image.loading = "lazy"; image.alt = asset.title || "表情包"; image.src = memePreviewUrl(asset, character);
  image.onerror = () => { image.hidden = true; if (!fallback.isConnected) frame.append(fallback); };
  frame.append(image); return frame;
}
function memeSourceLink(label, value) {
  try { const url = new URL(value); if (!["https:", "http:"].includes(url.protocol)) return null; const link = memeElement("a", label); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; return link; }
  catch { return null; }
}
function memeMetadata(asset) {
  const details = memeElement("details", null, "meme-metadata"); details.append(memeElement("summary", "来源与许可"));
  const source = asset.source || {}, license = asset.license || {};
  const sourceKind = {existing_user_assets: "已有素材", user_upload: "用户导入", licensed_pack: "许可素材包", generated: "生成图片"}[source.kind] || source.kind;
  const sourceLine = memeElement("p", `来源：${source.author || "未提供"}${sourceKind ? " · " + sourceKind : ""}`);
  const sourceLink = memeSourceLink("查看来源", source.url); if (sourceLink) { sourceLine.append(" · ", sourceLink); }
  const licenseLine = memeElement("p", `许可：${license.name || "未提供"}`);
  const licenseLink = memeSourceLink("许可说明", license.url); if (licenseLink) licenseLine.append(" · ", licenseLink);
  details.append(sourceLine, licenseLine);
  for (const [label, text] of [["来源说明", source.note], ["修订", source.revision], ["许可备注", license.note]]) if (text) details.append(memeElement("p", `${label}：${text}`));
  return details;
}

class MemeGallery {
  constructor(root, api, notify, onChange = () => {}) {
    this.root = root; this.api = api; this.notify = notify; this.onChange = onChange; this.character = null; this.items = []; this.pending = false; this.loading = false; this.sequence = 0;
    root.innerHTML = `<article class="panel meme-library"><div class="panel-heading"><h2>表情包图库</h2><button data-meme-refresh type="button">刷新图库</button></div><p class="hint">为当前角色管理私信与动态使用的表情包。已有素材默认禁用，可先预览来源与许可，再按需要启用；不会自动发送。</p><label class="meme-character">所属角色<select data-meme-character><option value="">正在读取角色…</option></select></label><p data-meme-state class="hint" role="status"></p><div data-meme-grid class="meme-grid"></div>
      <details class="meme-upload"><summary>导入表情包</summary><form data-meme-upload><fieldset data-meme-upload-fields class="meme-fields"><div class="form-grid"><label>名称<input name="title" maxlength="120" required></label><label>图片文件<input name="file" type="file" accept="image/png,image/jpeg,image/gif,image/webp,.png,.jpg,.jpeg,.gif,.webp" required></label></div><p class="hint">PNG、JPEG、GIF 或 WebP；最多8 MiB，宽高不超过4096，最多300帧。GIF按原文件保存。</p><fieldset class="meme-emotions"><legend>情绪（至少一项）</legend>${Object.entries(memeEmotions).map(([key, name]) => `<label><input type="checkbox" name="emotion" value="${key}"${key === "idle" ? " checked" : ""}>${name}</label>`).join("")}</fieldset><div class="form-grid"><label>来源作者<input name="author" maxlength="300" required placeholder="实际作者或权利人"></label><label>许可名称<input name="license_name" maxlength="300" required placeholder="实际许可或授权名称"></label></div><div class="form-grid"><label>来源链接（可选）<input name="source_url" type="url"></label><label>许可链接（可选）<input name="license_url" type="url"></label></div><label>来源备注（可选）<textarea name="source_note" rows="2" maxlength="2000"></textarea></label><label>许可备注（可选）<textarea name="license_note" rows="2" maxlength="2000"></textarea></label><div class="actions"><button type="submit" class="primary">上传到当前角色图库</button></div></fieldset></form></details><p data-meme-feedback class="hint" role="status"></p><p class="hint">当前未配置图片生成服务，可先导入已有图片。</p></article>`;
    this.$ = selector => root.querySelector(selector);
    this.$("[data-meme-refresh]").onclick = () => this.refresh();
    this.$("[data-meme-character]").onchange = () => { this.character = this.$("[data-meme-character]").value; this.refresh(); };
    this.$("[data-meme-upload]").onsubmit = event => { event.preventDefault(); this.upload(); };
  }
  setCharacters(characters, active) {
    const select = this.$("[data-meme-character]"), previous = this.character;
    select.replaceChildren(...characters.map(character => { const option = memeElement("option", `${character.name || character.id} · ${character.id}`); option.value = character.id; return option; }));
    this.character = characters.some(item => item.id === previous) ? previous : characters.some(item => item.id === active) ? active : characters[0]?.id || null;
    select.value = this.character || ""; this.controls();
  }
  feedback(text, error = false) { this.$("[data-meme-feedback]").textContent = text; this.$("[data-meme-feedback]").classList.toggle("error-text", error); }
  controls() {
    this.$("[data-meme-character]").disabled = this.pending;
    this.$("[data-meme-upload-fields]").disabled = this.pending || !this.character;
    this.$("[data-meme-refresh]").disabled = this.pending || this.loading || !this.character;
    for (const button of this.root.querySelectorAll("[data-meme-grid] button")) button.disabled = this.pending || this.loading || button.dataset.unavailable === "true";
  }
  async refresh() {
    const character = this.character; if (!character) { this.$("[data-meme-state]").textContent = "请选择所属角色。"; return; }
    const request = ++this.sequence; this.loading = true; this.controls();
    this.$("[data-meme-state]").textContent = "正在读取图库…";
    try {
      const value = await this.api(`/api/memes?character=${encodeURIComponent(character)}`);
      if (request !== this.sequence || character !== this.character) return;
      this.items = Array.isArray(value.items) ? value.items : []; this.render();
      this.$("[data-meme-state]").textContent = `${this.items.length} 张图片 · ${this.items.filter(item => item.enabled && !item.missing).length} 张可用`;
    } catch (error) { if (request === this.sequence) { this.$("[data-meme-state]").textContent = `图库读取失败：${error.message}`; this.$("[data-meme-grid]").replaceChildren(); } }
    finally { if (request === this.sequence) { this.loading = false; this.controls(); } }
  }
  render() {
    const grid = this.$("[data-meme-grid]"); grid.replaceChildren();
    if (!this.items.length) { grid.append(memeElement("p", "当前角色暂无表情包，可从下方导入图片。", "hint")); return; }
    for (const asset of this.items) {
      const card = memeElement("article", null, "meme-card"); card.append(memePreview(asset, this.character), memeElement("h3", asset.title));
      card.append(memeElement("p", `${asset.missing ? "文件缺失" : asset.enabled ? "已启用" : "已禁用"} · ${(asset.emotions || []).map(item => memeEmotions[item] || item).join(" / ")}`, "hint"));
      card.append(memeElement("p", `${asset.width} × ${asset.height} · ${asset.animated ? `${asset.frames} 帧动图` : "静态图"} · ${typeof asset.bytes === "number" ? (asset.bytes / 1024).toFixed(1) + " KiB" : "大小未知"}`, "hint"), memeMetadata(asset));
      const actions = memeElement("div", null, "actions");
      const toggle = memeElement("button", asset.enabled ? "禁用" : "为此角色启用"); toggle.type = "button"; toggle.setAttribute("aria-pressed", String(!!asset.enabled)); toggle.dataset.unavailable = String(!!asset.missing);
      toggle.onclick = () => this.action(async () => { await this.api(`/api/memes/${encodeURIComponent(asset.asset_id)}`, {method: "PUT", body: {character: this.character, enabled: !asset.enabled}}); await this.refresh(); this.feedback(asset.enabled ? "已禁用此表情包。" : "已为当前角色启用此表情包。"); });
      const remove = memeElement("button", "删除", "danger"); remove.type = "button"; remove.dataset.unavailable = String(!!asset.enabled); remove.title = asset.enabled ? "请先禁用再删除" : "草稿正在引用的图片由服务保护，无法删除";
      remove.onclick = () => { if (confirm(`删除「${asset.title}」？正在被草稿引用的图片无法删除。`)) this.action(async () => { await this.api(`/api/memes/${encodeURIComponent(asset.asset_id)}?character=${encodeURIComponent(this.character)}`, {method: "DELETE"}); await this.refresh(); this.feedback("图片已从图库删除。"); }); };
      actions.append(toggle, remove); card.append(actions); grid.append(card);
    }
    this.controls();
  }
  async action(work) { if (this.pending) return; this.pending = true; this.controls(); try { await work(); this.onChange(this.character); } catch (error) { this.feedback(error.message, true); } finally { this.pending = false; this.controls(); } }
  async upload() {
    const form = this.$("[data-meme-upload]"), file = form.elements.file.files[0];
    if (!file || !this.character) return;
    if (file.size > 8 * 1024 * 1024) { this.feedback("图片超过8 MiB，请选择更小的原文件。", true); return; }
    const emotions = Array.from(form.querySelectorAll('[name="emotion"]:checked'), item => item.value);
    if (!emotions.length) { this.feedback("请至少选择一种情绪。", true); return; }
    const body = {character: this.character, title: form.elements.title.value.trim(), emotions,
      source: {kind: "user_upload", author: form.elements.author.value.trim(), url: form.elements.source_url.value.trim(), revision: "", note: form.elements.source_note.value.trim()},
      license: {name: form.elements.license_name.value.trim(), url: form.elements.license_url.value.trim(), note: form.elements.license_note.value.trim()}};
    await this.action(async () => {
      body.content_base64 = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(new Error("图片文件读取失败，请重新选择。")); reader.onload = () => { const result = typeof reader.result === "string" ? reader.result.split(",", 2)[1] : null; if (!result) reject(new Error("图片文件未能读取为有效内容。")); else resolve(result); }; reader.readAsDataURL(file); });
      await this.api("/api/memes", {method: "POST", body}); form.reset(); await this.refresh(); this.feedback("图片已保存，可在图库预览并管理启用状态。");
    });
  }
}

class MemePicker {
  constructor(api, onSelect, {title = "选择私信表情包", description = "只显示此草稿所属角色已启用且可用的图片。选图后仍需审核，最终仅发送这一张图片。"} = {}) {
    this.api = api; this.onSelect = onSelect; this.sequence = 0; this.pending = false;
    const headingId = `meme-picker-heading-${++memePickerCount}`;
    this.dialog = document.createElement("dialog"); this.dialog.className = "meme-picker"; this.dialog.setAttribute("aria-labelledby", headingId);
    this.dialog.innerHTML = `<div class="panel-heading"><h2 id="${headingId}" data-picker-title></h2><button type="button" data-picker-close aria-label="关闭选图">关闭</button></div><p class="hint" data-picker-help></p><p data-picker-state role="status"></p><div data-picker-grid class="meme-grid"></div><div class="actions"><a href="#assets" data-picker-manage>管理表情包图库 →</a><button type="button" data-picker-refresh>重新读取</button></div>`;
    document.body.append(this.dialog); this.$ = selector => this.dialog.querySelector(selector);
    this.$("[data-picker-title]").textContent = title; this.$("[data-picker-help]").textContent = description;
    this.$("[data-picker-close]").onclick = () => this.close();
    this.$("[data-picker-manage]").onclick = event => { if (this.pending) { event.preventDefault(); return; } this.close(); };
    this.$("[data-picker-refresh]").onclick = () => this.load();
    this.dialog.addEventListener("cancel", event => { if (this.pending) event.preventDefault(); else this.sequence += 1; });
  }
  open(character, selected) { if (this.pending) return; this.character = character; this.selected = selected; this.dialog.showModal(); this.load(); }
  close(force = false) { if (this.pending && !force) return; this.sequence += 1; this.dialog.close(); }
  async load() {
    const request = ++this.sequence, character = this.character; this.$("[data-picker-state]").textContent = "正在读取可用图片…";
    this.$("[data-picker-grid]").replaceChildren(); this.$("[data-picker-refresh]").disabled = true;
    try {
      const value = await this.api(`/api/memes?character=${encodeURIComponent(character)}`);
      if (request !== this.sequence || !this.dialog.open) return;
      const items = (value.items || []).filter(item => item.enabled && !item.missing && (!Array.isArray(item.character_ids) || item.character_ids.includes(character)));
      this.$("[data-picker-state]").textContent = items.length ? `${items.length} 张可用图片` : "当前角色没有可用表情包，请先在图库启用或导入图片。";
      for (const asset of items) {
        const button = memeElement("button", null, "meme-choice"); button.type = "button"; button.setAttribute("aria-pressed", String(asset.asset_id === this.selected)); button.setAttribute("aria-label", `选用${asset.title}`);
        button.append(memePreview(asset, character), memeElement("strong", asset.title), memeElement("small", asset.license?.name || "许可未提供"));
        button.onclick = async () => {
          if (this.pending) return; this.pending = true; this.controls();
          try { await this.onSelect(asset.asset_id, asset); this.pending = false; this.close(); }
          catch (error) { this.$("[data-picker-state]").textContent = `选图未保存：${error.message}`; }
          finally { this.pending = false; this.controls(); }
        };
        this.$("[data-picker-grid]").append(button);
      }
    } catch (error) { if (request === this.sequence) this.$("[data-picker-state]").textContent = `图库读取失败：${error.message}`; }
    finally { if (request === this.sequence) this.$("[data-picker-refresh]").disabled = false; }
  }
  controls() { for (const button of this.dialog.querySelectorAll("button")) button.disabled = this.pending; this.$("[data-picker-manage]").setAttribute("aria-disabled", String(this.pending)); }
}
