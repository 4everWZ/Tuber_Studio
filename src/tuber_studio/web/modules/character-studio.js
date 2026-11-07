"use strict";

import { $ } from "./core.js";

export function renderPersona(model) {
  const persona = model.status?.persona;
  const builtIn = persona?.source === "fine_tuned";
  const view = model.personaView ?? (!persona || builtIn ? "model" : "cards");
  const cardName = model.status?.character?.name;
  $("persona-current").textContent = !persona ? "正在读取当前角色" :
    builtIn ? "大肥鱼 · 模型内建" : ["角色卡", cardName].filter(Boolean).join(" · ");
  $("persona-model-view").hidden = view !== "model";
  $("persona-card-view").hidden = view !== "cards";
  $("show-persona-model").setAttribute("aria-pressed", String(view === "model"));
  $("show-persona-cards").setAttribute("aria-pressed", String(view === "cards"));

  const states = { active: "节点已加载", installed: "节点已安装", not_installed: "节点未安装" };
  $("persona-model-state").textContent = states[persona?.node_model_state] ??
    "节点状态待确认";
  $("persona-model-detail").textContent = persona?.fine_tuned_model ?? "";
  const modeAction = $("persona-mode-action");
  const textMode = document.querySelector('[data-inference-mode="text"]');
  modeAction.hidden = !persona || builtIn || !textMode ||
    textMode.getAttribute("aria-pressed") === "true" ||
    model.settings?.work_mode_profiles?.text?.llm?.model !== persona.fine_tuned_model;
  modeAction.disabled = !textMode || textMode.disabled;
}

function editableCard(character) {
  const { persona_type, fine_tuned_model, compact_system_prompt, ...card } = character;
  return card;
}

export function syncCharacterJsonToVisual(character) {
  if (!character) return;
  if ($("oc-identity")) $("oc-identity").value = character.core?.identity || "";
  if ($("oc-background")) $("oc-background").value = character.core?.background || "";
  if ($("oc-traits")) {
    const traits = character.core?.traits;
    $("oc-traits").value = Array.isArray(traits) ? traits.join("\n") : (traits || "");
  }
  if ($("oc-min-chars")) $("oc-min-chars").value = character.style?.min_chars ?? 1;
  if ($("oc-max-chars")) $("oc-max-chars").value = character.style?.max_chars ?? 60;
  if ($("oc-catchphrases")) {
    const cp = character.style?.catchphrases;
    $("oc-catchphrases").value = Array.isArray(cp) ? cp.join("\n") : (cp || "");
  }
  if ($("oc-instructions")) {
    const inst = character.style?.instructions;
    $("oc-instructions").value = Array.isArray(inst) ? inst.join("\n") : (inst || "");
  }
  if ($("oc-voice-profile")) $("oc-voice-profile").value = character.voice_profile || "";
  if ($("oc-avatar-manifest")) $("oc-avatar-manifest").value = character.avatar_manifest || "";
}

export function syncVisualToCharacterJson(model, changedId) {
  const charObj = editableCard(JSON.parse($("character-editor").value));
  const changed = id => !changedId || changedId === id;

  charObj.core = charObj.core || {};
  if (changed("oc-identity")) charObj.core.identity = $("oc-identity").value.trim();
  if (changed("oc-background")) charObj.core.background = $("oc-background").value.trim();
  if (changed("oc-traits")) {
    charObj.core.traits = $("oc-traits").value.split("\n").map(s => s.trim()).filter(Boolean);
  }

  charObj.style = charObj.style || {};
  if (changed("oc-min-chars")) charObj.style.min_chars = Number($("oc-min-chars").value);
  if (changed("oc-max-chars")) charObj.style.max_chars = Number($("oc-max-chars").value);
  if (changed("oc-catchphrases")) {
    charObj.style.catchphrases = $("oc-catchphrases").value.split("\n").map(s => s.trim()).filter(Boolean);
  }
  if (changed("oc-instructions")) {
    const value = $("oc-instructions").value.trim();
    charObj.style.instructions = Array.isArray(charObj.style.instructions) ?
      value.split("\n").map(s => s.trim()).filter(Boolean) : value;
  }

  if (changed("oc-voice-profile")) charObj.voice_profile = $("oc-voice-profile").value.trim();
  if (changed("oc-avatar-manifest")) {
    if ($("oc-avatar-manifest").value.trim()) charObj.avatar_manifest = $("oc-avatar-manifest").value.trim();
    else delete charObj.avatar_manifest;
  }

  $("character-editor").value = JSON.stringify(charObj, null, 2);
  model.characterDirty = true;
  $("character-dirty").textContent = "有未保存修改";
}

