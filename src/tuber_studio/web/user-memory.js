export class UserMemoryEditor {
  constructor(api) {
    this.api = api;
    this.dialog = document.createElement("dialog");
    this.dialog.className = "account-dialog";
    this.dialog.setAttribute("aria-labelledby", "user-memory-heading");
    this.dialog.innerHTML = `<h2 id="user-memory-heading">用户档案</h2>
      <p data-identity class="hint"></p>
      <form><fieldset>
        <label><input name="personalization_enabled" type="checkbox"> 在评论和私信中使用个性化</label>
        <label>已确认的称呼<input name="nickname" maxlength="80"></label>
        <label>已确认的偏好（每行一项）<textarea name="preferences" rows="3"></textarea></label>
        <label>重要互动（每行一项）<textarea name="important_events" rows="3"></textarea></label>
        <p data-summary class="hint"></p>
        <div class="actions"><button type="submit" class="primary">确认并保存</button>
          <button data-clear type="button" class="danger">清空档案…</button></div>
      </fieldset></form>
      <p data-feedback class="hint" role="status"></p>
      <button data-close type="button">关闭</button>`;
    document.body.append(this.dialog);
    this.form = this.dialog.querySelector("form");
    this.fields = this.form.elements;
    this.fieldset = this.form.querySelector("fieldset");
    this.feedback = this.dialog.querySelector("[data-feedback]");
    this.dialog.querySelector("[data-close]").onclick = () => this.close();
    this.form.onsubmit = event => {
      event.preventDefault();
      const entries = name => this.fields[name].value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      void this.request("PUT", {
        personalization_enabled: this.fields.personalization_enabled.checked,
        nickname: this.fields.nickname.value,
        preferences: entries("preferences"),
        important_events: entries("important_events"),
      });
    };
    this.dialog.querySelector("[data-clear]").onclick = () => {
      if (confirm("清空该用户在当前账号和角色下的称呼、偏好、重要互动与好感度？个性化开关会保留。")) {
        void this.request("DELETE");
      }
    };
  }

  close() { this.dialog.close(); }

  open({ platform, userId, accountId, characterId, name, onChange }) {
    const query = new URLSearchParams({ account_id: accountId, character_id: characterId });
    this.selection = {
      path: `/api/user-memory/${encodeURIComponent(platform)}/${encodeURIComponent(userId)}?${query}`,
      onChange,
    };
    this.form.reset();
    this.dialog.querySelector("[data-summary]").textContent = "";
    this.dialog.querySelector("[data-identity]").textContent = `${name || "观众"} · UID ${userId} · ${characterId}`;
    if (!this.dialog.open) this.dialog.showModal();
    void this.request("GET");
  }

  async request(method, body) {
    const selection = this.selection;
    const current = () => this.selection === selection && this.dialog.open;
    this.fieldset.disabled = true;
    this.feedback.textContent = method === "GET" ? "正在读取档案…" : "正在更新档案…";
    try {
      const profile = await this.api(selection.path, { method, body });
      if (current()) {
        this.fields.personalization_enabled.checked = profile.personalization_enabled;
        this.fields.nickname.value = profile.nickname;
        this.fields.preferences.value = profile.preferences.join("\n");
        this.fields.important_events.value = profile.important_events.join("\n");
        const last = profile.last_interaction_at ? new Date(profile.last_interaction_at * 1000).toLocaleString() : "尚无记录";
        this.dialog.querySelector("[data-summary]").textContent = `好感度 ${profile.affinity} / 100 · ${profile.stage} · 最近互动 ${last}`;
        this.feedback.textContent = { GET: "", PUT: "已保存用户档案。", DELETE: "已清空用户档案。" }[method];
        this.fieldset.disabled = false;
      }
      if (method !== "GET") void selection.onChange?.();
    } catch (error) {
      if (current()) {
        this.feedback.textContent = error.message;
        this.fieldset.disabled = method === "GET";
      }
    }
  }
}
