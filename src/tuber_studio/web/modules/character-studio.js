"use strict";

import { $ } from "./core.js";

export function renderCharacterImage(model) {
  const image = $("character-image");
  if (!image) return;
  const id = model.status?.character?.id;
  const asset = model.assets?.find(item => item.character_id === id && ["idle", "default"].includes(item.tag || item.event_tag));
  const url = asset?.url || asset?.path;
  if (!url) {
    image.hidden = true;
    return;
  }
  if (image.dataset.url !== url) {
    image.dataset.url = url;
    image.src = url;
    image.hidden = false;
  }
}

export function updateOCTypeBadge(personaType) {
  const badge = $("oc-type-badge");
  const promptBox = $("oc-compact-prompt-box");
  const isFineTuned = personaType === "fine_tuned";
  if (badge) {
    badge.textContent = isFineTuned ? "内建角色" : "角色卡";
    badge.className = `oc-badge ${isFineTuned ? "fine-tuned" : "prompt-oc"}`;
  }
  if (promptBox) promptBox.hidden = !isFineTuned;
  $("oc-fine-tuned-model").closest("label").hidden = !isFineTuned;
  for (const tab of ["tab-oc-persona", "tab-oc-style"]) {
    const button = document.querySelector(`[data-tab="${tab}"]`);
    if (isFineTuned && button.classList.contains("active")) {
      document.querySelector('[data-tab="tab-oc-model"]').click();
    }
    button.hidden = isFineTuned;
  }
}

export function syncCharacterJsonToVisual(character) {
  if (!character) return;
  const personaType = character.persona_type || "prompt_oc";
  if ($("oc-persona-type")) $("oc-persona-type").value = personaType;
  if ($("oc-fine-tuned-model")) $("oc-fine-tuned-model").value = character.fine_tuned_model || "";
  if ($("oc-compact-prompt")) $("oc-compact-prompt").value = character.compact_system_prompt || "";
  if ($("oc-identity")) $("oc-identity").value = character.core?.identity || "";
  if ($("oc-background")) $("oc-background").value = character.core?.background || "";
  if ($("oc-traits")) {
    const traits = character.core?.traits;
    $("oc-traits").value = Array.isArray(traits) ? traits.join("、") : (traits || "");
  }
  if ($("oc-min-chars")) $("oc-min-chars").value = character.style?.min_chars ?? 15;
  if ($("oc-max-chars")) $("oc-max-chars").value = character.style?.max_chars ?? 60;
  if ($("oc-catchphrases")) {
    const cp = character.style?.catchphrases;
    $("oc-catchphrases").value = Array.isArray(cp) ? cp.join("\n") : (cp || "");
  }
  if ($("oc-instructions")) {
    const inst = character.style?.instructions;
    $("oc-instructions").value = Array.isArray(inst) ? inst.join("\n") : (inst || "");
  }
  if ($("oc-voice-profile")) $("oc-voice-profile").value = character.voice_profile_id || "";
  if ($("oc-avatar-manifest")) $("oc-avatar-manifest").value = character.avatar_manifest || "";

  updateOCTypeBadge(personaType);
}

