import {api} from "./api.js";

const path = "/api/settings/performance";

export class CharacterDisplay extends EventTarget {
  value = null;
  saving = false;
  revision = 0;

  constructor(request) { super(); this.request = request; }

  publish(value) {
    if (typeof value?.pets_enabled !== "boolean" || typeof value?.avatar_enabled !== "boolean")
      throw Error("无法读取角色显示设置");
    this.value = value;
    this.dispatchEvent(new Event("change"));
  }

  reset() {
    this.revision++;
    this.value = null;
    this.saving = false;
    this.dispatchEvent(new Event("change"));
  }

  async refresh() {
    if (this.saving) return;
    const revision = ++this.revision;
    const data = await this.request(path);
    if (revision === this.revision) this.publish(data);
  }

  async save(key, enabled) {
    if (this.saving || !this.value) return;
    const revision = ++this.revision;
    this.saving = true;
    this.dispatchEvent(new Event("change"));
    try {
      const data = await this.request(path, {method: "PUT", body: {[key]: enabled}});
      if (revision === this.revision) this.publish(data);
    } finally {
      if (revision === this.revision) {
        this.saving = false;
        this.dispatchEvent(new Event("change"));
      }
    }
  }
}

export const characterDisplay = new CharacterDisplay(api);

export function bindDisplaySettings(report) {
  for (const input of document.querySelectorAll("[data-display-setting]")) {
    const key = input.dataset.displaySetting;
    const render = () => {
      if (!characterDisplay.saving) input.checked = characterDisplay.value?.[key] === true;
      input.disabled = !characterDisplay.value || characterDisplay.saving;
    };
    characterDisplay.addEventListener("change", render);
    input.onchange = () => characterDisplay.save(key, input.checked).catch(error => report(error.message));
    render();
  }
}