export function setCharacterEditor(character, model) {
  model.character = character;
  model.characterDirty = false;
  $("character-editor").value = JSON.stringify(editableCard(character), null, 2);
  ["character-editor", "save-character", "reset-character", "activate-character"].forEach(id => {
    if ($(id)) $(id).disabled = false;
  });
  $("editor-title").textContent = `${character.name || character.id} · v${character.version}`;
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
    button.append(node("small", `v${character.version ?? character.active_version ?? "—"}${character.id === model.status?.character?.id ? " · 已选用" : ""}`));
    button.addEventListener("click", () => busy(button, async () => {
      if (model.characterDirty && !confirm("角色配置有未保存修改。放弃修改并切换所选角色？")) return;
      await onSelect(character.id);
    }));
    container.append(button);
  });
}

export async function selectCharacter(id, version, model, { api, list, node, busy, onRenderList }) {
  const request = (model.characterSelection || 0) + 1;
  model.characterSelection = request;
  const versions = list(await api(`/api/characters/${encodeURIComponent(id)}/versions`));
  if (request !== model.characterSelection) return;
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
  for (const [id, view] of [["show-persona-model", "model"], ["show-persona-cards", "cards"]]) {
    $(id).addEventListener("click", () => {
      model.personaView = view;
      renderPersona(model);
    });
  }
  $("persona-mode-action").addEventListener("click", () => {
    document.querySelector('[data-inference-mode="text"]').click();
  });
  const tabs = [...document.querySelectorAll(".oc-tab-btn")];
  tabs.forEach((btn, index) => {
    btn.addEventListener("click", () => {
      const targetId = btn.dataset.tab;
      if (targetId !== "tab-oc-json" && model.character) {
        try { syncCharacterJsonToVisual(JSON.parse($("character-editor").value)); }
        catch { notice("请先修正高级 JSON 的格式，再切换到可视化编辑。", true); return; }
      }
      tabs.forEach(tab => {
        tab.classList.toggle("active", tab === btn);
        tab.setAttribute("aria-selected", String(tab === btn));
        tab.tabIndex = tab === btn ? 0 : -1;
      });
      document.querySelectorAll(".oc-tab-content").forEach(content => {
        content.hidden = content.id !== targetId;
        content.classList.toggle("active", content.id === targetId);
      });
    });
    btn.addEventListener("keydown", event => {
      const offset = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (!offset) return;
      event.preventDefault();
      const next = tabs[(index + offset + tabs.length) % tabs.length];
      next.focus();
      next.click();
    });
  });

  const visualIds = [
    "oc-identity", "oc-background", "oc-traits",
    "oc-min-chars", "oc-max-chars", "oc-catchphrases", "oc-instructions",
    "oc-voice-profile", "oc-avatar-manifest"
  ];
  visualIds.forEach(id => {
    const el = $(id);
    if (el) {
      el.addEventListener("input", () => syncVisualToCharacterJson(model, id));
      el.addEventListener("change", () => syncVisualToCharacterJson(model, id));
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
    if (model.characterDirty && !confirm("角色配置有未保存修改。放弃修改并切换历史版本？")) {
      $("character-version").value = String(model.character.version);
      return;
    }
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
      const { persona_type, fine_tuned_model, compact_system_prompt } = model.character;
      const result = await api(`/api/characters/${encodeURIComponent(model.characterId)}`, {
        method: "PUT",
        body: { ...editableCard(character), persona_type, fine_tuned_model, compact_system_prompt },
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
