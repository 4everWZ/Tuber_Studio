"use strict";

import { $ } from "./core.js";

function editableCard(character) {
  const { version, persona_type, fine_tuned_model, compact_system_prompt, ...card } = character;
  return card;
}

function markCharacterDirty(model) {
  model.characterDirty = $("character-editor").value !== JSON.stringify(editableCard(model.character), null, 2);
  $("character-dirty").textContent = model.characterDirty ? "有未保存修改" : "";
  $("activate-character").textContent = model.characterDirty ? "保存并激活" : "激活此角色";
}

export function syncCharacterJsonToVisual(character) {
  if (!character) return;
  if ($("oc-name")) $("oc-name").value = character.name || "";
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
  if (changed("oc-name")) charObj.name = $("oc-name").value.trim();

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
  markCharacterDirty(model);
}

export function setCharacterEditor(character, model) {
  model.character = character;
  $("character-editor").value = JSON.stringify(editableCard(character), null, 2);
  ["oc-name", "character-editor", "save-character", "reset-character", "activate-character"].forEach(id => {
    if ($(id) && $(id).getAttribute?.("aria-busy") !== "true") $(id).disabled = false;
  });
  $("editor-title").textContent = `编辑角色卡 · ${character.name || character.id}`;
  markCharacterDirty(model);
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
    if (character.id === model.status?.character?.id) button.append(node("small", " · 已激活"));
    button.addEventListener("click", () => busy(button, async () => {
      if (model.characterDirty && !confirm("角色配置有未保存修改。放弃修改并切换所选角色？")) return;
      await onSelect(character.id);
    }));
    container.append(button);
  });
}

export async function selectCharacter(id, model, { api, onRenderList }) {
  const request = (model.characterSelection || 0) + 1;
  model.characterSelection = request;
  const character = await api(`/api/characters/${encodeURIComponent(id)}`);
  if (request !== model.characterSelection) return;
  if (!character) throw new Error("角色配置不可用");
  model.characterId = id;
  setCharacterEditor(character, model);
  onRenderList?.();
}

export async function loadCharacters(model, { api, list, selectCharacterFn, renderCharacterListFn }) {
  model.characters = list(await api("/api/characters"));
  renderCharacterListFn();
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
  renderStatus,
}) {
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
    "oc-name", "oc-identity", "oc-background", "oc-traits",
    "oc-min-chars", "oc-max-chars", "oc-catchphrases", "oc-instructions",
    "oc-voice-profile", "oc-avatar-manifest"
  ];
  visualIds.forEach(id => {
    const el = $(id);
    if (el) {
      el.addEventListener("input", () => {
        if (model.character) syncVisualToCharacterJson(model, id);
      });
    }
  });

  $("character-editor")?.addEventListener("input", () => {
    markCharacterDirty(model);
    try {
      const parsed = JSON.parse($("character-editor").value);
      if (parsed && typeof parsed === "object") {
        syncCharacterJsonToVisual(parsed);
      }
    } catch {
      // mid-typing invalid json
    }
  });

  const doSelect = async id => {
    await selectCharacter(id, model, {
      api,
      onRenderList: () => renderCharacterList(model, { busy, node, onSelect: doSelect }),
    });
  };

  const doRenderList = () => renderCharacterList(model, { busy, node, onSelect: doSelect });

  const doLoadCharacters = async () => {
    await loadCharacters(model, {
      api,
      list,
      selectCharacterFn: doSelect,
      renderCharacterListFn: doRenderList,
    });
  };

  $("reset-character")?.addEventListener("click", () => {
    if (!model.characterDirty || confirm("放弃未保存的角色修改？")) {
      setCharacterEditor(model.character, model);
    }
  });

  function readCard() {
    let character;
    try {
      character = JSON.parse($("character-editor").value);
    } catch (error) {
      throw new Error(`角色 JSON 格式错误：${error.message}`);
    }
    if (!character || typeof character !== "object" || Array.isArray(character)) {
      throw new Error("角色配置必须为 JSON 对象");
    }
    if (typeof character.name !== "string" || !character.name.trim() || character.name.trim().length > 60) {
      throw new Error("请填写 1 至 60 字的角色名称。");
    }
    return { ...editableCard(character), name: character.name.trim() };
  }

  async function saveCard(id, card) {
    const selection = model.characterSelection;
    const editor = $("character-editor");
    const input = editor.value;
    const result = await api(`/api/characters/${encodeURIComponent(id)}`, { method: "PUT", body: card });
    const sameEditor = model.characterSelection === selection && editor.value === input;
    if (sameEditor) setCharacterEditor(result, model);
    return sameEditor;
  }

  let writing = false;
  async function changeCard(button, action) {
    if (writing) return;
    writing = true;
    try {
      await busy(button, async () => {
        try {
          await action();
        } catch (error) {
          $("character-dirty").textContent = error.message;
          throw error;
        }
      });
    } finally {
      writing = false;
    }
  }

  $("create-character-form")?.addEventListener("submit", event => {
    event.preventDefault();
    return changeCard(event.submitter, async () => {
      if (model.characterDirty && !confirm("放弃当前角色的未保存修改并新建角色？")) return;
      const nameInput = $("new-character-name");
      const name = nameInput.value.trim();
      if (!name) throw new Error("请填写角色名称");
      const created = await api("/api/characters", { method: "POST", body: { name } });
      if (nameInput.value.trim() === name) nameInput.value = "";
      await doLoadCharacters();
      await doSelect(created.id);
      tabs[0]?.click();
      $("oc-identity")?.focus();
      notice("角色卡已创建。");
    });
  });

  $("character-form")?.addEventListener("submit", event => {
    event.preventDefault();
    return changeCard(event.submitter, async () => {
      const id = model.characterId;
      await saveCard(id, readCard());
      notice("角色已保存。");
      if (model.status?.character?.id === id && renderStatus) renderStatus(await api("/api/status"));
      await doLoadCharacters();
    });
  });

  $("activate-character")?.addEventListener("click", () => changeCard($("activate-character"), async () => {
    const id = model.characterId;
    const card = model.characterDirty ? readCard() : null;
    const alreadyActive = model.status?.character?.id === id;
    if (!alreadyActive && !confirm(`${card ? "保存并激活" : "激活"} ${card?.name || model.character.name}？旧角色尚未播报的任务将被取消。`)) return;
    if (card && !await saveCard(id, card)) {
      await doLoadCharacters();
      notice("角色已保存。请在当前编辑完成后激活。");
      return;
    }
    if (!alreadyActive) await api(`/api/characters/${encodeURIComponent(id)}/activate`, {
      method: "POST",
    });
    if (typeof renderStatus === "function") {
      renderStatus(await api("/api/status"));
    }
    await doLoadCharacters();
    notice(card && alreadyActive ? "角色已保存。" : "角色已激活。");
  }));

  return {
    selectCharacter: doSelect,
    renderCharacterList: doRenderList,
    loadCharacters: doLoadCharacters,
  };
}
