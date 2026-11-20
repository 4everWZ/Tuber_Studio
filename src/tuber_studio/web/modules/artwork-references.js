import {api} from "./api.js";
import {webCharacterName} from "./core.js";

export function initArtworkReferences() {
  const scope = document.getElementById("artwork-reference-scope");
  const container = document.getElementById("artwork-reference-variants");
  const result = document.getElementById("artwork-reference-result");
  const slots = new Map();
  let characterId, revision = 0, data, busy = false;
  const path = () => "/api/settings/artwork-references/" + encodeURIComponent(characterId);

  function render() {
    for (const [variant, slot] of slots) {
      const ref = data?.variants?.[variant];
      slot.upload.disabled = !data || busy;
      slot.restore.disabled = busy || ref?.source !== "upload";
      slot.status.textContent = !ref ? "" : ref.source === "upload" ? "已上传" : ref.available ? "默认图" : "暂无默认图";
      slot.preview.hidden = !ref?.available;
      if (ref?.available && ref.preview_url) {
        const url = new URL(ref.preview_url, location.origin);
        const thumbnail = new URL(ref.thumbnail_url || ref.preview_url, location.origin);
        if (slot.image.src !== thumbnail.href) slot.image.src = thumbnail.href;
        slot.preview.href = url.href;
      } else {
        slot.image.removeAttribute("src");
        slot.preview.removeAttribute("href");
      }
    }
  }

  async function change(variant, file) {
    if (busy || !data) return;
    if (file && (file.size > 4 * 1024 * 1024 || !["image/png", "image/jpeg"].includes(file.type))) {
      result.textContent = "请选择不超过 4 MiB 的 PNG 或 JPEG 图片。";
      return;
    }
    const version = ++revision;
    busy = true;
    result.textContent = file ? "正在上传参考图…" : "正在恢复默认图…";
    render();
    try {
      const next = await api(path() + "/" + variant, file ? {method: "PUT", body: file} : {method: "DELETE"});
      if (version !== revision || next.character_id !== characterId) return;
      data = next;
      result.textContent = file ? "参考图已更新。" : "已恢复默认设置。";
    } catch (error) {
      if (version === revision) result.textContent = error.message;
    } finally {
      if (version === revision) { busy = false; render(); }
    }
  }

  for (const [variant, name] of [["q", "Q 版"], ["mature", "常规版（mature）"]]) {
    const group = document.createElement("fieldset"), legend = document.createElement("legend");
    legend.textContent = name;
    const preview = document.createElement("a"), image = document.createElement("img");
    preview.target = "_blank";
    preview.rel = "noopener";
    image.alt = name + "参考图（打开原图）";
    image.width = 112;
    preview.append(image);
    const status = document.createElement("p"), label = document.createElement("label");
    label.textContent = "上传替换";
    const upload = document.createElement("input");
    upload.type = "file";
    upload.accept = "image/png,image/jpeg";
    upload.onchange = () => {
      const file = upload.files[0];
      upload.value = "";
      if (file) void change(variant, file);
    };
    label.append(upload);
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "win-btn";
    restore.textContent = "恢复默认";
    restore.onclick = () => {
      if (confirm("移除当前角色的" + name + "自定义参考图并恢复默认？")) void change(variant, null);
    };
    group.append(legend, preview, status, label, restore);
    container.append(group);
    slots.set(variant, {preview, image, status, upload, restore});
  }
  render();
  return async function load(character, force = false) {
    const id = character?.id;
    if (id === characterId && (!force || busy)) return;
    const version = ++revision;
    characterId = id;
    data = null;
    busy = false;
    scope.textContent = id ? webCharacterName(character) + " · PNG 或 JPEG，最多 4 MiB、800 万像素。" : "等待读取当前角色。";
    result.textContent = id ? "正在读取参考图…" : "";
    render();
    if (!id) return;
    try {
      const next = await api(path());
      if (version !== revision || next.character_id !== id) return;
      data = next;
      result.textContent = "";
      render();
    } catch (error) {
      if (version === revision) result.textContent = error.message;
    }
  };
}