export function syncVisualToCharacterJson(model) {
  let charObj;
  try {
    charObj = JSON.parse($("character-editor").value);
  } catch {
    charObj = model.character ? { ...model.character } : {};
  }
  const personaType = $("oc-persona-type")?.value || "prompt_oc";
  charObj.persona_type = personaType;
  if ($("oc-fine-tuned-model")?.value.trim()) {
    charObj.fine_tuned_model = $("oc-fine-tuned-model").value.trim();
  } else {
    delete charObj.fine_tuned_model;
  }
  if ($("oc-compact-prompt")?.value.trim()) {
    charObj.compact_system_prompt = $("oc-compact-prompt").value.trim();
  } else {
    delete charObj.compact_system_prompt;
  }

  charObj.core = charObj.core || {};
  if ($("oc-identity")) charObj.core.identity = $("oc-identity").value.trim();
  if ($("oc-background")) charObj.core.background = $("oc-background").value.trim();
  if ($("oc-traits")) {
    charObj.core.traits = $("oc-traits").value.split(/[、,，\n]+/).map(s => s.trim()).filter(Boolean);
  }

  charObj.style = charObj.style || {};
  if ($("oc-min-chars")) charObj.style.min_chars = Number($("oc-min-chars").value) || 15;
  if ($("oc-max-chars")) charObj.style.max_chars = Number($("oc-max-chars").value) || 60;
  if ($("oc-catchphrases")) {
    charObj.style.catchphrases = $("oc-catchphrases").value.split("\n").map(s => s.trim()).filter(Boolean);
  }
  if ($("oc-instructions")) {
    charObj.style.instructions = $("oc-instructions").value.split("\n").map(s => s.trim()).filter(Boolean);
  }

  if ($("oc-voice-profile")?.value.trim()) {
    charObj.voice_profile_id = $("oc-voice-profile").value.trim();
  } else {
    delete charObj.voice_profile_id;
  }
  if ($("oc-avatar-manifest")?.value.trim()) {
    charObj.avatar_manifest = $("oc-avatar-manifest").value.trim();
  } else {
    delete charObj.avatar_manifest;
  }

  $("character-editor").value = JSON.stringify(charObj, null, 2);
  $("character-preview").textContent = JSON.stringify({
    persona_type: charObj.persona_type,
    core: charObj.core,
    style: charObj.style,
    rules: charObj.rules,
    lore: charObj.lore,
  }, null, 2);
  updateOCTypeBadge(personaType);
  model.characterDirty = true;
  $("character-dirty").textContent = "有未保存修改";
}

export function setCharacterEditor(character, model) {
  model.character = character;
  model.characterDirty = false;
  $("character-editor").value = JSON.stringify(character, null, 2);
  ["character-editor", "save-character", "reset-character", "activate-character"].forEach(id => {
    if ($(id)) $(id).disabled = false;
  });
  $("editor-title").textContent = `${character.name || character.id} · v${character.version}`;
  $("character-preview").textContent = JSON.stringify({
    persona_type: character.persona_type,
    core: character.core,
    style: character.style,
    rules: character.rules,
    lore: character.lore,
  }, null, 2);
  $("character-dirty").textContent = "";
  syncCharacterJsonToVisual(character);
}

export function renderCharacterList(model, { busy, node, onSelect }) {
  const container = $("character-list");
  if (!container) return;
  container.replaceChildren();
  if (!model.characters.length) {
    container.append(node("p", "暂无角色配置。", "muted"));
    return;
  }
  model.characters.forEach(character => {
    const button = node("button", character.name || character.id, character.id === model.characterId ? "selected" : "");
    const isFineTuned = (character.persona_type || "prompt_oc") === "fine_tuned";
    const typeBadge = node("span", isFineTuned ? "★ 微调人设" : "⚙ 基座OC", `oc-badge ${isFineTuned ? "fine-tuned" : "prompt-oc"}`);
    button.append(typeBadge);
    button.append(node("small", `${character.id} · v${character.version ?? character.active_version ?? "—"}${character.id === model.status?.character?.id ? " · 当前角色" : ""}`));
    button.addEventListener("click", () => busy(button, async () => {
      if (model.characterDirty && !confirm("角色配置有未保存修改。放弃修改并切换所选角色？")) return;
      await onSelect(character.id);
    }));
    container.append(button);
  });
}

export async function selectCharacter(id, version, model, { api, list, node, busy, onRenderList }) {
  const versions = list(await api(`/api/characters/${encodeURIComponent(id)}/versions`));
  model.characterId = id;
  model.versions = versions;
  const select = $("character-version");
  if (select) {
    select.replaceChildren();
    versions.slice().sort((a, b) => b.version - a.version).forEach(character => {
      const option = node("option", `v${character.version}${character.name ? ` · ${character.name}` : ""}`);
      option.value = character.version;
      select.append(option);
    });
  }
  const target = versions.find(character => character.version === Number(version)) ||
    versions.slice().sort((a, b) => b.version - a.version)[0] ||
    model.characters.find(character => character.id === id);
  if (!target) throw new Error("角色配置不可用");
  if (select) {
    select.disabled = false;
    select.value = String(target.version);
  }
  setCharacterEditor(target, model);
  if (typeof onRenderList === "function") onRenderList();
}

