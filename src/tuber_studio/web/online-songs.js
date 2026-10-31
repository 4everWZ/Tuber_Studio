"use strict";
class OnlineSongRequests {
  constructor(api, feedback) {
    this.api = api; this.feedback = feedback; this.value = null; this.busy = false; this.candidateSignature = "";
    this.$ = id => document.getElementById("online-" + id);
    this.$("enabled").onchange = () => this.action(this.$("enabled"), async () => {
      this.update(await api("/api/live/online", {method:"PUT", body:{...this.value.settings, enabled:this.$("enabled").checked}}), true);
    });
    this.$("restart").onclick = () => this.action(this.$("restart"), async () => this.update(await api("/api/live/online/restart", {method:"POST"}), true));
    this.$("settings").onsubmit = event => {
      event.preventDefault(); this.action(event.submitter, async () => {
        const config = {...this.value.settings, usage:this.$("usage").value, sources:this.$("sources").value.split(","), quality:this.$("quality").value,
          fallback_bitrate:Number(this.$("bitrate").value), allow_lower_quality:this.$("lower").checked,
          cache_limit_mib:Number(this.$("cache").value), max_download_mib:Number(this.$("download").value),
          max_duration_seconds:Number(this.$("duration").value), license_note:this.$("license").value,
          live_allowed:this.$("live-allowed").checked, commercial_allowed:this.$("commercial").checked};
        this.update(await api("/api/live/online", {method:"PUT", body:config}), true);
        feedback("在线点歌设置已保存。");
      });
    };
    this.$("search-form").onsubmit = event => {
      event.preventDefault(); this.action(event.submitter, async () => {
        this.$("search-state").textContent = "正在搜索在线曲库…";
        const result = await api("/api/live/online/search?q=" + encodeURIComponent(this.$("query").value.trim()));
        this.$("results").replaceChildren(...result.songs.map(song => this.row(song, "点歌", () => api("/api/live/mock-event", {method:"POST", body:{type:"song", display_name:"主播", platform_user_id:"operator", text:song.title, metadata:{online_id:song.id, mode:"direct"}}}))));
        this.$("search-state").textContent = result.songs.length ? `找到 ${result.songs.length} 首，请确认歌手、专辑与版本。音质将在取到音频后显示。` : "没有找到歌曲，请补充歌手或版本。";
      });
    };
  }
  element(tag, text, cls) { const element = document.createElement(tag); if (text != null) element.textContent = text; if (cls) element.className = cls; return element; }
  async action(button, work) {
    const control = button === this.$("enabled") || button === this.$("restart") || button.form === this.$("settings");
    if (control && this.busy) { this.$("enabled").checked = this.value.settings.enabled; return; }
    if (control) this.busy = true;
    button.disabled = true; button.setAttribute("aria-busy", "true"); this.$("error").textContent = "";
    try { await work(); }
    catch (error) {
      if (control || this.value?.settings.enabled) this.$("error").textContent = error.message;
      if (button === this.$("search-button")) this.$("search-state").textContent = this.value?.settings.enabled ? "搜索失败，请重试。" : "搜索已取消。";
      if (button === this.$("enabled") && this.value) button.checked = this.value.settings.enabled;
    }
    finally { if (control) this.busy = false; button.disabled = false; button.removeAttribute("aria-busy"); }
  }
  row(song, label, request) {
    const row = this.element("div", null, "song-row"), info = this.element("div", null, "song-info");
    info.append(this.element("strong", song.title), this.element("small", `${song.artist} · ${song.album || "未知专辑"} · ${Math.floor(song.duration/60)}:${String(Math.round(song.duration)%60).padStart(2,"0")}`));
    const image = this.element("img");
    try { const url = new URL(song.cover); if (url.hostname.endsWith(".music.126.net") && ["http:","https:"].includes(url.protocol)) { url.protocol = "https:"; image.src = url.href; image.alt = ""; image.loading = "lazy"; image.referrerPolicy = "no-referrer"; image.width = image.height = 44; row.append(image); } } catch (_) { /* A cover is optional. */ }
    const button = this.element("button", label); button.onclick = () => this.action(button, async () => { const result = await request(); if (result.status !== "queued") throw new Error(result.reason || "未能加入队列"); this.feedback(`已加入队列：${song.title} · ${song.artist}`); });
    row.append(info, button); return row;
  }
  update(value, fields = false) {
    const initial = !this.value; this.value = value; const {settings, component} = value;
    if (!this.busy || fields) this.$("enabled").checked = settings.enabled;
    this.$("status").textContent = `${settings.enabled ? component.running ? "点歌组件运行中" : "点歌组件未运行" : "在线点歌已关闭"} · ${settings.usage === "local_test" ? "仅本机／OBS测试" : "直播用途"} · 缓存 ${(value.cache_bytes/1048576).toFixed(1)} / ${settings.cache_limit_mib} MiB${component.running && !component.migu_configured ? " · 咪咕尚未配置凭证" : ""}`;
    if (component.error || this.$("error").textContent === this.componentError) this.$("error").textContent = component.error || "";
    this.componentError = component.error;
    this.$("install").hidden = component.installed;
    this.$("restart").hidden = !settings.enabled;
    this.$("controls").hidden = !settings.enabled || !component.running;
    if (!settings.enabled) this.$("results").replaceChildren();
    if (initial || fields) {
      this.$("usage").value = settings.usage; this.$("sources").value = settings.sources.join(","); this.$("quality").value = settings.quality;
      this.$("bitrate").value = settings.fallback_bitrate; this.$("cache").value = settings.cache_limit_mib;
      this.$("download").value = settings.max_download_mib; this.$("duration").value = settings.max_duration_seconds;
      this.$("lower").checked = settings.allow_lower_quality; this.$("license").value = settings.license_note;
      this.$("live-allowed").checked = settings.live_allowed; this.$("commercial").checked = settings.commercial_allowed;
    }
    const signature = JSON.stringify(value.candidates);
    if (signature !== this.candidateSignature) {
      this.candidateSignature = signature; this.$("candidates").replaceChildren();
      for (const choice of value.candidates) {
        const group = this.element("section", null, "online-choice");
        group.append(this.element("h3", `${choice.requester} 的点歌 · 待确认版本`));
        choice.songs.forEach((song, index) => group.append(this.row(song, `确认 ${index + 1}`, () => this.api("/api/live/online/confirm", {method:"POST", body:{token:choice.token, number:index+1}}))));
        this.$("candidates").append(group);
      }
    }
  }
}