export async function loadCharacters(model, { api, list, memeGallery, selectCharacterFn, renderCharacterListFn }) {
  model.characters = list(await api("/api/characters"));
  renderCharacterListFn();
  if (memeGallery && typeof memeGallery.setCharacters === "function") {
    memeGallery.setCharacters(model.characters, model.status?.character?.id);
  }
  if (!model.characterId && model.characters.length) {
    await selectCharacterFn(model.status?.character?.id || model.characters[0].id);
  }
}

export function initCharacterStudioUI(model, {
  api,
  busy,
  notice,
  node,
  list,
  memeGallery,
  renderStatus,
}) {
  document.querySelectorAll(".oc-tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const targetId = btn.dataset.tab;
      document.querySelectorAll(".oc-tab-btn").forEach(b => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".oc-tab-content").forEach(content => {
        content.classList.toggle("active", content.id === targetId);
      });
    });
  });

  const visualIds = [
    "oc-persona-type", "oc-fine-tuned-model", "oc-compact-prompt",
    "oc-identity", "oc-background", "oc-traits",
    "oc-min-chars", "oc-max-chars", "oc-catchphrases", "oc-instructions",
    "oc-voice-profile", "oc-avatar-manifest"
  ];
  visualIds.forEach(id => {
    const el = $(id);
    if (el) {
      el.addEventListener("input", () => syncVisualToCharacterJson(model));
      el.addEventListener("change", () => syncVisualToCharacterJson(model));
    }
  });

  $("character-editor")?.addEventListener("input", () => {
    model.characterDirty = true;
    $("character-dirty").textContent = "有未保存修改";
    try {
      const parsed = JSON.parse($("character-editor").value);
      if (parsed && typeof parsed === "object") {
        syncCharacterJsonToVisual(parsed);
      }
    } catch {
      // mid-typing invalid json
    }
  });

  const doSelect = async (id, version) => {
    await selectCharacter(id, version, model, {
      api,
      list,
      node,
      busy,
      onRenderList: () => renderCharacterList(model, { busy, node, onSelect: doSelect }),
    });
  };

  const doRenderList = () => renderCharacterList(model, { busy, node, onSelect: doSelect });

  const doLoadCharacters = async () => {
    await loadCharacters(model, {
      api,
      list,
      memeGallery,
      selectCharacterFn: doSelect,
      renderCharacterListFn: doRenderList,
    });
  };

  $("character-version")?.addEventListener("change", () => {
    const version = Number($("character-version").value);
    const character = model.versions.find(item => item.version === version);
    if (character) setCharacterEditor(character, model);
  });

  $("reset-character")?.addEventListener("click", () => {
    if (!model.characterDirty || confirm("放弃未保存的角色修改？")) {
      setCharacterEditor(model.character, model);
    }
  });

  $("character-form")?.addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      let character;
      try {
        character = JSON.parse($("character-editor").value);
      } catch (error) {
        throw new Error(`角色 JSON 格式错误：${error.message}`);
      }
      if (!character || typeof character !== "object" || Array.isArray(character)) {
        throw new Error("角色配置必须为 JSON 对象");
      }
      const result = await api(`/api/characters/${encodeURIComponent(model.characterId)}`, {
        method: "PUT",
        body: character,
      });
      notice(`角色已保存为 v${result.version}。`);
      await doLoadCharacters();
      await doSelect(model.characterId, result.version);
    });
  });

  $("activate-character")?.addEventListener("click", () => busy($("activate-character"), async () => {
    if (model.characterDirty) throw new Error("请先保存或放弃未保存的角色修改，再激活版本。");
    if (!confirm(`激活 ${model.character.name} v${model.character.version}？旧角色尚未播报的任务将被取消。`)) return;
    await api(`/api/characters/${encodeURIComponent(model.characterId)}/activate`, {
      method: "POST",
      body: { version: model.character.version },
    });
    if (typeof renderStatus === "function") {
      renderStatus(await api("/api/status"));
    }
    await doLoadCharacters();
    notice("角色版本已激活。");
  }));

  return {
    selectCharacter: doSelect,
    renderCharacterList: doRenderList,
    loadCharacters: doLoadCharacters,
  };
}
